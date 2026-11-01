"""The ``Tdxs()`` fragment: source build, config, socket-activated units and account."""

from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pytest

from tundravm._source import GitSource
from tundravm.declarative import (
    RUNTIME_INIT,
    Declaration,
    Fragment,
    Git,
    Key,
    Recipe,
    Setting,
    compile,
    lock,
    lower,
)
from tundravm.declarative.utils import Tdxs
from tundravm.errors import ValidationError
from tundravm.testing._model import CompiledTree, compile_tree

TDXS_CONFIG = "mkosi.extra/etc/tdxs/config.yaml"


def _recipe(*items: Declaration | Fragment) -> Recipe:
    return Recipe("tdxs", items=items)


def _compile(tmp_path: Path, *items: Declaration | Fragment) -> CompiledTree:
    return compile_tree(_recipe(*items), path=tmp_path / "tree")


def _pinned(tmp_path: Path, *items: Declaration | Fragment) -> CompiledTree:
    """The tree with every source pinned, so the hooks build the fetched checkout."""
    recipe = _recipe(*items)
    tree = compile(recipe, lock=lock(recipe, resolver=lambda source: "a" * 40))
    tree.write(tmp_path / "tree")
    return CompiledTree(tmp_path / "tree", tree.profiles, default_profile=tree.profiles[0])


def test_tdxs_custom_source(tmp_path: Path) -> None:
    source = Git("https://github.com/custom/tdxs-fork", "v2.0")
    spec = lower(_recipe(Tdxs(source=source))).source_builds()["tdxs"]
    assert spec.source == GitSource("https://github.com/custom/tdxs-fork", "v2.0")
    assert "# unpinned: v2.0\n" in spec.render()
    assert "tdxs-aaaaaaaaaaaa" in _pinned(tmp_path, Tdxs(source=source)).script("build")


@pytest.mark.parametrize(
    ("items", "requires"),
    [
        ((Key("key_persistent"),), "Requires=runtime-init.service tdxs.socket"),
        ((), None),  # no runtime-init steps: nothing to wait for
    ],
    ids=["with-init-steps", "without-init-steps"],
)
def test_tdxs_needing_runtime_init_waits_only_when_steps_exist(
    tmp_path: Path, items: tuple[Declaration, ...], requires: str | None
) -> None:
    tree = _compile(tmp_path, *items, Tdxs(needs=(RUNTIME_INIT,)))

    service = tree.unit("tdxs.service")
    if requires is None:
        assert "runtime-init" not in service
    else:
        assert requires in service
    assert "runtime-init" not in tree.unit("tdxs.socket")


def test_tdxs_validator_config_supports_expected_measurements(tmp_path: Path) -> None:
    fragment = Tdxs(
        issuer=None,
        validator="tdx",
        expected_measurements={"mrtd": "abc123", "rtmr0": "def456"},
        check_revocations=True,
        get_collateral=True,
    )
    tree = _compile(tmp_path, fragment)

    config = tree.read(TDXS_CONFIG)
    assert "issuer:" not in config
    assert "validator:" in config
    assert "type: tdx" in config
    assert "expected_measurements:" in config
    assert 'mrtd: "abc123"' in config
    assert 'rtmr0: "def456"' in config
    assert "check_revocations: true" in config
    assert "get_collateral: true" in config


@pytest.mark.parametrize(
    ("validator", "flag"),
    [("azure", "verify_imds: true"), ("gcp", "verify_identity_token: true")],
)
def test_tdxs_platform_validator_verification_flags(
    tmp_path: Path, validator: Literal["azure", "gcp"], flag: str
) -> None:
    fragment = Tdxs(validator=validator, verify_imds=True, verify_identity_token=True)
    config = _compile(tmp_path, fragment).read(TDXS_CONFIG)

    assert flag in config
    other = {"verify_imds: true", "verify_identity_token: true"} - {flag}
    assert not any(line in config for line in other)


@pytest.mark.parametrize(
    ("declare", "match"),
    [
        (lambda: Tdxs(issuer="invalid"), "Unsupported tdxs type"),  # type: ignore[arg-type]
        (
            lambda: Tdxs(issuer=None, validator=None),
            "at least one of issuer_type or validator_type",
        ),
        (lambda: Tdxs(issuer="simulator"), "would ship in production too"),  # type: ignore[arg-type]
    ],
    ids=["invalid-issuer-type", "no-roles", "simulator-issuer"],
)
def test_tdxs_refuses_bad_roles(declare: Callable[[], Tdxs], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        declare()


def test_build_sources_setting_mounts_build_source(tmp_path: Path) -> None:
    tree = _compile(tmp_path, Setting("Build", "BuildSources", ("../services/tdxs:tdxs",)))

    # mounts are main-image settings: the root mkosi.conf, after the tree itself
    lines = tree.main_conf().splitlines()
    assert lines.index("BuildSources=.") < lines.index("BuildSources=../services/tdxs:tdxs")
    assert "BuildSources" not in tree.conf()


@pytest.mark.parametrize(
    ("tdxs", "families"),
    [
        (Tdxs(), "AF_UNIX"),
        (Tdxs(issuer=None, validator="tdx"), "AF_UNIX"),
        (Tdxs(issuer="azure"), "AF_UNIX AF_INET AF_INET6"),  # IMDS over HTTP
        (Tdxs(validator="tdx", get_collateral=True), "AF_UNIX AF_INET AF_INET6"),  # Intel PCS
    ],
)
def test_tdxs_reaches_the_network_only_when_its_config_does(
    tmp_path: Path, tdxs: Tdxs, families: str
) -> None:
    assert f"RestrictAddressFamilies={families}\n" in _compile(tmp_path, tdxs).unit("tdxs.service")


def test_tdxs_without_a_quoting_issuer_gets_no_quote_access(tmp_path: Path) -> None:
    svc = _compile(tmp_path, Tdxs(issuer=None, validator="tdx")).unit("tdxs.service")
    assert "PrivateDevices=yes\n" in svc
    assert "CapabilityBoundingSet=\n" in svc
    assert "DeviceAllow" not in svc
    assert "ReadWritePaths" not in svc
