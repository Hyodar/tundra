# Module Authoring Guide

This SDK uses a two-phase module pattern:

- `setup(image)`: one-time package/build prerequisites.
- `install(image)`: runtime configuration (files, services, users, hooks).

Modules call the Image API directly to declare packages, files, services, etc.
This ensures that all required binaries are available in the built image.

## Recommended Pattern

```python
from dataclasses import dataclass

from tdx.image import Image


@dataclass(slots=True)
class ExampleModule:
    enabled: bool = True

    def setup(self, image: Image) -> None:
        if self.enabled:
            image.install("example-package")

    def install(self, image: Image) -> None:
        if self.enabled:
            image.file("/etc/example/config.toml", content="enabled=true\n")
            image.service("example.service", enabled=True)

    def apply(self, image: Image) -> None:
        self.setup(image)
        self.install(image)
```

Usage:

```python
from tdx import Image

img = Image()
ExampleModule().apply(img)
```

## Lifecycle Phase Mapping

Use `image.hook(phase, ...)` (or `image.run(..., phase=phase)`) for phase-bound logic.

| Phase | Typical Use |
| --- | --- |
| `sync` | input sync/bootstrap |
| `skeleton` | base filesystem skeleton updates |
| `prepare` | pre-build setup |
| `build` | compilation steps |
| `extra` | optional source additions |
| `postinst` | user creation, service enablement, systemd debloat (runs in chroot via `mkosi-chroot`) |
| `finalize` | path removal, final image tweaks (runs on host with `$BUILDROOT`) |
| `postoutput` | post-output metadata |
| `clean` | cleanup hooks |
| `repart` | partition/layout hooks |
| `boot` | boot-time glue logic |

`after_phase` dependencies must reference an earlier phase.
