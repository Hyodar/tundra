"""Resolution: fragment expansion, variant ancestry, overlays and reference checks."""

from __future__ import annotations

import pytest

from tundravm.declarative import (
    Diagnostic,
    Disk,
    File,
    Fragment,
    Hook,
    Init,
    Key,
    Package,
    Recipe,
    Resolved,
    Secrets,
    Unit,
    Variant,
    identity,
    lint,
    resolve,
    resolve_all,
)
from tundravm.declarative.resolve import order_hooks, order_inits
from tundravm.errors import ValidationError


def codes(diagnostics: tuple[Diagnostic, ...]) -> list[str]:
    return [d.code for d in diagnostics]


def names(resolved: Resolved) -> list[str]:
    return [getattr(item, "name", type(item).__name__) for item in resolved.items]


# ── fragments ────────────────────────────────────────────────────────────


def test_identical_named_fragments_expand_once() -> None:
    shared = Fragment("shared", items=(Package("git", role="build"),))
    recipe = Recipe(
        "r",
        Fragment("common", items=(shared, Fragment("app", items=(shared, Package("curl"))))),
    )
    resolved = resolve(recipe, variant="default")
    assert resolved.items == (Package("git", role="build"), Package("curl"))
    assert resolved.fragments == ("common", "shared", "app")


def test_conflicting_fragments_with_one_name_fail() -> None:
    recipe = Recipe(
        "r",
        Fragment(
            "common",
            items=(Fragment("x", items=(Package("a"),)), Fragment("x", items=(Package("b"),))),
        ),
    )
    assert codes(lint(recipe)) == ["fragment-conflict"]
    with pytest.raises(ValidationError, match="fragment-conflict"):
        resolve(recipe, variant="default")


def test_fragment_requires_and_checks() -> None:
    def needs_curl(image: Resolved) -> tuple[Diagnostic, ...]:
        if Package("curl") in image.items:
            return ()
        return (Diagnostic("curl-missing", "needs curl", level="warning", subject="tool"),)

    tool = Fragment("tool", items=(Package("jq"),), requires=("storage",), checks=(needs_curl,))
    recipe = Recipe(
        "r",
        Fragment("common", items=(tool,)),
        variants=(
            Variant("default"),
            Variant("full", add=Fragment("storage", items=(Package("curl"),))),
        ),
    )
    found = lint(recipe)
    assert {(d.code, d.variant, d.level) for d in found} == {
        ("fragment-requires-missing", "default", "error"),
        ("curl-missing", "default", "warning"),
    }
    assert resolve(recipe, variant="full").fragments == ("common", "tool", "storage")


# ── ancestry and overlays ────────────────────────────────────────────────


def test_ancestry_parents_resolve_first_and_targets_inherit() -> None:
    recipe = Recipe(
        "r",
        Fragment("common", items=(Package("base-pkg"),)),
        variants=(
            Variant("default", target="qemu", add=Fragment("d", items=(Package("d"),))),
            Variant("azure", parent="default", target="azure"),
            Variant("azure-dev", parent="azure", add=Fragment("dev", items=(Package("gdb"),))),
            Variant("rescue", parent=None, add=Fragment("rescue", items=(Package("busybox"),))),
        ),
    )
    dev = resolve(recipe, variant="azure-dev")
    assert names(dev) == ["base-pkg", "d", "gdb"]
    assert dev.target == "azure"
    rescue = resolve(recipe, variant="rescue")
    assert names(rescue) == ["busybox"] and rescue.target == "qemu"
    assert [r.variant for r in resolve_all(recipe)] == ["default", "azure", "azure-dev", "rescue"]


def test_unknown_parent_and_cycles_are_malformed() -> None:
    unknown = Recipe("r", Fragment("c"), variants=(Variant("a", parent="ghost"),))
    with pytest.raises(ValidationError, match="unknown parent"):
        lint(unknown)
    cycle = Recipe(
        "r", Fragment("c"), variants=(Variant("a", parent="b"), Variant("b", parent="a"))
    )
    with pytest.raises(ValidationError, match="cycle"):
        resolve(cycle, variant="a")
    with pytest.raises(ValidationError, match="Unknown variant"):
        resolve(cycle, variant="nope")


def test_replace_and_remove_match_identity_not_object() -> None:
    recipe = Recipe(
        "r",
        Fragment(
            "common",
            items=(Package("curl"), File("/etc/motd", "hi\n"), Unit("ssh", enabled=False)),
        ),
        variants=(
            Variant(
                "default",
                replace=(File("/etc/motd", "changed\n", mode=0o600),),
                remove=(Package("curl"), Unit("ssh.service", masked=True)),
            ),
        ),
    )
    resolved = resolve(recipe, variant="default")
    assert resolved.items == (File("/etc/motd", "changed\n", mode=0o600),)


def test_replace_and_remove_need_an_inherited_identity() -> None:
    recipe = Recipe(
        "r",
        Fragment("common", items=(Package("curl"),)),
        variants=(
            Variant("default", replace=(Package("jq"),), remove=(Package("curl", role="build"),)),
        ),
    )
    assert codes(lint(recipe)) == ["replace-missing", "remove-missing"]


def test_identity_collisions_fail_and_equal_repeats_dedupe() -> None:
    recipe = Recipe(
        "r",
        Fragment(
            "common",
            items=(
                Package("git", role="build"),
                Package("git", role="build"),
                Package("git"),  # another role: another identity
                File("/etc/motd", "a\n"),
                File("/etc//motd", "b\n"),
            ),
        ),
        variants=(
            Variant("default"),
            Variant("other", add=Fragment("o", items=(File("/etc/motd", "c\n"),))),
        ),
    )
    found = lint(recipe)
    # The common-level collision is reported once, for no particular variant.
    assert [(d.code, d.variant) for d in found] == [
        ("identity-collision", ""),
        ("identity-collision", "other"),
    ]
    assert identity(File("/etc//motd", "x")) == ("File", "extra", "/etc/motd")


# ── reference checks ─────────────────────────────────────────────────────


def test_disk_and_secret_store_references() -> None:
    key = Key("k", output="/run/k")
    disk = Disk("d", "/data", key=key)
    recipe = Recipe(
        "r",
        Fragment("common", items=(disk, Secrets(store=Disk("other", "/other")))),
    )
    assert codes(lint(recipe)) == ["disk-key-undefined", "secret-store-undefined"]
    mismatched = Recipe(
        "r",
        Fragment("common", items=(Key("k", output="/run/other"), disk, Secrets(store=disk))),
    )
    assert codes(lint(mismatched)) == ["disk-key-mismatch"]


def test_init_references_and_ordering() -> None:
    recipe = Recipe(
        "r",
        Fragment(
            "common",
            items=(
                Init("a", "echo a\n", priority=10, after=("disks", "ghost")),
                Init("b", "echo b\n", priority=5, after=("a",)),
            ),
        ),
    )
    found = lint(recipe)
    assert codes(found) == ["init-after-undefined", "init-after-undefined", "init-order"]
    assert "runs later" in found[2].message


def test_order_inits_by_priority_then_after() -> None:
    late = Init("late", "echo late\n", priority=30, after=("secrets",))
    second = Init("second", "echo 2\n", priority=20, after=("first",))
    first = Init("first", "echo 1\n", priority=20, after=("disks",))
    ordered = order_inits((late, second, first), builtins={"disks": 20, "secrets": 30})
    assert [i.name for i in ordered] == ["first", "second", "late"]
    with pytest.raises(ValidationError, match="cycle"):
        order_inits((Init("x", "x\n", after=("y",)), Init("y", "y\n", after=("x",))), builtins={})


def test_order_hooks_within_phase_keeps_slots() -> None:
    hooks = (
        Hook("b", "postinst", "echo b", after=("a",)),
        Hook("sync", "sync", "echo sync"),
        Hook("a", "postinst", "echo a"),
        Hook("c", "postinst", "echo c", after=("sync",)),
    )
    assert [h.name for h in order_hooks(hooks)] == ["a", "sync", "b", "c"]
    with pytest.raises(ValidationError, match="later"):
        order_hooks((Hook("early", "sync", "x", after=("late",)), Hook("late", "build", "y")))


def test_hook_unit_and_target_checks() -> None:
    recipe = Recipe(
        "r",
        Fragment(
            "common",
            items=(
                Hook("h", "build", "make", after=("ghost",)),
                Unit("app.service", "[Unit]\n", enabled=True, after_init=True),
            ),
        ),
        variants=(
            Variant("default", target="azure"),
            Variant("local", parent="default", target="qemu"),
        ),
    )
    found = lint(recipe, variants=("local",))
    assert codes(found) == [
        "target-inconsistent",
        "hook-after-undefined",
        "unit-after-init-without-init",
    ]
    assert {d.variant for d in found} == {"local"}


def test_service_and_template_identities() -> None:
    from tundravm.declarative import Service, Template, identity

    assert identity(Service("app", "/bin/app")) == identity(Service("app.service", "/bin/x"))
    assert identity(Template("/etc/a", "x")) == ("Template", "extra", "/etc/a")
    recipe = Recipe(
        "r",
        Fragment("c", items=(Service("app", "/bin/app"),)),
        variants=(
            Variant("default"),
            Variant("v", replace=(Service("app", "/bin/app --verbose"),)),
        ),
    )
    (service,) = resolve(recipe, variant="v").items
    assert isinstance(service, Service) and service.exec_start == "/bin/app --verbose"


def test_chained_variants_remove_and_replace_any_kind() -> None:
    from tundravm.declarative import Key, Service

    key = Key("k")
    recipe = Recipe(
        "r",
        Fragment("c", items=(Package("curl"), key, Service("app", "/bin/app"))),
        variants=(
            Variant("default"),
            Variant("cloud", target="azure", add=Fragment("a", items=(Package("waagent"),))),
            Variant(
                "slim",
                parent="cloud",
                remove=(Package("curl"), key),
                replace=(Service("app", "/bin/app --slim"),),
            ),
        ),
    )
    slim = resolve(recipe, variant="slim")
    assert slim.targets == ("azure",)
    names = [getattr(item, "name", None) for item in slim.items]
    assert "curl" not in names and "k" not in names and "waagent" in names


def test_several_targets_inherit_and_stay_consistent() -> None:
    recipe = Recipe(
        "r",
        Fragment("c"),
        variants=(
            Variant("default"),
            Variant("cloud", targets=("azure", "gcp")),
            Variant("child", parent="cloud"),
            Variant("narrow", parent="cloud", target="qemu"),
        ),
    )
    assert resolve(recipe, variant="child").targets == ("azure", "gcp")
    codes = {(d.variant, d.code) for d in lint(recipe)}
    assert ("narrow", "target-inconsistent") in codes
