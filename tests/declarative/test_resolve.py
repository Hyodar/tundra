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
        items=(shared, Fragment("app", items=(shared, Package("curl")))),
    )
    resolved = resolve(recipe, variant="default")
    assert resolved.items == (Package("git", role="build"), Package("curl"))
    assert resolved.fragments == ("shared", "app")


def test_conflicting_fragments_with_one_name_fail() -> None:
    recipe = Recipe(
        "r",
        items=(Fragment("x", items=(Package("a"),)), Fragment("x", items=(Package("b"),))),
    )
    assert codes(lint(recipe)) == ["fragment-conflict"]
    with pytest.raises(ValidationError, match="fragment-conflict"):
        resolve(recipe, variant="default")


def test_fragment_requires_and_checks() -> None:
    def needs_curl(image: Resolved) -> tuple[Diagnostic, ...]:
        if Package("curl") in image.items:
            return ()
        return (Diagnostic("curl-missing", "warning", "needs curl", subject="tool"),)

    tool = Fragment("tool", items=(Package("jq"),), requires=("storage",), checks=(needs_curl,))
    recipe = Recipe(
        "r",
        items=(tool,),
        variants=(
            Variant("default"),
            Variant("full", add=Fragment("storage", items=(Package("curl"),))),
        ),
    )
    found = lint(recipe)
    assert {(d.code, d.variant, d.severity) for d in found} == {
        ("fragment-requires-missing", "default", "error"),
        ("curl-missing", "default", "warning"),
    }
    assert resolve(recipe, variant="full").fragments == ("tool", "storage")


# ── ancestry and overlays ────────────────────────────────────────────────


def test_ancestry_parents_resolve_first_and_targets_inherit() -> None:
    recipe = Recipe(
        "r",
        items=(Package("base-pkg"),),
        variants=(
            Variant("default", outputs=("qemu",), add=Fragment("d", items=(Package("d"),))),
            Variant("azure", inherits="default", outputs=("azure",)),
            Variant("azure-dev", inherits="azure", add=Fragment("dev", items=(Package("gdb"),))),
            Variant("rescue", inherits=None, add=Fragment("rescue", items=(Package("busybox"),))),
        ),
    )
    dev = resolve(recipe, variant="azure-dev")
    assert names(dev) == ["base-pkg", "d", "gdb"]
    assert dev.outputs == ("azure",)
    rescue = resolve(recipe, variant="rescue")
    assert names(rescue) == ["busybox"] and rescue.outputs == ("qemu",)
    assert [r.variant for r in resolve_all(recipe)] == ["default", "azure", "azure-dev", "rescue"]


def test_unknown_inherits_and_cycles_are_malformed() -> None:
    unknown = Recipe("r", variants=(Variant("a", inherits="ghost"),))
    with pytest.raises(ValidationError, match="inherits unknown variant"):
        lint(unknown)
    cycle = Recipe("r", variants=(Variant("a", inherits="b"), Variant("b", inherits="a")))
    with pytest.raises(ValidationError, match="cycle"):
        resolve(cycle, variant="a")
    with pytest.raises(ValidationError, match="Unknown variant"):
        resolve(cycle, variant="nope")


def test_changes_edit_and_remove_by_identity() -> None:
    motd = File("/etc/motd", "hi\n")
    recipe = Recipe(
        "r",
        items=(Package("curl"), motd, Unit("ssh", enabled=False)),
        variants=(
            Variant(
                "default",
                changes=(
                    motd.ref.edit(content="changed\n", mode=0o600),
                    Package.ref("curl").remove(),
                    Unit.ref("ssh").remove(),  # identity: ssh.service
                ),
            ),
        ),
    )
    resolved = resolve(recipe, variant="default")
    assert resolved.items == (File("/etc/motd", "changed\n", mode=0o600),)


def test_changes_need_an_inherited_identity_and_valid_values() -> None:
    recipe = Recipe(
        "r",
        items=(Package("curl"), Key("k")),
        variants=(
            Variant(
                "default",
                changes=(
                    Unit.ref("jq").edit(enabled=True),
                    Package.ref("curl", role="build").remove(),
                    Key.ref("k").edit(size=0),
                ),
            ),
        ),
    )
    found = lint(recipe)
    assert codes(found) == ["edit-missing", "remove-missing", "edit-invalid"]
    assert found[0].message == (
        "variant 'default' edits Unit(jq.service), which it does not inherit"
    )
    assert "size 0 must be positive" in found[2].message


def test_edits_reject_unknown_identity_and_repeated_fields() -> None:
    key = Key("k")
    with pytest.raises(ValidationError, match="Key has no field 'bits'"):
        key.ref.edit(bits=32)
    with pytest.raises(ValidationError, match="'name' identifies Key\\(k\\)"):
        key.ref.edit(name="other")
    with pytest.raises(ValidationError, match="sets no field"):
        key.ref.edit()
    with pytest.raises(ValidationError, match="edits Key\\(k\\).size twice"):
        Variant("v", changes=(key.ref.edit(size=32), key.ref.edit(size=16)))
    with pytest.raises(ValidationError, match="changes Key\\(k\\) twice"):
        Variant("v", changes=(key.ref.edit(size=32), key.ref.remove()))
    with pytest.raises(ValidationError, match="not an edit or a removal"):
        Variant("v", changes=(key,))  # type: ignore[arg-type]
    Variant("v", changes=(key.ref.edit(size=32), key.ref.edit(output="/run/k")))


def test_refs_from_values_and_types_agree() -> None:
    assert Unit("ssh", enabled=False).ref == Unit.ref("ssh.service")
    assert File("/etc//motd", "x", stage="skeleton").ref == File.ref("/etc/motd", stage="skeleton")
    assert str(Package.ref("jq")) == "Package(jq, runtime)"
    assert identity(Key("k")) == ("Key", "k")
    with pytest.raises(ValidationError, match="unexpected or repeated argument 'mode'"):
        File.ref("/etc/motd", mode=0o600)
    with pytest.raises(ValidationError, match=r"Key.ref\(\) needs name"):
        Key.ref()


def test_identity_collisions_fail_and_equal_repeats_dedupe() -> None:
    recipe = Recipe(
        "r",
        items=(
            Package("git", role="build"),
            Package("git", role="build"),
            Package("git"),  # another role: another identity
            File("/etc/motd", "a\n"),
            File("/etc//motd", "b\n"),
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
    assert disk.key == key.ref == Disk("d", "/data", key=Key.ref("k")).key
    recipe = Recipe(
        "r",
        items=(disk, Secrets(store=Disk("other", "/other"))),
    )
    found = lint(recipe)
    assert codes(found) == ["disk-key-undefined", "secret-store-undefined"]
    assert found[0].message == "Disk(d) references Key(k), which variant 'default' does not declare"
    # A ref names the identity: another Key("k") value is the one the disk uses.
    other = Recipe("r", items=(Key("k", output="/run/other"), disk, Secrets(store=disk)))
    resolved = resolve(other, variant="default")
    assert resolved.bind(disk.key) == Key("k", output="/run/other")  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="does not declare Disk\\(nope\\)"):
        resolved.bind(Disk.ref("nope"))
    with pytest.raises(ValidationError, match="key must be a Key, a Key ref"):
        Disk("d", "/data", key=Unit.ref("x.service"))


def test_editing_an_inherited_key_needs_no_rebuilt_disk() -> None:
    key = Key("k", output="/run/k")
    disk = Disk("d", "/data", key=key)
    recipe = Recipe(
        "r",
        items=(key, disk, Secrets(store=disk)),
        variants=(
            Variant("default"),
            Variant("small-key", inherits="default", changes=(key.ref.edit(size=32),)),
        ),
    )
    small = resolve(recipe, variant="small-key")
    assert small.bind(key.ref) == Key("k", output="/run/k", size=32)
    assert disk in small.items


def test_init_references_and_ordering() -> None:
    recipe = Recipe(
        "r",
        items=(
            Init("a", "echo a\n", priority=10, after=("disks", "ghost")),
            Init("b", "echo b\n", priority=5, after=("a",)),
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
        items=(
            Hook("h", "build", "make", after=("ghost",)),
            Unit("app.service", "[Unit]\n", enabled=True, after_init=True),
        ),
        variants=(
            Variant("default", outputs=("azure",)),
            Variant("local", inherits="default", outputs=("qemu",)),
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
        items=(Service("app", "/bin/app"),),
        variants=(
            Variant("default"),
            Variant("v", changes=(Service.ref("app").edit(exec_start="/bin/app --verbose"),)),
        ),
    )
    (service,) = resolve(recipe, variant="v").items
    assert isinstance(service, Service) and service.exec_start == "/bin/app --verbose"


def test_chained_variants_remove_and_edit_any_kind() -> None:
    from tundravm.declarative import Key, Service

    key = Key("k")
    recipe = Recipe(
        "r",
        items=(Package("curl"), key, Service("app", "/bin/app")),
        variants=(
            Variant("default"),
            Variant("cloud", outputs=("azure",), add=Fragment("a", items=(Package("waagent"),))),
            Variant(
                "slim",
                inherits="cloud",
                changes=(
                    Package.ref("curl").remove(),
                    key.ref.remove(),
                    Service.ref("app").edit(exec_start="/bin/app --slim"),
                ),
            ),
        ),
    )
    slim = resolve(recipe, variant="slim")
    assert slim.outputs == ("azure",)
    names = [getattr(item, "name", None) for item in slim.items]
    assert "curl" not in names and "k" not in names and "waagent" in names


def test_several_targets_inherit_and_stay_consistent() -> None:
    recipe = Recipe(
        "r",
        variants=(
            Variant("default"),
            Variant("cloud", outputs=("azure", "gcp")),
            Variant("child", inherits="cloud"),
            Variant("narrow", inherits="cloud", outputs=("qemu",)),
        ),
    )
    assert resolve(recipe, variant="child").outputs == ("azure", "gcp")
    codes = {(d.variant, d.code) for d in lint(recipe)}
    assert ("narrow", "target-inconsistent") in codes
