"""Plan section 3.1's prover as a file, the surge example over the component library, and
the backend the loader picks for an image file that names none."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import tundravm
from tests.compiler.test_native_tree import by_image, mkosi_summary
from tests.helpers import REPO_ROOT, SURGE_EXAMPLE
from tundravm import capabilities
from tundravm._image import recipe_of
from tundravm.backends import LimaMkosiBackend, LocalLinuxBackend
from tundravm.capabilities import Host
from tundravm.declarative import lock
from tundravm.errors import ValidationError
from tundravm.recipe import BACKEND_ENV, load_file
from tundravm.testing import assert_clean, plan
from tundravm.testing._model import compile_tree

pytestmark = pytest.mark.usefixtures("isolated_cwd")

PROVER_SOURCE = '''"""Plan section 3.1's prover.py."""

from tundravm import Image, RootFS
from tundravm.components import DevAccess, Egress, Network, Raiko
from tundravm.ports import TCP, AttestedTLS
from tundravm.secrets import Secret
from tundravm.storage import OperatorKey, Volume

data = Volume("data", mount="/persistent", unlock=OperatorKey())
config = Secret("raiko-config", rotation="restart")
raiko = Raiko(config=config, state=data.directory("raiko"), listen="127.0.0.1:8080")

image = Image(
    "prover",
    components=[raiko],
    ingress=[AttestedTLS(443, upstream=raiko.http)],
    network=Network(egress=Egress(ports=[TCP(443)])),
    rootfs=RootFS.verity(),
    dev=DevAccess(password="tdx"),
)
'''
"""The prover of plan section 3.1, as an image file: Raiko reads its configuration as a
secret (it names the L1/L2 endpoints and their keys) instead of the plan's ``jwt``."""


@pytest.fixture
def prover_file(tmp_path: Path) -> Path:
    path = tmp_path / "prover.py"
    path.write_text(PROVER_SOURCE, encoding="utf-8")
    return path


def test_the_prover_is_a_short_image_file() -> None:
    body = [line for line in PROVER_SOURCE.splitlines() if line.strip()]
    assert len(body[body.index("image = Image(") :]) < 12
    for internal in (" Key(", "Disk(", "Secrets(", "RuntimeTools", "Tdxs", "cryptsetup", "socat"):
        assert internal not in PROVER_SOURCE, internal


def test_the_prover_compiles_to_a_tree_mkosi_resolves(prover_file: Path, tmp_path: Path) -> None:
    image = tundravm.load(prover_file)
    tree = tmp_path / "mkosi"
    compile_tree(recipe_of(image), path=tree)
    summary = by_image(mkosi_summary(tree))
    assert list(summary) == ["default-initrd", "payload", "main"]
    assert summary["payload"]["Format"] == "disk"  # RootFS.verity()
    assert summary["payload"]["Release"] == "trixie"
    assert {"cryptsetup", "socat", "openssl", "openssh-server"} <= set(
        summary["payload"]["Packages"]
    )
    dev = by_image(mkosi_summary(tree, "--profile=dev"))
    assert dev["payload"]["ImageId"] == "prover-dev"
    repart = sorted(p.name for p in (tree / "mkosi.repart").iterdir())
    assert repart == ["00-esp.conf", "10-root.conf", "11-root-verity.conf"]
    manifest = json.loads(
        (tree / "payload/mkosi.extra/etc/tundravm/runtime.json").read_text(encoding="utf-8")
    )
    assert manifest["rootfs"] == "verity"
    assert [v["name"] for v in manifest["volumes"]] == ["data"]
    assert manifest["secrets"][0]["units"] == ["raiko.service"]
    assert manifest["ingress"][0]["port"] == 443


def test_the_prover_lints_clean_apart_from_unpinned_sources(prover_file: Path) -> None:
    assert_clean(tundravm.load(prover_file), allow=("source-unpinned",))


def test_the_prover_locks_without_the_network(prover_file: Path) -> None:
    """Every build from ``tools/`` is the one pinnable evidence source the hermetic resolver
    pins; Raiko is the only other source."""
    recipe = recipe_of(tundravm.load(prover_file))
    pins = {"https://github.com/NethermindEth/raiko.git": "2" * 40}

    def resolver(source: object) -> str:
        url = getattr(source, "url", "")
        return pins[url]

    payload = json.loads(lock(recipe, resolver=resolver).text())
    builds = payload["image"]["profiles"]["production"]["source_builds"]
    tools = {name for name in builds if name != "raiko"}
    assert tools == {"tdxs", "key-generation", "disk-encryption", "attested-endpoint"}


# ── Surge ───────────────────────────────────────────────────────────────


SURGE_LINES_BEFORE = 151
"""``examples/surge-tdx-prover/image.py`` on 4931008, over the internal declarative API."""


def test_surge_is_an_image_over_the_component_library() -> None:
    source = (SURGE_EXAMPLE / "image.py").read_text(encoding="utf-8")
    lines = source.splitlines()
    assert len(lines) <= 40 < SURGE_LINES_BEFORE
    assert "tundravm.declarative" not in source and "examples." not in source
    image = tundravm.load(SURGE_EXAMPLE / "image.py")
    assert [type(c).__name__ for c in image.components] == ["Nethermind", "TaikoClient", "Raiko"]
    assert image.targets == ("qemu", "azure", "gcp")


def test_surge_resolves_every_profile_and_wrapper() -> None:
    tree = SURGE_EXAMPLE / "mkosi"
    images = by_image(mkosi_summary(tree, "--dependency=azure", "--dependency=gcp"))
    assert list(images) == ["default-initrd", "payload", "azure", "gcp", "main"]
    assert images["payload"]["ImageId"] == "surge-tdx-prover"
    planned = plan(tundravm.load(SURGE_EXAMPLE / "image.py"), "production")
    for unit in ("nethermind", "taiko-client", "raiko", "attested-tls-443", "tdxs"):
        assert planned.service(unit).get("Service", "User") == unit


# ── Backend selection ───────────────────────────────────────────────────


def test_an_image_file_bakes_with_this_hosts_backend(
    prover_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(BACKEND_ENV, raising=False)
    monkeypatch.setattr(capabilities, "Host", lambda: Host(system="Linux", machine="x86_64"))
    linux = load_file(prover_file)
    assert isinstance(linux.backend, LocalLinuxBackend) and linux.backend_origin == "host"
    monkeypatch.setattr(capabilities, "Host", lambda: Host(system="Darwin", machine="arm64"))
    mac = load_file(prover_file)
    assert isinstance(mac.backend, LimaMkosiBackend) and mac.backend_origin == "host"


def test_tundravm_backend_and_an_explicit_backend_win(
    prover_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(BACKEND_ENV, "lima")
    chosen = load_file(prover_file)
    assert isinstance(chosen.backend, LimaMkosiBackend) and chosen.backend_origin == "env"
    prover_file.write_text(
        PROVER_SOURCE
        + "\nfrom tundravm.backends import LimaMkosiBackend\n\n"
        + 'backend = LimaMkosiBackend(cpus=2, memory="4GiB", disk="40GiB")\n',
        encoding="utf-8",
    )
    bound = load_file(prover_file)
    assert isinstance(bound.backend, LimaMkosiBackend) and bound.backend_origin == "recipe"
    monkeypatch.setenv(BACKEND_ENV, "docker")
    with pytest.raises(ValidationError, match="not a build backend"):
        load_file(REPO_ROOT / "examples" / "01_minimal.py")
