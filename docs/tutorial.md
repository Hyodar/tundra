# Tutorial

This walk-through builds a small image from an empty directory: write a recipe, inspect it, catch a broken reference with the linter, compile and diff the tree, lock it, bake, measure and deploy, then factor part of the recipe into a reusable fragment, test it and run the CI gate. Everything runs on the in-process backend, which needs no build tools and writes simulated artifacts. Every transcript below is real output; exit codes are shown as `[exit N]`.

You need Python 3.12+ and tundravm installed (`uv add tundravm`, or `uv sync` in this repository). Locking needs network access to resolve one git ref.

## 1. Start a project

```console
$ mkdir node && cd node
$ tundravm init . --backend inprocess
created node.py
created .gitignore
next:
  tundravm compile node.py --out mkosi
  tundravm lock node.py
  tundravm ci node.py --out mkosi
[exit 0]
```

The recipe is named after the directory (`--name` overrides it). `node.py` binds a `Recipe` to `recipe` and a backend to `backend`:

```python
from tundravm import Debloat, File, Fragment, Package, Recipe, Unit, User, Variant
from tundravm.backends.inprocess import InProcessBackend

APP_UNIT = """\
[Unit]
Description=app

[Service]
User=app
ExecStart=/usr/bin/true

[Install]
WantedBy=minimal.target
"""

backend = InProcessBackend()

recipe = Recipe(
    name="node",
    base="debian/trixie",
    common=Fragment(
        "node",
        items=(
            Package("systemd"),
            Package("curl"),
            Package("jq"),
            File("/etc/motd", "node\n"),
            User("app", shell="/bin/false"),
            Unit("app.service", APP_UNIT, enabled=True),
            Debloat(),
        ),
    ),
    variants=(
        Variant("default", target="qemu"),
        Variant("dev", parent="default", add=Fragment("dev", items=(Package("strace"),))),
    ),
)
```

`common` holds what every variant shares. `dev` inherits it and adds `strace`. The `.gitignore` block ignores `build/` but keeps `build/tundravm.lock`, which you commit.

## 2. Inspect

`inspect` is a dry run: what each variant will contain, without writing anything.

```console
$ tundravm inspect node.py --variant dev
Image: debian/trixie (x86_64)  variant=dev  reproducible=yes
Policy: mutable_ref_policy=warn network_mode=online require_frozen_lock=no require_integrity=yes
Parent: default
Fragments (2): node dev
Packages (4): curl jq strace systemd
Files (2):
  /etc/motd                            0644  5B  sha256:389b46f04457
  /usr/lib/systemd/system/app.service  0644  102B  sha256:b1b587049f47
Users (1):
  app  system  shell=/bin/false
Units: enable app.service
Hooks:
  finalize (1):
    sed -i '/^IMAGE_VERSION=/d' "$BUILDROOT/usr/lib/os-release"
Debloat: enabled, 30 paths removed, systemd minimize=yes
Targets: qemu
[exit 0]
```

`Parent:` is the variant `dev` builds on and `Fragments:` the fragments it includes: `common` (named `node`) and its own `add`. Without `--variant` every variant is shown. `--format json` adds the recipe digest the lockfile records; `--format markdown` renders tables for a CI job summary.

## 3. Lint a broken reference

Add an encrypted data disk. Edit `node.py`: import `Disk` and `Key`, create the key, and declare the disk, but forget to declare the key itself:

```python
from tundravm import Debloat, Disk, File, Fragment, Key, Package, Recipe, Unit, User, Variant
...
data_key = Key("data")
...
            Debloat(),
            Disk("data", mount="/data", key=data_key),
```

```console
$ tundravm lint node.py
error disk-key-undefined [default] data: disk 'data' uses key 'data', which this variant does not declare
error disk-key-undefined [dev] data: disk 'data' uses key 'data', which this variant does not declare
2 errors, 0 warnings, 0 infos
[exit 1]
```

The disk references the key object, so the resolver can tell that no variant declares it. Every other command refuses the recipe until it is fixed (`compile` exits 2 with `E_VALIDATION`). Add the key to the fragment:

```python
            Debloat(),
            data_key,
            Disk("data", mount="/data", key=data_key),
```

```console
$ tundravm lint node.py
warning source-unpinned [default] disk-encryption: source build 'disk-encryption' (git https://github.com/Hyodar/tundra-tools.git @ master) is not pinned in the lockfile
    hint: Run `tundravm lock RECIPE` to pin it, or declare an immutable source (Git(url, ref) with a 40-hex commit ref, or Http(url, sha256=...)).
warning source-unpinned [default] key-generation: source build 'key-generation' (git https://github.com/Hyodar/tundra-tools.git @ master) is not pinned in the lockfile
    hint: Run `tundravm lock RECIPE` to pin it, or declare an immutable source (Git(url, ref) with a 40-hex commit ref, or Http(url, sha256=...)).
warning source-unpinned [dev] disk-encryption: source build 'disk-encryption' (git https://github.com/Hyodar/tundra-tools.git @ master) is not pinned in the lockfile
    hint: Run `tundravm lock RECIPE` to pin it, or declare an immutable source (Git(url, ref) with a 40-hex commit ref, or Http(url, sha256=...)).
warning source-unpinned [dev] key-generation: source build 'key-generation' (git https://github.com/Hyodar/tundra-tools.git @ master) is not pinned in the lockfile
    hint: Run `tundravm lock RECIPE` to pin it, or declare an immutable source (Git(url, ref) with a 40-hex commit ref, or Http(url, sha256=...)).
0 errors, 4 warnings, 0 infos
[exit 0]
```

The errors are gone. The key and the disk need boot-time tools (`key-gen`, `disk-setup`) built from the `tundra-tools` repository, and those builds follow the `master` branch until you lock them. Warnings do not fail `lint` unless you pass `--strict`.

## 4. Lock and compile

```console
$ tundravm lock node.py
locked build/tundravm.lock
[exit 0]

$ tundravm compile node.py --out mkosi
compiled mkosi
  variants: default, dev
  digest:   1613e6bd0283fb0f6af93f78f35d5bcf0fa717c1116f91afaab3d12bde8b5bca
[exit 0]

$ tundravm lint node.py
no findings
[exit 0]
```

The lockfile records a digest per recipe section and the commit `master` resolved to for each source build, which is why the warnings disappeared. Lock before you compile: `compile`, `diff` and `bake` read `build/tundravm.lock` when it exists, so the build scripts in the tree fetch the pinned commit. `mkosi/` holds one directory per variant (`mkosi.conf`, `mkosi.extra/`, `mkosi.skeleton/`, `scripts/`). Commit it: it is the reviewable form of the image.

## 5. Change the recipe and see the diff

Add `Package("htop")` after `Package("jq")`, then ask what changed:

```console
$ tundravm diff node.py --against mkosi --variant default
diff --git a/default/mkosi.conf b/default/mkosi.conf
--- a/default/mkosi.conf
+++ b/default/mkosi.conf
@@ -19,6 +19,7 @@
 Packages=
     cryptsetup
     curl
+    htop
     jq
     systemd
     tpm2-tools
[exit 1]

$ tundravm compile node.py --out mkosi --check
M  default/mkosi.conf
M  dev/mkosi.conf
2 files changed
[exit 1]
```

`diff` and `compile --check` exit 1 when the tree is stale. That is the CI gate for a committed tree.

## 6. Lock drift

The lockfile drifted too, section by section:

```console
$ tundravm lock node.py --check
~ variants.default.packages: +htop
~ variants.dev.packages: +htop
[exit 1]

$ tundravm lock node.py --check --variant default
~ variants.default.packages: +htop
[exit 1]

$ tundravm lock node.py
locked build/tundravm.lock
[exit 0]

$ tundravm lock node.py --check
lock is up to date
[exit 0]

$ tundravm compile node.py --out mkosi
compiled mkosi
  variants: default, dev
  digest:   4f9338d43dd3afa50b9ac2d614f16b7f4586ea3867055c6f4b4d17f018b7e2c3
[exit 0]
```

Each line names a section, `variants.<variant>.<key>`, and what changed in it. `--variant` limits the check to those variants' sections (and the recipe-wide ones), so one lockfile of every variant serves any selection. Re-locking keeps the existing source pins; `tundravm lock node.py --update key-generation` would resolve that one source again.

## 7. Bake

```console
$ tundravm bake node.py
[tundravm] lint ...
[tundravm] lint ... ok (0.0s)
[tundravm] verify lockfile ...
[tundravm] verify lockfile ... ok (0.0s)
[tundravm] compile ...
[tundravm] compile ... ok (0.0s)
[default] prepare inprocess ...
[default] prepare inprocess ... ok (0.0s)
[default] build via inprocess ...
[default] build via inprocess ... ok (0.0s)
[default] artifact qemu build/default/disk.qcow2 (114 B)
[default] report build/default/report.json
[dev] prepare inprocess ...
[dev] prepare inprocess ... ok (0.0s)
[dev] build via inprocess ...
[dev] build via inprocess ... ok (0.0s)
[dev] artifact qemu build/dev/disk.qcow2 (110 B)
[dev] report build/dev/report.json
[tundravm] baked 2 variants in 0.0s

variant  target  artifact                  size   sha256        time
default  qemu    build/default/disk.qcow2  114 B  1fa043adea90  0.0s
dev      qemu    build/dev/disk.qcow2      110 B  9cfb8b5c2f31  0.0s
next: tundravm deploy build/bake-result.json --variant default --target qemu
[exit 0]
```

Progress lines go to stderr and the summary to stdout. The bake was frozen against `build/tundravm.lock`: had the recipe drifted from it, the `verify lockfile` step would have failed with `E_LOCKFILE`. The backend came from `node.py`; `--backend lima|nix|local|inprocess` overrides it. The manifest `build/bake-result.json` records every artifact with its digest, the recipe digest and the tree digest.

## 8. Measure

```console
$ tundravm measure build --variant default
error [E_MEASUREMENT]: Refusing to measure a simulated artifact.
Hint: Bake with a real backend, or pass allow_placeholder=True for test values.
  variant: default
  path: build/default/disk.qcow2
[exit 2]

$ tundravm measure build --variant default --allow-placeholder
PLACEHOLDER: not real measurements. These values are derived from artifact digests; never put them in an attestation policy.
measurements default (rtmr)
source: placeholder (build/default/disk.qcow2)
  RTMR0  7d4a783e463026f1bf368940a2756fecfa424b441f168a9ff7379e64694193e7
  RTMR1  dd1479258b13a05aae3225aafd02ed18734c750d8942fb0b90287461f5bcbbcd
  RTMR2  44d2ceafa105d213275b16a09db56cc14a2e5ca83a1233bbed1c806d31d53279
[exit 0]
```

`measure` takes the manifest (or the directory holding it). With a real bake and `measured-boot` or `dstack-mr` on `PATH`, the `source:` line names that tool instead of `placeholder`.

## 9. Deploy

```console
$ tundravm deploy build --variant default --target qemu
error [E_DEPLOYMENT]: Refusing to deploy a simulated artifact.
Hint: Bake with a real backend first.
  variant: default
  path: build/default/disk.qcow2
[exit 2]

$ tundravm deploy build --variant default --target qemu --allow-placeholder
error [E_DEPLOYMENT]: QEMU binary not found: qemu-system-x86_64
Hint: Install QEMU and ensure it is in PATH.
  binary: qemu-system-x86_64
[exit 2]
```

On a host with QEMU and a real bake, `deploy` boots the qcow2 and prints the deployment id and endpoint. Target settings are `--param KEY=VALUE` (`memory`, `cpus`, `ssh_port`, `tdx`, `daemonize` for qemu).

## 10. Factor out a fragment

Say every image that has the data disk should also prepare a directory for the app on it at boot. Write that as a `Composite`, a `Fragment` whose configuration is its own fields, in `fragments.py` next to the recipe:

```python
from dataclasses import dataclass

from tundravm import Diagnostic, Disk, Fragment, Init, Resolved
from tundravm.declarative.utils import Composite


def _needs_data_disk(image: Resolved) -> tuple[Diagnostic, ...]:
    if any(isinstance(item, Disk) and item.mount == "/data" for item in image.items):
        return ()
    return (Diagnostic("app-data-missing", "AppData needs a disk mounted at /data", subject="app-data"),)


@dataclass(frozen=True, slots=True, kw_only=True)
class AppData(Composite):
    """A per-boot directory for the app on the encrypted /data disk."""

    owner: str = "app"

    def compose(self) -> Fragment:
        return Fragment(
            "app-data",
            requires=("node",),  # the fragment that declares the owner account
            checks=(_needs_data_disk,),
            items=(
                Init("app-data-dir", f"install -d -o {self.owner} -m 0750 /data/app\n", priority=25, after=("disks",)),
            ),
        )
```

- `AppData(owner="svc")` is itself a `Fragment`: it goes in `common`, a variant's `add`, or another fragment's `items`. `compose()` runs once, at construction.
- `requires` names fragments that must be in the same variant; without the `node` fragment, lint reports `fragment-requires-missing`.
- `checks` are functions from the resolved variant to diagnostics; they run during `lint` (and `resolve`, which fails on errors).
- The `Init` step runs in `/usr/bin/runtime-init` at priority 25, after the built-in `disks` step (20) has opened and mounted `/data`.

Use it in `node.py` (`from fragments import AppData`, then `AppData(),` after the `Disk`):

```console
$ tundravm lint node.py
no findings
[exit 0]

$ tundravm inspect node.py --variant default | grep -E "Fragments|Runtime init"
Fragments (2): node app-data
Runtime init: 3 (priorities: 10, 20, 25)

$ tundravm compile node.py --out mkosi
compiled mkosi
  variants: default, dev
  digest:   20cd2ce39754b51bca26d99902f913c981bd1fc671b041ddc0dd532a162001b2
[exit 0]

$ cat mkosi/default/mkosi.extra/usr/bin/runtime-init
#!/bin/bash
set -euo pipefail

/usr/bin/key-gen setup /etc/tdx/key-gen.yaml

/usr/bin/disk-setup setup /etc/tdx/disk-setup.yaml

install -d -o app -m 0750 /data/app
[exit 0]

$ tundravm lock node.py
locked build/tundravm.lock
[exit 0]
```

The recipe directory is importable while the recipe loads, so `fragments.py` needs no packaging. The [fragment guide](module-authoring.md) covers builds, units and ordering in depth.

## 11. Test it

`tundravm.testing` works on the same values the lifecycle returns. `test_node.py`:

```python
from pathlib import Path

from fragments import AppData
from node import recipe

from tundravm import Fragment, Recipe, Variant, compile, lint, read_lock
from tundravm.testing import assert_clean, assert_diagnostic, assert_tree

HERE = Path(__file__).parent


def test_recipe_is_clean():
    assert_clean(lint(recipe))


def test_app_data_needs_the_disk():
    bare = Recipe(
        name="bare",
        common=Fragment("node", items=(AppData(),)),
        variants=(Variant("default", target="qemu"),),
    )
    assert_diagnostic(lint(bare), "app-data-missing", variant="default", subject="app-data")


def test_committed_tree_is_current():
    locked = read_lock(HERE / "build" / "tundravm.lock")
    assert_tree(compile(recipe, lock=locked), HERE / "mkosi")
```

```console
$ uv run pytest -q test_node.py
...                                                                      [100%]
3 passed in 0.03s
[exit 0]
```

- `assert_clean(lint(recipe))` fails on errors and, for a list of diagnostics, on warnings too (`allow=("code",)` to accept some).
- `assert_diagnostic` returns the first diagnostic matching every field you give, or fails listing what was found.
- `assert_tree` compares every path, byte, exec bit and symlink with a committed tree. `compile(recipe)` without `lock=` ignores the lockfile, so pass the lock when the committed tree was compiled with pins. Run with `TUNDRAVM_UPDATE_GOLDEN=1` to rewrite the golden tree instead.

Run the tests from the project directory: `lint()` reads `build/tundravm.lock` relative to it for `source-unpinned`.

## 12. CI

`tundravm ci` runs `lint --strict`, `compile --check` and `lock --check` and stops at the first failure:

```console
$ tundravm ci node.py --out mkosi
ok lint: no findings
ok compile: mkosi is up to date
ok lock: build/tundravm.lock is up to date
[exit 0]
```

A failing step prints its report and `FAIL`, the remaining steps print `skip`, and the command exits 1. Commit `mkosi/` and `build/tundravm.lock`; `tundravm init --ci github` writes a workflow that runs this gate on every push. See [CLI: CI](cli.md#ci).
