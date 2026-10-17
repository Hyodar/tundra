"""``Run`` renders build commands; ``setting-raw`` and ``hook-covered`` name the typed spelling."""

from pathlib import Path

import pytest

from tests.helpers import REPO_ROOT
from tundravm.declarative import (
    BootStep,
    Debian,
    Declaration,
    Fragment,
    Git,
    Hook,
    Kernel,
    Mkosi,
    Package,
    Recipe,
    Setting,
    Variant,
)
from tundravm.declarative.lifecycle import check_report
from tundravm.declarative.utils import DevTools, Run
from tundravm.diagnostics import Diagnostic
from tundravm.errors import ValidationError
from tundravm.templates import TEMPLATES, render_recipe_template

EXAMPLES = REPO_ROOT / "examples"

DEFAULT_MKOSI = Mkosi()


def _findings(
    *items: Declaration | Fragment,
    add: tuple[Declaration | Fragment, ...] = (),
    mkosi: Mkosi = DEFAULT_MKOSI,
) -> list[Diagnostic]:
    variants = (
        Variant("default", outputs=("qemu",)),
        Variant("dev", add=Fragment("dev", items=add)),
    )
    recipe = Recipe(
        "raw",
        items=(Package("jq"), *items),
        platform=Debian("trixie"),
        variants=variants,
        mkosi=mkosi,
    )
    return [
        d
        for d in check_report(recipe, None, variants=None)
        if d.code in ("setting-raw", "hook-covered")
    ]


@pytest.mark.parametrize(
    ("command", "script"),
    [
        ("update-ca-certificates --fresh", "mkosi-chroot update-ca-certificates --fresh"),
        (["install", "-d", "/var/lib/my app"], "mkosi-chroot install -d '/var/lib/my app'"),
        ("a && b > /x", "mkosi-chroot bash -euo pipefail -c 'a && b > /x'"),
        ("set -x\nls | wc -l", "mkosi-chroot bash -euo pipefail -c 'set -x\nls | wc -l'"),
    ],
)
def test_run_renders_a_command_inside_the_image(command: str | list[str], script: str) -> None:
    hook = Run(command)
    assert (hook.phase, hook.script) == ("postinst", script)


def test_run_names_stages_and_users() -> None:
    hook = Run("whoami", stage="finalize", user="app", after=("other",))
    assert (hook.name, hook.phase, hook.after) == ("run-whoami", "finalize", ("other",))
    assert hook.script == "mkosi-chroot runuser -u app -- whoami"
    assert Run(["/usr/bin/tool", "x"], name="tool").name == "tool"


@pytest.mark.parametrize(
    "bad", [{"stage": "sync"}, {"user": "Not A User"}, {"command": ""}, {"command": []}]
)
def test_run_rejects_what_it_cannot_run(bad: dict[str, object]) -> None:
    fields = {"command": "true", **bad}
    with pytest.raises(ValidationError):
        Run(**fields)  # type: ignore[arg-type]


def test_setting_raw_names_the_typed_field() -> None:
    [common, dev] = _findings(
        Setting("Output", "Seed", ("630b5f72-a36a-4e83-b23d-6ef47c82fd9c",)),
        Setting("Build", "ToolsTree", ("default",)),
        add=(Setting("Content", "CleanPackageMetadata", ("no",)),),
    )
    assert (common.code, common.severity, common.variant, common.subject) == (
        "setting-raw",
        "info",
        "",
        "Output.Seed",
    )
    assert "Mkosi(seed=...)" in common.message and common.location
    assert (dev.variant, dev.subject) == ("dev", "Content.CleanPackageMetadata")
    assert "Debian(clean_package_metadata=...)" in dev.message and "recipe-wide" in (dev.hint or "")


def test_setting_raw_reports_a_redundant_kernel_passthrough() -> None:
    source = Git("https://github.com/gregkh/linux", "v6.13.12")
    kernel = Kernel("6.13.12", source, config=Path(__file__))
    [finding] = _findings(
        kernel, Setting("Build", "Environment", ("KERNEL_IMAGE", "KERNEL_VERSION"))
    )
    assert "Kernel(config=...)" in finding.message


@pytest.mark.parametrize(
    ("item", "declaration"),
    [
        (Run("groupadd -r eth"), 'Group("eth")'),
        (Hook("u", "postinst", "mkosi-chroot useradd --system app"), 'User("app")'),
        (Run("usermod -a -G tdx,disk app"), 'User("app", groups=("tdx", "disk"))'),
        (Run("systemctl enable foo.service"), 'Unit("foo.service", enabled=True)'),
        (Run("systemctl mask foo.service"), 'Unit("foo.service", masked=True)'),
        (BootStep("s", "mkdir -p /var/lib/app && chown app /var/lib/app"), "RuntimeDirectory"),
    ],
)
def test_hook_covered_names_the_declaration(item: Declaration, declaration: str) -> None:
    [finding] = _findings(item)
    assert (finding.code, finding.severity) == ("hook-covered", "info")
    assert declaration in finding.message


def test_host_commands_shipped_fragments_and_nethermind_v1_report_nothing() -> None:
    assert (
        _findings(Hook("host", "postinst", "systemctl enable foo.service"), add=(DevTools(),)) == []
    )
    nethermind = Mkosi(dialect="nethermind-v1")
    assert (
        _findings(Run("groupadd -r eth"), Setting("Output", "Seed", ("x",)), mkosi=nethermind) == []
    )


def test_examples_and_templates_never_spell_the_chroot_prefix() -> None:
    sources = {path.name: path.read_text(encoding="utf-8") for path in EXAMPLES.rglob("*.py")}
    for template in TEMPLATES:
        sources[f"init-{template}"] = render_recipe_template(
            title=template,
            filename="node.py",
            release="trixie",
            backend="inprocess",
            template=template,
        )
    assert [name for name, text in sources.items() if "mkosi-chroot" in text] == []
