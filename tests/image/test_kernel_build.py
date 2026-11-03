"""``Kernel.build``: its spelling, the hardening fragment it merges first, and the check that
refuses ``hardening=False`` in a production profile."""

from __future__ import annotations

from pathlib import Path

import pytest

import tundravm
from tundravm import DebianConfig, Image, Kernel, KernelFragment, Profile
from tundravm.check import check
from tundravm.compiler.emit_mkosi import kernel_fragment
from tundravm.declarative._kernel_hardening import HARDENING, LOCKDOWN
from tundravm.errors import ValidationError
from tundravm.sources import Git

BASE = DebianConfig("6.12.113-1")
TABLE = [f"# {o} is not set" if v == "n" else f"{o}={v}" for o, v, _ in HARDENING]


def _fragment(kernel: Kernel, **image: object) -> list[str]:
    lowered = tundravm.lower(Image("k", kernel=kernel, **image))  # type: ignore[arg-type]
    assert lowered.kernel is not None
    return kernel_fragment(lowered.kernel).splitlines()


def test_the_default_fragment_is_the_table_in_order() -> None:
    assert _fragment(Kernel.build(base=BASE)) == ["# CONFIG_MODULES is not set", *TABLE]


@pytest.mark.parametrize(
    ("build", "present", "absent"),
    [
        (
            {},
            ["# CONFIG_KSM is not set", "# CONFIG_RD_XZ is not set", "CONFIG_DEBUG_INFO_NONE=y"],
            ["# CONFIG_RD_ZSTD is not set", "# CONFIG_NET_DEVMEM is not set"],
        ),
        (
            {"fragments": [KernelFragment({"CONFIG_IO_URING": "y"}, reason="the node needs it")]},
            ["CONFIG_IO_URING=y"],
            ["# CONFIG_IO_URING is not set"],
        ),
        (
            {"modules": True},
            ["# CONFIG_KEXEC is not set"],
            ["# CONFIG_MODULES is not set", *(f"{option}=y" for option in sorted(LOCKDOWN))],
        ),
        (
            {"hardening": False},
            ["# CONFIG_MODULES is not set"],
            ["# CONFIG_KSM is not set", "CONFIG_SECURITY_LOCKDOWN_LSM=y"],
        ),
    ],
    ids=["default", "a fragment keeps an option on", "modules", "unhardened"],
)
def test_the_hardening_fragment(
    build: dict[str, object], present: list[str], absent: list[str]
) -> None:
    lines = _fragment(Kernel.build(base=BASE, **build))  # type: ignore[arg-type]
    assert [line for line in present if line not in lines] == []
    assert [line for line in absent if line in lines] == []


def test_every_row_has_one_option_a_value_and_a_reason() -> None:
    options = [option for option, _, _ in HARDENING]
    assert len(options) == len(set(options)) > 150
    assert all(value in ("y", "n") and reason for _, value, reason in HARDENING)
    assert LOCKDOWN == {
        "CONFIG_SECURITY_LOCKDOWN_LSM",
        "CONFIG_SECURITY_LOCKDOWN_LSM_EARLY",
        "CONFIG_LOCK_DOWN_KERNEL_FORCE_CONFIDENTIALITY",
    }


def test_a_debian_config_names_the_release_it_compiles() -> None:
    kernel = Kernel.build(base=BASE)
    assert kernel.version == "6.12.113"
    assert kernel.source == Git("https://github.com/gregkh/linux", "v6.12.113")
    assert kernel.modules is False and kernel.hardening is True


def test_a_fragment_file_reads_as_its_options(tmp_path: Path) -> None:
    path = tmp_path / "xfs.config"
    path.write_text("# file systems\nCONFIG_XFS_FS=y\n\n# CONFIG_XFS_ONLINE_SCRUB is not set\n")
    lines = _fragment(Kernel.build(base=BASE, fragments=[path]))
    assert lines[-2:] == ["CONFIG_XFS_FS=y", "# CONFIG_XFS_ONLINE_SCRUB is not set"]
    path.write_text("XFS=y\n")
    with pytest.raises(ValidationError, match="xfs.config:1 is not a .config line"):
        _fragment(Kernel.build(base=BASE, fragments=[path]))


@pytest.mark.parametrize(
    ("make", "problem"),
    [
        (lambda: Kernel.build(base=BASE, config=Path("k.config")), "both name the config"),
        (lambda: Kernel.build("6.12", Git("https://k.example/linux", "v6.12")), "start from"),
        (lambda: Kernel.build(base=Path("k.config")), "base= must be DebianConfig"),  # type: ignore[arg-type]
        (lambda: Kernel(hardening=False), "applies to a built kernel"),
        (lambda: Kernel(modules=True), "says whether a built kernel loads modules"),
        (lambda: KernelFragment({"XFS_FS": "y"}), "is not a CONFIG_\\* name"),
    ],
    ids=["base and config", "no config", "a path base", "default", "default modules", "name"],
)
def test_a_mistake_is_refused_with_its_fix(make: object, problem: str) -> None:
    with pytest.raises(ValidationError, match=problem) as raised:
        make()  # type: ignore[operator]
    assert raised.value.hint


@pytest.mark.parametrize(
    ("profile", "severity"),
    [("production", "error"), ("dev", "warning")],
)
def test_an_unhardened_kernel_is_refused_in_production(profile: str, severity: str) -> None:
    image = Image(
        "k",
        kernel=Kernel.build(base=BASE, hardening=False),
        profiles={"dev": Profile(production=False)},
    )
    found = [d for d in check(tundravm.lower(image), profiles=[profile])]
    unhardened = [d for d in found if d.code == "kernel-unhardened"]
    assert [d.severity for d in unhardened] == [severity]
    assert "hardening=False" in (unhardened[0].hint or "")
    hardened = tundravm.lower(Image("k", kernel=Kernel.build(base=BASE)))
    assert "kernel-unhardened" not in {d.code for d in check(hardened)}
