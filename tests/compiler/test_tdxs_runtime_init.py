"""The ``Tdxs()`` fragment: source build, config, socket-activated units and account."""

from pathlib import Path
from typing import Literal

import pytest

from tests.helpers import conf_list
from tundravm._source import GitSource
from tundravm.declarative import (
    Declaration,
    Fragment,
    Git,
    Key,
    Package,
    Recipe,
    Setting,
    compile,
    lint,
    lock,
    lower,
)
from tundravm.declarative.utils import Tdxs
from tundravm.errors import ValidationError
from tundravm.testing import CompiledTree, compile_tree

TDXS_CONFIG = "mkosi.extra/etc/tdxs/config.yaml"


def _recipe(*items: Declaration | Fragment) -> Recipe:
    return Recipe("tdxs", Fragment("attestation", items=items))


def _compile(tmp_path: Path, *items: Declaration | Fragment) -> CompiledTree:
    return compile_tree(_recipe(*items), path=tmp_path / "tree")


def _pinned(tmp_path: Path, *items: Declaration | Fragment) -> CompiledTree:
    """The tree with every source pinned, so the hooks build the fetched checkout."""
    recipe = _recipe(*items)
    tree = compile(recipe, lock=lock(recipe, resolver=lambda source: "a" * 40))
    tree.write(tmp_path / "tree")
    return CompiledTree(tmp_path / "tree", tree.variants, default_profile=tree.variants[0])


def test_tdxs_declares_build_packages(tmp_path: Path) -> None:
    tree = _compile(tmp_path, Tdxs())

    build_packages = conf_list(tree.conf(), "BuildPackages")
    assert "golang" in build_packages
    assert "git" in build_packages
    assert "build-essential" in build_packages


def test_tdxs_adds_build_hook(tmp_path: Path) -> None:
    tree = _pinned(tmp_path, Tdxs())

    builds = [line for line in tree.script("build").splitlines() if "tundravm-sources/" in line]
    assert len(builds) == 1
    build_script = builds[0]
    assert '"$SRCDIR/tundravm-sources/tdxs-aaaaaaaaaaaa"' in build_script
    assert "tdxs-2bddc6a617e7-aaaaaaaaaaaa" in build_script  # Hyodar/tundra-tools at the pin
    assert "mkosi-chroot bash -c" in build_script
    assert "mkdir -p ./build" in build_script
    assert "go build" in build_script
    assert "./cmd/tdxs" in build_script
    assert "$DESTDIR/usr/bin/tdxs" in build_script
    assert "-trimpath" in build_script
    assert "-buildid=" in build_script
    assert "sync-constellation" not in build_script


def test_tdxs_custom_source(tmp_path: Path) -> None:
    source = Git("https://github.com/custom/tdxs-fork", "v2.0")
    spec = lower(_recipe(Tdxs(source=source))).source_builds()["tdxs"]
    assert spec.source == GitSource("https://github.com/custom/tdxs-fork", "v2.0")
    assert "-b v2.0" in spec.render()
    assert "tdxs-aaaaaaaaaaaa" in _pinned(tmp_path, Tdxs(source=source)).script("build")


def test_tdxs_generates_config_yaml_and_units(tmp_path: Path) -> None:
    tree = _compile(tmp_path, Tdxs())

    assert "golang" in conf_list(tree.conf(), "BuildPackages")

    config_content = tree.read(TDXS_CONFIG)
    assert "transport:" in config_content
    assert "type: socket" in config_content
    assert "systemd: true" in config_content
    assert "issuer:" in config_content
    assert "type: tdx" in config_content

    svc_content = tree.unit("tdxs.service")
    assert "User=tdxs" in svc_content
    assert "Group=tdx" in svc_content
    assert "Type=notify" in svc_content
    assert "ExecStart=/usr/bin/tdxs" in svc_content
    assert "--log-level info" in svc_content
    assert "Requires=tdxs.socket" in svc_content

    sock_content = tree.unit("tdxs.socket")
    assert "ListenStream=/var/tdxs.sock" in sock_content
    assert "SocketMode=0660" in sock_content
    assert "SocketUser=root" in sock_content
    assert "SocketGroup=tdx" in sock_content

    postinst = tree.script("postinst").splitlines()
    assert "mkosi-chroot groupadd --system tdx" in postinst
    (useradd,) = [line for line in postinst if "useradd" in line]
    assert useradd.startswith("mkosi-chroot useradd --system")
    assert useradd.endswith(" tdxs")
    assert postinst.index("mkosi-chroot groupadd --system tdx") < postinst.index(useradd)

    assert "mkosi-chroot systemctl enable tdxs.service" in postinst
    assert "mkosi-chroot systemctl enable tdxs.socket" in postinst


def test_tdxs_resolves_init_dependency_when_init_scripts_present(tmp_path: Path) -> None:
    tree = _compile(tmp_path, Key("key_persistent"), Tdxs(after_init=True))

    svc_content = tree.unit("tdxs.service")
    assert "After=runtime-init.service" in svc_content
    assert "Requires=runtime-init.service tdxs.socket" in svc_content

    sock_content = tree.unit("tdxs.socket")
    assert "After=runtime-init.service" in sock_content
    assert "Requires=runtime-init.service" in sock_content


def test_tdxs_no_init_dependency_when_no_init_scripts(tmp_path: Path) -> None:
    recipe = _recipe(Tdxs(after_init=True))
    tree = compile_tree(recipe, path=tmp_path / "tree")

    assert "runtime-init" not in tree.unit("tdxs.service")
    assert "runtime-init" not in tree.unit("tdxs.socket")
    codes = {d.code for d in lint(recipe)}
    assert "unit-after-init-without-init" in codes


def test_tdxs_renders_issuer_and_validator_types(tmp_path: Path) -> None:
    tree = _compile(tmp_path, Tdxs(issuer="azure", validator="gcp"))

    content = tree.read(TDXS_CONFIG)
    assert "issuer:" in content
    assert "type: azure" in content
    assert "validator:" in content
    assert "type: gcp" in content


def test_tdxs_validator_config_supports_expected_measurements(tmp_path: Path) -> None:
    fragment = Tdxs(
        issuer=None,
        validator="tdx",
        expected_measurements=(("mrtd", "abc123"), ("rtmr0", "def456")),
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


def test_tdxs_rejects_invalid_issuer_type() -> None:
    with pytest.raises(ValidationError, match="Unsupported tdxs type"):
        Tdxs(issuer="invalid")  # type: ignore[arg-type]


def test_tdxs_rejects_no_roles() -> None:
    with pytest.raises(ValidationError, match="at least one of issuer_type or validator_type"):
        Tdxs(issuer=None, validator=None)


def test_build_packages_lower_to_build_packages(tmp_path: Path) -> None:
    tree = _compile(tmp_path, Package("golang", role="build"), Package("git", role="build"))

    build_packages = conf_list(tree.conf(), "BuildPackages")
    assert "golang" in build_packages
    assert "git" in build_packages
    assert "golang" not in conf_list(tree.conf(), "Packages")


def test_build_sources_setting_mounts_build_source(tmp_path: Path) -> None:
    tree = _compile(tmp_path, Setting("Build", "BuildSources", ("../services/tdxs:tdxs",)))

    assert "BuildSources=../services/tdxs:tdxs" in tree.conf().splitlines()
