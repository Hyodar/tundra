import json
from copy import deepcopy

import pytest

from tundravm import (
    Debloat,
    File,
    Fragment,
    Git,
    Hook,
    Init,
    Kernel,
    Package,
    Partition,
    Recipe,
    Repository,
    Service,
    Template,
    User,
    Variant,
)
from tundravm.declarative import lower
from tundravm.errors import ValidationError
from tundravm.explain import describe, render


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
            Template("/etc/app/env", "A={a}\nB={b}\n", variables=(("b", "2"), ("a", "1"))),
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
            Init("init", "echo init", priority=10),
        ),
    )
    return Recipe(
        "explain",
        common,
        variants=(
            Variant("default", target="qemu"),
            Variant("azure", target="azure", add=Fragment("azure", items=(Package("waagent"),))),
            Variant("cloud", targets=("qemu", "gcp")),
        ),
        base="debian/trixie",
        mirror="https://deb.example",
    )


def test_explain_structure() -> None:
    image = lower(_recipe())
    info = describe(image, profile="default")

    assert info["base"] == "debian/trixie"
    assert info["arch"] == "x86_64"
    assert info["variant"] == "default"
    assert info["parent"] is None and info["fragments"] == []
    assert info["reproducible"] is True
    assert info["mirror"] == "https://deb.example"
    assert info["kernel"] is None
    assert info["packages"] == ["curl", "jq", "systemd"]
    assert info["build_packages"] == ["gcc"]
    assert info["targets"] == ["qemu"]
    assert info["policy"] == {
        "require_frozen_lock": False,
        "mutable_ref_policy": "warn",
        "network_mode": "online",
        "storage_safety": "warn",
    }

    repositories = info["repositories"]
    assert isinstance(repositories, list)
    assert repositories[0]["name"] == "debian-security"
    assert repositories[0]["priority"] == 10

    files = info["files"]
    assert isinstance(files, list)
    assert files == [{"bytes": 12, "mode": "0644", "path": "/etc/motd", "sha256": "a948904f2f0f"}]
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
    assert services[0]["after"] == ["network-online.target"]

    partitions = info["partitions"]
    assert isinstance(partitions, list)
    assert partitions[0] == {"fs": "ext4", "mount_at": "/data", "name": "data", "size": "4G"}

    hooks = info["hooks"]
    assert isinstance(hooks, dict)
    assert "finalize" in hooks  # strip_image_version hook from Recipe.epoch
    assert hooks["postinst"][0] == "echo first"
    assert hooks["postinst"][1].endswith("...")
    assert len(hooks["postinst"][1]) == 80

    assert info["runtime_init"] == {"count": 1, "priorities": [10]}

    debloat = info["debloat"]
    assert isinstance(debloat, dict)
    assert debloat == image.explain_debloat(profile="default")


def test_explain_is_json_serializable_and_stable() -> None:
    image = lower(_recipe())
    payload = json.dumps(describe(image, profile="default"), sort_keys=True)
    assert json.loads(payload)["variant"] == "default"
    assert payload == json.dumps(describe(image, profile="default"), sort_keys=True)
    assert payload == json.dumps(describe(lower(_recipe()), profile="default"), sort_keys=True)


def test_explain_does_not_mutate_state() -> None:
    image = lower(_recipe())
    digest_before = image.digest()
    first = describe(image, profile="default")
    state_after_first = deepcopy(image.state)
    second = describe(image, profile="default")

    assert first == second
    assert image.state == state_after_first
    assert image.digest() == digest_before


def test_explain_per_variant() -> None:
    image = lower(_recipe())
    azure = describe(image, profile="azure")

    assert azure["variant"] == "azure"
    assert azure["parent"] == "default"
    assert azure["packages"] == ["curl", "dmidecode", "jq", "systemd", "waagent"]
    assert "modules" not in azure and "profile" not in azure
    lineage = describe(image, profile="azure", parent="base", fragments=("common", "azure"))
    assert (lineage["parent"], lineage["fragments"]) == ("base", ["common", "azure"])
    assert azure["targets"] == ["azure"]
    assert azure["users"] == describe(image, profile="default")["users"]
    assert describe(image, profile="cloud")["targets"] == ["qemu", "gcp"]

    assert describe(image.select(("azure",))) == azure
    with pytest.raises(ValidationError):
        describe(image.select(("default", "azure")))


def test_explain_includes_kernel() -> None:
    kernel = Kernel("6.12.1", Git("https://github.com/gregkh/linux", "v6.12.1"), cmdline="quiet")
    image = lower(Recipe("kernel", Fragment("common", items=(kernel,))))
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

    assert text.startswith("Image: debian/trixie (x86_64)  variant=default  reproducible=yes\n")
    assert "Mirror: https://deb.example" in text
    assert "Packages (3): curl jq systemd" in text
    assert "Build packages (1): gcc" in text
    assert "debian-security  https://deb.example/security  trixie-security  main  prio=10" in text
    assert "/etc/motd  0644  12B  sha256:a948904f2f0f" in text
    assert "/etc/app/env  0644  vars=a,b" in text
    assert "app  uid=1000  gid=1000  shell=/bin/bash  groups=video" in text
    assert "app  /usr/bin/app --flag  restart=always  after=network-online.target" in text
    assert "data  4G  /data  ext4" in text
    assert "Hooks:\n" in text
    assert "  postinst (2):\n    echo first\n" in text
    assert "Runtime init: 1 (priorities: 10)" in text
    assert "Debloat: enabled," in text
    assert text.endswith("Targets: qemu\n")
    assert "hello world" not in text

    azure_text = render(describe(image, profile="azure"))
    assert "variant=azure" in azure_text
    assert "Parent: default\n" in azure_text
    assert "Packages (5): curl dmidecode jq systemd waagent" in azure_text
    assert "Parent" not in text
    assert azure_text.endswith("Targets: azure\n")
    assert render(describe(image, profile="cloud")).endswith("Targets: qemu gcp\n")


def test_summary_omits_empty_sections() -> None:
    bare = Variant("bare", add=Fragment("bare", items=(Debloat(enabled=False),)))
    recipe = Recipe(
        "bare", Fragment("common"), variants=(Variant("default", target="qemu"), bare), epoch=None
    )
    text = render(describe(lower(recipe), profile="bare"))

    assert "Packages" not in text
    assert "Files" not in text
    assert "Hooks" not in text
    assert "Debloat: disabled" in text
