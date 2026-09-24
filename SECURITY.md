# Security Policy

## Warranty and support

NetBox Scripts is provided **"as is"** under the [Apache License 2.0](LICENSE),
without warranties or conditions unless required by law or agreed in writing.
You are responsible for evaluating each release and deciding whether it is
suitable for your environment.

This community release does not include commercial support from NetBox Labs,
service-level agreements or guaranteed response or resolution times.

Security reports are welcome through the private reporting process below.
The security contact is for vulnerability disclosure, not commercial support.

## Reporting a Vulnerability

Please report suspected vulnerabilities privately to **security@netboxlabs.com**.
Include **NetBox Scripts** in the subject so the report can be directed to the
right maintainers. Do not open a public issue, discussion or pull request for an
undisclosed vulnerability.

Please include:

- The NetBox and plugin versions, or the commit you tested.
- Steps to reproduce, the expected behavior and the security impact.
- The permissions needed to reproduce the issue and any relevant configuration.
- A minimal example and relevant logs, with credentials and private data removed.

For a dependency alert, explain how the affected code can be reached through the
plugin. A scanner result alone may not provide enough information to reproduce it.

NetBox Scripts is in alpha. Reports about alpha versions are welcome. Check the
[compatibility matrix](COMPATIBILITY.md) and state the versions you tested.

## Script execution and trust

Scripts run Python with the permissions of the NetBox process that loads or
executes them. Code can load in a web process as well as a worker. Validation and
private import namespaces do not provide a sandbox. Only trusted authors should
supply source or write to its storage.

Authorized Script execution is an intended feature. Bypassing the plugin's
advertised permissions or object constraints is different. Examples include
unauthorized source changes, Script execution or access to protected records
through the plugin's UI or APIs. Please report suspected breaks in those
boundaries privately, even when you are unsure of their cause.

See [Permissions](https://netbox-community.github.io/netbox-scripts/permissions/)
and the [storage trust boundary](https://netbox-community.github.io/netbox-scripts/configuration/#storage-trust-boundary)
for the plugin's controls. NetBox's own
[security policy](https://github.com/netbox-community/netbox/blob/main/SECURITY.md)
and [threat model](https://github.com/netbox-community/netbox/blob/main/THREAT_MODEL.md)
provide the host application's security guidance.

## Operating an alpha release

NetBox Scripts is not recommended for production use. Test it with non-production
data and keep test workers separate from production queues. Review Script source
before use, restrict access to source storage and keep secrets out of run logs
and output. A dry run does not reverse actions against devices or other external
systems.
