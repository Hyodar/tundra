from dataclasses import dataclass

import pytest

from tdx import Image
from tdx.errors import ValidationError


@dataclass
class MissingHostCommandModule:
    missing: str = "tdxvm-command-that-should-not-exist-9f3a2c77"

    def required_host_commands(self) -> tuple[str, ...]:
        return (self.missing,)

    def setup(self, image: Image) -> None:
        _ = image

    def install(self, image: Image) -> None:
        _ = image


@dataclass
class NoDependencyModule:
    setup_called: bool = False
    install_called: bool = False

    def setup(self, image: Image) -> None:
        _ = image
        self.setup_called = True

    def install(self, image: Image) -> None:
        _ = image
        self.install_called = True


def test_use_rejects_module_when_required_host_command_is_missing() -> None:
    image = Image()
    module = MissingHostCommandModule()

    with pytest.raises(ValidationError):
        image.use(module)


def test_use_applies_module_without_dependencies() -> None:
    image = Image()
    module = NoDependencyModule()

    image.use(module)

    assert module.setup_called is True
    assert module.install_called is True
