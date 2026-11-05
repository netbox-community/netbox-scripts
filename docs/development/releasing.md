# Releasing

Releases follow [Semantic Versioning](https://semver.org/). Record changes in
`docs/releases.md` and use the checklist below to publish them. Publishing uses
PyPI Trusted Publishing, which needs a one-time publisher on the PyPI project:
owner `netbox-community`, repository `netbox-scripts`, workflow `release.yml`,
environment `pypi`.

In the repository's `pypi` environment, set **Deployment branches and tags** to
**Selected branches and tags** and add a rule with **Ref type: Tag** and the name
pattern `v*`. A Branch rule does not match the release tag, so GitHub refuses
the publishing job.

## Checklist

Run commands from the repository root. Replace `X.Y.Z` with the version being
released throughout the checklist.

Merge fixes and other changes into `main` through pull requests before preparing
the release. Then create a release branch from an up-to-date `main` and follow
the checklist below. Hotfixes use the same process.

1. **Check readiness.** Confirm CI passes on the default branch and the release's
    code and documentation changes are complete.

2. **Update the change log.** Add a `## vX.Y.Z (YYYY-MM-DD)` section above the
    previous release in `docs/releases.md`, separated by `---`. Group entries under
    `### Enhancements` and `### Bug Fixes`, using
    `* [#<issue>](<url>) - <summary>`. The first release lists only
    `* Initial release`, without subsections. An alpha release adds a warning
    that states its upgrade limits and links the open bug reports.

3. **Set the version.** Update `version = "X.Y.Z"` in `pyproject.toml` and
    `__version__ = "X.Y.Z"` in `netbox_scripts/__init__.py`. Both must match the
    version in the release tag. The workflow refuses to publish if they differ.
    Commit the release changes on that branch as `chore(release): Release vX.Y.Z`
    and open a pull request with the same title, targeting `main`.

4. **Check the package contents.** Run:

    ```bash
    pre-commit run --hook-stage manual check-manifest
    ```

    This compares tracked files with the source distribution and reports missing
    or unexpected files.

5. **Merge and tag the release.** After the package check and required CI pass,
    merge the release pull request into `main`. Replace `RELEASE_COMMIT` below
    with the final commit SHA added to `main` by the merged pull request. Squash
    and rebase merges change commit SHAs. Wait until CI passes on that commit.
    Fetch the commit, check that it is on `main`, create a signed tag using your
    configured signing key and push only that tag:

    ```bash
    git fetch origin main &&
        git merge-base --is-ancestor RELEASE_COMMIT origin/main &&
        git tag -s vX.Y.Z -m "Release vX.Y.Z" RELEASE_COMMIT &&
        git push --no-follow-tags origin tag vX.Y.Z
    ```

    The ancestry check exits silently if `RELEASE_COMMIT` is not on
    `origin/main`, and the remaining commands do not run.

    Release tags are protected, so use an account permitted to create them.

6. **Publish the GitHub release.** Draft it from the new tag, compare the
    generated notes with `docs/releases.md`, make any corrections and publish.

7. **Check the publishing workflow.** Publishing the release triggers
    `.github/workflows/release.yml`. It builds the wheel and source distribution,
    runs `twine check` and publishes to PyPI using this repository's Trusted
    Publisher. Check that the run succeeds, including package validation and the
    OIDC token exchange.

8. **Verify the published package.** Check the version on
    <https://pypi.org/project/netbox-scripts/> and confirm that a fresh
    `pip install netbox-scripts` selects the new version.

## See also

- [Contributing](contributing.md) for the development workflow and review requirements.
