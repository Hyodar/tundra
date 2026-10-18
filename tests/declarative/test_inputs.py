"""``recipe_inputs``: every host path a recipe's declarations read."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tundravm.declarative import (
    Debian,
    File,
    FileTree,
    Fragment,
    Git,
    Kernel,
    Package,
    Recipe,
    Template,
    Unit,
    Variant,
)
from tundravm.declarative.inputs import recipe_inputs
from tundravm.declarative.utils import Tdxs

R = "a" * 96


def test_recipe_inputs_names_every_path_in_every_variant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    policy = tmp_path / "peer.json"
    registers = {"RTMR0": [R], "RTMR1": [R], "RTMR2": [R]}
    payload = {"schema_version": 2, "scheme": "qemu", "tool": "t", "registers": registers}
    policy.write_text(json.dumps({**payload, "unverifiable": {"MRTD": "host"}}), encoding="utf-8")
    common = Fragment(
        "common",
        items=(
            Package("curl"),
            File("/etc/motd", Path("motd.txt")),
            File("/etc/inline", "inline\n"),
            Fragment("nested", items=(FileTree("/opt/app", Path("app")),)),
            Unit("a.service", Path("units/a.service")),
            Template("/etc/t.conf", Path("t.conf.tmpl")),
            Tdxs.from_policy(policy, allow_placeholder=True),
        ),
    )
    kernel = Kernel("6.12", Git("https://example.com/linux", "v6.12"), config=Path("k.config"))
    variant = Variant(
        "custom",
        add=Fragment("extra", items=(kernel,)),
        changes=(File.ref("/etc/motd").edit(content=tmp_path / "other.txt"),),
    )
    recipe = Recipe("inputs", items=(common,), variants=(Variant("default"), variant))

    assert recipe_inputs(recipe) == {
        tmp_path / name
        for name in (
            "motd.txt",
            "app",
            "units/a.service",
            "t.conf.tmpl",
            "peer.json",
            "k.config",
            "other.txt",
        )
    }


def test_recipe_inputs_include_what_the_platform_declares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    kernel = Kernel("6.12", Git("https://example.com/linux", "v6.12"), config=Path("k.config"))
    platform = Debian(
        "trixie",
        kernel_package=None,
        items=(File("/etc/asset", Path("asset.txt")), Fragment("k", items=(kernel,))),
    )
    recipe = Recipe("inputs", platform=platform, items=(Package("curl"),))

    assert recipe_inputs(recipe) == {tmp_path / "asset.txt", tmp_path / "k.config"}


def test_tdxs_policy_file_is_not_configuration(tmp_path: Path) -> None:
    policy = tmp_path / "peer.json"
    payload: dict[str, object] = {"schema_version": 2, "scheme": "qemu", "tool": "t"}
    payload["registers"] = {"RTMR0": [R], "RTMR1": [R], "RTMR2": [R]}
    payload["unverifiable"] = {"MRTD": "host firmware"}
    policy.write_text(json.dumps(payload), encoding="utf-8")

    from_file = Tdxs.from_policy(policy)

    assert from_file.policy_file == policy
    assert Tdxs.from_policy(payload).policy_file is None
    assert from_file == Tdxs.from_policy(payload)
    assert "policy_file" not in repr(from_file)
