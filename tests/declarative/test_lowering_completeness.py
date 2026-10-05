"""The compiler lowers every shape the declarative model expresses.

Each test compiles a recipe to a temporary tree and asserts on the emitted
files; the remaining errors are genuine impossibilities with a named
declaration and an alternative.
"""

from collections.abc import Sequence
from dataclasses import fields
from pathlib import Path

import pytest

from tundravm import Policy
from tundravm.declarative import (
    Build,
    Fragment,
    Git,
    Go,
    Http,
    Kernel,
    Key,
    Mkosi,
    Package,
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
)
from tundravm.errors import ValidationError
from tundravm.explain import describe, render

LINUX = "https://github.com/gregkh/linux"
SHA = "a" * 64


NATIVE = Mkosi(layout="native")


def _recipe(
    *items: object,
    variants: Sequence[Variant] = (Variant("default"),),
    mkosi: Mkosi | None = None,
) -> Recipe:
    common = Fragment("c", items=items)  # type: ignore[arg-type]
    return Recipe("r", common, variants=tuple(variants), mkosi=mkosi or Mkosi())


def _tree(recipe: Recipe, out: Path) -> Path:
    compile(recipe).write(out)
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


def test_tagged_kernel_keeps_the_historical_clone(tmp_path: Path, config: Path) -> None:
    kernel = Kernel("6.13.12", Git(LINUX, "v6.13.12"), config=config)
    build = _read(_tree(_recipe(kernel), tmp_path / "t"), "default/scripts/04-build.sh")
    assert (
        '    git clone --depth 1 --branch "v${KERNEL_VERSION}" \\\n'
        f'        {LINUX} "$KERNEL_CACHE/src"\n'
    ) in build


def test_kernel_from_a_branch(tmp_path: Path, config: Path) -> None:
    tagged = Kernel("6.13.12", Git(LINUX, "v6.13.12"), config=config)
    branch = Kernel("6.13.12", Git(LINUX, "linux-6.13.y"), config=config)
    out = _tree(_recipe(branch), tmp_path / "b")
    build = _read(out, "default/scripts/04-build.sh")
    clone = 'git clone --depth 1 --branch linux-6.13.y \\\n        {} "$KERNEL_CACHE/src"'
    assert clone.format(LINUX) in build
    cache = build.split("\n")[3]
    tagged_build = _read(_tree(_recipe(tagged), tmp_path / "t"), "default/scripts/04-build.sh")
    assert cache.startswith("KERNEL_CACHE=") and cache != tagged_build.split("\n")[3]


def test_kernel_from_a_commit_subdirectory_with_submodules(tmp_path: Path, config: Path) -> None:
    commit = "0123456789abcdef0123456789abcdef01234567"
    kernel = Kernel("6.13", Git(LINUX, commit, subdir="linux", submodules=True), config=config)
    build = _read(_tree(_recipe(kernel), tmp_path / "c"), "default/scripts/04-build.sh")
    assert f'git -C "$KERNEL_CACHE/repo" fetch --depth 1 {LINUX} {commit}' in build
    assert 'git -C "$KERNEL_CACHE/repo" checkout -q FETCH_HEAD' in build
    assert "submodule update --init --recursive --depth 1" in build
    assert 'ln -s repo/linux "$KERNEL_CACHE/src"' in build


def test_kernel_from_a_checked_tarball(tmp_path: Path, config: Path) -> None:
    url = "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.13.12.tar.xz"
    kernel = Kernel("6.13.12", Http(url, sha256=SHA), config=config, cmdline="console=ttyS0")
    out = _tree(_recipe(kernel), tmp_path / "h")
    build = _read(out, "default/scripts/04-build.sh")
    assert f'curl -fsSL {url} -o "$KERNEL_CACHE/linux.tar"' in build
    assert f'echo "{SHA}  $KERNEL_CACHE/linux.tar" | sha256sum -c -' in build
    assert 'tar -xf "$KERNEL_CACHE/linux.tar" -C "$KERNEL_CACHE/src" --strip-components=1' in build
    assert "git clone" not in build
    conf = _read(out, "default/mkosi.conf")
    assert "    curl\n" in conf and "KernelCommandLine=console=ttyS0" in conf
    info = describe(lower(_recipe(kernel)), profile="default")
    assert info["kernel"]["source"] == {"sha256": SHA, "url": url}  # type: ignore[index]


def test_unpinned_kernel_tarball_names_the_fix() -> None:
    with pytest.raises(ValidationError, match=r"Kernel 6\.1: Http.*needs sha256=") as err:
        lower(_recipe(Kernel("6.1", Http("https://example.com/linux.tar.xz"))))
    assert "sha256" in (err.value.hint or "")


# ── 2. Per-variant settings and kernels ─────────────────────────────────


def test_variant_settings_lower_into_its_own_conf(tmp_path: Path) -> None:
    seed = Setting("Output", "Seed", ("11111111-1111-1111-1111-111111111111",))
    plain = _recipe(Package("curl"), seed)
    zstd = Setting("Output", "CompressOutput", ("zstd",))
    variants = (
        Variant("default"),
        Variant("small", add=Fragment("s", items=(zstd,))),
        Variant("unseeded", remove=(seed,)),
        Variant("dev", add=Fragment("d", items=(Package("vim"),))),
    )
    out = _tree(_recipe(Package("curl"), seed, variants=variants), tmp_path / "v")
    assert "CompressOutput=zstd" in _read(out, "small/mkosi.conf")
    assert "CompressOutput" not in _read(out, "default/mkosi.conf")
    assert "Seed=1111" not in _read(out, "unseeded/mkosi.conf")
    assert "Seed=1111" in _read(out, "dev/mkosi.conf")
    img = lower(_recipe(Package("curl"), seed, variants=variants))
    assert img.state.profiles["dev"].extends == "default"  # same settings: still an overlay
    assert img.state.profiles["small"].extends is None
    # The default variant's bytes do not depend on its siblings
    assert _files(out / "default") == _files(_tree(plain, tmp_path / "p") / "default")


def test_variant_kernels(tmp_path: Path, config: Path) -> None:
    lts = Kernel("6.6.80", Git(LINUX, "v6.6.80"), config=config, cmdline="quiet")
    main = Kernel("6.13.12", Git(LINUX, "v6.13.12"), config=config)
    variants = (
        Variant("default"),
        Variant("lts", replace=(lts,)),
        Variant("bare", remove=(main,)),
    )
    recipe = _recipe(Package("curl"), main, variants=variants)
    out = _tree(recipe, tmp_path / "k")
    assert 'KERNEL_VERSION="6.6.80"' in _read(out, "lts/scripts/04-build.sh")
    assert "KernelCommandLine=quiet" in _read(out, "lts/mkosi.conf")
    assert 'KERNEL_VERSION="6.13.12"' in _read(out, "default/scripts/04-build.sh")
    assert "KernelCommandLine" not in _read(out, "default/mkosi.conf")
    assert (out / "lts/kernel/kernel.config").is_file()
    assert not (out / "bare/kernel").exists() and "Bootable" not in _read(out, "bare/mkosi.conf")
    img = lower(recipe)
    assert describe(img, profile="lts")["kernel"]["version"] == "6.6.80"  # type: ignore[index]
    assert describe(img, profile="bare")["kernel"] is None
    plain = _tree(_recipe(Package("curl"), main), tmp_path / "p")
    assert _files(out / "default") == _files(plain / "default")


def test_explain_shows_the_variants_own_build_options() -> None:
    variants = (
        Variant("default"),
        Variant("offline", add=Fragment("o", items=(Setting("Build", "WithNetwork", ("no",)),))),
    )
    img = lower(_recipe(Package("curl"), variants=variants))
    assert describe(img, profile="offline")["build_options"] == {"with_network": False}
    assert describe(img, profile="default")["build_options"] == {}


# ── 3. Several Secrets per variant ──────────────────────────────────────


def _secrets(name: str, path: str) -> Secrets:
    return Secrets(name, entries=(Secret(f"{name}_token", (SecretFile(path),)),))


def test_several_secrets_get_their_own_paths_and_init_steps(tmp_path: Path) -> None:
    recipe = _recipe(_secrets("app", "/run/app"), _secrets("ops", "/run/ops"))
    out = tmp_path / "s"
    compile(recipe, lock=lock(recipe, resolver=lambda source: "a" * 40)).write(out)
    extra = out / "default/mkosi.extra/etc/tdx"
    assert sorted(p.name for p in extra.iterdir()) == [
        "secrets-app.json",
        "secrets-app.yaml",
        "secrets-ops.json",
        "secrets-ops.yaml",
    ]
    assert '"app_token"' in (extra / "secrets-app.json").read_text()
    init = _read(out, "default/mkosi.extra/usr/bin/runtime-init")
    assert "secret-delivery setup /etc/tdx/secrets-app.yaml" in init
    assert "secret-delivery setup /etc/tdx/secrets-ops.yaml" in init
    build = _read(out, "default/scripts/04-build.sh")
    assert build.count("go build") == 1  # one binary serves both


def test_one_secrets_keeps_the_runtime_tools_paths(tmp_path: Path) -> None:
    out = _tree(_recipe(_secrets("app", "/run/app")), tmp_path / "s")
    assert (out / "default/mkosi.extra/etc/tdx/secrets.yaml").is_file()


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


# ── 4. The native layout ────────────────────────────────────────────────


def test_native_layout_emits_extending_variants(tmp_path: Path) -> None:
    variants = (Variant("default"), Variant("dev", add=Fragment("d", items=(Package("vim"),))))
    out = _tree(_recipe(Package("curl"), variants=variants, mkosi=NATIVE), tmp_path)
    assert "    curl\n" in _read(out, "mkosi.conf")
    overlay = _read(out, "mkosi.profiles/dev/mkosi.conf")
    assert "    vim\n" in overlay and "    curl\n" not in overlay


def test_native_layout_lists_every_variant_it_cannot_emit() -> None:
    seed = Setting("Output", "Seed", ("x",))
    variants = (
        Variant("default"),
        Variant("slim", remove=(Package("curl"),)),
        Variant("seeded", add=Fragment("s", items=(seed,))),
        Variant("rescue", parent=None),
        Variant("ok", add=Fragment("o", items=(Package("vim"),))),
    )
    with pytest.raises(ValidationError) as err:
        lower(_recipe(Package("curl"), variants=variants, mkosi=NATIVE))
    message = str(err.value)
    assert "'slim' (it removes Package(curl, runtime))" in message
    assert "'seeded' (it has its own Setting(Output, Seed))" in message
    assert "'rescue' (it is parentless)" in message
    assert "'ok'" not in message
    assert "layout='directories'" in (err.value.hint or "")


# ── 5. A default variant chained onto another ───────────────────────────


def test_default_variant_may_extend_another_variant(tmp_path: Path) -> None:
    variants = (
        Variant("hardened", add=Fragment("h", items=(Package("apparmor"),))),
        Variant("default", parent="hardened", add=Fragment("d", items=(Package("vim"),))),
    )
    img = lower(_recipe(Package("curl"), variants=variants))
    assert img.default_profile == "default"
    assert img.state.effective_profile("default").packages >= {"curl", "apparmor", "vim"}
    hardened = img.state.effective_profile("hardened")
    assert img.state.profiles["hardened"].extends is None and "vim" not in hardened.packages
    out = _tree(_recipe(Package("curl"), variants=variants), tmp_path)
    assert "    apparmor\n" in _read(out, "default/mkosi.conf")


# ── 6. Policy ───────────────────────────────────────────────────────────


def test_policy_has_no_inert_integrity_switch() -> None:
    assert [f.name for f in fields(Policy)] == [
        "require_frozen_lock",
        "mutable_ref_policy",
        "network_mode",
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
    conf = _read(out, "default/mkosi.conf")
    content = conf.split("[Content]\n", 1)[1]
    assert "WithDocs=no\nRemoveFiles=/usr/share/doc\nRemoveFiles=/usr/share/man\n" in content
    assert content.index("RemoveFiles") < content.index("ExtraTrees=")
    assert conf.endswith("\n[Validation]\nChecksum=yes\n")


@pytest.mark.parametrize(
    ("setting", "alternative"),
    [
        (Setting("Content", "Packages", ("vim",)), "Package(name)"),
        (Setting("Distribution", "Mirror", ("https://x",)), "Recipe.mirror"),
        (Setting("Content", "BuildScripts", ("x.sh",)), "Hook(name, phase, script)"),
        (Setting("Build", "Seed", ("x",)), "Setting('Output', 'Seed', ...)"),
    ],
)
def test_settings_the_compiler_writes_name_their_declaration(
    setting: Setting, alternative: str
) -> None:
    with pytest.raises(ValidationError, match="the compiler writes") as err:
        lower(_recipe(setting))
    assert alternative in (err.value.hint or "")


def test_variant_leaving_a_cloud_target_lowers_standalone(tmp_path: Path) -> None:
    variants = (Variant("default", target="azure"), Variant("local", target="qemu"))
    img = lower(_recipe(Package("curl"), variants=variants))
    assert img.state.profiles["local"].extends is None
    assert img.state.effective_profile("local").output_targets == ("qemu",)
    out = _tree(_recipe(Package("curl"), variants=variants), tmp_path)
    assert not (out / "local/scripts/azure-postoutput.sh").exists()


def test_build_recipe_hint_names_the_public_alias() -> None:
    with pytest.raises(ValidationError, match="takes packages and env") as err:
        Build("app", Git("https://x/app", "v1"), recipe=Go(output="app"), packages=("gcc",))
    assert (err.value.hint or "").startswith("Pass them to Go(")
