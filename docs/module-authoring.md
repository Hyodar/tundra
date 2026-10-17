# Writing fragments

A reusable piece of an image (a service, a monitoring stack, a hardened SSH setup) is a `Fragment`. There is no registration: a recipe puts the fragment into its `items`, into a variant's `add`, or into another fragment's `items`. Write it in one of two ways.

**A value**, for a one-off with nothing to configure: a `Fragment(...)` bound to a name.

```python
from tundravm import Fragment, Package

DEBUGGING = Fragment("debugging", items=(Package("strace"), Package("gdb")))
```

**A component**, the recommended way to ship a configurable fragment: a class decorated with `@component`, whose annotated fields are the configuration and whose `compose()` yields the contents. Each instance is itself a `Fragment`.

```python
from collections.abc import Iterator

from tundravm import Package, component
from tundravm.declarative.utils import Composite


@component
class Debugging(Composite):
    gdb: bool = True

    def compose(self) -> Iterator[Package]:
        yield Package("strace")
        if self.gdb:
            yield Package("gdb")
```

```python
>>> Debugging()
Debugging(gdb=True)
>>> Debugging(gdb=False).items
(Package(name='strace', role='runtime'),)
>>> Debugging() == Debugging()
True
```

The shipped `Tdxs`, `DevTools`, `EfiStub` and `Backports` (in `tundravm.declarative.utils`) are components whose `compose()` returns a whole `Fragment`, and `Raiko`, `TaikoClient` and `Nethermind` in [`examples/fragments/`](../examples/fragments/) and `NethermindBase` in [`examples/nethermind_base.py`](../examples/nethermind_base.py) are `Composite` subclasses written as plain dataclasses, which still works.

What every image of a family builds on (its kernel, EFI stub, backports and `Setting` lines) belongs in a platform rather than a fragment: subclass `Debian` with other defaults (`items=` for the declarations), as `NethermindPlatform` does next to `NethermindBase`, and recipes pass `platform=NethermindPlatform()`. Its declarations reach every variant, standalone ones included, with provenance `platform`.

## Writing a component

- `@component` makes the class a frozen, slotted dataclass with keyword-only fields (`Tdxs(issuer="tdx")`), so a required field may follow ones with defaults. A `str`, `int`, `float` or `bool` field (or one of those `| None`) refuses a value of another type where the recipe constructs it; list and dict values and defaults are frozen into tuples and `FrozenMap`s.
- Subclass `Composite` (from `tundravm.declarative.utils`) so a type checker sees instances as fragments; `@component` on a plain class makes it a `Composite` at runtime too.
- Construction calls `compose()` once. It returns or yields the fragment's declarations and nested fragments; it may instead return a whole `Fragment(...)` when the name, `requires` or `checks` depend on the fields.
- The fragment is named after the class (`PrometheusExporter` is `prometheus-exporter`), after an `id` (or `name`) field when there is one, or after `@component(name="...")`.
- `@component(requires=("node-exporter",))` names fragments the variant must also hold. A `check(self, resolved)` method returning diagnostics is the fragment's lint rule (see [Checks](#checks)).
- Validate other fields in `compose()` and raise `ValidationError` for bad values, as `EfiStub` does for an empty `snapshot`. The error surfaces where the recipe constructs the fragment.
- The fields alone are the instance's `repr` and equality, so two instances with the same configuration are the same fragment and expand once. Keep field values immutable (strings, numbers, declarations, other fragments, exports).

### Exports

A component publishes what others need from it as a typed value, in a property: `HttpEndpoint(host, port, path="/")` (with `url` and `address`) is the shipped one, and other facts subclass `Export` as frozen dataclasses. Build it with `provider=self`; a component that holds the value in a field then requires the provider's fragment, so the consumer neither names that fragment nor parses its files. The value stays bound to that instance (`origin`): a variant holding another configuration of the provider's fragment is an `export-stale` error, and a variant editing or removing a declaration the provider composed is an `export-stale` warning, because the value was computed from the configuration and does not follow edits. (`provider=self.name` binds the name only.)

```python
@component
class App(Composite):
    port: int

    @property
    def http(self) -> HttpEndpoint:
        return HttpEndpoint("127.0.0.1", self.port, provider=self)
    ...


@component
class HealthCheck(Composite):
    target: HttpEndpoint  # HealthCheck(target=app.http) requires "app"
    ...
```

### Config files

`ConfigFile.yaml(path, data)`, `ConfigFile.toml`, `ConfigFile.json`, `ConfigFile.ini` and `ConfigFile.env` (in `tundravm.declarative.utils`) render a config file from data and return an ordinary `File`, so the digest and tree are those of the hand-written file with the same bytes. Data is `str`, `int`, `float`, `bool`, `None`, lists and mappings with `str` keys. Keys are written sorted (`sort_keys=False` keeps insertion order), floats as `repr`, booleans as `true`/`false`, and each file ends in a newline. `mode`, `stage` and `allow_secret` are `File.text`'s.

```python
ConfigFile.yaml("/etc/prometheus/prometheus.yml", {"global": {"scrape_interval": "15s"}})
# global:
#   scrape_interval: 15s
ConfigFile.env("/etc/default/prometheus", {"ARGS": "--web.listen-address=127.0.0.1:9090"})
# ARGS="--web.listen-address=127.0.0.1:9090"
```

## Anatomy

`Fragment(name, items=(), requires=(), checks=())`:

- **`name`** identifies the fragment. Including the same fragment twice in a variant is fine (it expands once); two *different* fragments with the same name are a `fragment-conflict` error, so pick a name that is unique to what `compose()` returns, or put the distinguishing field in it.
- **`items`** are declarations and nested fragments, in order. Unpack generators with `*(...)`.
- **`requires`** names fragments that must also be in every variant that includes this one. It does not include them for you; a missing one is `fragment-requires-missing`.
- **`checks`** are functions `Resolved -> tuple[Diagnostic, ...]` that run against each variant after resolution, during `lint` (and `resolve`, which raises on error-level results).

Everything a fragment declares is ordinary: a recipe can edit or remove any of it in a variant's `changes` (`Unit.ref("tdxs.service").edit(enabled=False)`), and identity collisions with other fragments are reported, not silently merged.

## A complete example

A Prometheus node exporter that keeps its textfile collector directory on the encrypted `/persistent` disk. It takes the caller's `Disk` and `Secrets` as fields, so the caller decides which key and disk to use, and their `.ready` handles order its boot steps; it checks that a disk is mounted where it writes.

```python
# exporter.py
from collections.abc import Iterator

from tundravm import (
    Declaration,
    Diagnostic,
    Disk,
    Package,
    Resolved,
    Secrets,
    Unit,
    component,
)
from tundravm.declarative.utils import Composite, RuntimeDirectory, RuntimeFile

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
            "error",
            "the exporter needs a disk mounted at /persistent",
            subject="prometheus-node-exporter",
        ),
    )


@component
class PrometheusExporter(Composite):
    """The node exporter, writing its textfile collector under /persistent on *disk*."""

    disk: Disk
    secrets: Secrets

    def compose(self) -> Iterator[Declaration]:
        yield Package("prometheus-node-exporter")
        directory = RuntimeDirectory(
            "/persistent/exporter", after=[self.disk.ready], name="exporter-directory"
        )
        marker = RuntimeFile(
            "/persistent/exporter/boot.prom",
            "tundra_boot_ready 1\n",
            after=[directory.ready, self.secrets.ready],
            name="exporter-ready",
        )
        yield directory
        yield marker
        yield Unit(
            "prometheus-node-exporter.service", EXPORTER_UNIT, enabled=True, needs=[marker.ready]
        )

    def check(self, resolved: Resolved) -> tuple[Diagnostic, ...]:
        return exporter_check(resolved)
```

```python
# monitored.py
from exporter import PrometheusExporter

from tundravm import Disk, Fragment, Key, Recipe, Secrets, Variant

key = Key("key_persistent")
disk = Disk("disk_persistent", mount="/persistent", key=key, device="/dev/sdb")
secrets = Secrets(store=disk)
storage = Fragment("secure-storage", items=(key, disk, secrets))

recipe = Recipe(
    name="monitored",
    items=(storage, PrometheusExporter(disk=disk, secrets=secrets)),
    variants=(Variant("default", outputs=("qemu",)),),
)
```

The steps form a graph: `keys`, then `disks` (after `keys`), then `secrets` (after `disks`, since `store=disk`), `exporter-directory` after `disks` and `exporter-ready` after both `exporter-directory` and `secrets`. Each step compiles to its own oneshot unit that requires the steps it runs after:

```ini
# /usr/lib/systemd/system/runtime-init-exporter-ready.service
[Unit]
Description=Runtime Init: exporter-ready
After=network-online.target runtime-init-secrets.service runtime-init-exporter-directory.service
Wants=network-online.target
Requires=runtime-init-secrets.service runtime-init-exporter-directory.service

[Service]
Type=oneshot
ExecStart=/usr/lib/runtime-init/exporter-ready
RemainAfterExit=yes
```

and the exporter waits for exactly the step it needs, not for every step:

```ini
[Unit]
Description=Prometheus node exporter
After=runtime-init-exporter-ready.service
Requires=runtime-init-exporter-ready.service
...
```

Resolving the default variant shows what the fragment contributed:

```python
>>> resolve(recipe, variant="default").fragments
('secure-storage', 'prometheus-exporter')
>>> [type(item).__name__ for item in resolve(recipe, variant="default").items]
['Key', 'Disk', 'Secrets', 'Package', 'BootStep', 'BootStep', 'Unit']
```

## Checks

A check receives the `Resolved` variant: `variant`, `outputs` (never empty), every declaration in `items` (after inheritance, `replace` and `remove`) and the included fragment names in `fragments`. It returns diagnostics; an empty tuple means fine.

- `Diagnostic(code, severity, message, subject="", variant="", location="", hint=None, context=())`, the same `tundravm.Diagnostic` that `lint()` returns. `severity` is `"error"`, `"warning"` or `"info"`. Leave `variant` empty: resolution fills in the variant the check ran for. `location` is for a source position (`file:line`) when the check knows one; `lint --format github` annotates that line. A `hint` is printed under the finding and carried by `lint --format json`; `context` takes a dict or (key, value) string pairs.
- Use your own code prefix (`exporter-...`), so tests and `lint --format github` annotations identify the fragment.
- Use `"warning"` for advice. Error-severity results make `resolve`, `compile` and `bake` refuse the recipe.
- Checks must be pure: they run once per variant per lint, in any order.

## Ordering

**Runtime init.** `BootStep(name, script, after=[...], before=[...])` adds a step that runs at boot. There are no priorities: steps form a graph, and `after`/`before` take readiness handles. `key.ready`, `disk.ready` and `secrets.ready` are the built-in steps `keys`, `disks` and `secrets`, present when the variant declares a `Key`, `Disk` or `Secrets`. `disks` runs after `keys`, and `secrets` runs after `disks` when a `Secrets` has `store=`. A step's own `.ready` is its handle. A step name string works too, for a step declared in another fragment. The steps run in a deterministic topological order: when several are ready, the built-ins go first, then yours in declaration order. A cycle (`step-cycle`) or a reference to a step the variant does not declare (`step-undefined`) is an error that names the steps. `RuntimeDirectory(path, owner=, group=, mode=, after=)` and `RuntimeFile(path, text, owner=, group=, mode=, after=)` from `tundravm.declarative.utils` are `BootStep`s that create a directory or write a file.

In the current dialect each step is its own oneshot unit, `runtime-init-<step>.service`, running `/usr/lib/runtime-init/<step>`, with `After=`/`Requires=` on the steps it runs after. `runtime-init.service` requires all of them. Steps that do not depend on each other can run in parallel. Under `nethermind-v1`, one `/usr/bin/runtime-init` runs every step in the topological order.

**Units.** `Unit(..., needs=[disk.ready, step.ready])` and `Service(..., needs=[...])` make a unit wait for exactly those steps (`After=` and `Requires=` on their units). `needs=[RUNTIME_INIT]` waits for every step (`runtime-init.service`). A `Service` waits for every step by default, and `needs=()` starts it without waiting. A `needs` naming a step the variant does not declare is an error (`needs-undefined`).

**Build hooks.** `Hook(name, phase, script, after=())` runs a script in an mkosi phase (`build`, `postinst`, `finalize`, ...). Hooks keep declaration order inside a phase; `after` names hooks of the same phase that must run first.

## Building from source

A `Build` fetches a source, builds it during the mkosi build phase, and installs results into the image. The source is pinned by `tundravm lock`. For one Go or Rust program, `GoBinary` and `CargoBinary` write that `Build` for you and give you the installed path as `executable`, so the build output, the install path and the service command agree when the recipe is constructed. `executable` is a plain path computed then: if a variant edits that `Build`'s `install` or removes the `Build`, a `Service` whose command still names the old path is an `executable-stale` error (edit the service's command in the same variant):

```python
from dataclasses import dataclass

from tundravm import File, Fragment, Git, Resources, Service, User
from tundravm.declarative.utils import Composite, GoBinary

ASSETS = Resources.from_module(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class StatusPage(Composite):
    source: Git
    port: int = 9100

    def compose(self) -> Fragment:
        binary = GoBinary(
            "status-page", self.source, package="./cmd/status-page", env={"CGO_ENABLED": "0"}
        )
        return Fragment(
            "status-page",
            items=(
                binary,  # Build("status-page", using=Go(...)), installed at /usr/bin/status-page
                File.text("/etc/status-page.toml", f"port = {self.port}\n"),
                File.copy("/usr/share/status-page/index.html", ASSETS.file("assets/index.html")),
                User("status", shell="/bin/false"),
                Service(
                    "status-page",
                    f"{binary.executable} --config /etc/status-page.toml",
                    user="status",
                    restart="on-failure",
                    wanted_by="minimal.target",
                ),
            ),
        )
```

`GoBinary("status-page", source, package=...)` is `Build("status-page", source, using=Go(output="status-page", package=...), install=(Install("build/status-page", "/usr/bin/status-page"),))`; pass `install=` for another path. Write the `Build` yourself for anything more:

- Take the source as a field (`source: Git`) so a recipe can pin a fork or a tag; give it a default if there is a canonical repository, as `Tdxs.source` does.
- `using=Go(...)`, `Cargo(...)` or `Dotnet(...)` renders the toolchain's build command and installs its build packages (`golang`, `cargo`, `dotnet-sdk-8.0` by default; `packages=` and `env=` go on the toolchain). The output lands at `build/<output>` (`Go`), `target/<profile>/<bin>` (`Cargo`) or `publish/<output>` (`Dotnet`) in the source tree. See [API: sources and builds](api.md#sources-and-builds).
- For anything else pass `script=` instead: a shell script run inside the fetched source with `Build(env=...)` exported and `Build(packages=...)` installed for it. A `Build` takes exactly one of `script` and `recipe`.
- `Install(source, destination, mode=0o755)` copies one built file; `Install("out/", "/opt/app", mode=None, directory=True)` copies a directory.
- The built output is cached in the build directory under `<namespace>-<fingerprint16>`, the namespace being `cache_namespace` (default: the build's name) and the fingerprint a hash of the source pin, build, install steps, architecture and toolchain, so rebuilding an unchanged build is a copy and changing any of them rebuilds (`nethermind-v1` keeps its historical `<name>-<url digest>-<pin>` keys).
- Until the recipe is locked, `lint` warns `source-unpinned` for each build.

## Publishing a fragment with assets

A fragment that ships host files (unit files, configs, a kernel config) keeps them next to its module and names them through `Resources.from_module(__name__)`, never through the working directory or `Path(__file__)` arithmetic. The paths then resolve the same from the fragment's checkout, from a recipe in another directory, and from the installed package:

```
status_page/
  __init__.py          # from .fragment import StatusPage
  fragment.py          # ASSETS = Resources.from_module(__name__)
  assets/index.html    # ASSETS.file("assets/index.html")
```

- Ship the assets as package data. Hatchling puts every file inside the package directory in the wheel; with setuptools, list them under `[tool.setuptools.package-data]`. Check with `unzip -l dist/*.whl`. Zip imports are not supported: `Resources` needs the files on disk.
- `ASSETS.file(...)` and `ASSETS.directory(...)` fail when the module is imported if a file is missing, naming the module and the relative path, so a packaging mistake shows up at once rather than at lowering.
- Prefer `File.copy(path, ASSETS.file(...))` (read when the recipe lowers) to `File.text(path, ASSETS.text(...))` (read at import); both lower to the same bytes, and `tundravm watch` notices edits to either.

## Testing a fragment

Plan the fragment with `fragment_plan` and ask the result what it holds:

```python
from exporter import PrometheusExporter

from tundravm import Disk, Fragment, Key, Secrets
from tundravm.testing import assert_clean, assert_diagnostic, fragment_plan

key = Key("key_persistent")
disk = Disk("disk_persistent", mount="/persistent", key=key, device="/dev/sdb")
secrets = Secrets(store=disk)
STORAGE = Fragment("secure-storage", items=(key, disk, secrets))
EXPORTER = PrometheusExporter(disk=disk, secrets=secrets)


def test_exporter_is_clean():
    assert_clean(fragment_plan(EXPORTER, include=(STORAGE,)), allow=("source-unpinned",))


def test_exporter_needs_persistent_storage():
    elsewhere = Disk("disk_data", mount="/data", key=key)
    stored = Secrets(store=elsewhere)
    storage = Fragment("secure-storage", items=(key, elsewhere, stored))
    image = fragment_plan(PrometheusExporter(disk=elsewhere, secrets=stored), include=(storage,))
    assert_diagnostic(image, "exporter-storage-missing", variant="default")


def test_unit_waits_for_its_boot_step():
    unit = fragment_plan(EXPORTER, include=(STORAGE,)).unit("prometheus-node-exporter.service")
    assert unit.enabled
    assert unit.requires("runtime-init-exporter-ready.service")
    assert unit.after("runtime-init-exporter-ready.service")


def test_boot_steps_run_in_order():
    steps = fragment_plan(EXPORTER, include=(STORAGE,)).init_steps
    assert [step.name for step in steps] == [
        "keys", "disks", "secrets", "exporter-directory", "exporter-ready"
    ]
    assert steps[-1].after == ("exporter-directory", "secrets")


def test_exporter_ships_its_package():
    image = fragment_plan(EXPORTER, include=(STORAGE,))
    assert image.packages["prometheus-node-exporter"] == "runtime"
```

```console
$ uv run pytest -q test_exporter.py
.....                                                                    [100%]
5 passed in 0.05s
```

`fragment_plan` boots the fragment on a minimal platform that holds the kernel package, so `kernel-missing` stays quiet and the findings are the fragment's own. `assert_clean` fails on warnings too; `allow=("source-unpinned",)` accepts the one for the unlocked `tundra-tools` builds the key, disk and secrets need. A fragment that `requires` one it does not contain gets it through `include=`: `fragment_plan(EXPORTER, include=(STORAGE,))`. To pin a fragment's whole output, compare it with a committed tree: `assert_tree(fragment_plan(EXPORTER).tree, "tests/golden/exporter")`, regenerated with `TUNDRAVM_UPDATE_GOLDEN=1`. See [testing](testing.md).

## Guidelines

- Keep `compose()` a pure function of the fields; never keep module-level mutable state.
- Take dependencies as fields (the storage fragment, a source, a port) rather than constructing another fragment inside `compose()` with hidden defaults.
- Declare everything the fragment needs (packages, users, groups, files) so it works in a standalone variant, and use `requires` for what must come from elsewhere.
- Use `Service` for an ordinary service (it renders the unit and waits for runtime-init by default) and `Unit(name, content)` when you need exact bytes; tundravm does not edit `Unit` text beyond the `After=`/`Requires=` lines `needs=` adds.
- Keep hook and init names unique to the fragment (`exporter-...`): they are identities.
