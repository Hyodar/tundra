"""The starter compiles to a tree mkosi resolves, and carries the implied baseline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import tundravm
from tests.compiler.test_native_tree import by_image, mkosi_summary
from tundravm import TCP, File, Image, Service
from tundravm.image._values import MANAGEMENT_PORT
from tundravm.testing import PlannedImage, assert_clean, compile_tree, plan

from .conftest import STARTER_SOURCE, starter

pytestmark = pytest.mark.usefixtures("isolated_cwd")


def test_the_starter_is_under_twenty_lines() -> None:
    body = [line for line in STARTER_SOURCE.splitlines() if line.strip()]
    declaration = body[body.index("image = Image(") :]
    assert len(declaration) < 20, declaration


def test_load_reads_the_image_a_file_binds(starter_file: Path) -> None:
    loaded = tundravm.load(starter_file)
    assert isinstance(loaded, Image)
    assert loaded == starter()


def test_load_refuses_a_file_that_only_binds_a_recipe(tmp_path: Path) -> None:
    path = tmp_path / "old.py"
    path.write_text(
        "from tundravm.declarative import Recipe\n\nrecipe = Recipe('old')\n", encoding="utf-8"
    )
    with pytest.raises(tundravm.ValidationError) as caught:
        tundravm.load(path)
    assert "not an Image" in str(caught.value)
    assert "`image`" in str(caught.value)


def test_the_starter_lints_clean_apart_from_the_unpinned_evidence_service() -> None:
    assert_clean(tundravm.image.recipe_of(starter()), allow=("source-unpinned",))


def test_the_starter_compiles_to_a_tree_mkosi_resolves(tmp_path: Path) -> None:
    tree = tmp_path / "mkosi"
    compile_tree(tundravm.image.recipe_of(starter()), path=tree)
    summary = by_image(mkosi_summary(tree))
    assert set(summary) >= {"main", "payload"}
    assert summary["payload"]["Bootable"] in ("yes", "auto")
    assert summary["payload"]["Distribution"] == "debian"
    assert summary["payload"]["Release"] == "trixie"
    assert summary["main"]["Format"] == "disk"
    assert json.dumps(summary)  # the resolved summary is plain data


def test_the_starter_runs_the_service_as_its_own_hardened_user(planned: PlannedImage) -> None:
    unit = planned.service("app")
    assert unit.get("Service", "ExecStart") == "/usr/bin/busybox httpd -f -p 8080 -h /srv/www"
    assert unit.get("Service", "User") == "app"
    assert unit.get("Service", "Restart") == "on-failure"
    assert unit.get("Service", "Type") == "exec"
    assert unit.get("Service", "ProtectSystem") == "strict"
    assert unit.get("Service", "MemoryDenyWriteExecute") == "yes"


def test_the_firewall_opens_the_exposed_port_and_the_management_ingress(
    planned: PlannedImage,
) -> None:
    rules = planned.file("/etc/nftables.conf").text
    assert "tcp dport 8080 accept" in rules
    assert f"tcp dport {MANAGEMENT_PORT} accept" in rules
    assert "policy drop" in rules
    assert "hook output priority filter; policy accept" in rules  # unfiltered outbound


def test_the_tee_baseline_is_there(planned: PlannedImage) -> None:
    chrony = planned.file("/etc/chrony/chrony.conf").text
    assert "nts" in chrony and "time.cloudflare.com" in chrony
    journal = planned.file("/etc/systemd/journald.conf.d/50-tundravm-tee.conf").text
    assert "Storage = volatile" in journal
    assert "kexec_load_disabled = 1" in planned.file("/etc/sysctl.d/50-tundravm-tee.conf").text
    assert "nameserver 1.1.1.1" in planned.file("/etc/resolv.conf").text
    assert "DHCP=yes" in planned.file("/etc/systemd/network/50-tundravm.network").text


def test_the_evidence_service_is_implied(planned: PlannedImage) -> None:
    assert "tdxs" in planned.users
    assert planned.unit("tdxs.socket").get("Socket", "ListenStream")
    assert "tdxs" in planned.builds


def test_the_kernel_and_its_curated_modules_are_implied(planned: PlannedImage) -> None:
    assert planned.packages["linux-image-amd64"] == "runtime"
    modules = planned.settings("Content").get("KernelModules", ())
    assert "tdx_guest" in modules
    assert "virtio_blk" in modules


def test_the_file_lands_where_it_was_declared(planned: PlannedImage) -> None:
    assert planned.file("/srv/www/index.html").text == "<h1>demo</h1>\n"
    assert planned.packages["busybox"] == "runtime"


def test_plan_accepts_an_image_and_the_recipe_it_lowers_to() -> None:
    image = starter()
    from_image = plan(image, "production")
    from_recipe = plan(tundravm.image.recipe_of(image), "production")
    assert (
        from_image.file("/srv/www/index.html").text == from_recipe.file("/srv/www/index.html").text
    )
    assert plan(image).variant == "production"


def test_the_default_target_is_the_qemu_disk() -> None:
    assert starter().targets == ("qemu",)
    lowered = tundravm.lower(starter())
    assert lowered.profile_names == ("production",)


def test_a_component_in_services_is_refused() -> None:
    class App(tundravm.Component):
        def compose(self) -> list[Service]:
            return [Service("app", ["/usr/bin/true"])]

    with pytest.raises(tundravm.ValidationError) as caught:
        Image("x", services=[App()])  # type: ignore[list-item]
    assert "components=" in str(caught.value)


def test_a_service_in_components_is_refused() -> None:
    with pytest.raises(tundravm.ValidationError) as caught:
        Image("x", components=[Service("app", ["/usr/bin/true"])])  # type: ignore[list-item]
    assert "not a Component" in str(caught.value)


def test_files_takes_file_and_tree_only() -> None:
    with pytest.raises(tundravm.ValidationError) as caught:
        Image("x", files=["/etc/motd"])  # type: ignore[list-item]
    assert "not a File or Tree" in str(caught.value)
    only = Image("x", files=[File.text("/etc/motd", "hi\n")]).files[0]
    assert isinstance(only, File) and only.path == "/etc/motd"


def test_an_unknown_target_is_named() -> None:
    with pytest.raises(tundravm.ValidationError) as caught:
        Image("x", targets=("aws",))  # type: ignore[arg-type]
    assert "qemu, azure, gcp" in str(caught.value)


def test_expose_opens_udp_too() -> None:
    image = Image("x", services=[Service("dns", ["/usr/bin/true"], expose=[TCP(53)])])
    rules = plan(image, "production").file("/etc/nftables.conf").text
    assert "tcp dport 53 accept" in rules
