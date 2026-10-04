"""``examples/surge-tdx-prover/contents.py`` matches the committed surge tree byte for byte."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from tundravm.compiler.emit_mkosi import DEFAULT_TDX_INIT_SCRIPT

SURGE = Path(__file__).resolve().parent.parent / "examples" / "surge-tdx-prover"
GOLDEN = SURGE / "mkosi" / "default"
INIT_SERVICE = "runtime-init.service"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


contents = _load("surge_contents", SURGE / "contents.py")

VERBATIM: tuple[tuple[str, str], ...] = (
    ("TDX_INIT", "mkosi.skeleton/init"),
    ("RESOLV_CONF", "mkosi.skeleton/etc/resolv.conf"),
    ("NETWORK_SETUP_SERVICE", "mkosi.skeleton/etc/systemd/system/network-setup.service"),
    ("DROPBEAR_CONFIG", "mkosi.extra/etc/default/dropbear"),
    ("SYSCTL_CONF", "mkosi.extra/etc/sysctl.d/99-surge.conf"),
    ("TDX_GUEST_PERMISSIONS", "mkosi.extra/etc/udev/rules.d/65-tdx-guest.rules"),
    ("TDX_GUEST_SYMLINK", "mkosi.extra/etc/udev/rules.d/99-tdx-symlink.rules"),
    ("OPENNTPD_CONF", "mkosi.extra/etc/openntpd/ntpd.conf"),
    ("PROMETHEUS_DEFAULTS", "mkosi.extra/etc/default/prometheus"),
    ("NETHERMIND_ENV", "mkosi.extra/etc/nethermind-surge/env"),
    ("RAIKO_ENV", "mkosi.extra/etc/raiko/env"),
    ("TAIKO_CLIENT_ENV", "mkosi.extra/etc/taiko-client/env"),
)

UNITS: tuple[tuple[str, str], ...] = (
    ("RAIKO_UNIT", "mkosi.extra/usr/lib/systemd/system/raiko.service"),
    ("TAIKO_CLIENT_UNIT", "mkosi.extra/usr/lib/systemd/system/taiko-client.service"),
    ("NETHERMIND_UNIT", "mkosi.extra/usr/lib/systemd/system/nethermind-surge.service"),
)

# Shipped files the compiler or a built-in module renders, not contents.py.
GENERATED = frozenset(
    {
        "mkosi.skeleton/etc/systemd/system/minimal.target",
        "mkosi.extra/etc/tdx/disk-setup.yaml",
        "mkosi.extra/etc/tdx/key-gen.yaml",
        "mkosi.extra/etc/tdx/secrets.json",
        "mkosi.extra/etc/tdx/secrets.yaml",
        "mkosi.extra/etc/tdxs/config.yaml",
        "mkosi.extra/usr/bin/runtime-init",
        "mkosi.extra/usr/lib/systemd/system/runtime-init.service",
        "mkosi.extra/usr/lib/systemd/system/tdxs.service",
        "mkosi.extra/usr/lib/systemd/system/tdxs.socket",
    }
)


def _const(name: str) -> str:
    value = getattr(contents, name)
    assert isinstance(value, str)
    return value


def after_init(body: str) -> str:
    """The ``after_init`` injection the committed units carry.

    ``runtime-init.service`` becomes the first value of the ``[Unit]`` section's
    ``After=`` and ``Requires=`` lines; a missing line is appended to the end of
    ``[Unit]``, ``After=`` before ``Requires=``.
    """
    unit, blank, rest = body.partition("\n\n")
    lines = unit.split("\n")
    for key in ("After=", "Requires="):
        index = next((i for i, line in enumerate(lines) if line.startswith(key)), None)
        if index is None:
            lines.append(f"{key}{INIT_SERVICE}")
        else:
            lines[index] = f"{key}{INIT_SERVICE} {lines[index].removeprefix(key)}"
    return "\n".join(lines) + blank + rest


@pytest.mark.parametrize(("name", "path"), VERBATIM)
def test_constant_matches_golden_file(name: str, path: str) -> None:
    assert (GOLDEN / path).read_bytes() == _const(name).encode()


@pytest.mark.parametrize(("name", "path"), UNITS)
def test_unit_body_plus_after_init_matches_golden_file(name: str, path: str) -> None:
    body = _const(name)
    assert INIT_SERVICE not in body
    assert (GOLDEN / path).read_bytes() == after_init(body).encode()


def test_every_shipped_literal_file_is_covered() -> None:
    shipped = {
        str(path.relative_to(GOLDEN))
        for tree in ("mkosi.extra", "mkosi.skeleton")
        for path in (GOLDEN / tree).rglob("*")
        if path.is_file()
    }
    covered = {path for _, path in (*VERBATIM, *UNITS)}
    assert shipped - GENERATED == covered


def test_skeleton_init_matches_the_compiler_default() -> None:
    assert DEFAULT_TDX_INIT_SCRIPT == _const("TDX_INIT")
