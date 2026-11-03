"""``Container``: a program in a Debian root of its own, built as a subimage with the image."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tundravm import File, Image, Profile, Service
from tundravm._image import compile as compile_image
from tundravm.builds import _container_roots
from tundravm.components import Container, DevAccess, Egress
from tundravm.errors import ValidationError
from tundravm.models import ContainerSpec
from tundravm.ports import TCP, UDP
from tundravm.storage import OperatorKey, Volume
from tundravm.testing import plan

pytestmark = pytest.mark.usefixtures("isolated_cwd")

data = Volume("data", unlock=OperatorKey())
HTTPD = ["/usr/bin/busybox", "httpd", "-f", "-p", "8081", "-h", "/srv/www"]
PAGE = File.text("/srv/www/index.html", "<h1>web</h1>\n")


def web(**changes: object) -> Container:
    fields: dict[str, object] = {"packages": ["busybox"], "files": [PAGE], "expose": [TCP(8081)]}
    fields.update(changes)
    return Container("web", HTTPD, **fields)  # type: ignore[arg-type]


def tree(image: Image, out: Path) -> Path:
    compile_image(image).write(out)
    return out


def read(out: Path, path: str) -> str:
    return (out / path).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: web(packages=["ubuntu:24.04"]), "an OCI image reference"),
        (lambda: web(packages=["docker.io/library/debian@sha256:ab"]), "an OCI image reference"),
        (lambda: web(packages=[]), "packages= is empty"),
        (lambda: web(egress=Egress(hosts=["10.0.0.5:443"])), "a non-empty egress= needs a veth"),
        (lambda: web(expose=[UDP(53)]), "carries TCP only"),
        (lambda: web(user="root"), 'user="root"'),
        (lambda: web(files=[File.text("/srv/a", "a", owner="web")]), "every file in it is root's"),
        (lambda: Container("web", ["busybox", "httpd"], packages=["busybox"]), "program path"),
        (lambda: Container("Web", HTTPD, packages=["busybox"]), "not a container name"),
    ],
)
def test_a_container_refuses_what_would_fetch_or_leak(
    make: Callable[[], object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message) as raised:
        make()
    assert raised.value.hint


def test_a_container_lowers_to_a_subimage_its_unit_runs_from(tmp_path: Path) -> None:
    image = Image("box", components=[web(state=data.directory("web"))])
    out = tree(image, tmp_path / "tree")
    subimage = read(out, "mkosi.images/web/mkosi.conf")
    assert "Format=portable\nOutput=web\n" in subimage
    assert "Packages=\n    busybox\n" in subimage
    assert read(out, "mkosi.images/web/mkosi.extra/srv/www/index.html") == "<h1>web</h1>\n"
    mounts = read(out, "mkosi.images/web/mkosi.postinst.d/10-mount-points.sh")
    # the read-only root holds every mount point systemd's RootImage= namespace mounts over
    for path in ("/proc", "/sys", "/dev", "/run", "/tmp", "/var/tmp", "/persistent/data/web"):
        assert f'mkdir -p "$BUILDROOT"{path}\n' in mounts, path
    payload = read(out, "payload/mkosi.conf")
    assert payload.startswith("[Config]\nDependencies=web\n")
    assert "ExtraTrees=%O/web.raw:/usr/lib/tundravm/containers/web.raw" in payload
    assert "    loop\n" in payload
    units = out / "payload/mkosi.extra/usr/lib/systemd/system"
    assert (units / "web.service.d/20-container.conf").read_text(encoding="utf-8") == (
        "[Unit]\nAfter=web-loop.service\nRequires=web-loop.service\n\n"
        "[Service]\nBindPaths=/persistent/data/web:/persistent/data/web\nMountAPIVFS=yes\n"
        "PrivateNetwork=yes\nPrivateUsers=yes\nRootImage=/usr/lib/tundravm/containers/web.raw\n"
    )
    assert "WorkingDirectory=/persistent/data/web\n" in read(units, "web.service")
    loader = read(units, "web-loop.service")
    assert "ExecStart=/usr/sbin/modprobe loop\n" in loader
    assert "ConditionPathExists=!/dev/loop-control\n" in loader
    assert "User=" not in loader
    forward = read(units, "web-forward-8081.service.d/20-container.conf")
    assert "JoinsNamespaceOf=web.service\n" in forward
    assert "PrivateNetwork=yes\n" in forward
    planned = plan(image)
    assert planned.unit("web-forward-8081.socket").get("Socket", "ListenStream") == "8081"
    assert (
        'tcp dport 8081 accept comment "web-forward-8081.service"'
        in planned.file("/etc/nftables.conf").text
    )


def test_a_profile_container_is_that_profile_s_dependency_only(tmp_path: Path) -> None:
    image = Image("box", profiles={"tenant": Profile(add=[web()])})
    out = tree(image, tmp_path / "tree")
    assert (out / "mkosi.images/web/mkosi.conf").is_file()
    assert "Dependencies=web" not in read(out, "payload/mkosi.conf")
    overlay = read(out, "payload/mkosi.profiles/tenant/mkosi.conf")
    assert overlay.startswith("[Config]\nDependencies=web\n")
    assert "ExtraTrees=%O/web.raw:/usr/lib/tundravm/containers/web.raw" in overlay


@pytest.mark.parametrize(
    ("argv", "profile", "reported"),
    [
        (["/bin/sh", "-c", "httpd"], "production", True),
        (["/usr/bin/busybox", "sh"], "production", True),
        (["/bin/sh", "-c", "httpd"], "dev", False),
        (HTTPD, "production", False),
    ],
)
def test_container_shell_refuses_a_production_shell(
    argv: list[str], profile: str, reported: bool
) -> None:
    container = Container("web", argv, packages=["busybox"])
    image = Image("box", components=[container], dev=DevAccess(password="tdx"))
    found = {d.code: d.severity for d in plan(image, profile).lint() if d.profile == profile}
    assert (found.get("container-shell") == "error") is reported


@pytest.mark.parametrize(
    ("image", "reported"),
    [
        (Image("box", services=[Service("web", HTTPD)]), "nothing in the image ships"),
        (Image("box", components=[web()]), ""),
        (
            Image("box", packages=["busybox"], components=[web(packages=["curl"])]),
            "nothing in its root ships",
        ),
    ],
    ids=["host-missing", "container-root-ships-it", "container-root-lacks-it"],
)
def test_a_container_s_program_is_looked_up_in_its_own_root(image: Image, reported: str) -> None:
    found = [d for d in plan(image).lint() if d.code == "service-program-missing"]
    assert [reported in d.message for d in found] == ([True] if reported else [])


def test_the_probe_checks_the_container_in_a_development_profile() -> None:
    image = Image("box", components=[web()], dev=DevAccess(password="tdx"))
    script = plan(image, "dev").file("/usr/lib/tundravm/probe").text
    call = "probe_container web.service /usr/lib/tundravm/containers/web.raw 192.0.2.1 9"
    assert f"check=container:web\n{call}\n" in script
    assert "nsenter -t" in script


def test_the_manifest_records_each_container_root(tmp_path: Path) -> None:
    output = tmp_path / "mkosi.output"
    output.mkdir()
    (output / "web.raw").write_bytes(b"root")
    (output / "web.manifest").write_text("{}", encoding="utf-8")
    spec = ContainerSpec("web", ("busybox",))
    state = SimpleNamespace(effective_profile=lambda name: SimpleNamespace(containers=[spec]))
    artifact: Any = SimpleNamespace(path=output / "qemu.raw")
    img: Any = SimpleNamespace(state=state)
    out = tmp_path / "build"
    out.mkdir()
    found = _container_roots(img, "default", [artifact], out, set())
    assert found["web"]["path"] == "containers/web.raw"
    assert found["web"]["packages"] == "containers/web.manifest"
    assert found["web"]["size"] == 4
    assert (out / "containers/web.raw").read_bytes() == b"root"
