"""Declaration gaps closed after the surge dogfooding pass.

service() unit fields, enable/disable/mask, group(), pin_mirror(), inline
module specs, DiskSpec key=, and SecretDelivery store_at checks.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from tundravm import Image, SecretTarget, ValidationError
from tundravm.lockfile import recipe_digest
from tundravm.modules import (
    DiskEncryption,
    DiskSpec,
    KeyGeneration,
    KeySpec,
    SecretDelivery,
    SecretSpec,
)

ROOT = Path(__file__).resolve().parent.parent
UNIT_DIR = "default/mkosi.extra/usr/lib/systemd/system"


def _compile(img: Image, tmp_path: Path) -> Path:
    return img.compile(tmp_path / "tree").path


def _unit(img: Image, tmp_path: Path, name: str) -> str:
    return (_compile(img, tmp_path) / UNIT_DIR / name).read_text(encoding="utf-8")


def _postinst(img: Image, tmp_path: Path) -> list[str]:
    script = _compile(img, tmp_path) / "default/scripts/06-postinst.sh"
    return script.read_text(encoding="utf-8").splitlines()


def _payload(img: Image) -> dict[str, Any]:
    profiles = img._recipe_payload(profile_names=("default",))["profiles"]
    return cast(dict[str, Any], profiles)["default"]  # type: ignore[no-any-return]


def _digest(img: Image) -> str:
    return recipe_digest(img._recipe_payload(profile_names=("default",)))


def _codes(img: Image, profile: str | None = None) -> list[tuple[str, str | None]]:
    profiles = None if profile is None else (profile,)
    ignored = {"backend-missing", "source-unpinned"}
    return [(d.code, d.subject) for d in img.check(profiles=profiles) if d.code not in ignored]


# 1. service() unit fields


def test_service_unit_fields_render_in_place(tmp_path: Path) -> None:
    img = Image(reproducible=False)
    img.service(
        "app",
        command="/usr/bin/app",
        user="app",
        group="tdx",
        type="notify",
        restart="on-failure",
        limits={"NPROC": 512, "LimitNOFILE": "1048576"},
        kill_mode="mixed",
        timeout_stop="30s",
        wanted_by="multi-user.target",
    )
    assert _unit(img, tmp_path, "app.service") == (
        "[Unit]\n"
        "Description=app\n"
        "\n"
        "[Service]\n"
        "Type=notify\n"
        "ExecStart=/usr/bin/app\n"
        "User=app\n"
        "Group=tdx\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "LimitNOFILE=1048576\n"
        "LimitNPROC=512\n"
        "KillMode=mixed\n"
        "TimeoutStopSec=30s\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def test_service_unset_fields_keep_unit_and_digest(tmp_path: Path) -> None:
    img = Image(reproducible=False)
    img.service("app", command="/usr/bin/app", group=None, limits={}, timeout_stop="")
    unit = _unit(img, tmp_path, "app.service")
    assert "Type=simple\n" in unit
    assert "WantedBy=minimal.target\n" in unit
    for key in ("Group=", "Limit", "KillMode=", "TimeoutStopSec="):
        assert key not in unit

    plain = Image(reproducible=False)
    plain.service("app", command="/usr/bin/app")
    assert _digest(img) == _digest(plain)
    service = _payload(img)["services"][0]
    assert not {"group", "type", "limits", "kill_mode", "timeout_stop", "wanted_by"} & set(service)


def test_service_fields_enter_payload_when_set() -> None:
    img = Image(reproducible=False)
    img.service("app", command="/usr/bin/app", group="tdx", limits={"NOFILE": 8})
    service = _payload(img)["services"][0]
    assert service["group"] == "tdx"
    assert service["limits"] == {"NOFILE": "8"}


@pytest.mark.parametrize("limits", [{"FILES": 1}, {"NOFILE": "1 2"}, {"NOFILE": ""}])
def test_service_rejects_bad_limits(limits: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Image().service("app", command="/usr/bin/app", limits=limits)  # type: ignore[arg-type]


def test_explain_renders_new_service_fields() -> None:
    img = Image(reproducible=False)
    img.service("app", command="/usr/bin/app", type="oneshot", group="tdx", limits={"NOFILE": 8})
    line = next(line for line in img.summary().splitlines() if line.startswith("  app"))
    assert "type=oneshot" in line
    assert "group=tdx" in line
    assert "limits=NOFILE=8" in line


# 2. enable / disable / mask


def test_enable_links_units_without_writing_them(tmp_path: Path) -> None:
    via_enable = Image(reproducible=False).enable("openntpd", "dropbear")
    with pytest.raises(ValidationError, match="non-empty command"):
        Image().service("openntpd", command=())
    lines = _postinst(via_enable, tmp_path)
    assert "mkosi-chroot systemctl enable openntpd.service" in lines
    assert (
        'ln -sf "/etc/systemd/system/dropbear.service" '
        '"$BUILDROOT/etc/systemd/system/minimal.target.wants/"'
    ) in lines
    assert not (tmp_path / "tree" / UNIT_DIR / "openntpd.service").exists()


def test_enable_is_idempotent_and_reenables_a_disabled_service() -> None:
    img = Image(reproducible=False)
    img.service("app", command="/usr/bin/app", enabled=False)
    img.enable("app.service", "app", "x").enable("x.service")
    services = img.state.profiles["default"].services
    assert [(s.name, s.enabled) for s in services] == [("app", True), ("x", True)]


def test_disable_and_mask_run_after_postinst_hooks(tmp_path: Path) -> None:
    img = Image(reproducible=False)
    img.mask("ssh.service", "ssh.socket").disable("ssh", "ssh.socket")
    img.shell("echo hook", phase="postinst")
    img.mask("ssh.service")
    lines = _postinst(img, tmp_path)
    hook = lines.index("echo hook")
    assert lines[hook + 1 : hook + 3] == [
        "mkosi-chroot systemctl disable ssh.service ssh.socket",
        "mkosi-chroot systemctl mask ssh.service ssh.socket",
    ]
    assert lines.index("# Debloat: remove unwanted systemd binaries") > hook + 2


def test_unit_states_payload_only_when_set() -> None:
    img = Image(reproducible=False)
    before = _digest(img)
    assert "unit_states" not in _payload(img)
    img.disable("ssh.service").mask("ssh.service", "ssh.socket")
    profile = _payload(img)
    assert profile["unit_states"] == {
        "disable": ["ssh.service"],
        "mask": ["ssh.service", "ssh.socket"],
    }
    assert _digest(img) != before


def test_units_line_in_explain() -> None:
    img = Image(reproducible=False)
    img.service("app", command="/usr/bin/app")
    img.enable("a", "b.socket").mask("c")
    info = img.explain()
    assert info["units"] == {
        "enable": ["a.service", "b.socket"],
        "disable": [],
        "mask": ["c.service"],
    }
    text = img.summary()
    assert "Units: enable a.service, b.socket; mask c.service\n" in text
    assert "Services (1):" in text


def test_unit_states_inherit_into_profiles(tmp_path: Path) -> None:
    img = Image(reproducible=False)
    img.mask("ssh.service")
    img.profile("dev").disable("cron.service")
    states = img.state.effective_profile("dev").unit_states
    assert [(s.action, s.unit) for s in states] == [
        ("mask", "ssh.service"),
        ("disable", "cron.service"),
    ]


@pytest.mark.parametrize("units", [(), ("",), ("a b",)])
def test_unit_state_validation(units: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        Image().mask(*units)


# 3. group() and supplementary groups


def test_groups_emitted_before_users(tmp_path: Path) -> None:
    img = Image(reproducible=False)
    img.user("app", system=True, groups=("tdx", "video"))
    img.group("tdx", system=True, gid=990).group("eth")
    lines = _postinst(img, tmp_path)
    assert lines[3:6] == [
        "mkosi-chroot groupadd --system --gid 990 tdx",
        "mkosi-chroot groupadd eth",
        "mkosi-chroot useradd --system --shell /usr/sbin/nologin --groups tdx,video app",
    ]
    assert "Groups (2):\n  eth\n  tdx  system  gid=990\n" in img.summary()


def test_groups_payload_only_when_set() -> None:
    img = Image(reproducible=False)
    assert "groups" not in _payload(img)
    img.group("tdx", system=True)
    profile = _payload(img)
    assert profile["groups"] == [{"name": "tdx", "system": True, "gid": None}]


def test_group_validation() -> None:
    img = Image().group("tdx")
    with pytest.raises(ValidationError, match="Duplicate group"):
        img.group("tdx")
    with pytest.raises(ValidationError):
        img.group("Bad Name")


def test_user_group_undefined() -> None:
    img = Image(reproducible=False)
    img.user("app", system=True, groups=("tdx", "video", "kvm"))
    assert _codes(img) == [("user-group-undefined", "app")]
    img.group("tdx", system=True)
    assert _codes(img) == []


def test_user_group_defined_by_hook_or_other_user() -> None:
    img = Image(reproducible=False)
    img.shell("mkosi-chroot groupadd -r eth", phase="postinst")
    img.user("svc", system=True)
    img.user("app", system=True, groups=("eth", "svc"))
    assert _codes(img) == []


def test_user_group_declared_in_default_is_inherited() -> None:
    img = Image(reproducible=False)
    img.group("tdx")
    img.profile("dev").user("dev", groups=("tdx",))
    assert _codes(img, "dev") == []
    img.profile("solo", extends=None).user("solo", groups=("tdx",))
    assert ("user-group-undefined", "solo") in _codes(img, "solo")


# 4. pin_mirror()


def test_pin_mirror_sets_mirrors_and_chains(tmp_path: Path) -> None:
    img = Image(reproducible=False).pin_mirror("https://snap.example/").install("curl")
    assert (img.mirror, img.tools_tree_mirror) == ("https://snap.example/",) * 2
    conf = (_compile(img, tmp_path) / "default/mkosi.conf").read_text()
    assert "Mirror=https://snap.example/\n" in conf
    assert "ToolsTreeMirror=https://snap.example/\n" in conf

    only = Image().pin_mirror("https://m/", tools_tree=False)
    assert (only.mirror, only.tools_tree_mirror) == ("https://m/", None)
    with pytest.raises(ValidationError):
        Image().pin_mirror("")


def test_profile_wrappers_chain() -> None:
    img = Image(reproducible=False)
    dev = img.profile("dev")
    assert dev.group("tdx").enable("a").disable("b").mask("c") is dev
    own = img.state.profiles["dev"]
    assert [g.name for g in own.groups] == ["tdx"]
    assert [s.name for s in own.services] == ["a"]
    assert [(s.action, s.unit) for s in own.unit_states] == [
        ("disable", "b.service"),
        ("mask", "c.service"),
    ]


# 5. inline module specs, DiskSpec key=


def test_module_specs_configure_inline() -> None:
    img = Image(reproducible=False)
    img.apply(
        KeyGeneration(keys=(KeySpec("root", strategy="tpm", output="/run/root.key"),)),
        DiskEncryption(disks=(DiskSpec("data", key_name="root", key_path="/run/root.key"),)),
        SecretDelivery(
            secrets=(SecretSpec("token", targets=(SecretTarget.file("/run/token"),)),),
            store_at="data",
        ),
    )
    assert [type(m).__name__ for m in img.applied_modules()] == [
        "KeyGeneration",
        "DiskEncryption",
        "SecretDelivery",
    ]
    assert _codes(img) == []


def test_secret_delivery_takes_secret_specs() -> None:
    token = SecretSpec("token", targets=(SecretTarget.file("/run/t"),))
    delivery = SecretDelivery(secrets=(token,), store_at=None)
    assert delivery.secrets == (token,)
    with pytest.raises(ValidationError, match="non-empty"):
        SecretDelivery(secrets=(SecretSpec("", targets=token.targets),))
    with pytest.raises(ValidationError, match="at least one delivery target"):
        SecretDelivery(secrets=(SecretSpec("token"),))


def test_disk_key_spec_derives_key_path_without_encryption_key_line() -> None:
    key = KeySpec("root", output="/run/root.key")
    disks = DiskEncryption(disks=(DiskSpec("data", key=key, mount_at="/data"),))
    config = disks._render_config()
    assert 'encryption_key_path: "/run/root.key"' in config
    assert "encryption_key:" not in config
    assert disks.disks[0].key_path == "/run/root.key"


def test_disk_key_spec_checked_against_applied_keys() -> None:
    stray = KeySpec("root", output="/run/root.key")
    img = Image(reproducible=False)
    img.apply(
        KeyGeneration(keys=(KeySpec("other", output="/run/other.key"),)),
        DiskEncryption(disks=(DiskSpec("data", key=stray),)),
        SecretDelivery(store_at="data"),
    )
    assert _codes(img) == [("disk-key-undefined", "data")]


def test_disk_key_spec_output_mismatch_warns() -> None:
    declared = KeyGeneration(keys=(KeySpec("root", output="/run/a.key"),))
    stale = KeySpec("root", output="/run/b.key")
    img = Image(reproducible=False)
    img.apply(
        declared,
        DiskEncryption(disks=(DiskSpec("data", key=stale),)),
        SecretDelivery(store_at=None),
    )
    assert _codes(img) == [("disk-key-path-mismatch", "data")]


def test_disk_key_spec_validation() -> None:
    no_output = KeySpec("root")
    with pytest.raises(ValidationError, match="no output path"):
        DiskSpec("data", key=no_output)
    key = KeySpec("root", output="/run/root.key")
    with pytest.raises(ValidationError, match="differs from key"):
        DiskSpec("data", key=key, key_path="/run/other.key")
    with pytest.raises(ValidationError, match="key_name"):
        DiskSpec("data", key=key, key_name="other")


def test_disk_key_name_behaviour_unchanged() -> None:
    disk = DiskSpec("data", key_name="root", key_path="/run/root.key")
    assert 'encryption_key: "root"' in DiskEncryption(disks=(disk,))._render_config()


# 6. SecretDelivery store_at


def test_secret_store_undefined_warns() -> None:
    img = Image(reproducible=False)
    img.apply(SecretDelivery())
    diags = [d for d in img.check() if d.code == "secret-store-undefined"]
    assert [(d.level, d.subject) for d in diags] == [("warning", "disk_persistent")]
    assert diags[0].hint is not None and "Declared disks: none" in diags[0].hint


def test_secret_store_accepts_disk_spec_and_inherited_disks() -> None:
    disk = DiskSpec("disk_persistent", device=None)
    img = Image(reproducible=False)
    img.apply(DiskEncryption(disks=(disk,)))
    img.profile("dev").apply(SecretDelivery(store_at=disk))
    assert _codes(img, "dev") == []
    delivery = SecretDelivery(store_at=disk)
    assert delivery.store_disk == "disk_persistent"
    assert 'store_at: "disk_persistent"' in delivery._render_yaml_config()
    assert "store_at" not in SecretDelivery(store_at=None)._render_yaml_config()


# surge recipe


def test_surge_tree_unchanged() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tundravm",
            "compile",
            "examples/surge-tdx-prover/image.py",
            "--out",
            "examples/surge-tdx-prover/mkosi",
            "--check",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tree is up to date with the recipe" in result.stdout


# 7. profile-scoped init scripts


def test_init_scripts_do_not_leak_out_of_a_profile(tmp_path: Path) -> None:
    img = Image(reproducible=False)
    img.profile("dev").runtime_init("echo dev\n", priority=5)
    assert img.init_scripts("default") == ()
    assert [e.script for e in img.init_scripts("dev")] == ["echo dev\n"]
    with img.profiles("default", "dev"):
        out = img.compile(tmp_path / "tree").path
    assert not (out / "default/mkosi.extra/usr/bin/runtime-init").exists()
    assert "echo dev" in (out / "dev/mkosi.extra/usr/bin/runtime-init").read_text()
    assert img.explain()["runtime_init"] == {"count": 0, "priorities": []}
    assert img.explain(profile="dev")["runtime_init"] == {"count": 1, "priorities": [5]}


def test_extending_profile_runs_default_scripts_standalone_does_not(tmp_path: Path) -> None:
    img = Image(reproducible=False)
    img.runtime_init("echo base\n", priority=20)
    img.profile("dev").runtime_init("echo dev\n", priority=10)
    img.profile("solo", extends=None).install("curl")
    assert [e.script for e in img.init_scripts("dev")] == ["echo base\n", "echo dev\n"]
    assert img.init_scripts("solo") == ()
    with img.profiles("dev", "solo"):
        out = img.compile(tmp_path / "tree").path
    runtime_init = (out / "dev/mkosi.extra/usr/bin/runtime-init").read_text()
    assert runtime_init.index("echo dev") < runtime_init.index("echo base")
    assert not (out / "solo/mkosi.extra/usr/bin/runtime-init").exists()


def test_module_in_profile_scopes_after_dependency() -> None:
    from tundravm.modules import Tdxs

    img = Image(reproducible=False)
    img.profile("dev").apply(KeyGeneration(keys=(KeySpec("k", output="/run/k"),)))
    img.apply(Tdxs())
    unit = next(f for f in img.state.profiles["default"].files if f.path.endswith("tdxs.service"))
    assert "runtime-init.service" not in str(unit.content)


def test_init_priority_collision_reads_merged_profile_list() -> None:
    img = Image(reproducible=False)
    img.runtime_init("echo a\n", priority=1)
    img.profile("dev").runtime_init("echo b\n", priority=1)
    found = {(d.profile, d.code) for d in img.check(profiles=("default", "dev"))}
    assert ("dev", "init-priority-collision") in found
    assert ("default", "init-priority-collision") not in found


def test_init_scripts_payload_keeps_default_digest_shape() -> None:
    img = Image(reproducible=False)
    img.runtime_init("echo a\n", priority=1)
    payload = img._recipe_payload(profile_names=("default",))
    sha = hashlib.sha256(b"echo a\n").hexdigest()
    assert payload["init_scripts"] == [{"priority": 1, "sha256": sha}]
    assert "init_scripts" not in _payload(img)
    img.profile("dev").runtime_init("echo dev\n")
    dev = img._recipe_payload(profile_names=("dev",))["profiles"]
    assert len(cast(dict[str, Any], dev)["dev"]["init_scripts"]) == 1


# 8. reproducible strip hook in standalone profiles


def _finalize(img: Image, tmp_path: Path, profile: str) -> str:
    with img.profiles(profile):
        out = img.compile(tmp_path / profile).path
    return (out / profile / "scripts/07-finalize.sh").read_text()


def test_standalone_profile_keeps_image_version_strip(tmp_path: Path) -> None:
    img = Image()
    img.profile("solo", extends=None).install("curl")
    assert "/^IMAGE_VERSION=/d" in _finalize(img, tmp_path, "solo")

    plain = Image(reproducible=False)
    plain.profile("solo", extends=None).install("curl")
    assert plain.state.effective_profile("solo").phases.get("finalize") is None


def test_strip_hook_not_doubled_when_profile_extends_again() -> None:
    img = Image()
    img.profile("solo", extends=None)
    img.profile("solo", extends="default")
    finalize = img.state.effective_profile("solo").phases["finalize"]
    assert sum("IMAGE_VERSION" in c.argv[0] for c in finalize) == 1


# 9. Profile.applied_modules(inherited=)


def test_profile_applied_modules_inherited() -> None:
    img = Image(reproducible=False)
    keys = KeyGeneration(keys=(KeySpec("k", output="/run/k"),))
    img.apply(keys)
    dev = img.profile("dev")
    delivery = SecretDelivery(store_at=None)
    dev.apply(delivery)
    assert dev.applied_modules() == (delivery,)
    assert dev.applied_modules(inherited=True) == (keys, delivery)


# 10. LintError


def test_cli_bake_reports_lint_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from tundravm.cli import EXIT_SDK_ERROR, main

    recipe = tmp_path / "recipe.py"
    recipe.write_text(
        "from tundravm import Image\n"
        "from tundravm.backends.inprocess import InProcessBackend\n"
        f"img = Image(build_dir={str(tmp_path / 'build')!r}, backend=InProcessBackend())\n"
        "img.file('relative/path', content='x')\n",
        encoding="utf-8",
    )
    assert main(["bake", str(recipe)]) == EXIT_SDK_ERROR
    err = capsys.readouterr().err
    assert "error [E_LINT]: Recipe has 1 error-level diagnostics." in err
    assert "file-path-relative" in err
