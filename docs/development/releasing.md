# Releasing

Releases follow [Semantic Versioning](https://semver.org/). Record changes in
`docs/releases.md` and use the checklist below to publish them. Publishing uses
PyPI Trusted Publishing, which needs a one-time publisher on the PyPI project:
owner `netbox-community`, repository `netbox-scripts`, workflow `release.yml`,
environment `pypi`.

## Checklist

Run commands from the repository root. Replace `X.Y.Z` with the version being
released throughout the checklist.

1. **Check readiness.** Confirm CI passes on the default branch and the release's
    code and documentation changes are complete.

2. **Update the change log.** Add a `## vX.Y.Z` section above the previous release
    in `docs/releases.md`, separated by `---`. Group entries under
    `### Enhancements` and `### Bug Fixes`, using
    `* [#<issue>](<url>) - <summary>`. The first release uses only
    `* Initial release`, without subsections.

3. **Set the version.** Update `version = "X.Y.Z"` in `pyproject.toml` and
    `__version__ = "X.Y.Z"` in `netbox_scripts/__init__.py`. Both must match the
    version in the release tag. The workflow refuses to publish if they differ.
    Commit the release changes as `chore: release X.Y.Z`.

4. **Check the package contents.** Run:

    ```bash
    pre-commit run --hook-stage manual check-manifest
    ```

    This compares tracked files with the source distribution and reports missing
    or unexpected files.

5. **Tag the release commit.** Create an annotated tag and push it with the
    release commit:

    ```bash
    git tag -a vX.Y.Z -m "Release X.Y.Z"
    git push origin main vX.Y.Z
    ```

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

## Hotfix process

For an urgent fix to a published version, branch from its release tag and follow
the same preparation, packaging and publishing checks. Push the hotfix branch
and tag instead of using the checklist's `main` push command.

This example releases `1.2.4` from `v1.2.3`. Replace both versions with the actual
release numbers.

```bash
git switch -c hotfix/1.2.4 v1.2.3
```

Apply the fix, update the change log and both version declarations, and commit
the changes. After the checks pass, tag and push the hotfix:

```bash
git tag -a v1.2.4 -m "Release 1.2.4"
git push origin hotfix/1.2.4 v1.2.4
```

Continue with the GitHub release and package-verification steps. Merge the
hotfix branch into the default branch after publication.

## See also

- [Contributing](contributing.md) for the development workflow and review requirements.
