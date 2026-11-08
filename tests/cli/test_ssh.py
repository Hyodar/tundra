"""``tundravm ssh``: the argv it builds (``--print``) from the receipt, the build's tree and
what ``attest`` recorded, and its refusals."""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pytest

from tests.cli.conftest import Cli
from tundravm import instances
from tundravm import manifest as mf
from tundravm.builds import Build
from tundravm.cli import EXIT_ERROR, EXIT_OK

FLASHBOX = Path(__file__).parents[2] / "examples" / "flashbox" / "mkosi"
LOGIN = "payload/mkosi.extra/etc/ssh/sshd_config.d/60-tundravm-login-{name}.conf"
CONTAINER = "mkosi.images/bob/mkosi.extra/etc/ssh/tundravm/sshd_config"
HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
STRICT = (
    "-o UserKnownHostsFile=KNOWN -o StrictHostKeyChecking=yes "
    "-o ServerAliveInterval=15 -o ServerAliveCountMax=2"
)
SCRIPT = f"{STRICT} -o BatchMode=yes"


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def _instance(
    root: Path,
    *,
    logins: tuple[str, ...] = ("searcher",),
    kind: str = "production",
    platform: str = "gcp",
    known: tuple[int, ...] = (22, 10022),
    identity: str | None = None,
) -> instances.Instance:
    """An instance of a flashbox-like build: its *logins*' sshd blocks and the bob
    container's sshd in the tree, a receipt for *platform*, the host keys of the guest
    ports *known* in ``known_hosts``."""
    build = root / "build"
    for name in logins:
        path = build / mf.TREE / LOGIN.format(name=name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((FLASHBOX / LOGIN.format(name="searcher")).read_text())
    (build / mf.TREE / CONTAINER).parent.mkdir(parents=True)
    shutil.copyfile(FLASHBOX / CONTAINER, build / mf.TREE / CONTAINER)
    manifest = {"schema": mf.SCHEMA, "id": "b1", "image": "flashbox", "kind": kind}
    containers = {"bob": {"path": "containers/bob.raw"}}
    (build / mf.MANIFEST).write_text(
        json.dumps({**manifest, "profile": "default", "containers": containers})
    )
    found = instances.create(
        root / ".tundravm" / "instances",
        Build.open(build),
        kind="run",
        platform=platform,
        mode="tdx",
    )
    if platform == "qemu":
        found.data["ports"] = {"22": 2222, "8745": 18745, "10022": 10022}
    else:
        found.data["address"] = "203.0.113.7"
    if identity is not None:
        found.data["operator"] = {"identity": identity, "fingerprint": "SHA256:x"}
    found.save()
    names = [_ssh_name(found, port) for port in known]
    if names:
        instances.known_hosts(found).write_text("".join(f"{n} {HOST_KEY}\n" for n in names))
    return found


def _ssh_name(found: instances.Instance, guest: int) -> str:
    port = found.port(guest) or guest
    return found.host if port == 22 else f"[{found.host}]:{port}"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO())
    return tmp_path


def _printed(cli: Cli, found: instances.Instance, *words: str) -> str:
    result = cli("ssh", found.id, "--print", *words)
    assert result.code == EXIT_OK, result.err
    return result.out.strip().replace(str(instances.known_hosts(found)), "KNOWN")


@pytest.mark.parametrize(
    ("setup", "words", "argv"),
    [
        ({}, ("toggle", "maintenance"), f"{SCRIPT} -- searcher@203.0.113.7 toggle maintenance"),
        ({}, ("logs", "--lines", "500"), f"{SCRIPT} -- searcher@203.0.113.7 logs --lines 500"),
        ({}, ("--login", "bob"), f"-p 10022 {SCRIPT} -- bob@203.0.113.7"),
        ({"platform": "qemu"}, (), f"-p 2222 {SCRIPT} -- searcher@127.0.0.1"),
        ({"platform": "qemu"}, ("--login=bob", "id"), f"-p 10022 {SCRIPT} -- bob@127.0.0.1 id"),
        ({}, ("--login=bob", "--", "-x", "--"), f"-p 10022 {SCRIPT} -- bob@203.0.113.7 -x --"),
        (
            {"logins": ("searcher", "tenant")},
            ("--login", "tenant"),
            f"{SCRIPT} -- tenant@203.0.113.7",
        ),
        ({"kind": "development"}, ("--login", "root"), f"{SCRIPT} -- root@203.0.113.7"),
        (
            {"identity": "/keys/searcher"},
            ("status",),
            f"-i /keys/searcher -o IdentitiesOnly=yes {SCRIPT} -- searcher@203.0.113.7 status",
        ),
        (
            {"identity": "/keys/searcher"},
            ("--identity", "/keys/other", "status"),
            f"-i /keys/other -o IdentitiesOnly=yes {SCRIPT} -- searcher@203.0.113.7 status",
        ),
    ],
    ids=[
        "toggle",
        "logs",
        "container",
        "local",
        "local-container",
        "dashes",
        "second-login",
        "dev-root",
        "recorded-identity",
        "given-identity",
    ],
)
def test_the_argv(
    root: Path, cli: Cli, setup: dict[str, object], words: tuple[str, ...], argv: str
) -> None:
    found = _instance(root, **setup)  # type: ignore[arg-type]
    assert _printed(cli, found, *words) == f"ssh {argv}"


@pytest.mark.parametrize(
    ("setup", "words", "refusal"),
    [
        (
            {"logins": ("searcher", "tenant")},
            (),
            "flashbox-1's image has 2 logins; it lets in searcher, tenant, bob (container, "
            ":10022).\nfix: Name one: `tundravm ssh flashbox-1 --login searcher`.",
        ),
        ({}, ("--login", "carol"), "flashbox-1's image has no login 'carol'; it lets in"),
        ({"known": ()}, (), "flashbox-1 has no known_hosts yet: ssh would have to trust a key"),
        ({"known": (22,)}, ("--login", "bob"), "no host key for [203.0.113.7]:10022 in"),
        ({}, ("--login",), "argument --login: expected one argument"),
        (
            {},
            ("--login", "root"),
            "flashbox-1 runs a production build: root logs in only on a development build.",
        ),
    ],
    ids=[
        "two-logins",
        "unknown-login",
        "no-known-hosts",
        "container-not-pinned",
        "no-name",
        "production-root",
    ],
)
def test_refusals(
    root: Path,
    cli: Cli,
    capsys: pytest.CaptureFixture[str],
    setup: dict[str, object],
    words: tuple[str, ...],
    refusal: str,
) -> None:
    found = _instance(root, **setup)  # type: ignore[arg-type]
    try:
        result = cli("ssh", found.id, "--print", *words)
    except SystemExit as exc:
        assert (exc.code, refusal in capsys.readouterr().err) == (EXIT_ERROR, True)
        return
    assert result.code == EXIT_ERROR
    assert refusal in result.err
    if "known_hosts" in refusal:
        assert f"fix: Attest it first: `tundravm attest {found.id} --policy POLICY`" in result.err


def test_no_such_instance_names_the_ones_there(root: Path, cli: Cli) -> None:
    _instance(root)
    result = cli("ssh", "flashbox-9")
    assert result.code == EXIT_ERROR
    assert "No instance 'flashbox-9'" in result.err and "Instances: flashbox-1." in result.err


@pytest.mark.parametrize(
    ("terminal", "kind", "words", "tty"),
    [
        (True, "production", (), True),
        (True, "production", ("status",), False),
        (True, "development", ("shell",), True),
        (False, "development", (), False),
    ],
)
def test_a_terminal_gets_t_for_the_listing_or_a_dev_build_and_a_script_batch_mode(
    root: Path,
    cli: Cli,
    monkeypatch: pytest.MonkeyPatch,
    terminal: bool,
    kind: str,
    words: tuple[str, ...],
    tty: bool,
) -> None:
    if terminal:
        monkeypatch.setattr("sys.stdin", _Terminal())
    found = _instance(root, kind=kind)
    printed = _printed(cli, found, *words)
    assert (printed.startswith("ssh -t "), "-o BatchMode=yes" in printed) == (tty, not terminal)
