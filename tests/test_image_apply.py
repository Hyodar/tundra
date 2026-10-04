"""Tests for Image.apply()."""

from __future__ import annotations

import pytest

from tundravm import Image, ValidationError
from tundravm.modules import DevTools, KeyGeneration, KeySpec
from tundravm.modules.base import Module


class _Bundle(Module):
    def __init__(self) -> None:
        self.applied_to: list[Image] = []

    def configure(self, image: Image) -> None:
        self.applied_to.append(image)
        image.install("bundle-pkg")


def test_apply_runs_each_module_in_order_and_chains() -> None:
    img = Image()
    first = _Bundle()
    second = _Bundle()
    result = img.apply(first, second).install("after")
    assert result is img
    assert first.applied_to == [img]
    assert second.applied_to == [img]
    assert {"bundle-pkg", "after"} <= img.state.profiles["default"].packages


def test_apply_matches_module_apply() -> None:
    def keys() -> KeyGeneration:
        return KeyGeneration(keys=(KeySpec("root", strategy="tpm"),))

    via_method = Image()
    via_apply = Image()
    DevTools().apply(via_method)
    keys().apply(via_method)
    via_apply.apply(DevTools(), keys())
    assert via_method.state.profiles["default"].packages == (
        via_apply.state.profiles["default"].packages
    )
    assert len(via_method.state.profiles["default"].files) == len(
        via_apply.state.profiles["default"].files
    )


def test_apply_respects_active_profile() -> None:
    img = Image()
    with img.profile("dev"):
        img.apply(_Bundle())
    assert "bundle-pkg" in img.state.profiles["dev"].packages
    assert "bundle-pkg" not in img.state.profiles["default"].packages


def test_apply_rejects_non_modules() -> None:
    img = Image()
    with pytest.raises(ValidationError) as excinfo:
        img.apply("not a module")  # type: ignore[arg-type]
    assert "str is not a module" in str(excinfo.value)
    with pytest.raises(ValidationError):
        img.apply()
