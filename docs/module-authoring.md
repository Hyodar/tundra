# Writing fragments

A reusable piece of an image (a service, a monitoring stack, a hardened SSH setup) is a `Fragment`. There is no registration: a recipe puts the fragment into `common`, into a variant's `add`, or into another fragment's `items`. Write it in one of two ways.

**A value**, for a one-off with nothing to configure: a `Fragment(...)` bound to a name.

```python
from tundravm import Fragment, Package

DEBUGGING = Fragment("debugging", items=(Package("strace"), Package("gdb")))
```

**A `Composite` subclass**, the recommended way to ship a configurable fragment: a frozen dataclass whose fields are the configuration and whose `compose()` returns the contents. Each instance is itself a `Fragment`.

```python
from dataclasses import dataclass

from tundravm import Fragment, Package
from tundravm.declarative.utils import Composite


@dataclass(frozen=True, slots=True, kw_only=True)
class Debugging(Composite):
    gdb: bool = True

    def compose(self) -> Fragment:
        tools = ("strace", "gdb") if self.gdb else ("strace",)
        return Fragment("debugging", items=tuple(Package(name) for name in tools))
```

```python
>>> Debugging()
Debugging(gdb=True)
>>> Debugging(gdb=False).items
(Package(name='strace', role='runtime'),)
>>> Debugging() == Debugging()
True
```

The shipped `Tdxs`, `DevTools`, `EfiStub` and `Backports` (in `tundravm.declarative.utils`) are written this way, and so are `Raiko`, `TaikoClient` and `Nethermind` in [`examples/fragments/`](../examples/fragments/) and `NethermindBase` in [`examples/nethermind_tdx.py`](../examples/nethermind_tdx.py).

## Writing a `Composite`

- Decorate the subclass with `@dataclass(frozen=True, slots=True, kw_only=True)`. Keyword-only fields keep call sites readable (`Tdxs(after_init=True)`) and let a required field follow ones with defaults.
- Construction calls `compose()` once; the instance takes the returned fragment's `name`, `items`, `requires` and `checks`. `compose()` must return a `Fragment`.
- Validate fields in `compose()` and raise `ValidationError` for bad values, as `EfiStub` does for an empty `snapshot`. The error surfaces where the recipe constructs the fragment.
- The fields alone are the instance's `repr` and equality, so two instances with the same configuration are the same fragment and expand once. Lists passed for fields are frozen into tuples; keep the other fields immutable too (strings, numbers, declarations, other fragments).

## Anatomy

`Fragment(name, items=(), requires=(), checks=())`:

- **`name`** identifies the fragment. Including the same fragment twice in a variant is fine (it expands once); two *different* fragments with the same name are a `fragment-conflict` error, so pick a name that is unique to what `compose()` returns, or put the distinguishing field in it.
- **`items`** are declarations and nested fragments, in order. Unpack generators with `*(...)`.
- **`requires`** names fragments that must also be in every variant that includes this one. It does not include them for you; a missing one is `fragment-requires-missing`.
- **`checks`** are functions `Resolved -> tuple[Diagnostic, ...]` that run against each variant after resolution, during `lint` (and `resolve`, which raises on error-level results).

Everything a fragment declares is ordinary: a recipe can `replace` or `remove` any of it in a variant, and identity collisions with other fragments are reported, not silently merged.

## A complete example

A Prometheus node exporter that keeps its textfile collector directory on the encrypted `/persistent` disk. It takes the storage fragment as a field, so the caller decides which key and disk to use, and it checks that a disk is mounted where it writes.

```python
# exporter.py
from dataclasses import dataclass

from tundravm import Diagnostic, Disk, Fragment, Init, Package, Resolved, Unit
from tundravm.declarative.utils import Composite

EXPORTER_UNIT = """\
[Unit]
Description=Prometheus node exporter

[Service]
ExecStart=/usr/bin/prometheus-node-exporter --collector.textfile.directory=/persistent/exporter
User=prometheus
Restart=on-failure

[Install]
WantedBy=minimal.target
"""


def exporter_check(image: Resolved) -> tuple[Diagnostic, ...]:
    """The exporter writes to /persistent, so some disk must mount there."""
    if any(isinstance(item, Disk) and item.mount == "/persistent" for item in image.items):
        return ()
    return (
        Diagnostic(
            "exporter-storage-missing",
            "the exporter needs a disk mounted at /persistent",
            subject="prometheus-node-exporter",
        ),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class PrometheusExporter(Composite):
    """The node exporter, writing its textfile collector under /persistent on *storage*."""

    storage: Fragment

    def compose(self) -> Fragment:
        return Fragment(
            "prometheus-exporter",
            requires=(self.storage.name,),
            checks=(exporter_check,),
            items=(
                self.storage,
                Package("prometheus-node-exporter"),
                Init(
                    "exporter-directory",
                    "install -d -m 0755 /persistent/exporter\n",
                    priority=25,
                    after=("disks",),
                ),
                Init(
                    "exporter-ready",
                    "printf 'tundra_boot_ready 1\\n' > /persistent/exporter/boot.prom\n",
                    priority=40,
                    after=("exporter-directory", "secrets"),
                ),
                Unit(
                    "prometheus-node-exporter.service", EXPORTER_UNIT, enabled=True, after_init=True
                ),
            ),
        )
```

```python
# monitored.py
from exporter import PrometheusExporter

from tundravm import Disk, Fragment, Key, Recipe, Secrets, Variant

key = Key("key_persistent")
disk = Disk("disk_persistent", mount="/persistent", key=key)
storage = Fragment("secure-storage", items=(key, disk, Secrets(store=disk)))

recipe = Recipe(
    name="monitored",
    common=Fragment("monitored", items=(PrometheusExporter(storage=storage),)),
    variants=(Variant("default", target="qemu"),),
)
```

The compiled `/usr/bin/runtime-init` runs the built-in steps and the fragment's steps by priority:

```bash
#!/bin/bash
set -euo pipefail

/usr/bin/key-gen setup /etc/tdx/key-gen.yaml

/usr/bin/disk-setup setup /etc/tdx/disk-setup.yaml

install -d -m 0755 /persistent/exporter

/usr/bin/secret-delivery setup /etc/tdx/secrets.yaml

printf 'tundra_boot_ready 1\n' > /persistent/exporter/boot.prom
```

and the unit waits for it:

```ini
[Unit]
Description=Prometheus node exporter
After=runtime-init.service
Requires=runtime-init.service
...
```

Resolving the default variant shows what the fragment contributed:

```python
>>> resolve(recipe, variant="default").fragments
('monitored', 'prometheus-exporter', 'secure-storage')
>>> [type(item).__name__ for item in resolve(recipe, variant="default").items]
['Key', 'Disk', 'Secrets', 'Package', 'Init', 'Init', 'Unit']
```

## Checks

A check receives the `Resolved` variant: `variant`, `target`, every declaration in `items` (after inheritance, `replace` and `remove`) and the included fragment names in `fragments`. It returns diagnostics; an empty tuple means fine.

- `Diagnostic(code, message, level="error", variant="", subject="")`. Leave `variant` empty: resolution fills in the variant the check ran for.
- Use your own code prefix (`exporter-...`), so tests and `lint --format github` annotations identify the fragment.
- Use `level="warning"` for advice. Error-level results make `resolve`, `compile` and `bake` refuse the recipe.
- Checks must be pure: they run once per variant per lint, in any order.

## Ordering

**Runtime init.** `Init(name, script, priority=100, after=())` adds a step to `/usr/bin/runtime-init`. Steps run by ascending priority. The built-in steps are `keys` (10), `disks` (20) and `secrets` (30), present when the variant declares a `Key`, `Disk` or `Secrets`. `after` names steps that must run first, built-in or yours; it orders steps of equal priority and must agree with the priorities (`init-order` otherwise). Pick a priority between the built-in steps you depend on and those that depend on you: 25 is "after disks are mounted, before secrets arrive"; 40 is "after secrets".

**Units.** `Unit(..., after_init=True)` makes a shipped unit start after `runtime-init.service` (`After=` and `Requires=`). Use it for anything that reads keys, disks or secrets.

**Build hooks.** `Hook(name, phase, script, after=())` runs a script in an mkosi phase (`build`, `postinst`, `finalize`, ...). Hooks keep declaration order inside a phase; `after` names hooks of the same phase that must run first.

## Building from source

A `Build` fetches a source, builds it during the mkosi build phase, and installs results into the image. The source is pinned by `tundravm lock`.

```python
from dataclasses import dataclass

from tundravm import Build, File, Fragment, Git, Go, Install, Unit, User
from tundravm.declarative.utils import Composite

STATUS_UNIT = """\
[Unit]
Description=Status page

[Service]
User=status
ExecStart=/usr/bin/status-page --config /etc/status-page.toml
Restart=on-failure

[Install]
WantedBy=minimal.target
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class StatusPage(Composite):
    source: Git
    port: int = 9100

    def compose(self) -> Fragment:
        return Fragment(
            "status-page",
            items=(
                Build(
                    "status-page",
                    self.source,
                    recipe=Go(
                        output="status-page",
                        package="./cmd/status-page",
                        env={"CGO_ENABLED": "0"},
                    ),
                    install=(Install("build/status-page", "/usr/bin/status-page"),),
                ),
                File("/etc/status-page.toml", f"port = {self.port}\n"),
                User("status", shell="/bin/false"),
                Unit("status-page.service", STATUS_UNIT, enabled=True),
            ),
        )
```

- Take the source as a field (`source: Git`) so a recipe can pin a fork or a tag; give it a default if there is a canonical repository, as `Tdxs.source` does.
- `recipe=Go(...)`, `Cargo(...)` or `Dotnet(...)` renders the toolchain's build command and installs its build packages (`golang`, `cargo`, `dotnet-sdk-8.0` by default; `packages=` and `env=` go on the recipe). The output lands at `build/<output>` (`Go`), `target/<profile>/<bin>` (`Cargo`) or `publish/<output>` (`Dotnet`) in the source tree. See [API: sources and builds](api.md#sources-and-builds).
- For anything else pass `script=` instead: a shell script run inside the fetched source with `Build(env=...)` exported and `Build(packages=...)` installed for it. A `Build` takes exactly one of `script` and `recipe`.
- `Install(source, destination, mode=0o755)` copies one built file; `Install("out/", "/opt/app", mode=None, directory=True)` copies a directory.
- The built output is cached in the build directory under `cache_key` (default `<name>-<url digest>-<ref>`), so rebuilding an unchanged source is a copy.
- Until the recipe is locked, `lint` warns `source-unpinned` for each build.

## Testing a fragment

Build a small recipe around the fragment and assert on its diagnostics and its compiled output:

```python
from exporter import PrometheusExporter

from tundravm import Disk, Fragment, Key, Recipe, Secrets, Variant, compile, lint
from tundravm.testing import assert_clean, assert_diagnostic, compile_tree


def recipe_with(storage: Fragment) -> Recipe:
    return Recipe(
        name="t",
        common=Fragment("t", items=(PrometheusExporter(storage=storage),)),
        variants=(Variant("default", target="qemu"),),
    )


key = Key("key_persistent")
disk = Disk("disk_persistent", mount="/persistent", key=key)
STORAGE = Fragment("secure-storage", items=(key, disk, Secrets(store=disk)))


def test_exporter_is_clean():
    assert_clean(lint(recipe_with(STORAGE)), allow=("source-unpinned",))


def test_exporter_needs_persistent_storage():
    elsewhere = Disk("disk_data", mount="/data", key=key)
    storage = Fragment("secure-storage", items=(key, elsewhere, Secrets(store=elsewhere)))
    assert_diagnostic(lint(recipe_with(storage)), "exporter-storage-missing", variant="default")


def test_unit_waits_for_runtime_init():
    tree = compile_tree(recipe_with(STORAGE))
    unit = tree.unit("prometheus-node-exporter.service")
    assert "After=runtime-init.service" in unit
    assert "Requires=runtime-init.service" in unit
    assert "boot.prom" in tree.runtime_init()


def test_tree_digest_is_stable():
    assert compile(recipe_with(STORAGE)).digest == compile(recipe_with(STORAGE)).digest
```

```console
$ uv run pytest -q test_exporter.py
....                                                                     [100%]
4 passed in 0.03s
```

`allow=("source-unpinned",)` accepts the warning for the unlocked `tundra-tools` builds the key, disk and secrets need. To pin a fragment's whole output, compare it with a committed tree: `assert_tree(compile(recipe), "tests/golden/exporter")`, regenerated with `TUNDRAVM_UPDATE_GOLDEN=1`. See [testing](testing.md).

## Guidelines

- Keep `compose()` a pure function of the fields; never keep module-level mutable state.
- Take dependencies as fields (the storage fragment, a source, a port) rather than constructing another fragment inside `compose()` with hidden defaults.
- Declare everything the fragment needs (packages, users, groups, files) so it works in a standalone variant, and use `requires` for what must come from elsewhere.
- Use `Service` for an ordinary service (it renders the unit and waits for runtime-init by default) and `Unit(name, content)` when you need exact bytes; tundravm does not edit `Unit` text beyond `after_init`.
- Keep hook and init names unique to the fragment (`exporter-...`): they are identities.
