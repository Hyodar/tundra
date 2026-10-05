# Tutorial

This walk-through starts from an empty directory and follows the project `tundravm init` writes: inspect the starter recipe, lint it (and see what happens without a kernel), add a variant, lock and compile, change the recipe and see the diff and the lock drift, bake, measure and deploy, then write a fragment of your own, test it next to the generated tests and run the CI gate. Everything runs on the in-process backend, which needs no build tools and writes simulated artifacts. Every transcript below is real output; exit codes are shown as `[exit N]`.

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), and the `tundravm` command (`uv sync` in this repository; tundravm is not on PyPI yet). Step 11 runs the project's tests with `uv run`, which installs tundravm into the project's own environment, so run `uv add --editable PATH/TO/tundravm` in the project first, as the last line of `init`'s next steps says.

## 1. Start a project

```console
$ mkdir node && cd node
$ tundravm init . --backend inprocess
created node.py
created tests/test_node.py
created pyproject.toml
created README.md
created .gitignore
lint node.py: no findings
next:
  1. tundravm lock node.py                 record recipe sections and pin source repositories/downloads in build/tundravm.lock
  2. tundravm compile node.py --out mkosi  write the mkosi tree; commit it
  3. uv run pytest tests                   run test_node.py against mkosi/
  4. tundravm ci node.py --out mkosi       the lint, tree and lockfile checks CI runs
  5. tundravm bake node.py --out build     build the image
  tundravm is not on PyPI yet: `uv add --editable PATH/TO/tundravm` uses a local checkout

checking the inprocess backend (tundravm doctor --backend inprocess):
tundravm 0.1.0
python 3.12.3
backend inprocess: available
  no external tools required
measurement tools:
  missing (optional) measured-boot — Real RTMR measurements need it. Install measured-boot or dstack-mr and make sure it is on PATH.
  missing (optional) dstack-mr — Real RTMR measurements need it. Install measured-boot or dstack-mr and make sure it is on PATH.
[exit 0]
```

The recipe is named after the directory (`--name` overrides it). `init` writes the `service` starter (`--template` picks another, `tundravm init --list-templates` lists them), a tests module for it, and `pyproject.toml` and `README.md` when they are absent. It lints the recipe, prints the next steps, and ends with `tundravm doctor --backend inprocess`, which needs no tools; the measurement tools are optional (see [Measure](#8-measure)). `--no-doctor` skips the probe. For tab completion of verbs and flags, see [CLI: Shell completion](cli.md#shell-completion).

`node.py` binds a `Recipe` to `recipe` and a backend to `backend`. Below its docstring and imports:

```python
APP_VERSION = "0.1.0"
APP_PORT = 8080


@dataclass(frozen=True, slots=True, kw_only=True)
class App(Composite):
    """The app: its account, config file, a first-boot step, the service and a lint rule.

    Ship ``/usr/bin/app`` with a ``Build(...)`` or a ``Package(...)``. Fragment name: ``app``.
    """

    version: str
    port: int

    def compose(self) -> Fragment:
        config = f"version={self.version}\nlisten=0.0.0.0:{self.port}\n"
        return Fragment(
            "app",
            items=(
                User("app", home="/var/lib/app"),
                File("/etc/app/app.conf", config),
                # Runs in runtime-init before the service starts (after_init=True).
                Init("app-state", "mkdir -p /var/lib/app && chown app:app /var/lib/app"),
                Service(
                    "app",
                    "/usr/bin/app --config /etc/app/app.conf",
                    description=f"app {self.version}",
                    user="app",
                    working_dir="/var/lib/app",
                    restart="on-failure",
                    wanted_by="minimal.target",
                ),
            ),
            checks=(self.check_port,),
        )

    def check_port(self, resolved: Resolved) -> tuple[Diagnostic, ...]:
        """Lint rule: the app runs as a regular user, so it cannot bind a port below 1024."""
        if self.port >= 1024:
            return ()
        message = f"app runs as user app and cannot bind port {self.port}"
        return (Diagnostic("app-privileged-port", message, variant=resolved.variant),)


backend = InProcessBackend()

recipe = Recipe(
    name="node",
    base="debian/trixie",
    common=Fragment(
        "base",
        items=(
            # Boot: the distribution kernel, systemd as init, udev and the UKI's EFI stub.
            Package("linux-image-amd64"),
            Package("systemd"),
            Package("systemd-sysv"),
            Package("udev"),
            Package("kmod"),
            Package("systemd-boot-efi"),
            Package("ca-certificates"),
            App(version=APP_VERSION, port=APP_PORT),
        ),
    ),
    variants=(
        Variant("default", target="qemu"),
        Variant("dev", parent="default", add=DevTools()),  # never ship it
    ),
)
```

- `common` holds what every variant shares: the packages that make the image bootable and the `App` fragment.
- `App` is a `Composite`, a `Fragment` whose configuration is its own dataclass fields. `compose()` returns its contents: an account, a config file, a first-boot `Init` step and a generated systemd `Service`. Its `checks` run during `lint`: `App(port=80)` reports `app-privileged-port`.
- The service runs `/usr/bin/app`, which the recipe does not ship yet. Add a `Build(...)` or a `Package(...)` for it before you bake a real image.
- `dev` inherits `default` and adds `DevTools()`: a serial console, root login and debugging tools. Never ship it.

The `.gitignore` block ignores `build/` but keeps `build/tundravm.lock`, which you commit.

## 2. Inspect

`inspect` is a dry run: what each variant will contain, without writing anything.

```console
$ tundravm inspect node.py --variant dev
Image: debian/trixie (x86_64)  variant=dev  reproducible=yes
Policy: mutable_ref_policy=warn network_mode=online require_frozen_lock=no
Parent: default
Fragments (3): base app devtools
Packages (20): apt bash-completion ca-certificates curl dnsutils iputils-ping kmod linux-image-amd64 net-tools netcat-openbsd openssh-server socat strace systemd systemd-boot-efi systemd-sysv tcpdump tcpflow udev vim
Files (2):
  /etc/app/app.conf                               0644  34B  sha256:681b227aa566
  /usr/lib/systemd/system/serial-console.service  0644  263B  sha256:016dd361c717
Users (1):
  app  system  home=/var/lib/app  shell=/usr/sbin/nologin
Services (1):
  app  /usr/bin/app --config /etc/app/app.conf  restart=on-failure  user=app  cwd=/var/lib/app  wanted_by=minimal.target
Hooks:
  postinst (2):
    mkosi-chroot systemctl enable serial-console.service
    ROOT_PASS=$(openssl passwd -6 "tdx")
  finalize (1):
    sed -i '/^IMAGE_VERSION=/d' "$BUILDROOT/usr/lib/os-release"
Runtime init: 1 (priorities: 100)
Debloat: enabled, 30 paths removed, systemd minimize=yes
Targets: qemu
[exit 0]
```

`Parent:` is the variant `dev` builds on and `Fragments:` the fragments it includes: `base` (the `common` fragment), `app` from `App`, and `devtools` from its own `add`. Without `--variant` every variant is shown. `--format json` adds the recipe digest the lockfile records; `--format markdown` renders tables for a CI job summary.

## 3. Lint

```console
$ tundravm lint node.py
no findings
[exit 0]
```

Every variant here bakes a bootable UKI, and a UKI needs a kernel. Comment out `Package("linux-image-amd64")` in `common` and lint again:

```console
$ tundravm lint node.py
error kernel-missing [default]: variant builds a bootable UKI but installs no kernel, so mkosi stops with 'A kernel must be installed in the image to build a UKI'
    hint: Add Package("linux-image-amd64") for the distribution kernel, declare Kernel(...) to build one, or Setting("Content", "Bootable", ("no",)) for a non-bootable image.
error kernel-missing [dev]: variant builds a bootable UKI but installs no kernel, so mkosi stops with 'A kernel must be installed in the image to build a UKI'
    hint: Add Package("linux-image-amd64") for the distribution kernel, declare Kernel(...) to build one, or Setting("Content", "Bootable", ("no",)) for a non-bootable image.
2 errors, 0 warnings, 0 infos
[exit 1]

$ tundravm bake node.py
note: no lockfile at build/tundravm.lock; baking unpinned (run `tundravm lock node.py` to freeze the bake)
[tundravm] lint ...
[default] error kernel-missing: variant builds a bootable UKI but installs no kernel, so mkosi stops with 'A kernel must be installed in the image to build a UKI'
[dev] error kernel-missing: variant builds a bootable UKI but installs no kernel, so mkosi stops with 'A kernel must be installed in the image to build a UKI'
[tundravm] lint ... FAILED (0.0s)
[tundravm] failed after 0.0s
error [E_LINT]: Recipe has 2 error-level diagnostics.
Hint: Run `tundravm lint RECIPE` to see them.
  codes: kernel-missing, kernel-missing
[exit 2]
```

`bake` lints first, so the missing kernel stops it before mkosi starts instead of minutes into a real build; `ci` fails at its lint step too. `compile` and `inspect` still run. The hint lists the ways out: the distribution kernel package, a `Kernel(...)` built from source, or `Setting("Content", "Bootable", ("no",))` for a disk that is never booted. Restore the package:

```console
$ tundravm lint node.py
no findings
[exit 0]
```

Warnings do not fail `lint` unless you pass `--strict`.

## 4. Add a variant

`dev` adds root login, which you never ship. For a variant that is the shipped image plus `strace`, add a third entry to `variants`:

```python
        Variant("debug", parent="default", add=Fragment("debug", items=(Package("strace"),))),
```

```console
$ tundravm inspect node.py --diff-variants default debug
variants default -> debug: 1 added, 0 removed, 0 changed
  + Package(strace, runtime)
[exit 0]
```

The generated `tests/test_node.py` names the variants it expects to compile. Add `"debug"` to the set in `test_compiles_every_variant`:

```python
    assert set(tree.variants) == {"default", "dev", "debug"}
```

## 5. Lock and compile

```console
$ tundravm lock node.py
locked build/tundravm.lock
[exit 0]

$ tundravm compile node.py --out mkosi
compiled mkosi
  variants:      default, dev, debug
  recipe_digest: a6dc0dc1f656aa95bef9677463fd16e0a47e324acd21ae260aa0bdf441608ef2
  tree_digest:   62258b694af6a4a8097e2ea85fca3eb21bc0f5229109e70dccc64c46795e6ce1
[exit 0]
```

The lockfile (version 4) records a digest per recipe section (the distribution, the compiler, each variant's packages, files, kernel, debloat and the rest) and, for a recipe with source builds, the commit each one resolved to. `compile`, `diff` and `bake` read `build/tundravm.lock` when it exists, so lock before you compile: a recipe with source builds compiles its pins. `recipe_digest` is the digest the lockfile records and `tree_digest` the digest of the emitted tree. `mkosi/` holds one directory per variant (`mkosi.conf`, `mkosi.extra/`, `mkosi.skeleton/`, `scripts/`). Commit it: it is the reviewable form of the image.

## 6. Change the recipe

Set `APP_PORT = 9090`, then ask what changed:

```console
$ tundravm diff node.py --against mkosi --variant default
diff --git a/default/mkosi.extra/etc/app/app.conf b/default/mkosi.extra/etc/app/app.conf
--- a/default/mkosi.extra/etc/app/app.conf
+++ b/default/mkosi.extra/etc/app/app.conf
@@ -1,2 +1,2 @@
 version=0.1.0
-listen=0.0.0.0:8080
+listen=0.0.0.0:9090
[exit 1]

$ tundravm compile node.py --out mkosi --check
M  debug/mkosi.extra/etc/app/app.conf
M  default/mkosi.extra/etc/app/app.conf
M  dev/mkosi.extra/etc/app/app.conf
3 files changed
[exit 1]
```

`diff` and `compile --check` exit 1 when the tree is stale. That is the CI gate for a committed tree. The lockfile drifted too, section by section:

```console
$ tundravm lock node.py --check
~ variants.debug.files: ~/etc/app/app.conf
~ variants.default.files: ~/etc/app/app.conf
~ variants.dev.files: ~/etc/app/app.conf
[exit 1]

$ tundravm lock node.py --check --variant default
~ variants.default.files: ~/etc/app/app.conf
[exit 1]

$ tundravm lock node.py
locked build/tundravm.lock
[exit 0]

$ tundravm lock node.py --check
lock is up to date
[exit 0]

$ tundravm compile node.py --out mkosi
compiled mkosi
  variants:      default, dev, debug
  recipe_digest: 2f0041479c07b652bd4fad98f7e86c1547d4078945ac68948f19d704f31e4cd3
  tree_digest:   873688c6d6945f9349f34c551331afff8a627b58838fdc5e834358f4475b2efc
[exit 0]
```

Each line names a section, `variants.<variant>.<key>`, and what changed in it. `--variant` limits the check to those variants' sections (and the recipe-wide ones), so one lockfile of every variant serves any selection. Re-locking keeps existing source pins; `tundravm lock node.py --update NAME` resolves one source again.

## 7. Bake

```console
$ tundravm bake node.py
[tundravm] lint ...
[tundravm] lint ... ok (0.0s)
[tundravm] verify lockfile ...
[tundravm] verify lockfile ... ok (0.0s)
[tundravm] compile ...
[tundravm] compile ... ok (0.0s)
[debug] prepare inprocess ...
[debug] prepare inprocess ... ok (0.0s)
[debug] build via inprocess ...
[debug] build via inprocess ... ok (0.0s)
[debug] artifact qemu build/debug/disk.qcow2 (112 B)
[debug] report build/debug/report.json
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
[tundravm] baked 3 variants in 0.0s

variant  target  artifact                  size   sha256        time
debug    qemu    build/debug/disk.qcow2    112 B  b50a5a5aeaac  0.0s
default  qemu    build/default/disk.qcow2  114 B  1fa043adea90  0.0s
dev      qemu    build/dev/disk.qcow2      110 B  9cfb8b5c2f31  0.0s
next: tundravm deploy build/bake-result.json --variant debug --target qemu
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

`measure` takes the manifest (or the directory holding it) and first checks the artifact against the sha256 the bake recorded: a disk file changed since the bake is `E_ARTIFACT_CHANGED`. With a real bake and `measured-boot` or `dstack-mr` on `PATH`, the `source:` line names that tool instead of `placeholder`.

## 9. Deploy

```console
$ tundravm deploy build --variant default --target qemu
error [E_DEPLOYMENT]: Refusing to deploy a simulated artifact.
Hint: Bake with a real backend first, or pass allow_simulated=True for a test run.
  variant: default
  path: build/default/disk.qcow2
[exit 2]

$ tundravm deploy build --variant default --target qemu --allow-simulated-artifact
error [E_DEPLOYMENT]: QEMU binary not found: qemu-system-x86_64
Hint: Install QEMU and ensure it is in PATH.
  binary: qemu-system-x86_64
[exit 2]
```

On a host with QEMU and a real bake, `deploy` boots the qcow2 and prints the deployment id and endpoint. Target settings are `--param KEY=VALUE` (`memory`, `cpus`, `ssh_port`, `tdx`, `daemonize` for qemu).

## 10. Write a fragment

Say every image that runs the app should also probe it once a minute. Write that as a `Composite`, like `App`, in `fragments.py` next to the recipe:

```python
from dataclasses import dataclass

from tundravm import Diagnostic, File, Fragment, Package, Resolved, Service, Unit
from tundravm.declarative.utils import Composite

TIMER = """\
[Unit]
Description=Probe the app once a minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min

[Install]
WantedBy=minimal.target
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthCheck(Composite):
    """A timer that probes the app's port once a minute. Fragment name: ``healthcheck``."""

    port: int

    def compose(self) -> Fragment:
        return Fragment(
            "healthcheck",
            requires=("app",),  # the fragment App composes
            checks=(self.check_port,),
            items=(
                Package("curl"),
                Service(
                    "app-healthcheck",
                    f"/usr/bin/curl -fsS -o /dev/null http://127.0.0.1:{self.port}/",
                    description="Probe the app",
                    type="oneshot",
                ),
                Unit("app-healthcheck.timer", TIMER, enabled=True),
            ),
        )

    def check_port(self, resolved: Resolved) -> tuple[Diagnostic, ...]:
        """Lint rule: probe the port the app's config listens on."""
        listen = f"listen=0.0.0.0:{self.port}\n"
        for item in resolved.items:
            if isinstance(item, File) and item.path == "/etc/app/app.conf" and listen in item.content:
                return ()
        message = f"the app does not listen on port {self.port}"
        return (Diagnostic("healthcheck-port", message, variant=resolved.variant),)
```

- `HealthCheck(port=9090)` is itself a `Fragment`: it goes in `common`, a variant's `add`, or another fragment's `items`. `compose()` runs once, at construction.
- `requires` names fragments that must be in the same variant; without `App`, lint reports `fragment-requires-missing`.
- `checks` are functions from the resolved variant to diagnostics; they run during `lint` (and `resolve`, which fails on errors). `check_port` reads the config file `App` declared, so the probe and the service cannot disagree on the port.
- A generated `Service` is enabled into `minimal.target` unless `wanted_by` names another target, so the probe also runs once at boot; the timer repeats it every minute.

Use it in `node.py`: add `from fragments import HealthCheck` after the tundravm imports and `HealthCheck(port=APP_PORT),` after `App(...)` in `common`. A wrong port is a lint error:

```console
$ tundravm lint node.py --variant default
error healthcheck-port [default]: the app does not listen on port 8080
1 error, 0 warnings, 0 infos
[exit 1]
```

That was `HealthCheck(port=8080)`. With `port=APP_PORT`:

```console
$ tundravm lint node.py
no findings
[exit 0]

$ tundravm inspect node.py --variant default | grep -E "Fragments|Units|app-healthcheck "
Fragments (3): base app healthcheck
Units: enable app-healthcheck.timer
  app-healthcheck  /usr/bin/curl -fsS -o /dev/null http://127.0.0.1:9090/  restart=no  type=oneshot

$ tundravm diff node.py --against mkosi --variant default --stat
M  default/mkosi.conf
A  default/mkosi.extra/usr/lib/systemd/system/app-healthcheck.service
A  default/mkosi.extra/usr/lib/systemd/system/app-healthcheck.timer
M  default/scripts/06-postinst.sh
4 files changed
[exit 1]

$ tundravm lock node.py
locked build/tundravm.lock
[exit 0]

$ tundravm compile node.py --out mkosi
compiled mkosi
  variants:      default, dev, debug
  recipe_digest: d08f2d6df99bcab4956c5e0e402d57ac15116fc642bde8c0e3f61dac95be4586
  tree_digest:   a9f21aa2f44800185a4d187ff2144a4c3f7a7e0a9c75b297c4995b0217b03c8e
[exit 0]
```

`mkosi.conf` gained `curl`, and `06-postinst.sh` enables the probe and its timer. The recipe directory is importable while the recipe loads, so `fragments.py` needs no packaging. The [fragment guide](module-authoring.md) covers builds, units and ordering in depth.

## 11. Test it

`tests/test_node.py`, which `init` wrote, has three tests:

- `test_lints_clean`: `assert_clean(RECIPE, strict=True)`, so a warning fails it too;
- `test_compiles_every_variant`: every variant compiles, and the variants are the ones it names (step 4);
- `test_matches_committed_tree`: `mkosi/` is what the recipe compiles to with `build/tundravm.lock`. After an intended change, `tundravm compile node.py --out mkosi` (or `TUNDRAVM_UPDATE_GOLDEN=1 uv run pytest tests`) rewrites it.

Add tests for the fragment in `tests/test_healthcheck.py`. `tundravm.testing` works on the same values the lifecycle returns:

```python
"""Tests for the HealthCheck fragment in fragments.py."""

from fragments import HealthCheck
from node import App

from tundravm import Fragment, Package, Recipe, lint
from tundravm.testing import assert_clean, assert_diagnostic


def image(*items: Fragment) -> Recipe:
    return Recipe(name="t", common=Fragment("base", items=(Package("linux-image-amd64"), *items)))


def test_probes_the_app_port() -> None:
    assert_clean(lint(image(App(version="1", port=8080), HealthCheck(port=8080))))


def test_needs_the_app() -> None:
    assert_diagnostic(lint(image(HealthCheck(port=8080))), "fragment-requires-missing")


def test_probes_the_wrong_port() -> None:
    found = lint(image(App(version="1", port=8080), HealthCheck(port=9090)))
    assert_diagnostic(found, "healthcheck-port", variant="default")
```

- `image()` builds a one-variant recipe (`default`, for qemu) around the fragments under test, with the kernel package so `kernel-missing` stays quiet.
- `assert_clean(lint(recipe))` fails on errors and, for a list of diagnostics, on warnings too (`allow=("code",)` to accept some).
- `assert_diagnostic` returns the first diagnostic matching every field you give, or fails listing what was found.

The tests import `fragments` and `node` from the project directory, which the generated `pyproject.toml` puts on pytest's path:

```toml
[tool.pytest.ini_options]
pythonpath = ["."]
```

```console
$ uv run pytest -q tests
......                                                                   [100%]
6 passed in 0.07s
[exit 0]
```

In a project of your own, the loop is: edit the recipe, `tundravm compile node.py --out mkosi`, `uv run pytest tests`, `tundravm lock node.py`.

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
