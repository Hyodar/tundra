"""Tests for the recipe linter (``tundravm.lint`` / ``tundravm check``).

The compiler rules in ``tundravm.check`` run on the lowered recipe: ``report``
is the CLI's lint report (resolution diagnostics, then the compiler rules);
``check(lower(recipe))`` runs the rules alone.
"""

from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path

import pytest

from tundravm.check import Diagnostic, check, render, render_github
from tundravm.cli import main
from tundravm.declarative import (
    BootStep,
    Debloat,
    Declaration,
    ExternalDelivery,
    File,
    Fragment,
    Hook,
    Package,
    Recipe,
    Secret,
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

DEFAULT = Variant("default", outputs=("qemu",))
KERNEL = Package("linux-image-amd64")
NOT_BOOTABLE = Setting("Content", "Bootable", ("no",))
"""Quiets kernel-missing in the ``clean=False`` recipes, which test package-less variants."""
CLEAN: tuple[Declaration, ...] = (
    KERNEL,
    Package("curl"),
    User("app", system=True),
    Service("app", "/usr/bin/app", user="app"),
    File("/usr/bin/app", "#!/bin/sh\n", mode=0o755),
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
    [diag] = report(recipe(Service("worker", "/usr/bin/app", user="svc")))
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
        Service("a", "/usr/bin/app", user="root"),
        Service("b", "/usr/bin/app", user="nobody"),
        Hook("hookuser", "postinst", "mkosi-chroot useradd --system hookuser"),
        Service("c", "/usr/bin/app", user="hookuser"),
    )
    # User("hookuser") says it without the hook; root is fine but reported as info.
    assert codes(subject) == ["hook-covered", "service-runs-as-root"]


# b. file-path-duplicate


def test_file_path_duplicate_with_different_content() -> None:
    [diag] = report(recipe(Template("/etc/motd", "bye {x}\n", variables={"x": 1})))
    assert (diag.severity, diag.code, diag.subject) == ("error", "file-path-duplicate", "/etc/motd")
    assert diag.message.startswith("shipped 2 times with different content")


def test_identical_redeclaration_and_skeleton_override_are_fine() -> None:
    subject = recipe(
        File("/etc/motd", "hi\n"),
        Template("/etc/motd", "hi\n"),
        File("/etc/motd", "build-time\n", stage="skeleton"),
    )
    assert report(subject) == []


# c. file-path-relative


# d. service-program-missing


@pytest.mark.parametrize(
    ("items", "missing"),
    [
        ((Service("tool", "/usr/local/bin/tool --serve"),), "/usr/local/bin/tool"),
        ((Service("web", "/usr/bin/python3 app.py"), Package("curl")), "/usr/bin/python3"),
        ((Service("web", "node app.js"),), "/usr/bin/node"),
        (
            (
                File("/usr/local/bin/tool", "#!/bin/sh\n", mode=0o755),
                Service("tool", "/usr/local/bin/tool"),
                Hook("builder", "build", "cp out/builder $DESTDIR/opt/builder"),
                Service("builder", "/opt/builder"),
                Service("shell", "/usr/bin/bash -c true"),
                Service("perl", "/usr/bin/perl -e 1"),
                Service("py", "/usr/bin/python3 app.py"),
                Package("python3"),
                Service("keys", "/usr/bin/ssh-keygen -A"),
                Package("openssh-server"),
                Service("other", "/usr/sbin/other"),
                Package("other"),
                Service("resolved", "-/usr/lib/systemd/systemd-resolved"),
                Package("systemd"),
                Service("env", "${APP}"),
            ),
            None,
        ),
    ],
    ids=["unshipped", "interpreter", "bare-name", "shipped"],
)
def test_service_program_missing(items: tuple[Declaration, ...], missing: str | None) -> None:
    found = [d for d in report(recipe(*items, clean=False)) if d.code == "service-program-missing"]
    assert [d.message.split(" runs ")[1].split(",")[0] for d in found] == (
        [missing] if missing else []
    )
    assert all(d.severity == "error" for d in found)
    if missing == "/usr/bin/python3":
        assert "packages=['python3']" in (found[0].hint or "")


@pytest.mark.parametrize(
    ("item", "written"),
    [
        (Service("render", "/usr/bin/cp /run/app.service /etc/systemd/system/"), True),
        (Service("render", "/bin/sh -c 'envsubst < /run/t > /run/systemd/system/a.service'"), True),
        (BootStep("render", "cat /run/x >/etc/systemd/system/a.service.d/v.conf\n"), True),
        (File("/etc/tmpfiles.d/a.conf", "L+ /etc/systemd/system/a.service - - - - /run/a\n"), True),
        (Service("render", "/usr/bin/cat /etc/systemd/system/a.service"), False),
        (BootStep("render", "echo ok > /run/app.env\n"), False),
    ],
    ids=["cp", "sh-redirect", "boot-step", "tmpfiles", "read", "elsewhere"],
)
def test_unit_written_at_runtime(item: Declaration, written: bool) -> None:
    found = [d for d in report(recipe(item)) if d.code == "unit-written-at-runtime"]
    assert bool(found) is written
    assert all(d.severity == "error" for d in found)


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


# f. profile-empty


def test_profile_empty() -> None:
    [diag] = report(recipe(variants=(DEFAULT, Variant("bare"))), "bare")
    assert (diag.severity, diag.code, diag.profile) == ("info", "profile-empty", "bare")


def test_variant_with_content_or_default_is_not_empty() -> None:
    dev = Variant("dev", add=Fragment("dev", items=(Package("dropbear"),)))
    assert codes(recipe(variants=(DEFAULT, dev), clean=False), "default", "dev") == []


# g. runtime-init graph: cycles and unknown steps are errors naming the steps


# i. debloat-removes-needed-unit / debloat-removes-declared-file

NEEDS_NETWORKD = Service("net", "/usr/bin/app", requires=("systemd-networkd.service",))


def test_debloat_masks_needed_units() -> None:
    resolved = Service("systemd-resolved", "/usr/lib/systemd/systemd-resolved")
    tmpfiles = File.text("/etc/tmpfiles.d/app.conf", "d /var/lib/app 0750 app app -\n")
    diags = report(recipe(NEEDS_NETWORKD, resolved, tmpfiles))
    assert [(d.code, d.subject) for d in diags] == [
        ("debloat-removes-needed-unit", "/etc/tmpfiles.d/app.conf"),
        ("debloat-removes-needed-unit", "net"),
        ("debloat-removes-needed-unit", "systemd-resolved"),
    ]
    assert "report 'systemd-networkd.service'" in (diags[1].hint or "")


def test_debloat_keeps_the_targets_an_enabled_service_pulls_in() -> None:
    """The current dialect's masking keeps the targets enabled units reference."""
    online = Service("web", "/usr/bin/app", wants=("network-online.target", "time-set.target"))
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


def test_keep_modules_keeps_the_path_out_of_finalize() -> None:
    from tundravm.declarative import lower

    kept = lower(recipe(Debloat(keep_modules=True))).state.effective_profile("default")
    assert "/usr/lib/modules" not in kept.debloat.effective_paths_remove
    removed = lower(recipe(Debloat(keep_modules=False))).state.effective_profile("default")
    assert "/usr/lib/modules" in removed.debloat.effective_paths_remove


# j. secrets
#
# A Secret needs a delivery target, and the target needs a provider that writes it:
# the built-in secret-delivery provisions only the SSH key (secret-target-unsupported).


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
    # The delivery tool builds from an unpinned branch; that is source-unpinned's concern.
    assert [d for d in report(recipe(secrets)) if d.code != "source-unpinned"] == []


# k. kernel-missing


# l. image-arch-unsupported


# rendering + ordering


def test_ordering_is_deterministic() -> None:
    variants = (
        DEFAULT,
        Variant("b", add=Fragment("b", items=(Service("ghost", "/usr/bin/g", user="ghost"),))),
        Variant("a"),
    )
    subject = recipe(
        Service("unshipped", "/usr/bin/unshipped"),
        variants=variants,
        clean=False,
    )
    img = lower(subject)
    diags = check(img, profiles=["b", "a", "default"])
    keys = [(d.profile, d.severity, d.code) for d in diags]
    assert keys == [
        ("a", "error", "service-program-missing"),
        ("a", "info", "profile-empty"),
        ("b", "error", "service-program-missing"),
        ("b", "error", "service-program-missing"),
        ("b", "error", "service-user-missing"),
        ("default", "error", "service-program-missing"),
    ]
    assert diags == check(img, profiles=["default", "a", "b"])
    assert [(d.profile, d.code) for d in lint(subject)] == [
        (profile, code) for profile, _, code in keys
    ]


def test_render_text_and_dict() -> None:
    assert render([]) == "no findings"
    diag = Diagnostic(
        "service-user-missing",
        "error",
        "boom",
        subject="app",
        profile="default",
        hint="fix it",
        context=(("user", "app"), ("group", "app")),
    )
    info = Diagnostic("profile-empty", "info", "m", profile="x")
    whole = Diagnostic("lock-stale", "warning", "w")
    text = render([diag, info, whole])
    assert text.splitlines() == [
        "error service-user-missing [default] app: boom",
        "    hint: fix it",
        "info profile-empty [x]: m",
        "warning lock-stale [image]: w",
        "1 error, 1 warning, 1 info",
    ]
    assert diag.context == (("group", "app"), ("user", "app"))
    assert diag.to_dict() == {
        "code": "service-user-missing",
        "severity": "error",
        "message": "boom",
        "subject": "app",
        "profile": "default",
        "location": "",
        "hint": "fix it",
        "context": {"group": "app", "user": "app"},
    }


def test_render_joins_profiles_and_states_a_shared_hint_once() -> None:
    def unpinned(name: str, profile: str) -> Diagnostic:
        return Diagnostic("source-unpinned", "warning", name, name, profile, hint="lock")

    tools = [replace(unpinned(n, ""), context=(("source", "tundravm:tools"),)) for n in "tu"]
    found = [unpinned("a", "dev"), unpinned("b", "dev"), unpinned("a", "prod"), *tools]
    found[1:1] = [replace(unpinned("g", "dev"), hint="pin it")]
    found.append(Diagnostic("profile-empty", "info", "m", profile="prod", hint="drop it"))
    assert render(found).splitlines() == [
        "warning source-unpinned [dev, prod] a: a",
        "warning source-unpinned [dev] b: b",
        "    hint (all 2 above): lock",
        "warning source-unpinned [dev] g: g",
        "    hint: pin it",
        "warning source-unpinned [image] tundravm's tools (2): not in the lockfile yet: "
        "`tundravm lock` pins them",
        "info profile-empty [prod]: m",
        "    hint: drop it",
        "0 errors, 6 warnings, 1 info (5 lines)",
    ]
    assert len(render_github(found, "image.py").splitlines()) == len(found) + 1


def test_a_source_location_is_rendered_where_it_is_known() -> None:
    placed = Diagnostic("app-port", "warning", "port 80", profile="default", location="node.py:27")
    plain = Diagnostic("lock-stale", "warning", "w")
    assert render([placed]).splitlines()[:2] == [
        "warning app-port [default]: port 80",
        "    at: node.py:27",
    ]
    lines = render_github([placed, plain], "recipe.py").splitlines()
    assert lines[0].startswith("::warning file=node.py,line=27,title=app-port::[default]")
    assert lines[1].startswith("::warning file=recipe.py,title=lock-stale::[image]")


# CLI

IMAGE = """
from tundravm import File, Image, Profile, Service
from tundravm.backends.local_linux import LocalLinuxBackend

backend = LocalLinuxBackend()
profiles = {}
image = Image(
    "cli",
    packages=["curl"],
    files=[File.text("/usr/bin/app", "#!/bin/sh\\n", mode=0o755)],
    services=[Service("app", ["/usr/bin/app"])],
    profiles=profiles,
)
"""


def write_image(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "image.py"
    path.write_text(IMAGE.replace("profiles = {}\n", f"profiles = {{{extra}}}\n"), encoding="utf-8")
    return path


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    return main(["check", *argv], stdout=out), out.getvalue()


def test_cli_profile_selection(tmp_path: Path) -> None:
    image_path = write_image(tmp_path, '"bare": Profile()')
    code, out = run(str(image_path), "--profile", "production", "--profile", "bare")
    assert code == 0
    assert "info profile-empty [bare]" in out
    assert "profile-empty" not in run(str(image_path), "--profile", "production")[1]


# bake pre-flight
