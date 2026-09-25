# Contributing to NetBox Scripts

Thank you for helping improve NetBox Scripts. Bug reports, feature proposals,
code and documentation are all welcome. Please keep issues, pull requests and
discussions kind, constructive and respectful.

For suspected vulnerabilities, follow the
[security policy](https://github.com/netbox-community/netbox-scripts/blob/main/SECURITY.md)
instead of opening a public issue.

## General tips for working on GitHub

Search existing issues before opening one. Add useful details to an existing
report, and use reactions rather than comments that only ask for an update.
Avoid mentioning people with `@` unless they are already involved in the work.

A [GitHub account](https://github.com/signup) is needed to open issues and pull
requests. Use
[Markdown](https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/basic-writing-and-formatting-syntax)
to format examples, logs and screenshots.

## How we work: issue-first, then assignment, then PR

Open an issue using the appropriate template, or look for an existing issue
marked **status: needs owner**.

1. A maintainer triages the issue and reviews its scope.
2. When the work is ready for a contributor, the maintainer marks it
   **status: needs owner**.
3. Comment on the issue to let us know you would like to work on it.
4. A maintainer assigns the issue to you and changes its status to
   **status: accepted**.
5. Open a pull request linked to the assigned issue.

If you already plan to submit a pull request when opening an issue, mention
that in the description. After triage, a maintainer can assign it directly
to you and mark it **status: accepted**, without the **status: needs owner**
stage.

Please wait until the issue is assigned to you and marked **status: accepted**
before opening a pull request, including a draft.

## Types of contributions

You can report or fix bugs, propose features, improve examples and documentation,
or help clarify an existing issue. Look for **status: needs owner** to find work
awaiting a contributor. The assignment process above applies to documentation
changes as well as code.

## Reporting bugs

Use the **Bug report** template and include the NetBox and plugin versions,
reproduction steps, and expected and actual behavior. Add relevant logs or
screenshots, removing credentials and private data first. For an unreleased
checkout, include the commit and describe any local changes.

A bug report describes unintended behavior. Use a feature request for new
functionality. Report suspected security problems privately as described above.

## Requesting features

Describe the problem you need to solve, your proposed behavior and alternatives
you considered. Mention likely model, UI or API changes when relevant. Maintainers
may ask questions to agree on scope before accepting the proposal.

## Development environment

The plugin runs inside NetBox, not as a standalone Django application. Start with
NetBox's [development setup](https://docs.netbox.dev/en/stable/development/getting-started/)
and a release within the plugin's
[compatibility range](https://github.com/netbox-community/netbox-scripts/blob/main/COMPATIBILITY.md).
Use a Python version supported by both.

The examples below assume these sibling checkouts:

```text
workspace/
  netbox/          # NetBox checkout at the intended ref
  netbox-scripts/  # Your clone or fork of this plugin
```

**Run commands from `netbox-scripts/` with the NetBox development virtual
environment active.** Install NetBox's requirements and the plugin in that
same environment:

```bash
python -m pip install -r ../netbox/requirements.txt
python -m pip install -e '.[dev,test]'
python -m pip install pre-commit
pre-commit install
pre-commit run --all-files
```

### Running tests

The plugin uses Django's test runner. It does not use pytest. Review
`testing/configuration.py` before running tests and provide PostgreSQL and Redis
services matching that configuration. The database account must be able to create
the test database.

**Use isolated development services, never production.** The test configuration
uses Redis databases 15 for tasks and 14 for caching. No worker serving a different
NetBox database may consume those test queues. Redis database numbers do not
protect against commands that clear the whole server.

These exports apply only inside the subshell:

```bash
(
    export NETBOX_CONFIGURATION=configuration
    export PYTHONPATH="$PWD/testing${PYTHONPATH:+:$PYTHONPATH}"
    python ../netbox/netbox/manage.py test netbox_scripts.tests -v 2
)
```

Record the NetBox ref, test command and result in your pull request. State which
checks you could not run. Do not report a check as passing unless you ran it.

### Checking the UI and workers

Use a separate development configuration for interactive testing. Enable
`netbox_scripts`, configure persistent source storage accessible to the web and
worker processes, and follow NetBox's development startup instructions. The
plugin's [installation guide](https://netbox-community.github.io/netbox-scripts/administration/installation/)
covers its storage and activation requirements.

Do not use the unit-test configuration as a shared web/worker environment. Its
in-memory source storage is for tests, not for passing revisions between
processes. Keep independently started development workers off the unit-test queues
while running the suite.

### Database migrations (Django)

Include one logical migration per pull request that changes models, keeping the
schema change with the implementation and tests. Django also needs migrations
for model-state changes that produce no SQL.

See [Database migrations](https://netbox-community.github.io/netbox-scripts/development/conventions/#database-migrations)
for the initial-migration policy and pinned NetBox dependencies. Coordinate
consolidation with a maintainer and agree how to handle affected development
databases.

Prefer backward-compatible changes. Discuss destructive operations, nullability
changes and large-table indexes before implementing them. Data migrations should
be fast and safe to retry, using historical models rather than importing current
model classes.

### API and UI compatibility

Use NetBox's existing views, tables, filtersets and serializers where they fit.
Discuss breaking changes to API fields, choices or URL paths before starting,
and preserve the project's deprecation policy.

## Style, linting, and versions

Write readable Python and use Ruff through pre-commit for formatting and linting.
The rules live in `pyproject.toml`. See the
[development conventions](https://netbox-community.github.io/netbox-scripts/development/conventions/)
for code organization, NetBox integration, testing and documentation style.
Do not introduce a second formatter configuration.

Use the compatibility matrix and CI configuration for supported versions. Install
Django and other host dependencies from the chosen NetBox checkout's requirements
rather than selecting their versions independently.

## Pull request guidelines

A pull request should link its accepted, assigned issue using `Fixes #NNN` and
explain the change and how it was tested. Include relevant tests and update the
documentation when behavior changes. Run `pre-commit run --all-files` before
submitting.

Keep changes focused. Discuss large refactors in the issue first. Tests and the
implementation they cover belong in the same commit. Maintainers handle version
bumps and release notes, so do not include those in an ordinary contribution.

### Branching and commits

Use a descriptive branch name such as `fix-upload-validation` or
`feat-script-filter`. Pull request titles and commits must follow
[Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/), for example:

```text
fix(api): Preserve validated Script inputs
docs: Clarify source storage setup
feat(filters): Add a Script status filter
```

Add a commit body when it helps explain the change. Use a `BREAKING CHANGE:`
footer for an approved incompatible change.

### Target branch

Target the repository's default branch unless a maintainer requests another one.

## Documentation

Write for the page's audience: users need tasks and outcomes, administrators need
configuration and recovery guidance, and developers need contracts and examples.
Keep safety warnings explicit and link to shared explanations instead of repeating
them. Update relevant docstrings along with behavior changes.

From the plugin repository root:

```bash
python -m pip install -e '.[docs]'
zensical build --clean --strict
```

Use `zensical serve` to preview the site. Check the rendered pages, examples and
links as well as the build result. The contribution page includes this file, so
keep links usable both on GitHub and in the documentation site.

## Contribution licensing

NetBox Scripts is licensed under the
[Apache License 2.0](https://github.com/netbox-community/netbox-scripts/blob/main/LICENSE).
Contributions submitted for inclusion follow Section 5 of that license, including
its treatment of separate agreements.

Submit work you have the right to contribute. Identify any third-party material
and preserve the applicable license and attribution notices. Do not include
confidential code or data without permission to publish it.

## Security

Send suspected vulnerability reports to **security@netboxlabs.com**, following
[SECURITY.md](https://github.com/netbox-community/netbox-scripts/blob/main/SECURITY.md).
Do not put vulnerability details or a proposed security fix in a public issue or
pull request before coordinating privately.

## Releasing (maintainers)

Follow the [release checklist](https://netbox-community.github.io/netbox-scripts/development/releasing/)
for version updates, packaging, tagging and publishing.

## Thanks!

Your reports, reviews, code and documentation help make the plugin better for
everyone who uses it. Thank you for contributing.
