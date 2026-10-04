# Module Authoring Guide

A module is a class that subclasses `tundravm.modules.Module` and calls the `Image` API to declare packages, files, services, build hooks, and init scripts. Construct it, then call `module.apply(img)` or `img.apply(module, ...)`. Every built-in (`KeyGeneration`, `DiskEncryption`, `SecretDelivery`, `Tdxs`, `Devtools`, `AzurePlatform`, `GcpPlatform`) and the example modules in `examples/modules/` use the same base class.

| Member | Kind | Default | Purpose |
| --- | --- | --- | --- |
| `name` | `ClassVar[str]` | kebab-case class name (`KeyGeneration` -> `key-generation`) | Stable identifier |
| `requires` | `ClassVar[tuple[type[Module], ...]]` | `()` | Module classes that must already be applied to the same profile(s) |
| `init_priority` | `ClassVar[int \| None]` | `None` | When set, `init_script()` is registered with `add_init_script(script, priority=...)` |
| `setup(image)` | method | no-op | Build-time: build packages, build sources, build hooks |
| `install(image)` | method | no-op | Runtime: packages, files, users, services |
| `init_script(image)` | method | `None` | Bash fragment for `/usr/bin/runtime-init` |
| `check(image, profile)` | method | no findings | Module-specific diagnostics, run by `img.check()` / `tundravm check` |
| `apply(image)` | final method | | Runs the steps below; do not override |

`apply(image)` does, for the active profiles:

1. Verifies `requires` against `image.applied_modules(profile)` for every active profile. A missing dependency raises `ValidationError("Module X requires Y; apply Y first.")` with the hint `img.apply(Y(), X())`.
2. Calls `setup(image)`, then `install(image)`.
3. If `init_priority` is set and `init_script(image)` returns a non-empty string, registers it at that priority.
4. Records the module on each active profile; `img.applied_modules(profile)` lists them in apply order.

The base class has `__slots__ = ()` and only class variables, so `@dataclass(slots=True)` subclasses work as-is. `img.apply()` also accepts any object with an `apply(image)` method, but only `Module` subclasses are recorded, checked by `requires`, and linted.

## Example

`Agent` builds a service that reads a boot-generated key. It needs `KeyGeneration` in the same profile (`requires`), prepares its runtime directory after keys and disks are ready (`init_priority = 25`), and reports a key name that no `KeyGeneration` declares (`check`).

```python
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from tundravm.check import Diagnostic
from tundravm.modules import KeyGeneration, Module
from tundravm.modules.resolve import resolve_after

if TYPE_CHECKING:
    from tundravm.image import Image

AGENT_UNIT = """\
[Unit]
Description=Agent
After={after}

[Service]
User={user}
ExecStart=/usr/bin/agent --config /etc/agent/config.toml
Restart=on-failure

[Install]
WantedBy=default.target
"""


@dataclass(slots=True)
class Agent(Module):
    requires: ClassVar[tuple[type[Module], ...]] = (KeyGeneration,)
    init_priority: ClassVar[int | None] = 25

    user: str = "agent"
    key_name: str = "agent"
    key_path: str = "/persistent/agent.key"
    after: tuple[str, ...] = ("network-online.target",)

    def setup(self, image: Image) -> None:
        image.build_install("build-essential", "git")

    def install(self, image: Image) -> None:
        image.install("ca-certificates")
        image.file(
            "/etc/agent/config.toml",
            content=f'key_path = "{self.key_path}"\n',
        )
        image.file(
            "/usr/lib/systemd/system/agent.service",
            content=AGENT_UNIT.format(
                user=self.user,
                after=" ".join(resolve_after(self.after, image)),
            ),
        )
        image.run(
            f"mkosi-chroot useradd --system --shell /usr/sbin/nologin {self.user}",
            phase="postinst",
        )
        image.service("agent", enabled=True)

    def init_script(self, image: Image) -> str:
        return f"install -d -m 0750 -o {self.user} /run/agent\n"

    def check(self, image: Image, profile: str) -> Iterator[Diagnostic]:
        declared = {
            spec.name
            for module in image.applied_modules(profile)
            if isinstance(module, KeyGeneration)
            for spec in module.keys
        }
        if self.key_name not in declared:
            yield Diagnostic(
                level="error",
                code="agent-key-undefined",
                message=f"Agent reads key {self.key_name!r}, which is never generated",
                hint=f"Add keys.key({self.key_name!r}, output={self.key_path!r}).",
                profile=profile,
                subject=self.key_name,
            )
```

```python
from tundravm import Image
from tundravm.modules import KeyGeneration

img = Image()
keys = KeyGeneration()
keys.key("agent", strategy="tpm", output="/persistent/agent.key")
img.apply(keys, Agent())       # Agent() alone raises: requires KeyGeneration
errors = [d for d in img.check() if d.level == "error"]
assert errors == []            # Agent(key_name="other") reports agent-key-undefined
```

Notes on the example:

- `image.run(cmd, phase="postinst")` runs inside the build. Prefix with `mkosi-chroot` to execute inside the image root.
- `image.service("agent", enabled=True)` with no `command=` only enables an existing unit file. Pass `command=` to have the SDK generate the unit instead.
- `resolve_after(after, image)` prepends `runtime-init.service` when any init scripts are registered. Hand-written unit files need this; units generated by `image.service(command=...)` get `After=`/`Requires=runtime-init.service` injected automatically at `compile()`.
- Build order matters: apply init modules (`KeyGeneration`, etc.) before modules that render their own unit with `resolve_after`, otherwise `image.has_init_scripts()` is still `False`. Init scripts are scoped to the profile that registers them; extending profiles inherit the default's. Declaring them in `requires` enforces that order.
- Use `requires` only for dependencies that always hold. Conditional ones (a disk that names a key, a key path that must match) belong in `check()`, which sees every module applied to the profile regardless of order.

### Compiling a binary at build time

Use `image.hook("build", cmd)` for source builds. `tundravm.build_cache` provides stable paths and a build cache wrapper; see `examples/modules/raiko.py` for a full Rust pipeline.

```python
from tundravm.build_cache import Build, Cache

clone_dir = Build.build_path("agent")          # host path under $BUILDDIR
chroot_dir = Build.chroot_path("agent")        # same dir as seen from mkosi-chroot
cache = Cache.declare(
    "agent-v1",
    (Cache.file(src=Build.build_path("agent/out/agent"),
                dest=Build.dest_path("usr/bin/agent"), name="agent"),),
)
image.hook("build", cache.wrap(
    f'git clone --depth=1 https://example.com/agent.git "{clone_dir}" && '
    f"mkosi-chroot bash -c 'cd {chroot_dir} && make -o out/agent'"
))
```

## Init priorities

`init_priority` orders boot scripts: at `compile()` the SDK sorts all fragments by priority (lower first), writes `/usr/bin/runtime-init` and `runtime-init.service`, and makes every other service wait on it.

| Priority | Module |
| --- | --- |
| 10 | `KeyGeneration` |
| 20 | `DiskEncryption` |
| 30 | `SecretDelivery` |
| 100 | Default for a bare `img.add_init_script()` call |

Pick a priority relative to what your script depends on. Needs a key: `> 10`. Needs a mounted encrypted disk: `> 20`. Needs secrets: `> 30`. Two fragments with the same priority run in registration order and `img.check()` reports `init-priority-collision`.

Fragments run under `set -euo pipefail`; a failing fragment aborts `runtime-init` and every dependent service.

## Profile scoping

Every `Image` call writes to the active profile set. Outside any block that is the default profile. Inside `with img.profile("x"):` only profile `x` is touched, so a module applied there has no effect on other profiles.

```python
from tundravm import Image
from tundravm.modules import Devtools, KeyGeneration

img = Image()
keys = KeyGeneration()
keys.key("key_persistent", strategy="tpm")
keys.apply(img)                 # default profile, inherited by dev

with img.profile("dev"):
    Devtools().apply(img)       # dev profile only
```

A profile extends the default profile, so a module applied to the default reaches every extending profile: `dev` above gets key generation too. `requires` and `check()` see inherited modules, and `img.applied_modules("dev", inherited=True)` lists them first. A module applied inside a profile stays in that profile. A profile declared with `extends=None` inherits no declarations or modules; apply what it needs there.

Init-script fragments are stored on `img.init`, which is shared by the whole image, so `/usr/bin/runtime-init` has the same content in every compiled profile, standalone ones included.

## Build phases

`image.hook(phase, cmd)` and `image.run(cmd, phase=...)` accept these phases, executed in this order:

| Phase | Where it runs | Typical use |
| --- | --- | --- |
| `sync` | host | fetch inputs, generate apt sources |
| `skeleton` | host | files needed before the package manager |
| `prepare` | host, `$BUILDROOT` populated | pre-build setup |
| `build` | host, `mkosi-chroot` available | compile binaries into `$DESTDIR` |
| `extra` | host | extra source trees |
| `postinst` | host, use `mkosi-chroot` for in-image commands | users, `systemctl enable`, config fixes |
| `finalize` | host, `$BUILDROOT` | path removal, os-release tweaks |
| `postoutput` | host | metadata for the written disk image |
| `clean` | host | `mkosi clean` hooks |
| `repart` | host | partition layout hooks |
| `boot` | guest, systemd oneshot | boot-time glue (`image.on_boot`) |

`hook(..., after_phase=...)` must name a phase earlier than `phase`.

## Testing your module

`tundravm.testing` provides compile, lint, golden-tree, and CLI helpers, and its pytest plugin provides the `image`, `inprocess_image`, `compiled`, and `run_cli` fixtures automatically. No mkosi is needed. See [`testing.md`](testing.md) for the full reference. Using the `Agent` example above:

```python
from tundravm import Image
from tundravm.modules import KeyGeneration
from tundravm.testing import FakeModule, assert_diagnostic, compile_tree


def test_agent(image: Image) -> None:  # `image` comes from the tundravm pytest plugin
    keys = KeyGeneration()
    keys.key("agent", strategy="tpm", output="/persistent/agent.key")
    db = FakeModule("db", init_script="echo db-ready", init_priority=30)
    image.apply(keys, Agent(key_name="other"), db)

    assert_diagnostic(image, "agent-key-undefined", level="error", subject="other")
    init = compile_tree(image).runtime_init()
    assert init.index("/run/agent") < init.index("echo db-ready")  # 25 runs before 30
    assert db.applied_to == ["default"]
```

`FakeModule` stands in for the modules yours composes with. Use it to check `requires` ordering and init priorities. `image.state.profiles[name]` still exposes the raw declarations (`packages`, `files`, `services`, `phases`) when you need them. Pass `Image(reproducible=False)` to leave out the default `strip_image_version()` finalize hook, so `phases` holds only what the module added.
