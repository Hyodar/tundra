# Testing recipes and fragments

`tundravm.testing` checks recipes and fragments without a build backend. A test plans a fragment or a recipe in memory and asks the result typed questions (which packages, which files, how a unit is wired), so it reads like the code it tests. Importing it does not import pytest.

```python
from tundravm.testing import assert_clean, assert_diagnostic, fragment_plan, plan
```

## Fragment plans

```python
fragment_plan(*items, include=(), base=None, variant="default", lock=None) -> Image
plan(recipe, variant=None, *, lock=None) -> Image
```

`fragment_plan` puts a fragment (or bare declarations) on `TEST_PLATFORM`, a minimal bootable platform holding the kernel package, so `kernel-missing` stays quiet and only the fragment's own findings remain. `include=` adds what the fragment needs without being under test, such as the fragment its `requires` names. `base=` replaces the platform: another fragment, or a whole `Recipe` whose `items` gain them (then `variant=` picks one of its variants). `plan` takes a whole recipe and one of its variants (default: the first).

An `Image` is one variant. It compiles in memory, with no temporary directory to manage, the first time a query reads the tree, so `image.lint()` still works on a recipe that does not compile. Shipped things come from the compiled tree, declared things from the resolved variant:

| Query | Returns |
|---|---|
| `image.packages` | `{name: "runtime" or "build"}`, from the compiled `mkosi.conf`, including packages the compiler adds (`passwd`, the runtime tools' packages) |
| `image.file(path)`, `image.files` | `ImageFile`: `content` (bytes), `text`, `mode`, `stage` (`extra` or `skeleton`), `owner` (always `root`), `origin` (its provenance, empty for files the compiler writes) and `fragment` (the innermost fragment that declared it); `files` maps every absolute path to one |
| `image.unit(name)`, `image.service(name)` | `UnitView` of a shipped unit (`.service` is optional): `get(section, key)`, `values(section, key)`, `requires(unit)`, `wants(unit)`, `after(unit)`, `wanted_by`, `exec_start`, `user`, `environment` (a dict), `enabled`, `text` |
| `image.users`, `image.groups`, `image.builds` | The declared `User`, `Group` and `Build` values, by name |
| `image.init_steps` | `InitStep(name, priority, after, script, builtin)` in the order runtime-init runs them; the built-in `keys`, `disks` and `secrets` steps have `builtin=True` and no script |
| `image.hooks(stage)` | The `Hook`s of one stage (`postinst`, `finalize`...), in run order |
| `image.settings(section)` | `{key: values}` of one `mkosi.conf` section |
| `image.lint()` | The variant's findings, as `lint(recipe, variants=[variant])` reports them |
| `image.tree`, `image.resolved`, `image.recipe` | The raw `Tree` (for golden comparisons), the `Resolved` variant, the planned recipe |

```python
from tundravm import File, Fragment, Init, Package, Service, User
from tundravm.testing import assert_clean, fragment_plan

APP = Fragment(
    "app",
    (
        Package("curl"),
        File("/usr/bin/app", "#!/bin/sh\nexec sleep infinity\n", mode=0o755),
        User("app"),
        Init("app-state", "install -d -o app /var/lib/app", priority=40),
        Service("app", "/usr/bin/app", user="app", env={"PORT": "8080"}),
    ),
)


def test_app_is_clean():
    assert_clean(fragment_plan(APP))


def test_app_ships_and_wires_its_service():
    image = fragment_plan(APP)
    assert image.packages["curl"] == "runtime"
    assert image.file("/usr/bin/app").mode & 0o111
    unit = image.service("app")
    assert unit.enabled and unit.user == "app"
    assert unit.requires("runtime-init.service") and unit.after("runtime-init.service")
    assert unit.environment == {"PORT": "8080"}
    assert [step.name for step in image.init_steps] == ["app-state"]
```

A whole recipe works the same way, one variant at a time:

```python
from tundravm import Fragment, Package, Recipe, Variant
from tundravm.testing import plan

RECIPE = Recipe(
    name="web",
    items=(Package("linux-image-amd64"),),
    variants=(
        Variant("default", outputs=("qemu",)),
        Variant("azure", outputs=("azure",), add=Fragment("azure-tools", (Package("jq"),))),
    ),
)


def test_only_azure_ships_jq():
    assert plan(RECIPE, "azure").packages["jq"] == "runtime"
    assert "jq" not in plan(RECIPE).packages
```

## Diagnostics

```python
assert_clean(subject, /, *, warnings="fail", allow=(), variants=None) -> diagnostics
assert_diagnostic(subject, code, /, *, severity=None, variant=None, subject=None, location=None, variants=None) -> Diagnostic
```

Both take an `Image`, a `Recipe` (linted for `variants=`, default: all) or the diagnostics `lint()` returned.

- `assert_clean(image)` fails listing every error and warning whose code is not in `allow`, for every kind of input. `warnings="allow"` fails on errors only.
- `assert_diagnostic(image, "disk-key-undefined", variant="default")` returns the first diagnostic matching every field you give, or fails naming the fields it wanted (`no diagnostic with code='disk-key-undefined', variant='default'; found:`) and listing what was found. `variant` is the variant, `""` for the recipe as a whole (the common fragment, the lockfile, the host); `location` matches a finding's source position (`file:line`).

```python
from tundravm import Disk, Key, Service
from tundravm.testing import assert_diagnostic, fragment_plan


def test_disk_without_its_key():
    image = fragment_plan(Disk("data", mount="/data", key=Key("data")))
    found = assert_diagnostic(image, "disk-key-undefined", variant="default", subject="data")
    assert found.severity == "error"


def test_service_needs_its_user():
    image = fragment_plan(Service("app", "/usr/bin/app", user="ghost"))
    assert assert_diagnostic(image, "service-user-missing", subject="app").severity == "error"
```

## Trees on disk

```python
compile_tree(recipe, *, variants=None, path=None) -> CompiledTree
```

For a test that needs real files (a CLI run, a tool reading the tree), `compile_tree` writes the recipe (every variant, or the names in `variants`) into `path` or a fresh temporary directory, without a lockfile. `CompiledTree` has `root`, `variants` and `default_variant`; `variant(name)` is a variant's directory, and `files`, `read`, `exists`, `path`, `conf`, `unit`, `script` and `runtime_init` take an optional `variant=` (default: the first compiled variant). Prefer `fragment_plan` and `plan` for assertions: they need no disk and answer typed questions.

## Golden trees

```python
assert_tree(tree: Tree, golden: str | Path, *, update=None) -> None
```

Fails unless `golden` holds exactly `tree`: every file path, its bytes, its executable bit and every symlink target. Empty directories are ignored, because git does not keep them. With `update=True`, or `TUNDRAVM_UPDATE_GOLDEN=1` in the environment, it writes `tree` to `golden` instead.

```python
from pathlib import Path

from tundravm import compile, read_lock


def test_committed_tree_is_current():
    locked = read_lock(Path("build/tundravm.lock"))
    assert_tree(compile(recipe, lock=locked), Path("mkosi"))
```

`compile(recipe)` without `lock=` ignores any lockfile and builds sources from their refs, while `tundravm compile` applies `build/tundravm.lock` when it exists. Compare like with like: pass the lock when the committed tree was compiled with one.

To pin one variant of a fragment's output, compare its plan's tree: `assert_tree(fragment_plan(APP).tree, Path("tests/golden/app"))` (the tree's top directory is the variant, `default`).

```bash
TUNDRAVM_UPDATE_GOLDEN=1 uv run pytest tests/test_recipe.py   # accept the new tree, then review the git diff
```

## Simulated artifacts

```python
fake_bake(tree: Tree, *, variant: str, target: Target, build: str | Path) -> Artifact
```

Writes a simulated disk file for one variant into the build directory `build` and records it in its `bake-result.json` (merging with one already there), so `read_artifacts()`, `tundravm measure` and `tundravm deploy` find it. The artifact is `simulated`: `measure` refuses it unless `allow_placeholder=True`, and `deploy` unless `allow_simulated=True` (`tundravm deploy --allow-simulated-artifact`). Both first check the file against the sha256 recorded in `bake-result.json` (`verify_artifact`), so a test that rewrites the disk file gets `ArtifactError`.

```python
from tundravm import compile
from tundravm.declarative import measure
from tundravm.testing import fake_bake


def test_measure_placeholder(recipe, tmp_path):  # `recipe` is the plugin's minimal fixture
    artifact = fake_bake(compile(recipe), variant="default", target="qemu", build=tmp_path)
    assert measure(artifact, allow_placeholder=True).tool == "placeholder"
```

For a full bake without tools, `bake_in_process(recipe, build=tmp_path)` runs the real pipeline (lint, lock check, compile) on the in-process backend and returns simulated artifacts. Without `lock=` it locks offline first, which fails for a recipe with source builds: pass a `Lock` (for example `lock(recipe, resolver=...)`) for those.

## Reproducibility, SBOM and evidence in tests

The in-process backend is deterministic, so a test can bake twice and compare digests, or let `bake(..., verify_reproducible=True)` do it (it raises `ReproducibilityError` on a mismatch, as `tundravm bake --verify-reproducible` does). `sbom(artifact, lock=...)` returns the bill of materials as values: `Sbom.packages` and `Sbom.sources` are tuples of `Component` (`name`, `version`, `url`, `ref`, `commit`, `installs`, ...), and `Sbom.render(format)` gives the document `tundravm sbom` writes.

```python
from tundravm import Backend, bake, lock, sbom
from tundravm.testing import bake_in_process


def test_bakes_the_same_bytes_twice(recipe, tmp_path):
    first = bake_in_process(recipe, build=tmp_path / "a")
    second = bake_in_process(recipe, build=tmp_path / "b")
    assert [a.sha256 for a in first] == [a.sha256 for a in second]


def test_bake_verifies_itself(recipe, tmp_path):
    locked = lock(recipe, offline=True)
    bake(recipe, lock=locked, backend=Backend("inprocess"), build=tmp_path, verify_reproducible=True)


def test_sbom_lists_the_declared_kernel(recipe, tmp_path):
    locked = lock(recipe, offline=True)
    (artifact,) = bake_in_process(recipe, build=tmp_path, lock=locked)
    bom = sbom(artifact, lock=locked)
    assert {p.name for p in bom.packages} == {"linux-image-amd64"}
    assert all(p.declared and not p.version for p in bom.packages)  # simulated: no mkosi manifest
    assert bom.sources == ()
```

An in-process bake leaves no mkosi manifest, so its packages are the ones the recipe declares, unversioned (`Component.declared`, `Sbom.manifest_found` false). For a recipe with source builds, lock with a `resolver=` and assert on the pins: `{s.name: s.commit for s in bom.sources}`. To test against versioned packages, pass `manifest=` a JSON file in mkosi's shape (`{"manifest_version": 1, "config": {...}, "packages": [{"type", "name", "version", "architecture"}]}`).

`evidence(recipe, build=tmp_path, lock=locked)` reads a bake back and returns its verdict as a value, so a test can assert that the bake would pass review: `Evidence.passed` (`Evidence.verdict` is `pass`, `fail` or `incomplete`, and `Evidence.reasons` says why), or `Evidence.index["verdict"]` for each check (`integrity`, `lock`, `reproducible`, `lint`). `require=("reproducible",)` makes an unchecked reproducibility `incomplete` instead of a pass, as `evidence --require` does. A bake never run with `verify_reproducible=True` reads `not checked`, which still passes; an artifact changed after the bake reads `mismatch` and fails. The `secret-in-file` and `secret-in-env` rules are tested like any lint rule: plant a key or token in a `File`, `Template` or `Service` (`env`, `exec_start`...) and assert the diagnostic, then check that `allow_secret=True` exempts it. The planted key needs a body after its PEM header: a header alone does not trigger the rule.

```python
from tundravm import File, Fragment, Package, Recipe, evidence, lint, lock
from tundravm.testing import assert_diagnostic, bake_in_process

KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)


def image(*items):
    return Recipe(name="t", items=(Package("linux-image-amd64"), *items))


def test_the_bake_passes_review(tmp_path):
    recipe = image()
    locked = lock(recipe, offline=True)
    bake_in_process(recipe, build=tmp_path, lock=locked)
    record = evidence(recipe, build=tmp_path, lock=locked)
    assert record.passed, record.index["verdict"]
    assert record.index["verdict"]["reproducible"] == "not checked"


def test_a_changed_artifact_fails_review(tmp_path):
    recipe = image()
    locked = lock(recipe, offline=True)
    (artifact,) = bake_in_process(recipe, build=tmp_path, lock=locked)
    artifact.path.write_bytes(artifact.path.read_bytes() + b"x")
    assert evidence(recipe, build=tmp_path, lock=locked).index["verdict"]["integrity"] == "mismatch"


def test_a_baked_key_is_refused():
    found = lint(image(File("/etc/app/deploy.key", KEY)))
    assert_diagnostic(found, "secret-in-file", variant="default", subject="/etc/app/deploy.key")


def test_allow_secret_exempts_a_lookalike():
    found = lint(image(File("/etc/app/deploy.key", KEY, allow_secret=True)))
    assert "secret-in-file" not in {d.code for d in found}
```

## Composition fakes

```python
fake_fragment(name="fake", *, packages=(), files=None, init=None, priority=50, requires=(), checks=()) -> Fragment
```

A small fragment for testing how fragments compose: a `Package` per name, a `File` per `{path: content}`, an `Init` step when `init` is given, and the `requires`/`checks` you pass.

## CLI tests

```python
recipe_file(tmp_path, source, name="recipe.py") -> Path
run_cli(*argv) -> tuple[int, str, str]
```

`recipe_file` writes dedented source; `run_cli` runs `tundravm` in-process and returns the exit code, stdout and stderr.

```python
from tundravm.testing import recipe_file, run_cli


def test_lint_fails_on_a_dangling_key(tmp_path):
    path = recipe_file(tmp_path, """
        from tundravm import Disk, Fragment, Key, Recipe
        recipe = Recipe(name="t", items=(Disk("data", mount="/data", key=Key("data")),))
    """)
    code, out, err = run_cli("lint", path)
    assert code == 1
    assert "disk-key-undefined" in out
```

`watch` polls until Ctrl-C, so `run_cli("watch", ...)` never returns. To drive it from a test, or from tooling of your own, call `tundravm.cli.main(argv, stdout=out, ticks=...)`: `ticks` is any iterable, and `watch` polls once per item (after sleeping `--interval`, so pass a tiny one) and returns 0 when it runs out. A generator can edit the recipe between `yield`s, so each poll after an edit prints that edit's line, and raising `KeyboardInterrupt` from it is Ctrl-C: `watch` prints `stopped` and returns 0. The first line is the `watching ... (Ctrl-C stops)` header, then one `HH:MM:SS` line for the check at start and one per change. Underneath, `tundravm.watch.Watch(sources, check, out, interval=, sleep=, clock=)` is the bare loop, with `run(ticks)`, for a `check` of your own.

```python
import io

from tundravm.cli import main
from tundravm.testing import recipe_file


def test_watch_reports_a_stale_tree(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = recipe_file(tmp_path, """
        from tundravm import File, Fragment, Package, Recipe
        recipe = Recipe(name="t", items=(Package("linux-image-amd64"), File("/etc/motd", "hi\\n")))
    """)
    assert main(["compile", str(path)], stdout=io.StringIO()) == 0

    def ticks():
        yield  # one poll: nothing changed
        path.write_text(path.read_text().replace('"hi', '"hello'))
        yield  # the next poll sees the edit
        raise KeyboardInterrupt  # what Ctrl-C does

    out = io.StringIO()
    assert main(["watch", str(path), "--interval", "0.001"], stdout=out, ticks=ticks()) == 0
    header, start, edited, stopped = out.getvalue().splitlines()
    assert start.endswith("; tree up to date")
    assert edited.endswith("; tree stale (1 file)")
    assert stopped == "stopped"
```

## Pytest fixtures

The `tundravm` pytest plugin is registered through an entry point, so installing tundravm makes its fixtures available (disable with `pytest -p no:tundravm`):

| Fixture | Gives |
|---|---|
| `recipe` | A minimal lint-clean `Recipe(name="test", items=(Package("linux-image-amd64"),))` with one `default` variant; the kernel package keeps `kernel-missing` quiet |
| `compiled` | `compiled(recipe, variants=None) -> CompiledTree`, compiled into a fresh directory under `tmp_path` |
| `run_cli` | The `run_cli` helper |

A test module that defines its own `recipe` fixture overrides the plugin's.

## Running the repository's tests

```bash
uv run pytest                                  # everything
uv run pytest tests/compiler/test_surge_golden.py  # the surge recipe against its committed tree
uv run pytest tests/unit/test_public_surface.py    # tundravm.__all__ against the frozen list
```

`tests/unit/test_public_surface.py` freezes `tundravm.__all__` and checks that every name in the `__all__` of `tundravm`, `tundravm.declarative` and `tundravm.declarative.utils` imports. Adding, renaming or removing a public name means updating its `EXPECTED_TOP_LEVEL` list in the same change, so the API change is visible in review.

### Template bakes

`tests/integration/test_templates_bake.py` (marker `integration`) bakes two `init` templates for real with `LocalLinuxBackend(privilege="sudo", mkosi_args=["--format=directory"])`, so it needs mkosi 25+ and non-interactive `sudo` and is skipped without them. The directory format skips the UKI and the disk image but still runs every build script and installs the kernel, which is why the CI workflow installs `python3-pefile` next to mkosi and `uv run pytest` runs both bakes on every push.

- **`service`**: the generated `app.service` (its `ExecStart`, `User=app`, `Requires=runtime-init.service`) is linked into `minimal.target.wants`, the runtime-init step creates `/var/lib/app`, and `/etc/app/app.conf` and the `app` user are in the tree.
- **`cloud`** (the `default` variant, plus two extra `Repository` declarations, one with `in_image=False`): `mkosi.conf` sets `Snapshot=20251113T083151Z` and no `Mirror=` or `SandboxTrees=`; `Backports` writes `mkosi.sandbox/etc/apt/sources.list.d/debian-backports.sources` and pins sid to 100, and the image still runs trixie's 6.12 kernel; `EfiStub` installs its pinned `systemd-boot-efi` from the image root and leaves no `.deb` behind; only the `in_image=True` repository is listed in the image's `/etc/apt`; `runtime-init.service` waits for `network-online.target` because the variant ships no `network-setup.service`.

```bash
uv run pytest tests/integration/test_templates_bake.py -m integration   # the template bakes alone
uv run pytest -m "not integration"                                      # everything but them
```
