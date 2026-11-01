import json
import re
from copy import deepcopy

import pytest

from tundravm._describe import describe, render
from tundravm.declarative import (
    BootStep,
    Debian,
    Debloat,
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


def test_describe_structure() -> None:
    image = lower(_recipe())
    info = describe(image, profile="default")

    assert info["base"] == "debian/trixie"
    assert info["arch"] == "x86_64"
    assert info["profile"] == "default"
    assert info["inherits"] is None and info["fragments"] == []
    assert info["reproducible"] is True
    assert info["mirror"] == "https://deb.example"
    assert info["kernel"] is None
    # The payload carries the glue of every cloud target the recipe names.
    assert info["packages"] == [
        "curl",
        "dmidecode",
        "iproute2",
        "jq",
        "nvme-cli",
        "systemd",
        "udev",
    ]
    assert info["build_packages"] == ["gcc"]
    assert info["targets"] == ["qemu"]
    assert info["policy"] == {
        "require_frozen_lock": False,
        "mutable_ref_policy": "warn",
        "network_mode": "online",
        "storage_safety": "error",
    }

    repositories = info["repositories"]
    assert isinstance(repositories, list)
    assert repositories[0]["name"] == "debian-security"
    assert repositories[0]["priority"] == 10

    files = info["files"]
    assert isinstance(files, list)
    assert {"bytes": 12, "mode": "0644", "path": "/etc/motd", "sha256": "a948904f2f0f"} in files
    # the payload carries the recipe's cloud glue (azure and gcp targets), inert on qemu
    assert {f["path"] for f in files} >= {"/etc/hosts", "/usr/bin/azure-complete-provisioning"}
    assert "content" not in files[0]

    templates = info["templates"]
    assert isinstance(templates, list)
    assert templates[0]["path"] == "/etc/app/env"
    assert templates[0]["variables"] == ["a", "b"]

    users = info["users"]
    assert isinstance(users, list)
    assert users[0]["name"] == "app"
    assert users[0]["uid"] == 1000
    assert users[0]["groups"] == ["video"]

    services = info["services"]
    assert isinstance(services, list)
    assert services[0]["name"] == "app"
    assert services[0]["command"] == ["/usr/bin/app", "--flag"]
    assert services[0]["restart"] == "always"
    assert services[0]["after"] == ["network-online.target", "tdx-evidence.service"]

    partitions = info["partitions"]
    assert isinstance(partitions, list)
    assert partitions[0] == {"fs": "ext4", "mount_at": "/data", "name": "data", "size": "4G"}

    hooks = info["hooks"]
    assert isinstance(hooks, dict)
    assert "finalize" in hooks  # strip_image_version hook from Recipe.epoch
    assert hooks["postinst"][0] == "echo first"
    assert hooks["postinst"][1].endswith("...")
    assert len(hooks["postinst"][1]) == 80

    assert info["runtime_init"] == {"count": 1, "steps": ["init"]}

    debloat = info["debloat"]
    assert isinstance(debloat, dict)
    assert debloat == image.explain_debloat(profile="default")


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
    assert payload == json.dumps(second, sort_keys=True)
    assert payload == json.dumps(describe(lower(_recipe()), profile=profile), sort_keys=True)


def test_describe_per_variant() -> None:
    image = lower(_recipe())
    azure = describe(image, profile="azure")

    assert azure["profile"] == "azure"
    assert azure["inherits"] == "default"
    assert azure["packages"] == [
        "curl",
        "dmidecode",
        "iproute2",
        "jq",
        "nvme-cli",
        "systemd",
        "udev",
        "waagent",
    ]
    assert "modules" not in azure
    lineage = describe(image, profile="azure", inherits="base", fragments=("common", "azure"))
    assert (lineage["inherits"], lineage["fragments"]) == ("base", ["common", "azure"])
    assert azure["targets"] == ["azure"]
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
    assert "Kernel: 6.12.1  tdx=yes  cmdline=quiet" in render(info)


def test_summary_contains_key_strings() -> None:
    image = lower(_recipe())
    text = render(describe(image, profile="default"))

    assert text.startswith("profile=default  debian/trixie (x86_64)  reproducible=yes\n")
    assert "Mirror: https://deb.example" in text
    assert "Packages (7): curl dmidecode iproute2 jq nvme-cli systemd udev" in text
    assert "Build packages (1): gcc" in text
    assert "debian-security  https://deb.example/security  trixie-security  main  prio=10" in text
    assert re.search(r"\n  /etc/motd +0644  12B  sha256:a948904f2f0f\n", text)
    assert "/etc/app/env  0644  vars=a,b" in text
    assert "app  uid=1000  gid=1000  shell=/bin/bash  groups=video" in text
    assert re.search(
        r"\n  app +/usr/bin/app --flag  restart=always  type=exec  user=app  "
        r"after=network-online\.target,tdx-evidence\.service  requires=tdx-evidence\.service\n",
        text,
    )
    assert "data  4G  /data  ext4" in text
    assert "Hooks:\n" in text
    assert "  postinst (3):\n    echo first\n" in text
    assert "Runtime init: 1 (steps: init)" in text
    assert "Debloat: enabled," in text
    assert text.endswith("Targets: qemu\n")
    assert "hello world" not in text

    azure_text = render(describe(image, profile="azure"))
    assert "profile=azure" in azure_text
    assert "Extends: default\n" in azure_text
    assert "Packages (8): curl dmidecode iproute2 jq nvme-cli systemd udev waagent" in azure_text
    assert "Extends" not in text
    for internal in ("variant=", "Inherits", "Fragments", "Policy:"):
        assert internal not in text and internal not in azure_text
    assert azure_text.endswith("Targets: azure\n")
    assert render(describe(image, profile="cloud")).endswith("Targets: qemu gcp\n")


def test_summary_omits_empty_sections() -> None:
    bare = Variant("bare", add=Fragment("bare", items=(Debloat(enabled=False),)))
    recipe = Recipe(
        "bare",
        variants=(Variant("default", outputs=("qemu",)), bare),
        platform=Debian("trixie", epoch=None, boot=None),
    )
    text = render(describe(lower(recipe), profile="bare"))

    assert "Packages" not in text
    # what every profile ships: the execution gate, its script and its unit
    assert "Files (1):\n  /usr/lib/tundravm/tdx-evidence  0755" in text
    assert "Services (1):\n  tdx-evidence.service  /usr/lib/tundravm/tdx-evidence" in text
    assert "Hooks" not in text
    assert "Debloat: disabled" in text
