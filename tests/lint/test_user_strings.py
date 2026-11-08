"""User-facing strings must not cite the removed fluent API, CLI verbs or CLI flags."""

from __future__ import annotations

import ast
import importlib
import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

import tundravm
from tests.helpers import REPO_ROOT
from tundravm import File, Image, Package, Profile, Service, errors, show
from tundravm._image import recipe_of
from tundravm._reference import MODULES, lint_markdown
from tundravm.check import RULES
from tundravm.components import Component
from tundravm.sources import GoBinary, Local
from tundravm.systemd import Every, TcpProbe
from tundravm.testing import plan

PACKAGE = Path(tundravm.__file__).parent
EXAMPLES = REPO_ROOT / "examples"

NAMESPACED = sorted(
    (
        name
        for module in MODULES
        if module != "tundravm"
        for name in importlib.import_module(module).__all__
        if name[:1].isupper()
    ),
    key=len,
    reverse=True,
)
"""Public names whose one home is a namespace, so the root never spells them."""

REMOVED_VERB = "removed CLI verb (new, check, lock, build, show, run, deploy, attest, ...)"
"""The verbs folded into the thirteen commands (plan 3.4): no hint, template or doc names one."""

DENYLIST = {
    "fluent method call": re.compile(r"\bimg\.[a-z_]+\("),
    "internal Setting( (Mkosi(settings=))": re.compile(r"(?<![\w.])Setting\("),
    "fluent profile block": re.compile(r"\bwith img\b"),
    "frozen bake flag": re.compile(r"\bbake\([^)]*\bfrozen=True\b"),
    "internal source class": re.compile(r"\b(?:Http|Git|Local)Source\("),
    "the tundra-tools repository (tools/ of this repository)": re.compile(
        r"\btundra-tools\b|\bTUNDRA_TOOLS\b"
    ),
    "removed init hook": re.compile(r"\badd_init_script\b"),
    "module application": re.compile(r"\.apply\("),
    "internal module spec": re.compile(r"\b(?:Key|Disk|Secret)Spec\("),
    "internal secret target": re.compile(r"\bSecretTarget\."),
    "removed --out flag (--tree, --build, --output)": re.compile(r"--out\b"),
    "removed --against flag (--tree)": re.compile(r"--against\b"),
    "removed deploy --target flag (deploy azure|gcp)": re.compile(
        r"\btundravm deploy\b[^\n`]*--target\b"
    ),
    "removed MANIFEST positional (--build)": re.compile(r"\b(?:measure|deploy|sbom) MANIFEST\b"),
    "removed verify --artifacts flag (--build)": re.compile(r"\bverify\b[^\n]*--artifacts\b"),
    "removed clean part flags (--what)": re.compile(
        r"\bclean\b[^\n]*--(?:sources|tree|artifacts|state|all)\b"
    ),
    "elided measure flags (--build, --variant)": re.compile(r"\bmeasure \.\.\."),
    "removed init --base flag (--release)": re.compile(r"\binit\b[^\n]*--base\b"),
    "removed doc (docs/guide.md, docs/reference/)": re.compile(
        r"\bdocs/(?:api|cli|concepts|tutorial|testing|reproducibility|policy|module-authoring)\.md"
    ),
    REMOVED_VERB: re.compile(
        r"(?<!from )\btundravm (?:init|inspect|lint|compile|diff|fetch|bake|measure|sbom|evidence"
        r"|changes|release|witness|explore|preview|watch|status|clean|config|ci|completion|units"
        r"|import)\b"
    ),
    "RECIPE placeholder (IMAGE)": re.compile(r"\btundravm \w+ RECIPE\b"),
    "IMAGE placeholder in a lock hint (project.lock_command())": re.compile(
        r"`tundravm lock IMAGE\b"
    ),
    "bake for a build (build)": re.compile(
        r"\bbake\(\)|\b(?:the|an?|its|offline|frozen) bake\b|\b[Bb]ake (?:again|the)\b|\bbaked \d"
    ),
    "Policy knob an Image cannot set (build --offline, tundravm lock)": re.compile(
        r"\b(?:network_mode|mutable_ref_policy|require_frozen_lock)\b"
    ),
    "Recipe for the image file (image file, Image)": re.compile(
        r"\b(?:an?|the|your|no) Recipe\b|\bRecipe (?:files?|has|factory|factories|attribute|"
        r"defines|objects?)\b|\bbinds a Recipe\b|\brecipe\.py\b"
    ),
    "removed workload (Raiko) or example (surge; examples/attested-node)": re.compile(
        r"(?i)\braiko\b|\bsurge\b(?!-taiko-mono\b)"
    ),
    "variant for a profile (profile)": re.compile(
        r"\b[Uu]nknown variant|\bDeclared variants\b|\bin variant\b|\bvariant=(?:\{|\s*$)"
        r"|\bInherits: |\bvariants\.[a-z]"
    ),
    "login argument mini-language (Int(name, low, high), Choice(name, ...))": re.compile(
        r"\{[a-z][a-z0-9_]*:(?:int|choice)\b"
    ),
    "a login command as a plain list (Command(path, word, ...))": re.compile(
        r"\bcommands=\{\s*[\"'][a-z][a-z0-9-]*[\"']:\s*\["
    ),
}
"""Phrases a hint, message or template must not contain, by what they name."""

RENAMED = {
    "Recipe.common (Recipe.items)": re.compile(r"\b[Rr]ecipe\.common\b|\bcommon="),
    "Variant parent= (inherits=)": re.compile(r"\bparent="),
    "Build cache_key= (cache_namespace=)": re.compile(r"\bcache_key="),
    "Directory (FileTree)": re.compile(r"\bDirectory\("),
    "assert_clean strict= (warnings=)": re.compile(r"\bassert_clean\([^)]*\bstrict="),
    "Variant.replace/Variant.remove (Variant.changes)": re.compile(
        r"\bVariant\.(?:replace|remove)\b"
    ),
    "replace-missing/-mismatch lint codes (edit-missing; refs)": re.compile(
        r"\b(?:replace-missing|disk-key-mismatch|secret-store-mismatch)\b"
    ),
    "Recipe.base/arch/mirror/snapshot/epoch (Recipe.platform)": re.compile(
        r"\bRecipe\.(?:base|arch|mirror|tools_mirror|snapshot|epoch)\b"
    ),
    "BOOT package tuple (Debian(boot=...))": re.compile(r"\bBOOT\s*=\s*\(|\*BOOT\b"),
    "Init (BootStep)": re.compile(r"(?<![\w.])Init\("),
    "Tdxs simulator issuer (a development profile picks it at boot)": re.compile(
        r"\bissuer=[\"']simulator[\"']"
    ),
    "after_init (needs=)": re.compile(r"\bafter_init\b"),
    "BUILTIN_INITS priorities (BUILTIN_STEPS, readiness handles)": re.compile(
        r"\bBUILTIN_INITS\b|\binit-priority-collision\b"
    ),
    "flat import of a namespaced name (its namespace)": re.compile(
        rf"\bfrom tundravm import (?:\([^)]*|[^\n]*)\b(?:{'|'.join(NAMESPACED)})\b"
    ),
    "tundravm.<namespaced name> (its namespace)": re.compile(
        rf"\btundravm\.(?:{'|'.join(NAMESPACED)})\b"
    ),
    "tundravm.image (the public namespaces)": re.compile(r"\btundravm\.image\b"),
    "tundravm.bake (tundravm.build)": re.compile(
        r"\btundravm\.bake\b|\bfrom tundravm import (?:\([^)]*|[^\n]*)\bbake\b"
    ),
    "tundravm.components.<workload> (tundravm.components)": re.compile(
        r"\btundravm\.components\.(?:nethermind|taiko)\b"
    ),
    "tundravm.observability/platforms (internal: _reporting, _platforms)": re.compile(
        r"\btundravm\.(?:observability|platforms)\b"
    ),
    "variant-empty, resolved.variant (profile-empty, resolved.profile)": re.compile(
        r"\bvariant-empty\b|\bresolved\.variant\b"
    ),
    "resolved.outputs/fragments (resolved.targets/components)": re.compile(
        r"\bresolved\.(?:outputs|fragments)\b"
    ),
    "fragment-*/recipe-* codes (component-conflict, component-requires-missing, "
    "image-arch-unsupported)": re.compile(
        r"\b(?:fragment-conflict|fragment-requires-missing|recipe-arch-unsupported)\b"
    ),
    "internal-model test helpers (plan, image_file)": re.compile(
        r"\b(?:fragment_plan|fake_fragment|recipe_file|compile_tree|assert_tree_matches)\b"
    ),
    "Component fragment= (name=)": re.compile(r"\(Component, fragment="),
    "service-command-not-shipped (service-program-missing)": re.compile(
        r"\bservice-command-not-shipped\b"
    ),
    "Sudo (Command(..., sudo=True))": re.compile(r"(?<![\w.])Sudo\("),
    "firewall.status/switch (firewall.commands.status/switch)": re.compile(
        r"\b[Ff]irewall\.(?:status|switch)\b"
    ),
}
"""Removed recipe-authoring spellings, by their replacement."""
RENAMED_KEYWORDS = {
    "Variant": {
        "target": "outputs=",
        "targets": "outputs=",
        "parent": "inherits=",
        "replace": "changes=(value.ref.edit(...),)",
        "remove": "changes=(value.ref.remove(),)",
    },
    "Build": {"recipe": "using=", "cache_key": "cache_namespace="},
    "Image": {
        "volumes": "collected from Service(state=volume.directory(...))",
        "secrets": "collected from Service(credentials=[...])",
    },
    "BootStep": {"priority": "after=/before= readiness handles"},
    "Secret": {"rotation": "gone: the endpoint delivers each secret once per boot"},
    "fake_fragment": {"priority": "after="},
    **{
        helper: {"variants": "profiles="}
        for helper in ("assert_clean", "compile_tree", "assert_tree_matches", "compiled")
    },
    "assert_diagnostic": {"variants": "profiles=", "variant": "profile="},
    "fragment_plan": {"variant": "profile="},
    "Diagnostic": {"variant": "profile="},
    "Resolved": {"variant": "profile="},
    "Recipe": {
        "common": "items=",
        **{
            name: f"platform=Debian(..., {name}=...)"
            for name in ("arch", "mirror", "tools_mirror", "snapshot", "epoch")
        },
        "base": "platform=Debian(release)",
    },
}
"""Keyword arguments of these calls that no longer exist, with their replacement."""
INTERNAL_MODEL = {
    "Unit(": re.compile(r"(?<![\w.])Unit\("),
    "Fragment, Recipe": re.compile(r"\b(?:Fragment|Recipe)\b"),
    "SecretEnv, SecretFile, Secrets(), ExternalDelivery": re.compile(
        r"\bSecret(?:Env|File)\b|\bSecrets\(|\bExternalDelivery\b"
    ),
    "Debloat": re.compile(r"\bDebloat\b"),
    "needs=": re.compile(r"\bneeds="),
    "Key(, Disk(": re.compile(r"(?<![\w.])(?:Key|Disk)\("),
    "Setting": re.compile(r"\bSetting\b"),
    "Task, BootStep, Condition": re.compile(r"\bTask\b|\bBootStep\b|\bCondition\."),
    "Hook(, Mount(, Run(, Repository(, FileTree(, Partition(, Template(": re.compile(
        r"(?<![\w.])(?:Hook|Mount|Run|Repository|FileTree|Partition|Template)\("
    ),
    "Variant, Platform(, RuntimeInit, RuntimeTools, KernelConfig": re.compile(
        r"\bVariant\b|(?<![\w.])Platform\(|\bRuntimeInit\b|\bRuntimeTools\b|\bKernelConfig\b"
    ),
    "UnitReady, Ready(, Root(), Dynamic()": re.compile(
        r"\bUnitReady\b|(?<![\w.])Ready\(|\bRoot\(\)|\bDynamic\(\)"
    ),
    "Firewall(collect=, ssh=)": re.compile(r"\bcollect=(?!=)|\bFirewall\([^)]*\bssh="),
    "File(stage, path), Package(name, role)": re.compile(
        r"\bFile(?:Tree)?\(\w+, /|\bPackage\([\w.+-]+, \w+\)"
    ),
    "Build(name), a source build by its identity": re.compile(r"(?<![\w.])Build\(\w+\)"),
    "the lowering's tree or stage (the image, the end of the build)": re.compile(
        r"\bin mkosi\.(?:extra|skeleton)\b|\bat finalize\b"
    ),
    "an internal module path": re.compile(
        r"\btundravm\.(?:declarative|_(?!reference\b)\w+)\b|\b_(?:image|modules|units)\.[a-z_]+\b"
    ),
}
"""The internal model's names, which no lint fix or finding hint names: an image file cannot
write them (a fix it cannot express says tundravm sets it)."""
PUBLIC_RULES = sorted(RULES)
"""The codes an image file can hit: every rule of the registry."""
DOCS = [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").glob("*.md"))]
"""The current docs; ``docs/design`` keeps the dated design records as written."""
CLI_DOCS = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "guide.md",
    REPO_ROOT / "docs" / "trust.md",
    REPO_ROOT / "docs" / "reference" / "cli.md",
]
"""The docs that name commands: the README, the guide, the trust page, the CLI reference."""


def _runtime_strings(tree: ast.Module) -> Iterator[tuple[int, str]]:
    """``(line, text)`` of string constants that can reach output, minus bare string statements."""
    bare = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in bare:
            yield node.lineno, node.value


def _hits(text: str) -> list[str]:
    return [what for what, pattern in DENYLIST.items() if pattern.search(text)]


def _removed_keywords(text: str) -> Iterator[str]:
    """``Callee(kw=)`` for each removed keyword passed directly to a ``RENAMED_KEYWORDS`` call."""
    for callee, renamed in RENAMED_KEYWORDS.items():
        for match in re.finditer(rf"(?<![\w.]){callee}\(", text):
            depth, index = 1, match.end()
            while index < len(text) and depth:
                char = text[index]
                depth += (char in "([{") - (char in ")]}")
                keyword = re.match(r"(\w+)=(?!=)", text[index:]) if depth == 1 else None
                if keyword and text[index - 1] in "(, \n" and keyword[1] in renamed:
                    yield f"{callee}({keyword[1]}=) is {renamed[keyword[1]]}"
                index += 1


def _renamed(text: str) -> list[str]:
    hits = [what for what, pattern in RENAMED.items() if pattern.search(text)]
    return [*hits, *_removed_keywords(text)]


def _package_findings() -> list[str]:
    found: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line, text in _runtime_strings(tree):
            for what in _hits(text):
                found.append(f"{path.relative_to(PACKAGE)}:{line}: {what}: {text[:80]!r}")
    return found


def test_package_strings_cite_only_the_declarative_api() -> None:
    assert _package_findings() == []


def test_examples_cite_only_the_declarative_api() -> None:
    found = [
        f"{path.relative_to(EXAMPLES)}:{number}: {what}"
        for path in sorted(EXAMPLES.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for what in _hits(line)
    ]
    assert found == []


@pytest.mark.parametrize("path", CLI_DOCS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_cli_docs_cite_only_the_thirteen_commands(path: Path) -> None:
    found = [
        f"{number}: {line.strip()[:80]}"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if DENYLIST[REMOVED_VERB].search(line)
    ]
    assert found == []


@pytest.mark.parametrize(
    "text",
    [
        "run `tundravm bake RECIPE --build out`",
        "`tundravm units convert x.service`",
        "tundravm measure --export-policy FILE",
        "uv run tundravm ci node.py",
        "tundravm import ./tree",
        "echo 'tundravm: tool is not pinned: run tundravm lock, then tundravm fetch' >&2",
        "tundravm: tool-0123 is not fetched: run tundravm fetch RECIPE",
    ],
)
def test_removed_verbs_are_caught(text: str) -> None:
    assert REMOVED_VERB in _hits(text)


@pytest.mark.parametrize(
    ("text", "what"),
    [
        ("Run `tundravm lock RECIPE` to pin them.", "RECIPE placeholder (IMAGE)"),
        ("Run bake() successfully before deploy().", "bake for a build (build)"),
        ("An offline bake cannot build a tree", "bake for a build (build)"),
        ("Bake the variant again to rewrite it.", "bake for a build (build)"),
        ("baked 2 variants in 3s", "bake for a build (build)"),
        (
            "or relax network_mode.",
            "Policy knob an Image cannot set (build --offline, tundravm lock)",
        ),
        ("Recipe file not found: x.py", "Recipe for the image file (image file, Image)"),
        ("Recipe factory build() returned None.", "Recipe for the image file (image file, Image)"),
        ("Pass the path of a Recipe.", "Recipe for the image file (image file, Image)"),
        ("Run `python recipe.py`.", "Recipe for the image file (image file, Image)"),
        ("Unknown variant(s): staging.", "variant for a profile (profile)"),
        ("Duplicate user name 'a' in variant 'dev'.", "variant for a profile (profile)"),
        ("  variant=", "variant for a profile (profile)"),
        ("variants.dev.files changed since the lock", "variant for a profile (profile)"),
        (
            "Name one Nethermind ships, e.g. config='surge-hoodi'.",
            "removed workload (Raiko) or example (surge; examples/attested-node)",
        ),
    ],
)
def test_internal_names_are_caught(text: str, what: str) -> None:
    assert what in _hits(text)


def test_removed_docs_are_caught() -> None:
    assert _hits("keep the ones the program does not need (docs/api.md: Hardening)") == [
        "removed doc (docs/guide.md, docs/reference/)"
    ]


@pytest.mark.parametrize(
    "text",
    [
        "from tundravm import Image",
        "tundravm run status demo-1",
        "tundravm show --diff OLD NEW",
        "tundravm check --tree mkosi",
        "tundravm convert ./units --from systemd",
        "echo 'tundravm: tool is not pinned: run tundravm lock, then tundravm build' >&2",
        "Run `tundravm lock` to pin them.",
        "Deliver it at boot instead of baking it into the image",
        "Unknown profile(s): staging.",
        "profile=dev  debian/trixie (x86_64)  reproducible=yes",
    ],
)
def test_the_thirteen_commands_pass(text: str) -> None:
    assert _hits(text) == []


@pytest.mark.parametrize(
    "source",
    [
        'error(hint="Call bake(frozen=True) first.")',
        'error(hint=f"img.apply({theirs}(), {mine}())")',
        "error(hint='pin it with HttpSource(sha256=...)')",
        "MESSAGE = 'see `tundravm lint RECIPE`'",
        "HINT = 'run tundravm init --base debian/sid'",
        """HINT = 'Sudo(["/usr/bin/journalctl", "-n", "{lines:int:1-1000}"])'""",
        """HINT = 'Login("t", commands={"status": ["/usr/bin/app-status"]})'""",
    ],
)
def test_denylist_catches_fluent_hints(source: str) -> None:
    assert any(_hits(text) for _, text in _runtime_strings(ast.parse(source)))


def test_bare_string_statements_are_documentation() -> None:
    tree = ast.parse('"""Call img.apply(x)."""\nNAME = 1\n"""Attribute doc: Image()."""\n')
    assert list(_runtime_strings(tree)) == []


@pytest.mark.parametrize(
    ("text", "what"),
    [
        (
            "from tundravm import TCP, File, Image",
            "flat import of a namespaced name (its namespace)",
        ),
        (
            "from tundravm import (\n    Image,\n    Volume,\n)",
            "flat import of a namespaced name (its namespace)",
        ),
        ("a tundravm.Secret holding the key", "tundravm.<namespaced name> (its namespace)"),
        ("from tundravm.image import Image", "tundravm.image (the public namespaces)"),
        (
            "tundravm.bake(image, lock=lock, backend=backend, build=out)",
            "tundravm.bake (tundravm.build)",
        ),
        ("from tundravm import Image, bake", "tundravm.bake (tundravm.build)"),
        (
            "from tundravm.components.taiko import TaikoClient",
            "tundravm.components.<workload> (tundravm.components)",
        ),
    ],
)
def test_namespaced_spellings_are_caught(text: str, what: str) -> None:
    assert what in _renamed(text)


@pytest.mark.parametrize(
    "text",
    [
        "from tundravm import File, Image, Service\nfrom tundravm.ports import TCP",
        "from tundravm import secrets",
        "Bind a tundravm.Image to `image`.",
        "from tundravm.components.access import DevAccess",
        "from tundravm.declarative import Recipe, bake",
    ],
)
def test_namespaced_spellings_pass(text: str) -> None:
    assert _renamed(text) == []


def test_package_strings_use_the_current_recipe_spellings() -> None:
    found = [
        f"{path.relative_to(PACKAGE)}:{line}: {what}"
        for path in sorted(PACKAGE.rglob("*.py"))
        for line, text in _runtime_strings(ast.parse(path.read_text(encoding="utf-8")))
        for what in _renamed(text)
    ]
    assert found == []


@pytest.mark.parametrize(
    "path",
    [*sorted(EXAMPLES.rglob("*.py")), *DOCS],
    ids=lambda path: str(path.relative_to(REPO_ROOT)),
)
def test_examples_and_docs_use_the_current_recipe_spellings(path: Path) -> None:
    assert _renamed(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "source",
    [
        'Variant("dev", parent="default")',
        'Variant("cloud", targets=("azure",))',
        'Variant(\n    "x",\n    add=Fragment("a", (Package("b"),)),\n    target="qemu",\n)',
        'Recipe("r", common=Fragment("c"))',
        "recipe.common.items",
        'Build("b", GIT, recipe=Go(output="b"), install=(Install("b", "/b"),))',
        'Build("b", GIT, script="make", cache_key="b")',
        'Directory("/opt", Path("src"))',
        "assert_clean(RECIPE, strict=True)",
        'Variant("dev", replace=(File("/etc/motd", "dev\\n"),))',
        'Variant("slim", inherits="default", remove=(Package("jq"),))',
        "for item in variant.replace: ...  # Variant.replace",
        "`replace-missing` and `disk-key-mismatch`",
        'Recipe("r", base="debian/trixie")',
        'Recipe(\n    name="r",\n    snapshot=SNAPSHOT,\n    items=(),\n)',
        'Recipe("r", arch="aarch64", mirror="https://m", tools_mirror="https://m", epoch=None)',
        "pin it with Recipe.snapshot",
        'BOOT = ("linux-image-amd64", "systemd")',
        "packages(*BOOT, 'curl')",
        'Init("state", "install -d /var/lib/app\\n", priority=25, after=("disks",))',
        'Unit("app.service", UNIT, enabled=True, after_init=True)',
        'BootStep("state", "true", priority=25)',
        'fake_fragment("late", init="echo late", priority=40)',
        "the init-priority-collision warning",
        'Diagnostic("app-x", "error", "m", variant=resolved.variant)',
        "the variant-empty finding",
        'Secret("jwt", rotation="restart")',
        '"reboot": Sudo("/usr/bin/systemctl", "reboot"),',
        '"toggle": firewall.switch,',
    ],
)
def test_renamed_guard_catches_the_removed_spellings(source: str) -> None:
    assert _renamed(source)


def test_renamed_guard_passes_the_current_spellings() -> None:
    source = (
        'Recipe("r", items=(packages("curl"),), variants=(Variant("d", outputs=("qemu",)),))\n'
        'Build("b", GIT, using=Go(output="b"), cache_namespace="b", install=(Install("b", "/"),))\n'
        'Secret("s", targets=(SecretFile("/s"),))\nFileTree("/opt", Path("src"))\n'
        "ArtifactRef(target='qemu', path=p)\nassert_clean(RECIPE, warnings='allow')\n"
        'Variant("slim", changes=(Package.ref("jq").remove(), motd.ref.edit(mode=0o600)))\n'
        'Variant("d", add=Fragment("f", (Debloat(remove=("/x",)),)))\n'
        'Recipe("r", platform=Debian("trixie", snapshot=SNAPSHOT, arch="x86_64", epoch=0))\n'
        'EfiStub(snapshot=SNAPSHOT, version="257.8-1~deb13u1")\nmcopy ::EFI/BOOT/BOOTX64.EFI\n'
        'BootStep("s", "true", after=[disk.ready])\nUnit("a.service", U, needs=[s.ready])\n'
        'Repository("r", URL, "s", priority=10)\nRuntimeInit("x")\n'
        '{"toggle": firewall.commands.switch, "reboot": Command("/x", sudo=True)}\n'
    )
    assert _renamed(source) == []


def _internal(text: str) -> list[str]:
    return [what for what, pattern in INTERNAL_MODEL.items() if pattern.search(text)]


def _hint_strings(tree: ast.Module) -> Iterator[tuple[int, str]]:
    """``(line, text)`` of the strings a finding's message and hint are built from:
    ``message=`` and ``hint=`` of a ``Diagnostic`` or a ``report`` (or its message argument),
    the hint of ``_finding``, and module-level ``*_HINT``."""
    for node in ast.walk(tree):
        parts: list[ast.AST] = []
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if name in ("Diagnostic", "report"):
                parts = [k.value for k in node.keywords if k.arg in ("message", "hint")]
                parts += node.args[2:3] if name == "Diagnostic" else node.args[1:2]
            elif name == "_finding" and len(node.args) == 6:
                parts = [node.args[5]]
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id.endswith("_HINT") for t in node.targets
        ):
            parts = [node.value]
        for part in parts:
            for leaf in ast.walk(part):
                if isinstance(leaf, ast.Constant) and isinstance(leaf.value, str):
                    yield leaf.lineno, leaf.value


def test_lint_fixes_and_hints_name_the_public_api() -> None:
    found = [f"lint.md: {what}" for what in _internal(lint_markdown())]
    found += [
        f"{path.relative_to(PACKAGE)}:{line}: {what}: {text[:80]!r}"
        for path in sorted(PACKAGE.rglob("*.py"))
        for line, text in _hint_strings(ast.parse(path.read_text(encoding="utf-8")))
        for what in _internal(text)
    ]
    assert found == []


@pytest.mark.parametrize(
    "text",
    [
        "Ship it as Unit(name, text).",
        "Keep it with Debloat(keep_units_extra=(UNIT,)).",
        "or drop it from needs=.",
        "Declare Key(NAME, ...) in the same profile.",
        "a Secret with a SecretEnv target, written by the profile's ExternalDelivery",
        "Declare the step or the Task in the profile.",
        'Add conditions=(Condition.virtualization("google"),).',
        "Setting is for keys tundravm has no field for.",
        "Order it with Hook(name, phase, script, after=[...]).",
        "Mount it with Mount(ref, where).",
        "Open it with Firewall(collect=True), or Firewall(ssh=True) for port 22.",
        "Wait for it with needs=[UnitReady(unit)] or Ready('disks').",
        "Run as user=Root() when the unit must.",
        "See tundravm._modules.init, or tundravm.declarative.utils.",
        "profile 'dev' edits File(extra, /etc/motd), which it does not inherit",
        "profile 'dev' removes Package(jq, runtime), which it does not inherit",
        "declared Build(server) via app > server",
        "declared 2 times in mkosi.extra with different content (file, template)",
        "debloat deletes /var/log at finalize, after this file is placed",
    ],
)
def test_internal_model_names_are_caught(text: str) -> None:
    assert _internal(text)


@pytest.mark.parametrize(
    "text",
    [
        "Declare Service(NAME, argv) with requires=[other.ready] and expose=[TCP(8080)].",
        "Add AptRepository(url, suite, key=...) to Debian(repositories=[...]).",
        "Its readiness is a ServiceReady, bound as ComponentReady(self).",
        "Pass dev=DevAccess(ssh_keys=[...]), or SshAccess(from_delivery=True).",
        "Declare the code with Rule from tundravm.components.",
    ],
)
def test_public_spellings_pass_the_internal_model_guard(text: str) -> None:
    assert _internal(text) == []


def test_resolve_messages_name_the_value_an_image_file_wrote() -> None:
    motd = File.text("/etc/motd", "x\n")
    ghost = Service("ghost", ["/usr/bin/ghost"])
    tick = Service("tick", ["/usr/bin/tick"], timer=Every("1h"))
    edit, removal = motd.ref.edit(mode=0o600), Package("jq").ref.remove()
    profile = Profile(changes=[edit, ghost.ref.edit(user="www"), tick.ref.remove(), removal])
    image = Image("x", profiles={"s": profile})
    found = [d for d in plan(image, "s").lint() if d.code in ("edit-missing", "remove-missing")]
    assert [d.subject for d in found] == [
        "File('/etc/motd')",
        "Service('ghost')",
        "Service('tick')",
        "Package('jq')",
    ]
    assert [what for d in found for what in _internal(d.message)] == []


def test_show_json_provenance_names_the_public_objects() -> None:
    class App(Component):
        def compose(self) -> Iterator[object]:
            server = [GoBinary(Local("."), name="server")]
            yield Service("server", server, readiness=TcpProbe(8080))

    image = Image("x", components=[App()])
    found = show.provenance(recipe_of(image), ["production"], declared=image)["production"]
    rows = {row["declaration"]: row["origins"][0]["components"] for row in found}  # type: ignore[index]
    assert {key: rows[key] for key in ("GoBinary('server')", "Service('server').readiness")} == {
        "GoBinary('server')": ["App"],
        "Service('server').readiness": ["App"],
    }
    assert _internal(json.dumps(found)) == []


@pytest.mark.parametrize("code", PUBLIC_RULES)
def test_a_rule_an_image_file_can_hit_speaks_the_public_api(code: str) -> None:
    rule = RULES[code]
    found = [
        f"{field}: {what}"
        for field, text in (("summary", rule.summary), ("fix", rule.fix))
        for what in (*_hits(text), *_renamed(text), *_internal(text))
    ]
    assert found == []


ERROR_DENYLIST = {
    "a private name": re.compile(r"(?<![\w.])_[A-Z][A-Za-z]+\b"),
    "recipe (the image file, Image)": re.compile(r"\b[Rr]ecipes?\b"),
    "variant (profile)": re.compile(r"\b[Vv]ariants?\b"),
}
"""Beyond the internal model's names, what no error message, fix or context key says."""
ERROR_CALLS = frozenset({"_fail", *(name for name in errors.__all__ if name.endswith("Error"))})


def _error_strings(tree: ast.Module) -> Iterator[tuple[int, str]]:
    """``(line, text)`` of every string an error raised or returned is built from: its
    message, its fix and its context."""
    for node in ast.walk(tree):
        value = node.exc if isinstance(node, ast.Raise) else None
        value = node.value if isinstance(node, ast.Return) else value
        if not isinstance(value, ast.Call):
            continue
        if getattr(value.func, "id", getattr(value.func, "attr", "")) in ERROR_CALLS:
            for leaf in ast.walk(value):
                if isinstance(leaf, ast.Constant) and isinstance(leaf.value, str):
                    yield leaf.lineno, leaf.value


def _error_hits(text: str) -> list[str]:
    return [*_internal(text), *(what for what, p in ERROR_DENYLIST.items() if p.search(text))]


def test_errors_name_the_public_api() -> None:
    """An error says what is wrong and what to write in the image file's words."""
    found = [
        f"{path.relative_to(PACKAGE)}:{line}: {what}: {text[:80]!r}"
        for path in sorted(PACKAGE.rglob("*.py"))
        for line, text in _error_strings(ast.parse(path.read_text(encoding="utf-8")))
        for what in _error_hits(text)
    ]
    assert found == []


@pytest.mark.parametrize(
    "source",
    [
        'raise _fail(self, "Declare at least one Disk().", hint="h")',
        'raise ValidationError("x", hint="Give Key(strategy=\'pipe\') a pipe= path.")',
        'raise ValidationError("Setting Build.BuildSources: x", hint="y")',
        'raise ValidationError("x", hint="y", context={"recipe": path})',
        'return SourceError("see tundravm.declarative.model", source="s", reason="r", hint="h")',
        'raise _fail(self, "x", hint="Pass delivery=_Delivery().")',
    ],
)
def test_error_denylist_catches_internal_names(source: str) -> None:
    assert any(_error_hits(text) for _, text in _error_strings(ast.parse(source)))
