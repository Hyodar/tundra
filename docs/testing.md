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
| `image.init_steps` | `InitStep(name, after, script, builtin)` in the order runtime-init runs them (the steps' topological order; `after` is every step it waits for, its `before=` edges included); the built-in `keys`, `disks` and `secrets` steps have `builtin=True` and no script |
| `image.hooks(stage)` | The `Hook`s of one stage (`postinst`, `finalize`...), in run order |
| `image.settings(section)` | `{key: values}` of one `mkosi.conf` section |
| `image.lint()` | The variant's findings, as `lint(recipe, variants=[variant])` reports them |
| `image.tree`, `image.resolved`, `image.recipe` | The raw `Tree` (for golden comparisons), the `Resolved` variant, the planned recipe |

```python
from tundravm import BootStep, File, Fragment, Package, Service, User
from tundravm.testing import assert_clean, fragment_plan

STATE = BootStep("app-state", "install -d -o app /var/lib/app")
APP = Fragment(
    "app",
    (
        Package("curl"),
        File("/usr/bin/app", "#!/bin/sh\nexec sleep infinity\n", mode=0o755),
        User("app"),
        STATE,
        Service("app", "/usr/bin/app", user="app", env={"PORT": "8080"}, needs=[STATE.ready]),
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
    assert unit.requires("runtime-init-app-state.service")
    assert unit.after("runtime-init-app-state.service")
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
fake_fragment(name="fake", *, packages=(), files=None, init=None, after=(), requires=(), checks=()) -> Fragment
```

A small fragment for testing how fragments compose: a `Package` per name, a `File` per `{path: content}`, a `BootStep` (running after the `after` handles) when `init` is given, and the `requires`/`checks` you pass.

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

## Booting an image in tests

`boot(artifact, *, timeout=300, memory="2G", cpus=2, firmware=None, kvm="auto", markers=DEFAULT_MARKERS, expect_units=(), cmdline=CONSOLE_CMDLINE, settle=3.0, log=None, network=True, qemu="qemu-system-x86_64", extra_args=(), forward=None, wait_http=()) -> BootResult` boots a baked image under QEMU with OVMF and reads its serial console. It boots the way `tundravm deploy --target qemu` boots without TDX: OVMF from pflash (a writable copy of the variable store beside the log) and the UKI through `-kernel`; a `.raw`, `.qcow2` or `.vhd` disk goes on a virtio drive with `snapshot=on`, so the artifact is never written. `artifact` is an `Artifact` from `bake_image`, a path, or a directory holding exactly one `.efi`.

Nothing is added to the image. The console settings reach the kernel through SMBIOS (`io.systemd.stub.kernel-cmdline-extra`, the way `mkosi vm` passes them), which systemd-stub appends to the UKI's own command line when Secure Boot is off: `CONSOLE_CMDLINE` is `console=ttyS0 systemd.show_status=yes systemd.status_unit_format=name systemd.log_color=no panic=-1`. QEMU runs with `-no-reboot`, no display and no monitor, a user-mode virtio NIC (`network=False` for none), and KVM when `/dev/kvm` is usable (`kvm="auto"`), TCG otherwise.

The boot ends when every marker (by default `Reached target minimal.target`, the target every tundravm image boots into) is on the console and every unit in `expect_units` (`"app"` means `app.service`) started or failed, plus `settle` seconds for a unit that fails just after; on a fatal line (`Kernel panic`, emergency or rescue mode, a dead PID 1); when QEMU exits; or after `timeout` seconds. `forward` maps host ports (on `127.0.0.1`) to guest TCP ports; with `wait_http`, the boot also waits, once the markers and units are in, until each URL answers with a 2xx. QEMU is then stopped. The `BootResult` holds:

| Field | |
|---|---|
| `console` | The serial console, ANSI escapes stripped; `log` is the file it was written to |
| `started`, `failed` | Units from systemd's status lines: `Started`/`Finished`/`Reached target`/`Listening on`/`Mounted`, and `Failed to start`/`Failed to mount`/`Dependency failed for` |
| `fatal` | Console lines that ended the boot |
| `missing` | Markers not seen, expected units not started and `wait_http` URLs that never answered |
| `http` | One `HttpResponse(url, status, body)` per `wait_http` URL; `http_get(url)` returns its body, or raises `AssertionError` when it never answered with a 2xx |
| `duration`, `timed_out`, `returncode`, `stderr`, `command` | Seconds taken, whether the timeout hit, QEMU's exit status (`None` when the harness stopped it), its output, and the command line |

`ok` is true when nothing is missing, failed or fatal and the boot did not time out; `assert_booted()` raises an `AssertionError` carrying `summary()` (the problems, QEMU's stderr and the console's last lines) otherwise. `boot` raises `BootUnavailable` when QEMU or OVMF is missing, and `boot_unavailable()` returns the reason (or `None`) for a `skipif`. The `boot` fixture wraps it: the log goes under `tmp_path` and a missing QEMU or OVMF skips the test.

```python
import pytest

from tundravm.backends.local_linux import LocalLinuxBackend
from tundravm.declarative.lifecycle import bake_image
from tundravm.recipe import load_file


@pytest.mark.boot
def test_image_boots(boot, tmp_path):
    lowered = load_file("image.py").lowered()
    backend = LocalLinuxBackend(privilege="sudo")
    _, artifacts = bake_image(lowered, ("default",), locked=None, backend=backend, build=tmp_path)
    (uki,) = [artifact for artifact in artifacts if artifact.target == "qemu"]
    result = boot(uki, expect_units=("app",), timeout=900)
    result.assert_booted()
    assert "app.service" in result.started
```

`scan_console(text)` reads a transcript the same way (`ConsoleScan(started, failed, fatal)`) for a log captured elsewhere. A booted check needs a real bake (mkosi and root), QEMU and OVMF; `tests/testing_toolkit/test_boot.py` covers the harness itself with a fake QEMU that prints a console transcript.

## Pytest fixtures

The `tundravm` pytest plugin is registered through an entry point, so installing tundravm makes its fixtures available (disable with `pytest -p no:tundravm`):

| Fixture | Gives |
|---|---|
| `recipe` | A minimal lint-clean `Recipe(name="test", items=(Package("linux-image-amd64"),))` with one `default` variant; the kernel package keeps `kernel-missing` quiet |
| `compiled` | `compiled(recipe, variants=None) -> CompiledTree`, compiled into a fresh directory under `tmp_path` |
| `run_cli` | The `run_cli` helper |
| `boot` | `boot(artifact, **kwargs) -> BootResult`: `tundravm.testing.boot` with the console log under `tmp_path`; skips the test when QEMU or OVMF is missing. The plugin also registers the `boot` marker |

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

- **`service`**: the generated `app.service` (`ExecStart=/usr/bin/busybox httpd -f -p 8080 -h /srv/www`, `User=app`, the strict sandbox) is linked into `minimal.target.wants`, Debian's busybox has its `httpd` applet, and `/srv/www/index.html` and the `app` user are in the tree.
- **`cloud`** (the `default` variant, plus two extra `Repository` declarations, one with `in_image=False`): `mkosi.conf` sets `Snapshot=20251113T083151Z` and no `Mirror=` or `SandboxTrees=`; `Backports` writes `mkosi.sandbox/etc/apt/sources.list.d/debian-backports.sources` and pins sid to 100, and the image still runs trixie's 6.12 kernel; `EfiStub` installs its pinned `systemd-boot-efi` from the image root and leaves no `.deb` behind; only the `in_image=True` repository is listed in the image's `/etc/apt`; `runtime-init.service` waits for `network-online.target` because the variant ships no `network-setup.service`.

```bash
uv run pytest tests/integration/test_templates_bake.py -m integration   # the template bakes alone
uv run pytest -m "not integration"                                      # everything but them
```

### Template boots

`tests/integration/test_templates_boot.py` (markers `integration` and `boot`) bakes the `minimal` and `service` templates' `default` variants as UKIs with `LocalLinuxBackend(privilege="sudo")` and boots each with `boot()` (4G of memory: the UKI unpacks the whole image into RAM). The service template boots unchanged: `boot(forward={HOST: 8080}, wait_http=("http://127.0.0.1:HOST/",))` forwards a free host port to its busybox httpd and the test asserts the page it serves, so DHCP, the nftables opening and the sandboxed service are proven end to end. It needs mkosi 25+, non-interactive `sudo`, `qemu-system-x86_64` and OVMF, and is skipped without them; `TUNDRAVM_BOOT_REQUIRED=1` turns the skip into a failure.

- **`minimal`**: reaches `minimal.target`, the `hello` task finishes, no unit fails.
- **`service`**: TeeBase, debloat, the curated kernel modules and the strict sandbox still reach `minimal.target`, `app.service` starts, no unit fails; `tdx_guest` ships but the VM has no TDX, so it stays unloaded and no console line reports a TDX failure.

CI's `boot` job installs mkosi, `qemu-system-x86` and `ovmf`, opens `/dev/kvm` to the runner user when the runner has it (the boots fall back to TCG otherwise), runs the file with `TUNDRAVM_BOOT_REQUIRED=1` and `TUNDRAVM_BOOT_TIMEOUT=1500`, and uploads the console logs (`TUNDRAVM_BOOT_LOGS`) when it fails. The `test` job leaves QEMU out, so there the boots skip.

```bash
uv run pytest tests/integration/test_templates_boot.py -m boot -v   # the template boots alone
```
