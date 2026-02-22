# Module Authoring Guide

This SDK uses a two-phase module pattern:

- `setup(image)`: one-time package/build prerequisites.
- `install(image)`: runtime configuration (files, services, users, hooks).

## Recommended Pattern

```python
from dataclasses import dataclass

from tdx.image import Image


@dataclass(slots=True)
class ExampleModule:
    enabled: bool = True

    def required_host_commands(self) -> tuple[str, ...]:
        return ("mkosi",)

    def setup(self, image: Image) -> None:
        if self.enabled:
            image.install("example-package")

    def install(self, image: Image) -> None:
        if self.enabled:
            image.file("/etc/example/config.toml", content="enabled=true\n")
            image.service("example.service", enabled=True)
```

`img.use(module)` validates `required_host_commands()` first and raises `E_VALIDATION`
if any command is missing from `PATH`.

## Lifecycle Phase Mapping

Use `image.hook(phase, ...)` (or `image.run(..., phase=phase)`) for phase-bound logic.

| Phase | Typical Use |
| --- | --- |
| `sync` | input sync/bootstrap |
| `skeleton` | base filesystem skeleton updates |
| `prepare` | pre-build setup |
| `build` | compilation steps |
| `extra` | optional source additions |
| `postinst` | package post-install adjustments |
| `finalize` | final image tweaks |
| `postoutput` | post-output metadata |
| `clean` | cleanup hooks |
| `repart` | partition/layout hooks |
| `boot` | boot-time glue logic |

`after_phase` dependencies must reference an earlier phase.
