import json
from copy import deepcopy

import pytest

from tundravm._describe import Row, Section, describe, diff_rows, render_sections, resolved_rows
from tundravm.declarative import (
    BootStep,
    Debian,
    File,
    Fragment,
    Git,
    Hook,
    Kernel,
    Package,
    Partition,
    Recipe,
    Repository,
    Service,
    Template,
    User,
    Variant,
    lower,
    resolve,
)
from tundravm.errors import ValidationError


def _recipe() -> Recipe:
    common = Fragment(
        "common",
        items=(
            Package("systemd"),
            Package("curl"),
            Package("jq"),
            Package("gcc", role="build"),
            Repository(
                "debian-security", "https://deb.example/security", "trixie-security", priority=10
            ),
            File("/etc/motd", "hello world\n", mode=0o644),
            Template("/etc/app/env", "A={a}\nB={b}\n", variables={"b": "2", "a": "1"}),
            User(
                "app",
                system=False,
                uid=1000,
                primary_group=1000,
                shell="/bin/bash",
                groups=("video",),
            ),
            Service(
                "app",
                "/usr/bin/app --flag",
                restart="always",
                after=("network-online.target",),
            ),
            Partition("data", "4G", "/data"),
            Hook("first", "postinst", "echo first\necho second"),
            Hook("long", "postinst", "x" * 200),
            BootStep("init", "echo init"),
        ),
    )
    return Recipe(
        "explain",
        items=(common,),
        variants=(
            Variant("default", outputs=("qemu",)),
            Variant(
                "azure", outputs=("azure",), add=Fragment("azure", items=(Package("waagent"),))
            ),
            Variant("cloud", outputs=("qemu", "gcp")),
        ),
        platform=Debian("trixie", mirror="https://deb.example", boot=None),
    )


def test_describe_says_what_a_profile_builds_in_public_terms() -> None:
    """Contents become digests, a hook what it does, and a service its unit: the shell and
    the internal policy are not in it."""
    recipe = _recipe()
    image = lower(recipe)
    info = describe(image, profile="default", resolved=resolve(recipe, variant="default"))

    assert info["packages"] == [
        "azure-vm-utils",
        "curl",
        "dmidecode",
        "iproute2",
        "jq",
        "nvme-cli",
        "openssl",
        "systemd",
        "udev",
    ]
    assert info["build_packages"] == ["gcc"]
    files = info["files"]
    assert isinstance(files, list)
    assert {"bytes": 12, "mode": "0644", "path": "/etc/motd", "sha256": "a948904f2f0f"} in files
    assert info["build_steps"] == [
        "runs first (postinst)",
        "runs long (postinst)",
        "drops the NVMe host ids, which GCP sets per instance",
        "removes IMAGE_VERSION from os-release, so a rebuild matches",
    ]
    units = info["units"]
    assert isinstance(units, dict)
    assert "app.service" in units["enabled"]
    for internal in ("policy", "services", "hooks", "runtime_init"):
        assert internal not in info
    text = render_sections([Section("x", tuple(resolved_rows(info)))])
    assert "Partitions (1):\n    data  4G  /data  ext4\n" in text
    assert "Templates (1):\n    /etc/app/env  0644  vars=a,b\n" in text
    assert "debian-security  https://deb.example/security  trixie-security  main  prio=10" in text
    assert "hello world" not in text


@pytest.mark.parametrize("profile", ["default", "azure", "cloud"])
def test_describe_is_pure_and_serializable(profile: str) -> None:
    """JSON-serializable, the same on one image twice and across lowerings, and the image's
    state and digest are untouched."""
    image = lower(_recipe())
    digest_before = image.digest()
    first = describe(image, profile=profile)
    state_after_first = deepcopy(image.state)
    second = describe(image, profile=profile)

    assert first == second
    assert image.state == state_after_first
    assert image.digest() == digest_before
    payload = json.dumps(first, sort_keys=True)
    assert json.loads(payload)["profile"] == profile
    assert payload == json.dumps(describe(lower(_recipe()), profile=profile), sort_keys=True)


def test_describe_per_variant() -> None:
    image = lower(_recipe())
    azure = describe(image, profile="azure")

    assert (azure["profile"], azure["extends"], azure["targets"]) == ("azure", "default", ["azure"])
    assert "waagent" in azure["packages"]  # type: ignore[operator]
    assert azure["users"] == describe(image, profile="default")["users"]
    assert describe(image, profile="cloud")["targets"] == ["qemu", "gcp"]
    assert describe(image.select(("azure",))) == azure
    with pytest.raises(ValidationError):
        describe(image.select(("default", "azure")))


def test_describe_includes_kernel() -> None:
    kernel = Kernel("6.12.1", Git("https://github.com/gregkh/linux", "v6.12.1"), cmdline="quiet")
    image = lower(Recipe("kernel", items=(kernel,)))
    info = describe(image, profile="default")
    assert info["kernel"] == {
        "cmdline": "quiet",
        "config_file": None,
        "pinned": None,
        "source": {
            "ref": "v6.12.1",
            "repo": "https://github.com/gregkh/linux",
            "subdir": None,
            "submodules": False,
        },
        "tdx": True,
        "version": "6.12.1",
    }
    text = render_sections([Section("x", tuple(resolved_rows(info)))])
    assert "Kernel: 6.12.1  tdx=yes  cmdline=quiet" in text


@pytest.mark.parametrize(
    ("old", "new", "changed"),
    [
        (
            (Row("Packages", ("a", "b"), inline=True),),
            (Row("Packages", ("b", "c"), inline=True),),
            [Row("Packages: -a +c")],
        ),
        ((Row("Files", ("x", "y")),), (Row("Files", ("y", "z")),), [Row("Files", ("- x", "+ z"))]),
        (
            (Row("Kernel: 6.1"),),
            (Row("Kernel: 6.2"),),
            [Row("- Kernel: 6.1"), Row("+ Kernel: 6.2")],
        ),
        ((Row("Files", ("x",)),), (Row("Files", ("x",)),), []),
    ],
    ids=["inline", "block", "line", "same"],
)
def test_diff_rows_keeps_only_what_differs(
    old: tuple[Row, ...], new: tuple[Row, ...], changed: list[Row]
) -> None:
    assert diff_rows(old, new) == changed
