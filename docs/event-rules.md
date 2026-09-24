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
[Authoring](authoring.md#what-a-run-knows-about-its-own-context).

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
