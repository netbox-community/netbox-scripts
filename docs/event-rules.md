# Event Rules

Custom Scripts connect to NetBox's Event Rules in both directions. A rule can run a Custom
Script when something happens, and the plugin's own objects can be what a rule reacts to.

## Running a Custom Script from an Event Rule

Choose **Run Custom Script** as the rule's action type, then pick the script it should run.
The picker lists every published Custom Script, including ones currently disabled, so an
administrator can build a rule before the script is ready to serve.

The rule needs the NetBox release that supports plugin-provided Event Rule actions. On an
earlier release the plugin registers nothing and the action type simply does not appear.
Nothing else about the plugin changes.

### What the script receives

| Input | Value |
|---|---|
| The form data | The rule's **Data** field, merged with the event payload, passed through unchanged. Whatever an author would have typed into the run form, the rule supplies instead. |
| `self.event` | The event context, in the JSON-safe form described below. |
| `self.request` | Nothing. A rule is not a browser request, so this is `None`. |

An event-driven run always commits. A dry run would make the rule a no-op with no way to
report that it did nothing, so the choice is not offered.

Runs are queued, not immediate, and each one is a Job like any other. Read it under
*Custom Scripts > Custom Scripts*, on the script's Jobs tab, the same place a run somebody
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

The requesting user and the HTTP request are deliberately absent. Neither survives being
written to a Job row, and both already reach the script by their own route.

### What is refused, and when

A Custom Script has several states that stop it running, and they are reported at two
different moments on purpose.

| State | When it is reported |
|---|---|
| Retired | When the rule is saved. Retirement is permanent, because the active revision has stopped publishing the class, so a rule naming one is misconfigured rather than idle. |
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

## Custom Scripts as event sources

All four of the plugin's object types can drive an Event Rule, so a rule can react to them
exactly as it would to a Device or an IP address.

| Object | A rule can react to |
|---|---|
| Custom Script Project | A project created, changed, or deleted |
| Custom Script Project Revision | A revision appearing, or its validation status changing |
| Custom Script Module | An entrypoint declaration added, changed, or removed |
| Custom Script | A script appearing, retiring, or being enabled or disabled |

A webhook fired by one of these carries the same body the REST API returns for that object,
so the receiver reads the fields it already knows. A revision's stored documents are not part
of it: the manifest and the entrypoint snapshot stay in NetBox.

Reacting to a revision reaching `invalid` is the useful case worth calling out. It is how an
operator finds out that synchronized source stopped being importable, without watching the
Project page.
