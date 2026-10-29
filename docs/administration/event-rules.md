# Event Rules

Use Event Rules to run a Script in response to an event, or to react to changes
to the plugin's objects.

## Running a Script from an Event Rule

Choose **Run Script** as the action type and select a published Script. The
picker includes disabled Scripts so you can prepare a rule before enabling them.

### What the script receives

| Input | Value |
|---|---|
| `data` | The rule's **Data** field merged with the event payload. |
| `self.event` | The JSON-safe event context described below. |
| `self.request` | A stripped copy of the triggering request, without uploaded files. `None` when the event has no request. |

Event Rule input is passed directly to `run()` without validation through the
Script's form. Form defaults are not applied, and object identifiers remain
plain values. Check the fields and types your Script requires.

Event-driven runs always commit. A dry-run option is not available.

Each run is queued as a Job. Open the Script's **Jobs** tab under
*Scripts > Scripts* to follow its status and results.

### What `self.event` carries

Read the event context from `self.event`. It is `None` for a manually requested
run, so check it before use. See
[Authoring](../user-guide/authoring.md#run-context).

| Key | Value |
|---|---|
| `event_type` | The event type, such as `object_created` or `job_completed`. |
| `event_rule` | The rule's name. |
| `event_rule_id` | The rule's numeric ID. |
| `object_type` | The object's type as `app_label.model`, or `None` when the event has no object. |
| `object_id` | The object's numeric ID, or `None`. |
| `snapshots` | The before and after representations captured by NetBox, when available. |

The user and HTTP request are not included in this stored payload. They reach
the Script separately through its request context.

### What is refused, and when

Some Script states are checked when saving the rule, and others when it fires.

| State | When it is reported |
|---|---|
| Retired | When saving the rule. A later activation can republish the class and lift its retirement. |
| Disabled | When the rule fires. The reason is logged and no run is queued. |
| Its Project is disabled, or serves no revision | When the rule fires. The reason is logged and no run is queued. |

Rules may reference temporarily disabled Scripts or Projects and start working
when those conditions are resolved. One rule's inability to run its Script does
not stop the other rules responding to that event.

### Permissions

The Event Rule authorizes the run, not the permissions of the user who triggered
the event. Restrict who can create or edit Event Rules, because those rules choose
which Scripts run. See [Permissions](permissions.md) for plugin action permissions.

## Practical examples

These examples report device changes in the Job log. Upload and activate each
Script as described in [Uploading](uploading.md#creating-a-project-from-one-script),
then create its rule under **Operations > Integrations > Event Rules**.
See [Event Rule execution permissions](permissions.md#event-rule-execution-permissions)
before granting access to manage these rules.

With **Conditions** empty, each rule queues a Job for every selected device
event, and the Script decides what to report. NetBox's
[snapshot conditions](https://docs.netbox.dev/en/stable/reference/conditions/#snapshot-conditions-event-rules)
can filter events before queuing a Job. For the status example, use
`{"attr": "status", "op": "changed"}` to skip unrelated updates. Keep the
Script's checks: `changed` can also match an update without a `prechange`
snapshot.

Snapshots describe the event, not the object's current state. They use model
values: `status` is a string such as `active`, and `tags` is a list of tag
**names**. A create event normally has no `prechange` snapshot, and a delete
event has no `postchange` snapshot. An update needs both snapshots to compare
values. An update has no `prechange` snapshot if the code that saved it did not
call `snapshot()` before saving. This can happen in Scripts or when NetBox saves
devices installed in a parent device's bays.

### Report device status changes

This Script reports creation and deletion, and skips updates where the status
did not change. It identifies deleted devices by their recorded ID without
looking them up in the database.

| Setting | Value |
|---|---|
| Name | Report device status changes |
| Object types | DCIM > Device |
| Enabled | Yes |
| Event types | Object created, Object updated, Object deleted |
| Conditions | Leave empty |
| Action type | Run Script |
| Script | Report device status changes |
| Action data | Leave empty |

```python
from netbox_scripts import Script


class ReportDeviceStatus(Script):
    class Meta:
        name = 'Report device status changes'
        notifications_default = 'on_failure'

    def run(self, data, commit):
        event = self.event
        if event is None:
            self.log_info('No event context. Nothing to report.')
            return
        if event.get('object_type') != 'dcim.device':
            return

        device_id = event['object_id']
        event_type = event.get('event_type')
        if event_type == 'object_created':
            self.log_info(f'Device {device_id} was created.')
            return
        if event_type == 'object_deleted':
            self.log_info(f'Device {device_id} was deleted.')
            return
        if event_type != 'object_updated':
            return

        snapshots = event.get('snapshots') or {}
        before = snapshots.get('prechange')
        after = snapshots.get('postchange')
        if before is None or after is None or 'status' not in before or 'status' not in after:
            self.log_warning('Status snapshots are incomplete. Skipping this event.')
            return
        if before['status'] == after['status']:
            return

        self.log_info(f'Device {device_id}: status changed from {before["status"]} to {after["status"]}.')
```

### Report a tag being added or removed

This Script reports changes to membership of the `monitored` tag. Replace
`monitored` with the exact tag name you use. Changes to other tags are ignored.
A device created with the tag is not reported, because the rule reacts only to
updates.

| Setting | Value |
|---|---|
| Name | Report monitoring tag changes |
| Object types | DCIM > Device |
| Enabled | Yes |
| Event types | Object updated |
| Conditions | Leave empty |
| Action type | Run Script |
| Script | Report monitoring tag changes |
| Action data | Leave empty |

```python
from netbox_scripts import Script


class ReportMonitoringTag(Script):
    class Meta:
        name = 'Report monitoring tag changes'
        notifications_default = 'on_failure'

    def run(self, data, commit):
        event = self.event
        if event is None:
            self.log_info('No event context. Nothing to report.')
            return
        if event.get('object_type') != 'dcim.device' or event.get('event_type') != 'object_updated':
            return

        snapshots = event.get('snapshots') or {}
        before = snapshots.get('prechange')
        after = snapshots.get('postchange')
        if before is None or after is None or 'tags' not in before or 'tags' not in after:
            self.log_warning('Tag snapshots are incomplete. Skipping this event.')
            return

        tag_name = 'monitored'
        was_monitored = tag_name in before['tags']
        is_monitored = tag_name in after['tags']
        if was_monitored == is_monitored:
            return

        change = 'added' if is_monitored else 'removed'
        self.log_info(f'Device {event["object_id"]}: tag {tag_name!r} was {change}.')
```

A missing snapshot or field does not mean an empty tag list. Skipping incomplete
snapshots avoids reporting a change that cannot be established. An empty list,
however, is valid and allows the first tag addition or last tag removal to be
detected.

### Adapting the examples

The examples only report events. Repeated delivery can repeat a log entry, but
does not modify devices or call an external service. Each matching rule queues
its own run, so multiple rules can report the same change more than once.

Before adding a write, fetch the live object and check its current state. It may
have changed again or been deleted while the Job was waiting. Use snapshots to
understand the original change. Do not reconstruct it from a current database
query.

Return without saving when the intended state is already present. Otherwise,
call the object's `snapshot()` method before changing any fields, so change
logging and subsequent events include the original values. Committed Scripts can
trigger further Event Rules, so ensure any update you make no longer matches the
same transition check. Comparing snapshots alone does not prevent duplicate
actions: the same event still contains the same change on a repeat run. For
external actions, use an idempotent operation or the service's supported
deduplication mechanism.

Log only the identifiers and selected values needed to explain the action.
Anyone who can view the Job can read its logs, so do not log complete snapshots,
request data or secrets.

## Scripts as event sources

The plugin's four object types can be Event Rule sources, like Devices or IP
addresses.

| Object | A rule can react to |
|---|---|
| Script Project | A Project being created, changed or deleted. |
| Script Project Revision | A revision appearing. |
| Script File | A declaration being added, changed or removed. |
| Script | A Script appearing, retiring, or being enabled or disabled. |

Webhooks use the object's REST representation. A revision's stored manifest and
Script File snapshot are not included.

Changes made through UI or REST requests can trigger matching rules. A successful
committed Script run also publishes its queued object-change events, even without
a request. Dry runs and failed runs do not publish those events.

Background staging, validation, migration and job-driven activation do not deliver
object-change events. Follow their outcomes through revision status and migration
Job results. Job start and completion events are separate.
