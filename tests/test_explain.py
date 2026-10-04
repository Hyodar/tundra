import json
from copy import deepcopy

import pytest

from tundravm import Image, Kernel
from tundravm.errors import ValidationError
from tundravm.explain import describe, render
from tundravm.lockfile import recipe_digest


def _build_image() -> Image:
    image = Image(base="debian/trixie", mirror="https://deb.example")
    image.install("systemd", "curl", "jq")
    image.build_install("gcc")
    image.repository("https://deb.example/security", name="debian-security", priority=10)
    image.file("/etc/motd", content="hello world\n", mode="0644")
    image.template("/etc/app/env", template="A={a}\nB={b}\n", variables={"b": "2", "a": "1"})
    image.user("app", uid=1000, gid=1000, shell="/bin/bash", groups=("video",))
    image.service(
        "app",
        command="/usr/bin/app --flag",
        restart="always",
        after=("network-online.target",),
    )
    image.partition("data", size="4G", mount="/data")
    image.hook("postinst", "echo first\necho second")
    image.hook("postinst", "x" * 200)
    image.add_init_script("echo init", priority=10)
    image.output_targets("qemu", "gcp")
    with image.profile("azure"):
        image.install("waagent")
        image.output_targets("azure")
    return image


def test_explain_structure() -> None:
    image = _build_image()
    info = image.explain()

    assert info["base"] == "debian/trixie"
    assert info["arch"] == "x86_64"
    assert info["profile"] == "default"
    assert info["reproducible"] is True
    assert info["mirror"] == "https://deb.example"
    assert info["kernel"] is None
    assert info["packages"] == ["curl", "jq", "systemd"]
    assert info["build_packages"] == ["gcc"]
    assert info["output_targets"] == ["qemu", "gcp"]
    assert info["policy"] == {
        "require_frozen_lock": False,
        "mutable_ref_policy": "warn",
        "require_integrity": True,
        "network_mode": "online",
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
    assert partitions[0] == {"fs": "ext4", "mount": "/data", "name": "data", "size": "4G"}

    hooks = info["hooks"]
    assert isinstance(hooks, dict)
    assert "finalize" in hooks  # strip_image_version hook from reproducible=True
    assert hooks["postinst"][0] == "echo first"
    assert hooks["postinst"][1].endswith("...")
    assert len(hooks["postinst"][1]) == 80

    assert info["init_scripts"] == {"count": 1, "priorities": [10]}

    debloat = info["debloat"]
    assert isinstance(debloat, dict)
    assert debloat == image.explain_debloat()


def test_explain_is_json_serializable_and_stable() -> None:
    image = _build_image()
    payload = json.dumps(image.explain(), sort_keys=True)
    assert json.loads(payload)["profile"] == "default"
    assert payload == json.dumps(image.explain(), sort_keys=True)


def test_explain_does_not_mutate_state() -> None:
    image = _build_image()
    digest_before = recipe_digest(image._recipe_payload(profile_names=image._active_profiles))
    first = image.explain()
    state_after_first = deepcopy(image.state)
    second = image.explain()

    assert first == second
    assert image.state == state_after_first
    assert (
        recipe_digest(image._recipe_payload(profile_names=image._active_profiles)) == digest_before
    )


def test_explain_per_profile() -> None:
    image = _build_image()
    azure = image.explain(profile="azure")

    assert azure["profile"] == "azure"
    assert azure["packages"] == ["waagent"]
    assert azure["output_targets"] == ["azure"]
    assert azure["users"] == []

    with image.profile("azure"):
        assert image.explain() == azure
        assert describe(image) == azure

    with image.profiles("default", "azure"):
        with pytest.raises(ValidationError):
            image.explain()


def test_explain_includes_kernel() -> None:
    image = Image(kernel=Kernel.tdx_kernel("6.12.1", cmdline="quiet"))
    kernel = image.explain()["kernel"]
    assert kernel == {
        "cmdline": "quiet",
        "config_file": None,
        "source_repo": "https://github.com/gregkh/linux",
        "tdx": True,
        "version": "6.12.1",
    }
    assert "Kernel: 6.12.1  tdx=yes  cmdline=quiet" in image.summary()


def test_summary_contains_key_strings() -> None:
    image = _build_image()
    text = image.summary()

    assert text.startswith("Image: debian/trixie (x86_64)  profile=default  reproducible=yes\n")
    assert "Mirror: https://deb.example" in text
    assert "Packages (3): curl jq systemd" in text
    assert "Build packages (1): gcc" in text
    assert "debian-security  https://deb.example/security  prio=10" in text
    assert "/etc/motd  0644  12B  sha256:a948904f2f0f" in text
    assert "/etc/app/env  0644  vars=a,b" in text
    assert "app  uid=1000  gid=1000  shell=/bin/bash  groups=video" in text
    assert "app  /usr/bin/app --flag  restart=always  after=network-online.target" in text
    assert "data  4G  /data  ext4" in text
    assert "Hooks:\n" in text
    assert "  postinst (2):\n    echo first\n" in text
    assert "Init scripts: 1 (priorities: 10)" in text
    assert "Debloat: enabled," in text
    assert text.endswith("Output targets: qemu gcp\n")
    assert "hello world" not in text

    azure_text = image.summary(profile="azure")
    assert "profile=azure" in azure_text
    assert "Packages (1): waagent" in azure_text
    assert "Users" not in azure_text
    assert azure_text.endswith("Output targets: azure\n")
    assert render(image.explain(profile="azure")) == azure_text


def test_summary_omits_empty_sections() -> None:
    image = Image(reproducible=False)
    with image.profile("bare"):
        image.debloat(enabled=False)
    text = image.summary(profile="bare")

    assert "Packages" not in text
    assert "Files" not in text
    assert "Hooks" not in text
    assert "Debloat: disabled" in text
