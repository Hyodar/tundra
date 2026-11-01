"""The ``tundravm.declarative.utils`` fragments, file by file; the evidence service's
against the committed attested node tree, which holds it."""

from __future__ import annotations

import pytest

from tests.helpers import ATTESTED_NODE
from tundravm.declarative import (
    BootStep,
    Build,
    Debian,
    Disk,
    Fragment,
    Git,
    Group,
    Hook,
    Install,
    Ready,
    Recipe,
    Tree,
    User,
    Variant,
    compile,
)
from tundravm.declarative.utils import (
    Backports,
    DevTools,
    EfiStub,
    RuntimeDirectory,
    RuntimeFile,
    Tdxs,
)
from tundravm.errors import ValidationError

GOLDEN = ATTESTED_NODE / "mkosi"
BASE_SNAPSHOT = "20251113T083151Z"
EFI_STUB_VERSION = "255.4-1"
SNAPSHOT = "https://snapshot.debian.org/archive/debian/20251113T083151Z/"
PLATFORM = Debian("trixie", snapshot=BASE_SNAPSHOT, boot=None)
"""A trixie archive, without its packages: the files the fragments render
depend on the snapshot (backports sources, the EFI stub URL)."""
GENERIC = ("payload/mkosi.conf", "payload/mkosi.conf.d/", "payload/mkosi.build.d/")
"""What every fragment touches (its packages, its build hooks): not the fragment's own files."""


def _recipe(*items: Fragment, variants: tuple[Variant, ...] = (Variant("default"),)) -> Recipe:
    common = Fragment("module", items=items)
    return Recipe(name="module", platform=PLATFORM, items=(common,), variants=variants)


def _text(tree: Tree, path: str) -> str:
    content = next(e.content for e in tree.entries if e.path == path)
    assert content is not None, path
    return content.decode()


def _files(tree: Tree) -> dict[str, bytes]:
    """The tree's files by tree path."""
    return {e.path: e.content for e in tree.entries if e.content is not None}


# ── The utils fragments ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("fragment", "golden", "paths"),
    [
        (
            Tdxs(),
            "payload/",
            (
                "payload/mkosi.extra/etc/tdxs/config.yaml",
                "payload/mkosi.extra/usr/lib/systemd/system/tdxs.service",
                "payload/mkosi.extra/usr/lib/systemd/system/tdxs.socket",
                "payload/mkosi.extra/usr/lib/udev/rules.d/65-tundravm-tdx-guest.rules",
            ),
        ),
        (
            DevTools(reproducible=False),
            None,
            ("payload/mkosi.extra/usr/lib/systemd/system/serial-console.service",),
        ),
        (
            Backports(),
            None,
            (
                "mkosi.sandbox/etc/apt/preferences.d/debian-backports.pref",
                "mkosi.sandbox/etc/apt/sources.list.d/debian-backports.sources",
            ),
        ),
    ],
    ids=["tdxs", "devtools", "backports"],
)
def test_fragment_files_match_the_committed_attested_node_tree(
    fragment: Fragment, golden: str | None, paths: tuple[str, ...]
) -> None:
    """Each fragment adds exactly its files; those the node ships match its tree byte for byte."""
    files = _files(compile(_recipe(fragment)))
    base = _files(compile(_recipe()))
    added = {path for path in set(files) - set(base) if not path.startswith(GENERIC)}
    assert sorted(added) == sorted(paths)
    if golden is None:
        return
    for path in paths:
        rel = path.removeprefix("payload/") if golden else path
        assert files[path] == (GOLDEN / golden / rel).read_bytes(), path


def test_efi_stub_hook_lands_in_the_postinst_script() -> None:
    stub = EfiStub(snapshot=BASE_SNAPSHOT, version=EFI_STUB_VERSION)
    (hook,) = stub.items
    assert isinstance(hook, Hook) and hook.script == stub.render_script()
    tree = compile(_recipe(stub))
    assert hook.script.strip() + "\n" in _text(tree, "payload/mkosi.postinst.d/50-postinst.sh")


def test_runtime_directory_is_a_boot_step_that_installs_the_directory() -> None:
    disk = Disk("d", "/persistent")
    step = RuntimeDirectory("/persistent/exporter/", owner="app", group="eth", after=[disk.ready])
    assert isinstance(step, BootStep) and step.name == "dir-persistent-exporter"
    assert step.script == "install -d -m 0755 -o app -g eth /persistent/exporter\n"
    assert step.after == (disk.ready,) and step.ready == Ready("dir-persistent-exporter")
    named = RuntimeDirectory("/var/lib/my app", mode=0o700, name="state")
    assert (named.name, named.script) == ("state", "install -d -m 0700 '/var/lib/my app'\n")
    assert RuntimeDirectory("/var/lib/my app").name == "dir-var-lib-my_app"
    for bad in ("relative", "/", 7):
        with pytest.raises(ValidationError, match="absolute path"):
            RuntimeDirectory(bad)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="mode"):
        RuntimeDirectory("/x", mode=0o10000)


def test_runtime_file_is_a_boot_step_that_writes_the_file() -> None:
    directory = RuntimeDirectory("/persistent/exporter")
    step = RuntimeFile(
        "/persistent/exporter/boot.prom",
        "up 1\nit's\n",
        owner="app",
        mode=0o640,
        after=[directory.ready, Ready("secrets")],
    )
    assert step.name == "file-persistent-exporter-boot.prom"
    assert step.after == (Ready("dir-persistent-exporter"), Ready("secrets"))
    assert step.script == (
        "printf '%s' 'up 1\nit'\"'\"'s\n' > /persistent/exporter/boot.prom\n"
        "chmod 0640 /persistent/exporter/boot.prom\n"
        "chown app /persistent/exporter/boot.prom\n"
    )
    assert RuntimeFile("/etc/x", "x", group="eth").script.endswith("chown :eth /etc/x\n")
    assert "chown" not in RuntimeFile("/etc/x", "x").script
    with pytest.raises(ValidationError, match="text must be a str"):
        RuntimeFile("/etc/x", b"x")  # type: ignore[arg-type]


def test_devtools_root_password() -> None:
    hooks = [item for item in DevTools(root_password="s3cret").items if isinstance(item, Hook)]
    script = hooks[-1].script
    assert 'openssl passwd -6 -salt "$SALT" "s3cret"' in script
    assert "SALT=$(printf 'tundravm-devtools:%s' \"${IMAGE_ID:-devtools}\" | sha256sum" in script
    with pytest.raises(ValidationError):
        DevTools(root_password='a"b')


def test_devtools_salt_is_fixed_unless_reproducible_is_false() -> None:
    def login(tools: DevTools) -> str:
        return [item for item in tools.items if isinstance(item, Hook)][-1].script

    assert 'SALT="devtools01"\nROOT_PASS=$(openssl passwd -6 -salt "$SALT" "tdx")' in login(
        DevTools(salt="devtools01")
    )
    historical = login(DevTools(reproducible=False))
    assert 'ROOT_PASS=$(openssl passwd -6 "tdx")' in historical and "SALT" not in historical
    for bad in ({"salt": "has space"}, {"salt": "x" * 17}, {"salt": "ok", "reproducible": False}):
        with pytest.raises(ValidationError, match="salt"):
            DevTools(**bad)


def test_utils_freeze_lists_and_reject_bad_values() -> None:
    measurements = {"rtmr0": "ab"}
    tdxs = Tdxs(expected_measurements=measurements)
    measurements["rtmr0"] = "cd"
    assert tdxs.expected_measurements == {"rtmr0": "ab"} and hash(tdxs) == hash(
        Tdxs(expected_measurements={"rtmr0": "ab"})
    )
    with pytest.raises(ValidationError, match="must be a mapping"):
        Tdxs(expected_measurements=(("rtmr0", "ab"),))  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="EfiStub"):
        EfiStub(snapshot="", version="1")


def test_accounts_are_the_postinst_prelude_groups_first() -> None:
    accounts = Fragment(
        "accounts",
        items=(
            Group("tdx"),
            Group("x", system=False, gid=7),
            Group("eth"),
            User("svc", home="/home/svc", uid=9, primary_group="tdx", groups=("eth", "x")),
        ),
    )
    postinst = _text(compile(_recipe(accounts)), "payload/mkosi.postinst.d/50-postinst.sh")
    assert postinst.splitlines()[3:7] == [
        "mkosi-chroot groupadd --system tdx",
        "mkosi-chroot groupadd --gid 7 x",
        "mkosi-chroot groupadd --system eth",
        "mkosi-chroot useradd --system --home-dir /home/svc --create-home "
        "--shell /usr/sbin/nologin --uid 9 --gid tdx --groups eth,x svc",
    ]


def test_an_account_a_variant_replaces_moves_into_each_overlay() -> None:
    """useradd cannot run twice: an account the profiles declare differently is created by
    each profile's overlay, the default profile's included, and by no shared script."""
    recipe = _recipe(
        Fragment("users", items=(User("svc"),)),
        variants=(
            Variant("default"),
            Variant("other", inherits="default", changes=(User.ref("svc").edit(uid=5),)),
        ),
    )
    tree = compile(recipe)
    assert "useradd" not in _text(tree, "payload/mkosi.postinst.d/50-postinst.sh")
    own = _text(tree, "payload/mkosi.profiles/default/mkosi.postinst.d/50-postinst.sh")
    other = _text(tree, "payload/mkosi.profiles/other/mkosi.postinst.d/50-postinst.sh")
    assert "useradd" in own and "--uid 5" not in own
    assert "--uid 5 svc" in other


def test_build_cache_namespace_and_unpinned_marker() -> None:
    build = Build(
        "tool",
        Git("https://example.com/tool.git", "feat/x"),
        script="make",
        install=(Install("tool", "/usr/bin/tool"),),
        cache_namespace="tool-feat/x",
    )
    current = Recipe(name="m", items=(build,))
    script = _text(compile(current), "payload/mkosi.build.d/50-build.sh")
    assert "\n# unpinned: feat/x\n" in script
    assert "tundravm: source build tool is not pinned" in script
    with pytest.raises(ValidationError, match="cache_namespace"):
        Build("t", build.source, script="make", install=build.install, cache_namespace="a b")


def test_backports_include_sid_false_drops_the_sid_source_and_pin() -> None:
    with_sid, without = Backports(), Backports(include_sid=False)
    assert with_sid.include_sid is True
    sources = without.render_sources(mirror=None, release="trixie")
    assert "Suites: trixie-backports" in sources and "sid" not in sources
    assert "sid" in with_sid.render_sources(mirror=None, release="trixie")
    assert "sid" not in without.render_preferences(release="trixie")
    assert without.items == ()  # lowering writes the sandbox files; the fragment runs nothing
    with pytest.raises(ValidationError, match="include_sid"):
        Backports(include_sid="no")  # type: ignore[arg-type]
