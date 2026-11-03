"""The shared payload and the profile overlays (plan section 3.3).

tundravm resolves every profile's whole image, then writes what all of them build once,
in the shared payload, and what each builds beyond it in its overlay
(``payload/mkosi.profiles/<name>/``; the default profile's is included by a ``[Match]
Profiles=`` file, so mkosi reads it when no profile is selected). So:

- the shared payload (everything outside ``mkosi.profiles/``) is byte-identical whichever
  profiles a compile selects, for every example and fixture;
- ``mkosi summary --json`` of each profile is the shared payload's plus exactly that
  profile's overlay, nothing else;
- a removal (``tests/fixtures/profile-chain``: ``bench`` extends ``staging`` and removes
  ``worker``) takes the unit out of the shared payload and into the overlays of the
  profiles that keep it.
"""

from __future__ import annotations

import copy
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from tests.compiler.test_native_tree import FIXTURE, mkosi_summary, normalised, recipe_with
from tests.helpers import ATTESTED_NODE, EXAMPLE_RECIPES, REPO_ROOT
from tundravm.compiler import PAYLOAD, PHASE_DIRECTORIES
from tundravm.compiler.emit_mkosi import PHASE_TO_MKOSI_KEY
from tundravm.declarative import Recipe, Tree, compile, lower, mkosi_argv
from tundravm.recipe import load_file

CHAIN = REPO_ROOT / "tests" / "fixtures" / "profile-chain" / "chain.py"
IMAGES = [*EXAMPLE_RECIPES, ATTESTED_NODE / "image.py", FIXTURE / "fixture.py", CHAIN]
OVERLAYS = "mkosi.profiles/"


def _shared(tree: Tree) -> dict[str, Any]:
    """Every entry of *tree* outside a profile overlay."""
    return {e.path: e for e in tree.entries if OVERLAYS not in e.path}


def _overlay(tree: Tree, name: str) -> dict[str, Any]:
    prefixes = (f"{PAYLOAD}/{OVERLAYS}{name}/", f"{OVERLAYS}{name}/")
    return {e.path: e for e in tree.entries if e.path.startswith(prefixes)}


@pytest.mark.parametrize("path", IMAGES, ids=lambda p: Path(p).parent.name + "/" + Path(p).stem)
def test_the_shared_payload_is_the_same_whatever_the_selection(path: Path) -> None:
    recipe = load_file(path).recipe
    names = [variant.name for variant in recipe.variants]
    whole = compile(recipe)
    for name in names:
        alone = compile(recipe, variants=[name])
        assert _shared(alone) == _shared(whole), f"{name} alone"
        assert _overlay(alone, name) == _overlay(whole, name), f"{name}'s overlay"
        others = [other for other in names if other != name]
        if others:
            assert _shared(compile(recipe, variants=others)) == _shared(whole), f"without {name}"


# ── as mkosi resolves it ────────────────────────────────────────────────


def _parse_conf(path: Path) -> dict[str, list[str]]:
    """``key: [values]`` of a tree ``mkosi.conf`` (one value per line or per indented line)."""
    found: dict[str, list[str]] = {}
    key = ""
    if not path.is_file():
        return found
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(" ") and key:
            found[key].append(line.strip())
        elif "=" in line:
            key, _, value = line.partition("=")
            found.setdefault(key, []).extend([value] if value else [])
    return found


def _expected(shared: Mapping[str, Any], tree: Path, name: str, default: str) -> dict[str, Any]:
    """The payload image *shared* resolves to, plus *name*'s overlay in *tree*."""
    found = copy.deepcopy(dict(shared))
    overlay = tree / PAYLOAD / "mkosi.profiles" / name
    where = f"<tree>/{PAYLOAD}/mkosi.profiles/{name}"
    for key, directory in (("ExtraTrees", "mkosi.extra"), ("SkeletonTrees", "mkosi.skeleton")):
        if (overlay / directory).is_dir():
            found[key] = [*found[key], {"Source": f"{where}/{directory}", "Target": None}]
    if (tree / "mkosi.profiles" / name / "mkosi.sandbox").is_dir():
        source = f"<tree>/mkosi.profiles/{name}/mkosi.sandbox"
        found["SandboxTrees"] = [*found["SandboxTrees"], {"Source": source, "Target": None}]
    for phase, directory in PHASE_DIRECTORIES.items():
        if (overlay / directory).is_dir():
            key = PHASE_TO_MKOSI_KEY[phase]
            scripts = sorted(
                f"{where}/{directory}/{p.name}" for p in (overlay / directory).iterdir()
            )
            found[key] = [*found[key], *scripts]
    conf = _parse_conf(overlay / "mkosi.conf")
    for key in ("Packages", "BuildPackages"):
        found[key] = sorted({*found[key], *conf.get(key, [])})
    found["KernelModules"] = [*found["KernelModules"], *conf.get("KernelModules", [])]
    # a container's root image: its subimage comes before the default initrd mkosi adds
    found["Dependencies"] = [*conf.get("Dependencies", []), *found["Dependencies"]]
    for value in conf.get("ExtraTrees", []):
        source, _, target = value.partition(":")
        output = source.replace("%O", "<tree>/mkosi.output")
        found["ExtraTrees"] = [*found["ExtraTrees"], {"Source": output, "Target": target}]
    if name != default:
        found["ImageId"] = f"{shared['ImageId']}-{name}"
        found["Profiles"] = [name]
    return found


def _payload(summary: Mapping[str, Any]) -> dict[str, Any]:
    (image,) = [image for image in summary["Images"] if image["Image"] == PAYLOAD]
    found: dict[str, Any] = image
    return found


def _shared_summary(tree: Path, scratch: Path, default: str) -> dict[str, Any]:
    """The payload image of *tree* without the default profile's overlay: the shared one."""
    copied = scratch / "shared"
    shutil.copytree(tree, copied, symlinks=True)
    for overlay in (copied / PAYLOAD / "mkosi.profiles" / default, copied / "mkosi.profiles"):
        if overlay.is_dir() and overlay.name == default:
            shutil.rmtree(overlay)
    for conf in (copied / PAYLOAD / "mkosi.conf.d", copied / "mkosi.conf.d"):
        for include in conf.glob(f"*-{default}.conf"):
            include.unlink()
    return _payload(normalised(mkosi_summary(copied), copied))


def _resolved(tree: Path, recipe: Recipe, scratch: Path) -> dict[str, dict[str, Any]]:
    """Per content profile of *recipe*, the payload image ``summary --json`` resolves for
    the selection :func:`mkosi_argv` names, checked against the shared payload plus that
    profile's overlay."""
    lowered = lower(recipe)
    default = lowered.default_profile
    shared = _shared_summary(tree, scratch, default)
    found: dict[str, dict[str, Any]] = {}
    for name in (default, *lowered.overlays):
        argv = mkosi_argv(lowered, name)
        profiles = [arg for arg in argv if arg.startswith("--profile")]
        assert profiles == ([] if name == default else [f"--profile={name}"])
        resolved = _payload(normalised(mkosi_summary(tree, *argv), tree))
        assert resolved == _expected(shared, tree, name, default), name
        found[name] = resolved
    return found


@pytest.mark.parametrize(
    ("tree", "recipe"),
    [
        (FIXTURE / "tree-initramfs", lambda: recipe_with("initramfs")),
        (FIXTURE / "tree-verity", lambda: recipe_with("verity")),
        (ATTESTED_NODE / "mkosi", lambda: load_file(ATTESTED_NODE / "image.py").recipe),
    ],
    ids=["fixture-initramfs", "fixture-verity", "attested-node"],
)
def test_each_profile_resolves_to_the_shared_payload_plus_its_overlay(
    tree: Path, recipe: Any, tmp_path: Path
) -> None:
    resolved = _resolved(tree, recipe(), tmp_path)
    assert "dev" in resolved


def _unit_sources(tree: Path, image: Mapping[str, Any], unit: str) -> list[str]:
    """The extra trees of *image* that ship *unit*."""
    root = str(tree.resolve())
    found = []
    for entry in image["ExtraTrees"]:
        source = Path(entry["Source"].replace("<tree>", root))
        if (source / "usr/lib/systemd/system" / unit).is_file():
            found.append(entry["Source"])
    return found


def _enables(tree: Path, image: Mapping[str, Any], unit: str) -> bool:
    root = str(tree.resolve())
    return any(
        f"systemctl enable {unit}" in Path(s.replace("<tree>", root)).read_text(encoding="utf-8")
        for s in image["PostInstallationScripts"]
    )


def test_a_chain_with_an_edit_and_a_removal_resolves_for_every_profile(tmp_path: Path) -> None:
    recipe = load_file(CHAIN).recipe
    tree = tmp_path / "tree"
    compile(recipe).write(tree)
    resolved = _resolved(tree, recipe, tmp_path)
    assert sorted(resolved) == ["bench", "production", "staging"]
    shared = tree / PAYLOAD
    # worker: bench removes it, so the shared payload carries neither its unit nor its
    # enablement, and the two profiles that keep it carry both in their overlays
    assert not (shared / "mkosi.extra/usr/lib/systemd/system/worker.service").exists()
    assert "worker" not in (shared / "mkosi.postinst.d/50-postinst.sh").read_text(encoding="utf-8")
    for name in ("production", "staging"):
        overlay = f"<tree>/{PAYLOAD}/mkosi.profiles/{name}/mkosi.extra"
        assert _unit_sources(tree, resolved[name], "worker.service") == [overlay], name
        assert _enables(tree, resolved[name], "worker.service"), name
    assert _unit_sources(tree, resolved["bench"], "worker.service") == []
    assert not _enables(tree, resolved["bench"], "worker.service")
    # web: every profile ships it; staging's edit (inherited by bench) overrides the shared
    # unit from the overlay, and the shared payload enables it once for all
    for name in ("production", "staging", "bench"):
        assert _enables(tree, resolved[name], "web.service")
        sources = _unit_sources(tree, resolved[name], "web.service")
        assert sources[0] == f"<tree>/{PAYLOAD}/mkosi.extra", name
        last = Path(sources[-1].replace("<tree>", str(tree.resolve())))
        unit = (last / "usr/lib/systemd/system/web.service").read_text(encoding="utf-8")
        assert ("Environment=STAGE=staging" in unit) == (name != "production"), name
    # the motd staging edits: the last tree that ships it wins
    for name, text in (
        ("production", "production\n"),
        ("staging", "staging\n"),
        ("bench", "staging\n"),
    ):
        motds = [
            Path(e["Source"].replace("<tree>", str(tree.resolve()))) / "etc/motd"
            for e in resolved[name]["ExtraTrees"]
        ]
        assert [m for m in motds if m.is_file()][-1].read_text(encoding="utf-8") == text, name
    # the packages each adds
    assert "fio" in resolved["bench"]["Packages"]
    assert "strace" in resolved["bench"]["Packages"] and "strace" in resolved["staging"]["Packages"]
    assert not {"fio", "strace"} & set(resolved["production"]["Packages"])
