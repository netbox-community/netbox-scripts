# Releasing

We follow [Semantic Versioning](https://semver.org/) and keep a Change Log in `docs/releases.md`.

## Checklist

1. **Finish code and docs.** Confirm CI is green on the default branch, all
   relevant documentation updates have landed, and `docs/releases.md`
   reflects the changes in this release.

2. **Change log.** Add a new `## vX.Y.Z` section at the top of
   `docs/releases.md`, above the previous release and separated from it by a
   `---` rule. Group entries under `### Enhancements` and `### Bug Fixes`
   subsections, each bullet `* [#<issue>](<url>) - <summary>`. The very first
   release is a bare `* Initial release` with no subsections.

3. **Version bump.** Update `version = "X.Y.Z"` in `pyproject.toml` and
   `__version__ = "X.Y.Z"` in `netbox_scripts/__init__.py`. The release
   workflow compares the tag against both pins and refuses to publish if
   any of the three disagree. Commit with a message of the form
   `chore: release X.Y.Z`.

4. **Packaging.** Run `pre-commit run --hook-stage manual check-manifest`.
   It compares what git tracks against what the sdist would carry, so a
   file missing from the distribution, or one that does not belong in it,
   is caught before the tag exists.

5. **Tag.** Create an annotated tag matching the version and push it
   together with the release commit:

   ```bash
   git tag -a vX.Y.Z -m "Release X.Y.Z"
   git push origin main vX.Y.Z
   ```

6. **Publish release.** Draft a GitHub release from the new tag, review
   the auto-generated notes against `docs/releases.md`, edit as needed,
   and publish.

7. **CI.** Publishing the release fires `.github/workflows/release.yml`,
   which builds the wheel and sdist, validates them with `twine check`,
   and publishes to PyPI through the Trusted Publisher registered for
   this repository. Watch the run for failures, especially `twine check`
   and the OIDC token exchange.

8. **Post-release.** Verify the release on
   <https://pypi.org/project/netbox-scripts/> and that a fresh
   `pip install netbox-scripts` resolves the new version.

## Hotfix process

For an urgent fix on a published version, branch from the release tag,
apply the fix, and follow the same checklist with the patched version
number. Substitute the real version numbers:

```bash
# Example: release a 0.0.2 fix for 0.0.1.
git switch -c hotfix/0.0.2 v0.0.1
# Apply the fix, update the changelog and bump both version declarations.
git tag -a v0.0.2 -m "Release 0.0.2"
git push origin hotfix/0.0.2 v0.0.2
```

Merge the hotfix branch back into the default branch after the release
goes out.

## See also

- [Contributing](contributing.md) for the pre-release development flow.
