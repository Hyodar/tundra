"""The compiler lowers every shape the declarative model expresses.

Each test compiles a recipe to a temporary tree and asserts on the emitted
files; the remaining errors are genuine impossibilities with a named
declaration and an alternative.
"""

from collections.abc import Sequence
from dataclasses import fields
from pathlib import Path

import pytest

from tundravm._describe import describe, render
from tundravm.declarative import (
    Build,
    Fragment,
    Git,
    Go,
    Http,
    Kernel,
    Key,
    Package,
    Policy,
    Recipe,
    RuntimeTools,
    Secret,
    SecretEnv,
    SecretFile,
    Secrets,
    Setting,
    Variant,
    compile,
    lock,
    lower,
    mkosi_argv,
)
from tundravm.errors import ValidationError

LINUX = "https://github.com/gregkh/linux"
SHA = "a" * 64


KERNEL_SCRIPT = "payload/mkosi.build.d/10-kernel.sh"
BUILD_SCRIPT = "payload/mkosi.build.d/50-build.sh"


def _recipe(
    *items: object,
    variants: Sequence[Variant] = (Variant("default"),),
) -> Recipe:
    common = Fragment("c", items=items)  # type: ignore[arg-type]
    return Recipe("r", items=(common,), variants=tuple(variants))


def _tree(recipe: Recipe, out: Path, *, pinned: bool = False) -> Path:
    locked = lock(recipe, resolver=lambda source: "a" * 40) if pinned else None
    compile(recipe, lock=locked).write(out)
    return out


def _read(out: Path, path: str) -> str:
    return (out / path).read_text(encoding="utf-8")


def _files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "kernel.config"
    path.write_text("CONFIG_TDX_GUEST_DRIVER=y\n")
    return path


# ── 1. Kernel sources ───────────────────────────────────────────────────


def test_kernel_sources_key_the_build_cache(tmp_path: Path, config: Path) -> None:
    tagged = Kernel("6.13.12", Git(LINUX, "v6.13.12"), config=config)
    branch = Kernel("6.13.12", Git(LINUX, "linux-6.13.y"), config=config)
    caches = [
        _read(_tree(_recipe(kernel), tmp_path / name), KERNEL_SCRIPT).split("\n")[3]
        for name, kernel in (("t", tagged), ("b", branch))
    ]
    assert all(cache.startswith("KERNEL_CACHE=") for cache in caches)
    assert caches[0] != caches[1]
    # unpinned, the script only names the fix: the build sandbox never fetches a kernel
    unpinned = _read(tmp_path / "b", KERNEL_SCRIPT)
    assert "kernel 6.13.12 is not pinned: run tundravm lock" in unpinned
    assert "git clone" not in unpinned


def test_kernel_from_a_commit_subdirectory_with_submodules(tmp_path: Path, config: Path) -> None:
    commit = "0123456789abcdef0123456789abcdef01234567"
    kernel = Kernel("6.13", Git(LINUX, commit, subdir="linux", submodules=True), config=config)
    build = _read(_tree(_recipe(kernel), tmp_path / "c", pinned=True), KERNEL_SCRIPT)
    assert '"$SRCDIR/tundravm-sources/kernel-0123456789ab-' in build
    assert '-exec cp -a --no-preserve=ownership -t "$KERNEL_CACHE/repo" {} +' in build
    assert 'ln -s repo/linux "$KERNEL_CACHE/src"' in build
    assert "cp payload/kernel/kernel.config" in build
    assert "git clone" not in build and "git -C" not in build


def test_kernel_from_a_checked_tarball(tmp_path: Path, config: Path) -> None:
    url = "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.13.12.tar.xz"
    kernel = Kernel("6.13.12", Http(url, sha256=SHA), config=config, cmdline="console=ttyS0")
    out = _tree(_recipe(kernel), tmp_path / "h")
    build = _read(out, KERNEL_SCRIPT)
    assert f'"$SRCDIR/tundravm-sources/kernel-{SHA[:12]}-' in build
    assert "! -name linux-6.13.12.tar.xz" in build  # the archive's unpacked tree is copied
    assert "curl" not in build and "git clone" not in build
    assert "KernelCommandLine=console=ttyS0\n" in _read(out, "payload/mkosi.conf")
    info = describe(lower(_recipe(kernel)), profile="default")
    assert info["kernel"]["source"] == {"sha256": SHA, "url": url}  # type: ignore[index]


def test_unpinned_kernel_tarball_names_the_fix() -> None:
    with pytest.raises(ValidationError, match=r"Kernel 6\.1: an http kernel source needs") as err:
        lower(_recipe(Kernel("6.1", Http("https://example.com/linux.tar.xz"))))
    assert "lockfile" not in str(err.value)
    assert "sha256=" in (err.value.hint or "") and "Git(url, ref)" in (err.value.hint or "")


# ── 2. Variants are additions to the payload ─────────────────────────────


def test_variants_with_their_own_settings_or_kernel_are_refused(config: Path) -> None:
    seed = Setting("Output", "Seed", ("11111111-1111-1111-1111-111111111111",))
    zstd = Setting("Output", "CompressOutput", ("zstd",))
    lts = Kernel("6.6.80", Git(LINUX, "v6.6.80"), config=config, cmdline="quiet")
    main = Kernel("6.13.12", Git(LINUX, "v6.13.12"), config=config)
    variants = (
        Variant("default"),
        Variant("small", add=Fragment("s", items=(zstd,))),
        Variant("unseeded", changes=(seed.ref.remove(),)),
        Variant(
            "lts",
            changes=(main.ref.edit(version=lts.version, source=lts.source, cmdline=lts.cmdline),),
        ),
        Variant("bare", changes=(main.ref.remove(),)),
        Variant("dev", add=Fragment("d", items=(Package("vim"),))),
    )
    with pytest.raises(ValidationError) as err:
        lower(_recipe(Package("curl"), seed, main, variants=variants))
    message = str(err.value)
    assert "'small' (it has its own Setting(Output, CompressOutput))" in message
    assert "'unseeded' (it has its own Setting(Output, Seed))" in message
    for name in ("lts", "bare"):
        assert f"'{name}' (it has its own Kernel" in message
    assert "'dev'" not in message
    assert "an image of its own" in (err.value.hint or "")


def test_an_overlay_variant_leaves_the_payload_alone(tmp_path: Path) -> None:
    variants = (Variant("default"), Variant("dev", add=Fragment("d", items=(Package("vim"),))))
    out = _tree(_recipe(Package("curl"), variants=variants), tmp_path / "v")
    assert "    curl\n" in _read(out, "payload/mkosi.conf.d/10-c.conf")
    overlay = _read(out, "payload/mkosi.profiles/dev/mkosi.conf")
    assert "    vim\n" in overlay and "    curl\n" not in overlay
    assert "ImageId=%i-dev\n" in overlay
    img = lower(_recipe(Package("curl"), variants=variants))
    assert img.state.profiles["dev"].extends == "default" and img.overlays == ("dev",)
    # The payload's bytes do not depend on its overlays
    plain = _tree(_recipe(Package("curl")), tmp_path / "p")
    payload = {k: v for k, v in _files(out).items() if not k.startswith("payload/mkosi.profiles")}
    assert payload == _files(plain)


def test_show_describes_the_recipes_build_options() -> None:
    online = _recipe(Package("curl"), Setting("Build", "WithNetwork", ("yes",)))
    assert describe(lower(online), profile="default")["build_options"] == {"with_network": True}
    assert describe(lower(_recipe(Package("curl"))), profile="default")["build_options"] == {}


# ── 3. Several Secrets per variant ──────────────────────────────────────


def _secrets(name: str, path: str) -> Secrets:
    return Secrets(name, entries=(Secret(f"{name}_token", (SecretFile(path),)),))


def test_several_secrets_get_their_own_paths_and_init_steps(tmp_path: Path) -> None:
    recipe = _recipe(_secrets("app", "/run/app"), _secrets("ops", "/run/ops"))
    out = tmp_path / "s"
    compile(recipe, lock=lock(recipe, resolver=lambda source: "a" * 40)).write(out)
    extra = out / "payload/mkosi.extra/etc/tdx"
    assert sorted(p.name for p in extra.iterdir()) == [
        "secrets-app.json",
        "secrets-app.yaml",
        "secrets-ops.json",
        "secrets-ops.yaml",
    ]
    assert '"app_token"' in (extra / "secrets-app.json").read_text()
    init = _read(out, "payload/mkosi.extra/usr/lib/runtime-init/secrets")
    assert "secret-delivery setup /etc/tdx/secrets-app.yaml" in init
    assert "secret-delivery setup /etc/tdx/secrets-ops.yaml" in init
    build = _read(out, BUILD_SCRIPT)
    assert build.count("go build") == 1  # one binary serves both


def test_one_secrets_keeps_the_runtime_tools_paths(tmp_path: Path) -> None:
    out = _tree(_recipe(_secrets("app", "/run/app")), tmp_path / "s")
    assert (out / "payload/mkosi.extra/etc/tdx/secrets.yaml").is_file()


@pytest.mark.parametrize(
    ("items", "match"),
    [
        (
            (_secrets("a", "/run/same"), _secrets("b", "/run/same")),
            r"Secrets\(b\) and Secrets\(a\) both deliver a secret to /run/same",
        ),
        (
            (
                RuntimeTools(Git(LINUX, "v1"), key_config="/etc/tdx/secrets-a.yaml"),
                Key("k"),
                _secrets("a", "/run/a"),
                _secrets("b", "/run/b"),
            ),
            r"Secrets\(a\) writes /etc/tdx/secrets-a.yaml, which RuntimeTools.key_config",
        ),
    ],
    ids=["same-file-target", "path-overlap"],
)
def test_overlapping_secrets_raise(items: tuple[object, ...], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        lower(_recipe(*items))


def test_env_targets_may_repeat_across_secrets() -> None:
    env = (SecretEnv("TOKEN"),)
    a = Secrets("a", entries=(Secret("x", env),))
    b = Secrets("b", entries=(Secret("y", env),))
    lower(_recipe(a, b))


# ── 4. Variants the payload cannot carry ─────────────────────────────


def test_only_variants_with_their_own_settings_are_listed() -> None:
    """A removal or a standalone variant is lowered whole; a variant's own settings are the
    one thing a tree cannot carry per profile."""
    seed = Setting("Output", "Seed", ("x",))
    variants = (
        Variant("default"),
        Variant("slim", changes=(Package.ref("curl").remove(),)),
        Variant("seeded", add=Fragment("s", items=(seed,))),
        Variant("rescue", inherits=None),
        Variant("ok", add=Fragment("o", items=(Package("vim"),))),
    )
    with pytest.raises(ValidationError) as err:
        lower(_recipe(Package("curl"), variants=variants))
    message = str(err.value)
    assert "'seeded' (it has its own Setting(Output, Seed))" in message
    for name in ("slim", "rescue", "ok"):
        assert f"'{name}'" not in message
    assert "make the profile an image of its own" in (err.value.hint or "")


# ── 5. A default variant inheriting another ───────────────────────────


def test_default_variant_may_extend_another_variant(tmp_path: Path) -> None:
    variants = (
        Variant("hardened", add=Fragment("h", items=(Package("apparmor"),))),
        Variant("default", inherits="hardened", add=Fragment("d", items=(Package("vim"),))),
    )
    recipe = _recipe(Package("curl"), variants=variants)
    img = lower(recipe, variants=("default",))
    assert img.default_profile == "default"
    assert img.state.effective_profile("default").packages >= {"curl", "apparmor", "vim"}
    out = tmp_path / "t"
    compile(recipe).write(out)
    assert "    apparmor\n" in _read(out, "payload/mkosi.conf.d/20-h.conf")
    # hardened lacks the default's vim: it moves out of the shared payload into the
    # default's own overlay, which mkosi reads when no profile is selected
    assert "vim" not in "".join(
        p.read_text(encoding="utf-8") for p in (out / "payload").glob("mkosi.conf*") if p.is_file()
    )
    assert "    vim\n" in _read(out, "payload/mkosi.profiles/default/mkosi.conf")
    assert "Include=mkosi.profiles/default" in _read(out, "payload/mkosi.conf.d/99-default.conf")
    assert lower(recipe).state.ensure_profile("hardened").extends is None


# ── 6. Policy ───────────────────────────────────────────────────────────


def test_policy_has_no_inert_integrity_switch() -> None:
    assert [f.name for f in fields(Policy)] == [
        "require_frozen_lock",
        "mutable_ref_policy",
        "network_mode",
        "storage_safety",
    ]
    text = render(describe(lower(_recipe(Package("curl"))), profile="default"))
    assert "integrity" not in text


# ── 7. Settings, targets and builds ─────────────────────────────────────


def test_unmapped_settings_are_written_verbatim(tmp_path: Path) -> None:
    out = _tree(
        _recipe(
            Package("curl"),
            Setting("Content", "WithDocs", ("no",)),
            Setting("Validation", "Checksum", ("yes",)),
            Setting("Content", "RemoveFiles", ("/usr/share/doc", "/usr/share/man")),
        ),
        tmp_path,
    )
    conf = _read(out, "payload/mkosi.conf")
    content = conf.split("[Content]\n", 1)[1]
    assert content.endswith(
        "WithDocs=no\nRemoveFiles=/usr/share/doc\nRemoveFiles=/usr/share/man\n\n"
        "[Validation]\nChecksum=yes\n"
    )


def test_settings_mkosi_reads_in_the_main_image_only_go_to_the_root(tmp_path: Path) -> None:
    out = _tree(
        _recipe(Setting("Runtime", "RAM", ("4G",)), Setting("Content", "WithDocs", ("no",))),
        tmp_path,
    )
    assert "\n[Runtime]\nFirmware=uefi\nRAM=4G\n" in _read(out, "mkosi.conf")
    assert "RAM" not in _read(out, "payload/mkosi.conf")
    assert "WithDocs=no" in _read(out, "payload/mkosi.conf")


@pytest.mark.parametrize(
    ("setting", "alternative"),
    [
        (Setting("Content", "Packages", ("vim",)), "Package(name)"),
        (Setting("Distribution", "Mirror", ("https://x",)), "Debian(mirror=...)"),
        (Setting("Content", "BuildScripts", ("x.sh",)), "Mkosi(scripts=[(stage, script)])"),
        (Setting("Distribution", "Distribution", ("ubuntu",)), "base=Debian(release)"),
        (Setting("Build", "Seed", ("x",)), "Setting('Output', 'Seed', ...)"),
    ],
)
def test_settings_the_compiler_writes_name_their_declaration(
    setting: Setting, alternative: str
) -> None:
    with pytest.raises(ValidationError, match="the compiler writes") as err:
        lower(_recipe(setting))
    assert alternative in (err.value.hint or "")


def test_variant_leaving_a_cloud_target_is_the_qemu_disk_alone(tmp_path: Path) -> None:
    variants = (Variant("default", outputs=("azure",)), Variant("local", outputs=("qemu",)))
    img = lower(_recipe(Package("curl"), variants=variants))
    assert img.targets["local"] == ("qemu",) and img.clouds("local") == ()
    assert img.overlays == ()
    # the payload carries the azure glue for every variant: one UKI for both
    assert "dmidecode" in img.state.effective_profile("default").packages
    out = tmp_path / "t"
    compile(_recipe(Package("curl"), variants=variants), variants=["local"]).write(out)
    # the tree is the recipe's whatever the selection; mkosi_argv builds no wrapper for local
    assert (out / "mkosi.images/azure/mkosi.conf").is_file()
    assert (out / "payload/mkosi.extra/usr/bin/azure-complete-provisioning").is_file()
    argv = mkosi_argv(img, "local")
    assert not any(arg.startswith("--dependency") for arg in argv)


def test_build_recipe_hint_names_the_public_alias() -> None:
    with pytest.raises(ValidationError, match="takes packages and env") as err:
        Build("app", Git("https://x/app", "v1"), using=Go(output="app"), packages=("gcc",))
    assert (err.value.hint or "").startswith("Pass them to Go(")
