# Event Rules

Scripts connect to NetBox's Event Rules in both directions. A rule can run a Script when
something happens, and the plugin's own objects can be what a rule reacts to.

## Running a Script from an Event Rule

Choose **Run Script** as the rule's action type, then pick the script it should run.
The picker lists every published Script, including ones currently disabled, so an
administrator can build a rule before the script is ready to serve.

The rule needs the NetBox release that supports plugin-provided Event Rule actions. On an
earlier release the plugin registers nothing and the action type simply does not appear.
Nothing else about the plugin changes.

### What the script receives

| Input | Value |
|---|---|
| `data` | The rule's **Data** field, merged with the event payload and passed to `run()` unchanged. It is not validated through the Script's form, so no default is applied and an object's key stays a plain value. Check the fields and types your script requires. |
| `self.event` | The event context, in the JSON-safe form described below. |
| `self.request` | The request behind the change that triggered the rule, as a stripped copy carrying no uploaded files. Nothing when the event carried no request. |

An event-driven run always commits. A dry run would make the rule a no-op with no way to
report that it did nothing, so the choice is not offered.

Runs are queued, not immediate, and each one is a Job like any other. Read it under
*Scripts > Scripts*, on the script's Jobs tab, the same place a run somebody
requested by hand appears.

### What `self.event` carries

An author reads the context off the instance. It is `None` for a run a person requested, so
guard before using it. See [Authoring](authoring.md#what-a-run-knows-about-its-own-context).

| Key | Value |
|---|---|
| `event_type` | The event that fired, such as `object_created` or `job_completed`. |
| `event_rule` | The name of the rule that ran the script. |
| `event_rule_id` | The rule's numeric ID. |
| `object_type` | The changed object's type as `app_label.model`, or nothing for an event with no object. |
| `object_id` | The changed object's numeric ID, or nothing. |
| `snapshots` | The before and after representations NetBox captured, when the event has them. |

The requesting user and the HTTP request are deliberately absent from this payload. Neither
survives being written to a Job row, and both already reach the script by their own route.

### What is refused, and when

A Script has several states that stop it running, and they are reported at two
different moments on purpose.

| State | When it is reported |
|---|---|
| Retired | When the rule is saved. The active revision has stopped publishing the class, so a rule naming one is misconfigured rather than idle. Retirement lifts if a later activation publishes the class again. |
| Disabled | When the rule fires. The reason is written to the pass's log and nothing is queued. |
| Its Project is disabled, or serves no revision | When the rule fires, the same way. |

The second group is temporary state an administrator turns back on, so a rule is allowed to
name a script in that state and start working again once it is fixed. A rule that cannot run
its script never stops the other rules responding to the same event.

### Permissions

Starting a run this way is not checked against the permissions of whoever triggered the
event. The rule itself is the authorization: an administrator who can create an Event Rule
has already chosen what it runs. Restrict who may manage Event Rules accordingly. See
[Permissions](permissions.md) for what the plugin's own actions require.

## Scripts as event sources

All four of the plugin's object types can drive an Event Rule, so a rule can react to them
exactly as it would to a Device or an IP address.

| Object | A rule can react to |
|---|---|
| Script Project | A project created, changed, or deleted |
| Script Project Revision | A revision appearing |
| Script File | A script file declaration added, changed, or removed |
| Script | A script appearing, retiring, or being enabled or disabled |

A webhook fired by one of these carries the same body the REST API returns for that object,
so the receiver reads the fields it already knows. A revision's stored documents are not part
of it: the manifest and the script file snapshot stay in NetBox.

A change made while handling a UI or REST request can trigger matching rules. A Script run also
publishes the object-change events it queued when it finishes successfully with commit enabled,
even without a request. A dry run, or a run that fails, publishes none of them.

Staging, validation and migration jobs, and an activation a job performs, deliver no
object-change events, so a revision staged by a Data Source synchronization, a validation
verdict and the rows the migration passes create reach no rule. Follow those through the
revision's status and the migration Job results. Job start and completion events are separate
from these.
