"""Built-in TDX quote service module."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from tdx.errors import ValidationError
from tdx.image import Image

TdxsMode = Literal["issuer", "validator"]


@dataclass(frozen=True, slots=True)
class TdxsServiceConfig:
    service_name: str = "tdxs.service"
    socket_path: str = "/run/tdx/quote.sock"
    extra_args: tuple[str, ...] = ()


@dataclass(slots=True)
class Tdxs:
    mode: TdxsMode = "issuer"
    config: TdxsServiceConfig = field(default_factory=TdxsServiceConfig)

    def __post_init__(self) -> None:
        if self.mode not in {"issuer", "validator"}:
            raise ValidationError("Invalid Tdxs mode.", context={"mode": self.mode})

    @classmethod
    def issuer(cls, config: TdxsServiceConfig | None = None) -> Tdxs:
        return cls(mode="issuer", config=config or TdxsServiceConfig())

    @classmethod
    def validator(cls, config: TdxsServiceConfig | None = None) -> Tdxs:
        return cls(mode="validator", config=config or TdxsServiceConfig())

    def setup(self, image: Image) -> None:
        package = "tdx-attestation-issuer" if self.mode == "issuer" else "tdx-attestation-validator"
        image.install(package)

    def apply(self, image: Image) -> None:
        self.setup(image)
        self.install(image)

    def install(self, image: Image) -> None:
        image.service(self.config.service_name, enabled=True)
        config_payload = json.dumps(
            {
                "mode": self.mode,
                "socket_path": self.config.socket_path,
                "extra_args": list(self.config.extra_args),
            },
            indent=2,
            sort_keys=True,
        )
        image.file("/etc/tdx/tdxs.json", content=config_payload + "\n")
