"""Every variant builds the payload's sources: one pin per build, whatever the variants.

A variant is a profile overlay on the payload, so it cannot build another source of a
build the payload declares; the per-variant ``<variant>/<name>`` pins of the per-variant
trees are gone with them.
"""

from __future__ import annotations

import pytest

from tests.helpers import source_dir
from tundravm.declarative import (
    Build,
    Fragment,
    Git,
    Install,
    Package,
    Recipe,
    Variant,
    compile,
    lock,
    lock_status,
    lower,
)
from tundravm.errors import ValidationError

pytestmark = pytest.mark.usefixtures("isolated_cwd")

URL = "https://git.example/app"
PINS = {"stable": "a" * 40, "dev": "b" * 40}
STABLE = Build("app", Git(URL, "stable"), script="make", install=(Install("app", "/usr/bin/app"),))


def _recipe(*variants: Variant) -> Recipe:
    return Recipe(
        "pins",
        items=(Package("linux-image-amd64"), STABLE),
        variants=(Variant("default", outputs=("qemu",)), *variants),
    )


def _resolve(source: object) -> str:
    return PINS[source.ref]  # type: ignore[attr-defined]


def test_a_variant_cannot_build_another_source_of_a_payload_build() -> None:
    other = Variant("dev", changes=(STABLE.ref.edit(source=Git(URL, "dev")),))
    with pytest.raises(ValidationError, match=r"'dev' \(it replaces source build 'app'\)"):
        lower(_recipe(other))


def test_every_variant_shares_the_payloads_pin() -> None:
    recipe = _recipe(Variant("prod"), Variant("azure", outputs=("azure",)))
    locked = lock(recipe, resolver=_resolve)
    assert [(f.name, f.ref, f.digest) for f in locked.lockfile.fetches] == [
        ("app", "stable", PINS["stable"])
    ]
    for variants in (None, ["prod"], ["azure"]):
        assert lock_status(recipe, locked, variants=variants) == ()
    tree = compile(recipe, lock=locked)
    hooks = [e for e in tree.entries if e.path.endswith("-build.sh")]
    assert [e.path for e in hooks] == ["payload/mkosi.build.d/50-build.sh"]
    assert source_dir("app", PINS["stable"], URL) in (hooks[0].content or b"").decode()


def test_a_variant_adding_a_build_pins_it_under_its_own_name() -> None:
    tool = Build(
        "tool", Git(URL, "dev"), script="make", install=(Install("tool", "/usr/bin/tool"),)
    )
    dev = Variant("dev", add=Fragment("dev-tools", (tool,)))
    recipe = _recipe(dev)
    locked = lock(recipe, resolver=_resolve)
    assert [(f.name, f.ref) for f in locked.lockfile.fetches] == [
        ("app", "stable"),
        ("tool", "dev"),
    ]
    tree = compile(recipe, lock=locked)
    overlay = "payload/mkosi.profiles/dev/mkosi.build.d/50-build.sh"
    (hook,) = (e for e in tree.entries if e.path == overlay)
    assert source_dir("tool", PINS["dev"], URL) in (hook.content or b"").decode()
    assert lock_status(recipe, locked, variants=["default"]) == ()
