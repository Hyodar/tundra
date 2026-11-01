# Releasing tundravm

A `v*` tag releases tundravm. The tag is the version: `v2.0.0` builds `tundravm 2.0.0`,
and a commit after it reports `2.0.0.post1.dev<n>+g<sha>`.

## Cut a release

1. On a green master, move the Unreleased notes under the version:
   `uv run python scripts/release.py cut 2.0.0`.
2. Read the new `## 2.0.0` section of `CHANGELOG.md`, then commit it alone:
   `git commit -am "docs: the changelog releases 2.0.0"`.
3. Push master and wait for `ci` to pass on that commit.
4. Tag that commit and push the tag: `git tag -a v2.0.0 -m "tundravm 2.0.0"`, then
   `git push origin v2.0.0`.
5. Watch the `release` workflow, then read the release page it creates.

A tag whose `CHANGELOG.md` has no section for its version is refused, so a release always
carries its notes in the tagged tree. Nothing is committed by the workflow.

## What the workflow does

1. Runs `ci.yml`'s test job: lint, types, the unit suite, the Go tests of `tools/`.
2. Builds the sdist and the wheel with `uv build` and checks both carry the tag's version.
3. Installs the wheel in a clean venv: `--version` prints the tag's version, `doctor` runs,
   and the default tools source is `Git(<repo>, <the tag's commit>, subdir="tools")`.
4. Creates the GitHub release with the tag's `CHANGELOG.md` section as its body and both
   files attached.
5. Publishes to PyPI by trusted publishing, only once a `pypi` environment exists.

## What a user installs

```console
$ uv tool install git+https://github.com/Hyodar/tundra@v2.0.0
```

The tag pins the release, and so does the wheel attached to it. Its images build `tools/`
at the tag's commit, with no clone. mkosi is not on PyPI, so the wheel names mkosi 27.1 by
its git commit, which PyPI refuses: leave the `pypi` environment uncreated until mkosi is
on PyPI.
