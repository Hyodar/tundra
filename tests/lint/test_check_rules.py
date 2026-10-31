"""Tests for the recipe linter (``tundravm.lint`` / ``tundravm check``).

The compiler rules in ``tundravm.check`` run on the lowered recipe: ``report``
is the CLI's lint report (resolution diagnostics, then the compiler rules);
``check(lower(recipe))`` runs the rules alone.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from tundravm.check import Diagnostic, check, render, render_github, render_markdown
from tundravm.cli import main
from tundravm.declarative import (
    Debian,
    Debloat,
    Declaration,
    ExternalDelivery,
    File,
    Fragment,
    Git,
    Hook,
    Kernel,
    Package,
    Recipe,
    Secret,
    SecretEnv,
    SecretFile,
    Secrets,
    Service,
    Setting,
    Template,
    Unit,
    User,
    Variant,
    compile,
    lint,
    lower,
)
from tundravm.declarative.lifecycle import check_report
from tundravm.declarative.utils import EfiStub
from tundravm.errors import ValidationError

DEFAULT = Variant("default", outputs=("qemu",))
KERNEL = Package("linux-image-amd64")
NOT_BOOTABLE = Setting("Content", "Bootable", ("no",))
"""Quiets kernel-missing in the ``clean=False`` recipes, which test package-less variants."""
CLEAN: tuple[Declaration, ...] = (
    KERNEL,
    Package("curl"),
    User("app", system=True),
    Service("app", "/usr/bin/app", user="app"),
    File("/etc/motd", "hi\n"),
)


def recipe(
    *items: Declaration, variants: tuple[Variant, ...] = (DEFAULT,), clean: bool = True
) -> Recipe:
    common = (*CLEAN, *items) if clean else (NOT_BOOTABLE, *items)
    return Recipe("check", items=common, variants=variants)


def report(subject: Recipe, *variants: str) -> list[Diagnostic]:
    return check_report(subject, None, variants=variants or None)


def codes(subject: Recipe, *variants: str) -> list[str]:
    return [d.code for d in report(subject, *variants)]


# a. service-user-missing


def test_service_user_missing() -> None:
    [diag] = report(recipe(Service("worker", "/usr/bin/worker", user="svc")))
    assert (diag.severity, diag.code, diag.subject) == ("error", "service-user-missing", "worker")
    assert "'svc'" in (diag.hint or "")


def test_service_user_inherited_from_default() -> None:
    dev = Variant(
        "dev", add=Fragment("dev", items=(Service("worker", "/usr/bin/app", user="app"),))
    )
    assert report(recipe(variants=(DEFAULT, dev)), "dev") == []


def test_a_standalone_variant_is_checked_as_its_own_image() -> None:
    """A standalone variant is lowered whole: its service's user is missing from it even
    though the default variant declares one."""
    items = (KERNEL, Package("curl"), Service("app", "/usr/bin/app", user="app"))
    dev = Variant("dev", inherits=None, add=Fragment("dev", items=items))
    subject = recipe(variants=(DEFAULT, dev))
    assert lower(subject).state.ensure_profile("dev").extends is None
    assert "service-user-missing" in codes(subject, "dev")


def test_service_user_root_base_or_hook_created_is_fine() -> None:
    subject = recipe(
        Service("a", "/usr/bin/a", user="root"),
        Service("b", "/usr/bin/b", user="nobody"),
        Hook("hookuser", "postinst", "mkosi-chroot useradd --system hookuser"),
        Service("c", "/usr/bin/c", user="hookuser"),
    )
    # User("hookuser") says it without the hook; root is fine but reported as info.
    assert codes(subject) == ["hook-covered", "service-runs-as-root"]


# b. file-path-duplicate


def test_file_path_duplicate_with_different_content() -> None:
    [diag] = report(recipe(Template("/etc/motd", "bye {x}\n", variables={"x": 1})))
    assert (diag.severity, diag.code, diag.subject) == ("error", "file-path-duplicate", "/etc/motd")
    assert "file, template" in diag.message


def test_identical_redeclaration_and_skeleton_override_are_fine() -> None:
    subject = recipe(
        File("/etc/motd", "hi\n"),
        Template("/etc/motd", "hi\n"),
        File("/etc/motd", "build-time\n", stage="skeleton"),
    )
    assert report(subject) == []


# c. file-path-relative


def test_file_path_relative() -> None:
    with pytest.raises(ValidationError, match="must be an absolute path"):
        File("etc/relative", "x")
    subject = recipe(File("/etc/../../escape", "x", stage="skeleton"), File("/opt/../../up", "y"))
    diags = report(subject)
    assert [(d.code, d.subject) for d in diags] == [
        ("file-path-relative", "/etc/../../escape"),
        ("file-path-relative", "/opt/../../up"),
    ]
    assert all(d.severity == "error" for d in diags)


# d. service-command-not-shipped


def test_service_command_not_shipped() -> None:
    [diag] = report(recipe(Service("tool", "/usr/local/bin/tool --serve"), clean=False))
    assert (diag.severity, diag.code, diag.subject) == (
        "warning",
        "service-command-not-shipped",
        "tool",
    )
    assert "no packages" in (diag.hint or "")


def test_service_command_shipped_by_file_hook_package_or_essential() -> None:
    shipped = (
        File("/usr/local/bin/tool", "#!/bin/sh\n", mode=0o755),
        Service("tool", "/usr/local/bin/tool"),
        Hook("builder", "build", "cp out/builder $DESTDIR/opt/builder"),
        Service("builder", "/opt/builder"),
        Service("shell", "/usr/bin/bash -c true"),
    )
    assert report(recipe(*shipped, clean=False)) == []
    packaged = recipe(*shipped, Service("other", "/usr/bin/other"), Package("other"), clean=False)
    assert report(packaged) == []


# e. cloud targets
#
# Lowering applies a cloud target's platform module to the variant that sets it,
# and a variant cannot change a cloud target it inherits, so a platform without
# its target (or a target without its platform) cannot be declared.


def cloud_recipe() -> Recipe:
    variants = (DEFAULT, Variant("azure", outputs=("azure",)), Variant("gcp", outputs=("gcp",)))
    return recipe(variants=variants)


def test_cloud_variants_lint_clean() -> None:
    agent = Variant("agent", outputs=("azure",), add=Fragment("agent", items=(Package("waagent"),)))
    both = Variant("both", outputs=("azure", "gcp"))
    subject = recipe(variants=(*cloud_recipe().variants, agent, both))
    assert report(subject, "azure", "gcp", "agent", "both") == []


# f. variant-empty


def test_variant_empty() -> None:
    [diag] = report(recipe(variants=(DEFAULT, Variant("bare"))), "bare")
    assert (diag.severity, diag.code, diag.variant) == ("info", "variant-empty", "bare")


def test_variant_with_content_or_default_is_not_empty() -> None:
    dev = Variant("dev", add=Fragment("dev", items=(Package("dropbear"),)))
    assert codes(recipe(variants=(DEFAULT, dev), clean=False), "default", "dev") == []


# g. runtime-init graph: cycles and unknown steps are errors naming the steps


# i. debloat-removes-needed-unit / debloat-removes-declared-file

NEEDS_NETWORKD = Service("net", "/usr/bin/net", requires=("systemd-networkd.service",))


def test_debloat_masks_needed_units() -> None:
    resolved = Service("systemd-resolved", "/usr/lib/systemd/systemd-resolved")
    diags = report(recipe(NEEDS_NETWORKD, resolved))
    assert [(d.code, d.subject) for d in diags] == [
        ("debloat-removes-needed-unit", "net"),
        ("debloat-removes-needed-unit", "systemd-resolved"),
    ]
    assert "keep_units" in (diags[0].hint or "")


def test_debloat_keeps_the_targets_an_enabled_service_pulls_in() -> None:
    """The current dialect's masking keeps the targets enabled units reference."""
    online = Service("web", "/usr/bin/web", wants=("network-online.target", "time-set.target"))
    assert report(recipe(online)) == []


@pytest.mark.parametrize(
    "items",
    [
        (
            NEEDS_NETWORKD,
            Debloat(keep_units_extra=("systemd-networkd.service",), keep_modules=True),
        ),
        (NEEDS_NETWORKD, Debloat(enabled=False)),
        (Debloat(keep_modules=True),),
        (Debloat(keep_modules=False),),
        (Debloat(keep_paths=("/usr/lib/modules",)),),
        (Debloat(enabled=False),),
        # The implicit default (no Debloat declared) keeps the modules mkosi selected.
        (),
    ],
    ids=[
        "keep-units-extra",
        "disabled-with-needed-unit",
        "keep-modules",
        "modules-knowingly-removed",
        "keep-paths-modules",
        "disabled",
        "implicit-default",
    ],
)
def test_debloat_settings_that_report_nothing(items: tuple[Declaration, ...]) -> None:
    assert report(recipe(*items)) == []


def test_debloat_removes_declared_file() -> None:
    network = File("/etc/systemd/network/10-eth.network", "[Match]\n")
    [diag] = report(recipe(network, Debloat(keep_modules=True)))
    assert (diag.code, diag.subject) == (
        "debloat-removes-declared-file",
        "/etc/systemd/network/10-eth.network",
    )
    kept = Debloat(keep_paths=("/etc/systemd/network",), keep_modules=True)
    assert report(recipe(network, kept)) == []


def test_implicit_debloat_keeps_declared_files_and_enabled_systemd_units() -> None:
    network = File("/etc/systemd/network/10-eth.network", "[Match]\n")
    networkd = Unit("systemd-networkd.service", enabled=True)
    assert report(recipe(network, networkd)) == []
    tree = {e.path: (e.content or b"").decode() for e in compile(recipe(network, networkd)).entries}
    finalize = next(text for path, text in tree.items() if path.endswith("finalize.sh"))
    postinst = next(text for path, text in tree.items() if path.endswith("postinst.sh"))
    assert '"$BUILDROOT/etc/systemd/network"' not in finalize
    assert '"$BUILDROOT/usr/lib/systemd/network"' in finalize
    assert '"systemd-networkd.service"' in postinst.split("systemd_svc_whitelist=")[1]


def test_declared_debloat_removing_modules_of_a_modular_kernel_is_an_error() -> None:
    [diag] = report(recipe(Debloat()))
    assert (diag.code, diag.severity, diag.subject) == (
        "debloat-removes-modules",
        "error",
        "/usr/lib/modules",
    )
    assert "keep_modules=True" in (diag.hint or "")
    # A parent path or a glob removes them too.
    custom = Debloat(remove=("/usr/lib/mod*",))
    assert codes(recipe(custom)) == ["debloat-removes-modules"]


def test_keep_modules_keeps_the_path_out_of_finalize() -> None:
    from tundravm.declarative import lower

    kept = lower(recipe(Debloat(keep_modules=True))).state.effective_profile("default")
    assert "/usr/lib/modules" not in kept.debloat.effective_paths_remove
    removed = lower(recipe(Debloat(keep_modules=False))).state.effective_profile("default")
    assert "/usr/lib/modules" in removed.debloat.effective_paths_remove


# j. secrets
#
# A Secret needs a delivery target, and the target needs a provider that writes it:
# the built-in tundra-tools one provisions only the SSH key (secret-target-unsupported).


@pytest.mark.parametrize(
    "secrets",
    [
        Secrets(),
        Secrets(
            entries=(Secret("token", (SecretFile("/run/token"),)),),
            delivery=ExternalDelivery("vault-agent", supports=("file",), unit="vault.service"),
        ),
    ],
    ids=["ssh-key-only", "external"],
)
def test_secret_delivery_applied_is_fine(secrets: Secrets) -> None:
    # no Disk store: store=None, otherwise secret-store-undefined
    # The delivery tool builds from an unpinned branch; that is source-unpinned's concern.
    # The recipe brings up no interface for the SSH key POST: network-missing's concern.
    ignored = ("source-unpinned", "network-missing")
    assert [d for d in report(recipe(secrets)) if d.code not in ignored] == []


def test_secret_target_unsupported_by_the_builtin_provider() -> None:
    token = Secret("token", (SecretFile("/run/token"), SecretEnv("TOKEN")))
    found = [
        d
        for d in report(recipe(Secrets(entries=(token,))))
        if d.code not in ("source-unpinned", "network-missing")
    ]
    assert [(d.severity, d.code, d.subject) for d in found] == [
        ("error", "secret-target-unsupported", "token"),
        ("error", "secret-target-unsupported", "token"),
    ]
    [file] = [d for d in found if "file /run/token" in d.message]
    assert "provisions only the SSH key" in file.message
    assert "ExternalDelivery('NAME', supports=('file',))" in (file.hint or "")
    assert "reach the VM with the SSH key" in (file.hint or "")


def test_secret_target_unsupported_by_an_external_provider() -> None:
    token = Secret("token", (SecretFile("/run/token"), SecretEnv("TOKEN", "app")))
    secrets = Secrets(entries=(token,), delivery=ExternalDelivery("vault-agent", ("file",)))
    [found] = [d for d in report(recipe(secrets)) if d.code == "secret-target-unsupported"]
    assert "environment variable TOKEN" in found.message
    assert found.hint == (
        "Add 'env' to ExternalDelivery('vault-agent', supports=) if it writes the target, "
        "or remove the target from the Secret."
    )


# k. kernel-missing


def test_kernel_missing() -> None:
    subject = Recipe("check", items=CLEAN[1:], variants=(DEFAULT,))
    [diag] = report(subject)
    assert (diag.severity, diag.code, diag.variant) == ("error", "kernel-missing", "default")
    assert "A kernel must be installed" in diag.message
    for fix in ("linux-image-amd64", "Kernel(...)", '"Bootable", ("no",)'):
        assert fix in (diag.hint or "")


@pytest.mark.parametrize(
    "boot",
    [
        Package("linux-image-amd64"),
        Package("linux-image-cloud-amd64"),
        Package("linux-image-6.12.48+deb13-amd64"),
        Package("linux-image-arm64/trixie-backports"),
        Package("linux-generic"),
        Kernel("6.12.1", Git("https://example.com/linux", "a" * 40)),
        NOT_BOOTABLE,
    ],
    ids=lambda item: getattr(item, "name", None) or type(item).__name__,
)
def test_kernel_package_kernel_or_not_bootable_is_fine(boot: Declaration) -> None:
    subject = Recipe("check", items=(*CLEAN[1:], boot), variants=(DEFAULT,))
    assert "kernel-missing" not in codes(subject)


def test_kernel_missing_is_per_variant() -> None:
    booted = Variant("booted", add=Fragment("booted", items=(KERNEL,)))
    subject = Recipe("check", items=CLEAN[1:], variants=(DEFAULT, booted))
    assert [(d.variant, d.code) for d in report(subject, "default", "booted")] == [
        ("default", "kernel-missing")
    ]


# l. recipe-arch-unsupported


def _arm(*items: Declaration | Fragment, target: str = "azure") -> Recipe:
    variant = Variant("default", outputs=(target,))  # type: ignore[arg-type]
    common = Fragment("common", items=(*CLEAN, *items))
    return Recipe(
        "check",
        items=(common,),
        variants=(variant,),
        platform=Debian("trixie", arch="aarch64", boot=None),
    )


def test_aarch64_with_efi_stub_is_unsupported() -> None:
    [diag] = [
        d
        for d in report(_arm(EfiStub(snapshot="20251113T083151Z", version="257")))
        if d.code == "recipe-arch-unsupported" and d.subject == "EfiStub"
    ]
    assert (diag.severity, diag.subject) == ("error", "EfiStub")
    assert "_amd64.deb" in (diag.hint or "")


def test_x86_64_is_fine_and_aarch64_always_builds_the_qemu_disk() -> None:
    efi = EfiStub(snapshot="20251113T083151Z", version="257")
    x86 = Recipe("check", items=(*CLEAN, efi), variants=(DEFAULT,))
    assert "recipe-arch-unsupported" not in codes(x86)
    # The tree's main image is the QEMU disk, whatever the variant's outputs.
    found = [d.subject for d in report(_arm()) if d.code == "recipe-arch-unsupported"]
    assert found == ["qemu"]


# rendering + ordering


def test_ordering_is_deterministic() -> None:
    variants = (
        DEFAULT,
        Variant("b", add=Fragment("b", items=(File("/etc/../../escape", "x"),))),
        Variant("a"),
    )
    subject = recipe(
        Service("unshipped", "/usr/bin/unshipped"),
        variants=variants,
        clean=False,
    )
    img = lower(subject)
    diags = check(img, profiles=["b", "a", "default"])
    keys = [(d.variant, d.severity, d.code) for d in diags]
    assert keys == [
        ("a", "warning", "service-command-not-shipped"),
        ("a", "info", "variant-empty"),
        ("b", "error", "file-path-relative"),
        ("b", "warning", "service-command-not-shipped"),
        ("default", "warning", "service-command-not-shipped"),
    ]
    assert diags == check(img, profiles=["default", "a", "b"])
    assert [(d.variant, d.code) for d in lint(subject)] == [
        (profile, code) for profile, _, code in keys
    ]


def test_render_text_and_dict() -> None:
    assert render([]) == "no findings"
    diag = Diagnostic(
        "service-user-missing",
        "error",
        "boom",
        subject="app",
        variant="default",
        hint="fix it",
        context=(("user", "app"), ("group", "app")),
    )
    info = Diagnostic("variant-empty", "info", "m", variant="x")
    whole = Diagnostic("lock-stale", "warning", "w")
    text = render([diag, info, whole])
    assert text.splitlines() == [
        "error service-user-missing [default] app: boom",
        "    hint: fix it",
        "info variant-empty [x]: m",
        "warning lock-stale [common]: w",
        "1 error, 1 warning, 1 info",
    ]
    assert diag.context == (("group", "app"), ("user", "app"))
    assert diag.to_dict() == {
        "code": "service-user-missing",
        "severity": "error",
        "message": "boom",
        "subject": "app",
        "variant": "default",
        "location": "",
        "hint": "fix it",
        "context": {"group": "app", "user": "app"},
    }


def test_a_source_location_is_rendered_where_it_is_known() -> None:
    placed = Diagnostic("app-port", "warning", "port 80", variant="default", location="node.py:27")
    plain = Diagnostic("lock-stale", "warning", "w")
    assert render([placed]).splitlines()[:2] == [
        "warning app-port [default]: port 80",
        "    at: node.py:27",
    ]
    table = render_markdown([placed, plain])
    assert table.startswith("| Severity | Code | Profile | Location | Subject | Message | Hint |")
    assert "| `default` | `node.py:27` |" in table
    assert "| Profile | Subject |" in render_markdown([plain])
    lines = render_github([placed, plain], "recipe.py").splitlines()
    assert lines[0].startswith("::warning file=node.py,line=27,title=app-port::[default]")
    assert lines[1].startswith("::warning file=recipe.py,title=lock-stale::[common]")


# CLI

RECIPE = """
from tundravm.declarative import (
    Fragment,
    Package,
    RUNTIME_INIT,
    Recipe,
    Service,
    Unit,
    User,
    Variant,
)
from tundravm.backends.local_linux import LocalLinuxBackend

backend = LocalLinuxBackend()
items = [
    Package("linux-image-amd64"),
    Package("curl"),
    User("app", system=True),
    Service("app", "/usr/bin/app", user="app"),
]
variants = [Variant("default", outputs=("qemu",))]
"""

BIND = """
recipe = Recipe("cli", items=tuple(items), variants=tuple(variants))
"""


def write_recipe(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "recipe.py"
    path.write_text(f"{RECIPE}{extra}{BIND}", encoding="utf-8")
    return path


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    return main(["check", *argv], stdout=out), out.getvalue()


def test_cli_variant_selection(tmp_path: Path) -> None:
    recipe_path = write_recipe(tmp_path, 'variants.append(Variant("bare"))\n')
    code, out = run(str(recipe_path), "--profile", "default", "--profile", "bare")
    assert code == 0
    assert "info variant-empty [bare]" in out
    assert run(str(recipe_path), "--profile", "default")[1] == "no findings\n"


# bake pre-flight
