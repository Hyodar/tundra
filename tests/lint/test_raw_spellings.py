"""``Run`` renders build commands; ``setting-raw`` and ``hook-covered`` name the typed spelling."""

import pytest

from tests.helpers import REPO_ROOT
from tundravm._image._values import RESERVED_SETTINGS
from tundravm.check import TYPED_SETTINGS, declaration_check
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


def test_setting_raw_says_what_the_public_mkosi_refuses() -> None:
    [clean, seed] = _findings(
        Setting("Output", "Seed", ("630b5f72-a36a-4e83-b23d-6ef47c82fd9c",)),
        Setting("Content", "CleanPackageMetadata", ("no",)),
    )
    assert (seed.code, seed.severity, seed.profile, seed.subject) == (
        "setting-raw",
        "info",
        "",
        "Output.Seed",
    )
    assert "sets what tundravm writes itself" in seed.message and seed.location
    assert seed.hint == RESERVED_SETTINGS["Seed"]
    assert clean.subject == "Content.CleanPackageMetadata"
    assert clean.hint == RESERVED_SETTINGS["CleanPackageMetadata"]


@pytest.mark.parametrize(("section", "key"), sorted(TYPED_SETTINGS))
def test_every_typed_setting_hint_is_the_reserved_keys_sentence(section: str, key: str) -> None:
    assert TYPED_SETTINGS[(section, key)] == RESERVED_SETTINGS[key]


def test_settings_the_tree_writes_itself_are_refused() -> None:
    with pytest.raises(ValidationError, match="ToolsTree= itself"):
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


@pytest.mark.parametrize(
    ("setting", "code"),
    [
        (Setting("Config", "ConfigureScripts", ("configure.sh",)), "mkosi-configure-script"),
        (Setting("Build", "Environment", ("HOME",)), "mkosi-environment-passthrough"),
        (Setting("Config", "PassEnvironment", ("TERM",)), "mkosi-environment-passthrough"),
    ],
)
def test_settings_that_lie_to_summary_or_leak_the_host_are_errors(
    setting: Setting, code: str
) -> None:
    recipe = Recipe("r", items=(Package("jq"), setting), platform=Debian("trixie"))
    findings = [d for d in declaration_check(recipe) if d.code == code]
    assert [d.severity for d in findings] == ["error"]
    with pytest.raises(ValidationError, match=code):
        lower(recipe)


def test_environment_with_values_passes() -> None:
    recipe = Recipe(
        "r", items=(Setting("Build", "Environment", ("A=1",)),), platform=Debian("trixie")
    )
    codes = {"mkosi-environment-passthrough", "mkosi-configure-script"}
    assert [d for d in declaration_check(recipe) if d.code in codes] == []
    assert lower(recipe).mkosi.environment == {"A": "1"}


def test_examples_and_templates_never_spell_the_chroot_prefix() -> None:
    sources = {path.name: path.read_text(encoding="utf-8") for path in EXAMPLES.rglob("*.py")}
    for template in TEMPLATES:
        sources[f"init-{template}"] = render_recipe_template(
            title=template,
            filename="node.py",
            release="trixie",
            template=template,
        )
    assert [name for name, text in sources.items() if "mkosi-chroot" in text] == []
