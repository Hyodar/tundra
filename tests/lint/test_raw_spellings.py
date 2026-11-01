"""``Run`` renders build commands; ``hook-covered`` names the typed spelling."""

import pytest

from tests.helpers import REPO_ROOT
from tundravm.declarative import (
    BootStep,
    Debian,
    Declaration,
    Fragment,
    Hook,
    Mkosi,
    Package,
    Recipe,
    Setting,
    Variant,
)
from tundravm.declarative.lifecycle import check_report
from tundravm.declarative.lower import lower
from tundravm.declarative.utils import DevTools, Run
from tundravm.diagnostics import Diagnostic
from tundravm.errors import ValidationError
from tundravm.templates import render_recipe_template

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
    return [d for d in check_report(recipe, None, variants=None) if d.code == "hook-covered"]


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


def test_settings_the_tree_writes_itself_are_refused() -> None:
    with pytest.raises(ValidationError, match="sets ToolsTree=, which tundravm writes itself"):
        lower(Recipe("r", items=(Setting("Build", "ToolsTree", ("default",)),)))


@pytest.mark.parametrize(
    ("item", "declaration"),
    [
        (Run("groupadd -r eth"), 'Group("eth")'),
        (Hook("u", "postinst", "mkosi-chroot useradd --system app"), 'User("app")'),
        (Run("usermod -a -G tdx,disk app"), 'User("app", groups=("tdx", "disk"))'),
        (Run("systemctl enable foo.service"), 'Service("foo", argv)'),
        (BootStep("s", "mkdir -p /var/lib/app && chown app /var/lib/app"), "RuntimeDirectory"),
    ],
)
def test_hook_covered_names_the_declaration(item: Declaration, declaration: str) -> None:
    [finding] = _findings(item)
    assert (finding.code, finding.severity) == ("hook-covered", "info")
    assert declaration in finding.message


def test_host_commands_and_shipped_fragments_report_nothing() -> None:
    assert (
        _findings(Hook("host", "postinst", "systemctl enable foo.service"), add=(DevTools(),)) == []
    )


def test_examples_and_templates_never_spell_the_chroot_prefix() -> None:
    sources = {path.name: path.read_text(encoding="utf-8") for path in EXAMPLES.rglob("*.py")}
    sources["new"] = render_recipe_template(title="node", filename="node.py", release="trixie")
    assert [name for name, text in sources.items() if "mkosi-chroot" in text] == []
