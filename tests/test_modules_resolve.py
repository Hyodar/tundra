"""Tests for the shared resolve_after utility."""

from tundravm import Image
from tundravm.modules import KeyGeneration
from tundravm.modules.resolve import resolve_after


def test_resolve_after_prepends_init_when_scripts_present() -> None:
    image = Image()
    keys = KeyGeneration()
    keys.key("k", strategy="tpm")
    keys.apply(image)

    result = resolve_after(("network.target",), image)
    assert result == ("runtime-init.service", "network.target")


def test_resolve_after_noop_when_no_init() -> None:
    image = Image()
    result = resolve_after(("network.target",), image)
    assert result == ("network.target",)


def test_resolve_after_no_duplicate_if_already_present() -> None:
    image = Image()
    keys = KeyGeneration()
    keys.key("k", strategy="tpm")
    keys.apply(image)

    result = resolve_after(("runtime-init.service", "network.target"), image)
    assert result == ("runtime-init.service", "network.target")
