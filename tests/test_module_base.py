"""Tests for the ``Module`` base class and the applied-module registry."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pytest

from tundravm import Diagnostic, Image, ValidationError
from tundravm.lockfile import recipe_digest
from tundravm.models import InitScriptEntry
from tundravm.modules import DiskEncryption, KeyGeneration, Module, SecretDelivery, Tdxs
from tundravm.platforms import AzurePlatform


@dataclass(slots=True)
class _Marker(Module):
    package: str = "marker-pkg"

    def install(self, image: Image) -> None:
        image.install(self.package)


@dataclass(slots=True)
class _NeedsMarker(Module):
    requires: ClassVar[tuple[type[Module], ...]] = (_Marker,)
    init_priority: ClassVar[int | None] = 42

    def init_script(self, image: Image) -> str:
        return "echo needs-marker\n"

    def check(self, image: Image, profile: str) -> Iterator[Diagnostic]:
        yield Diagnostic(
            level="info",
            code="needs-marker-applied",
            message="applied",
            hint=None,
            profile=profile,
        )


class _Named(Module):
    name = "custom-name"


def _keys(*names: str) -> KeyGeneration:
    module = KeyGeneration()
    for name in names:
        module.key(name, strategy="tpm", output=f"/persistent/{name}.key")
    return module


def test_default_name_is_kebab_case_of_class_name() -> None:
    assert KeyGeneration.name == "key-generation"
    assert DiskEncryption.name == "disk-encryption"
    assert Tdxs.name == "tdxs"
    assert AzurePlatform.name == "azure-platform"
    assert _NeedsMarker.name == "needs-marker"
    assert _Named.name == "custom-name"
    assert KeyGeneration().name == "key-generation"


def test_requires_rejects_missing_dependency() -> None:
    img = Image()
    with pytest.raises(ValidationError) as excinfo:
        _NeedsMarker().apply(img)
    assert str(excinfo.value).startswith(
        "Module _NeedsMarker requires _Marker; apply _Marker first."
    )
    assert excinfo.value.hint == "img.apply(_Marker(), _NeedsMarker())"
    assert img.applied_modules() == ()
    assert not img.has_init_scripts()


def test_requires_is_checked_per_active_profile() -> None:
    img = Image()
    img.apply(_Marker())
    img.profile("dev", extends=None)
    with img.profiles("default", "dev"), pytest.raises(ValidationError) as excinfo:
        img.apply(_NeedsMarker())
    assert excinfo.value.context == {"profile": "dev"}


def test_apply_in_dependency_order_records_modules() -> None:
    img = Image()
    marker, needs = _Marker(), _NeedsMarker()
    assert img.apply(marker, needs) is img
    assert img.applied_modules() == (marker, needs)
    assert img.applied_modules("default") == (marker, needs)
    assert "marker-pkg" in img.state.profiles["default"].packages
    assert img.init_scripts() == (InitScriptEntry(script="echo needs-marker\n", priority=42),)


def test_applying_the_same_instance_twice_records_it_once() -> None:
    img = Image()
    marker = _Marker()
    img.apply(marker, marker)
    assert img.applied_modules() == (marker,)


def test_registry_is_per_profile() -> None:
    img = Image()
    base = _Marker()
    img.apply(base)
    dev = _Marker(package="dev-pkg")
    with img.profile("dev"):
        img.apply(dev)
        assert img.applied_modules() == (dev,)
    both = _Marker(package="both-pkg")
    with img.profiles("default", "dev"):
        img.apply(both)
    assert img.applied_modules() == (base, both)
    assert img.applied_modules("dev") == (dev, both)
    assert img.applied_modules("unknown") == ()


def test_duck_typed_modules_still_apply_but_are_not_recorded() -> None:
    class Bundle:
        def apply(self, image: Image) -> None:
            image.install("bundle-pkg")

    img = Image()
    img.apply(Bundle())
    assert "bundle-pkg" in img.state.profiles["default"].packages
    assert img.applied_modules() == ()


def test_init_priority_registers_the_same_runtime_init(tmp_path: Path) -> None:
    img = Image(reproducible=False)
    img.apply(_keys("root"))
    disks = DiskEncryption()
    disks.disk("data", key_name="root", key_path="/persistent/root.key")
    img.apply(disks, SecretDelivery())

    assert img.init_scripts() == (
        InitScriptEntry(script="/usr/bin/key-gen setup /etc/tdx/key-gen.yaml\n", priority=10),
        InitScriptEntry(script="/usr/bin/disk-setup setup /etc/tdx/disk-setup.yaml\n", priority=20),
        InitScriptEntry(
            script="/usr/bin/secret-delivery setup /etc/tdx/secrets.yaml\n", priority=30
        ),
    )
    out = img.compile(tmp_path / "mkosi")
    runtime_init = (out.path / "default/mkosi.extra/usr/bin/runtime-init").read_text()
    assert runtime_init == (
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "\n"
        "/usr/bin/key-gen setup /etc/tdx/key-gen.yaml\n"
        "\n"
        "/usr/bin/disk-setup setup /etc/tdx/disk-setup.yaml\n"
        "\n"
        "/usr/bin/secret-delivery setup /etc/tdx/secrets.yaml\n"
    )


def test_module_check_surfaces_through_image_check() -> None:
    img = Image()
    img.apply(_Marker(), _NeedsMarker())
    with img.profile("dev"):
        img.install("curl")
    with img.profile("solo", extends=None):
        img.install("curl")
    assert "needs-marker-applied" in [d.code for d in img.check()]
    assert "needs-marker-applied" in [d.code for d in img.check(profiles=["dev"])]
    assert "needs-marker-applied" not in [d.code for d in img.check(profiles=["solo"])]


def test_disk_key_undefined_is_reported_with_declared_keys() -> None:
    img = Image()
    img.apply(_keys("root", "logs"))
    disks = DiskEncryption()
    disks.disk("data", key_name="missing", key_path="/persistent/missing.key")
    img.apply(disks)

    [diag] = [d for d in img.check() if d.code == "disk-key-undefined"]
    assert diag.level == "error"
    assert diag.subject == "data"
    assert diag.hint is not None
    assert "Declared keys: logs, root." in diag.hint


def test_disk_key_defined_in_another_profile_only_is_undefined() -> None:
    img = Image()
    with img.profile("keys"):
        img.apply(_keys("root"))
    disks = DiskEncryption()
    disks.disk("data", key_name="root", key_path="/persistent/root.key")
    img.apply(disks)
    [diag] = [d for d in img.check() if d.code.startswith("disk-key")]
    assert diag.code == "disk-key-undefined"
    assert diag.hint is not None
    assert "Declared keys: none." in diag.hint


def test_disk_key_path_mismatch_and_match() -> None:
    img = Image()
    img.apply(_keys("root"))
    disks = DiskEncryption()
    disks.disk("data", key_name="root", key_path="/elsewhere/root.key")
    img.apply(disks)
    [diag] = [d for d in img.check() if d.code.startswith("disk-key")]
    assert (diag.level, diag.code, diag.subject) == ("warning", "disk-key-path-mismatch", "data")

    aligned = Image()
    aligned.apply(_keys("root"))
    ok = DiskEncryption()
    ok.disk("data", key_name="root", key_path="/persistent/root.key")
    ok.disk("plain", device=None, mount_point="/scratch")
    aligned.apply(ok)
    assert not [d for d in aligned.check() if d.code.startswith("disk-key")]


def test_key_pipe_outside_run_is_info() -> None:
    img = Image()
    keys = KeyGeneration()
    keys.key("a", strategy="pipe", pipe_path="/var/keys/a.pipe")
    keys.key("b", strategy="pipe", pipe_path="/run/keys/b.pipe", output="/persistent/b.key")
    img.apply(keys)
    found = [(d.level, d.code, d.subject) for d in img.check() if d.code.startswith("key-")]
    assert found == [("info", "key-pipe-outside-run", "a")]


def test_registry_does_not_change_digest_or_compiled_output(tmp_path: Path) -> None:
    def build() -> Image:
        img = Image(reproducible=False)
        img.apply(_keys("root"), Tdxs())
        return img

    recorded = build()
    payload = recorded._recipe_payload(profile_names=("default",))
    digest = recipe_digest(payload)
    assert len(recorded.applied_modules()) == 2

    recorded._modules.clear()
    assert recorded.applied_modules() == ()
    assert recipe_digest(recorded._recipe_payload(profile_names=("default",))) == digest

    first = build().compile(tmp_path / "a")
    stripped = build()
    stripped._modules.clear()
    second = stripped.compile(tmp_path / "b")
    files_a = sorted(p.relative_to(first.path) for p in first.path.rglob("*") if p.is_file())
    files_b = sorted(p.relative_to(second.path) for p in second.path.rglob("*") if p.is_file())
    assert files_a == files_b
    for rel in files_a:
        assert (first.path / rel).read_bytes() == (second.path / rel).read_bytes(), rel
