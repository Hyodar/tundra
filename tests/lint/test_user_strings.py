"""User-facing strings must not cite the removed fluent API, CLI verbs or CLI flags."""

from __future__ import annotations

import ast
import importlib
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

import tundravm
from tests.helpers import REPO_ROOT
from tundravm._reference import MODULES

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
"""The verbs folded into the twelve commands (plan 3.4): no hint, template or doc names one."""

DENYLIST = {
    "fluent method call": re.compile(r"\bimg\.[a-z_]+\("),
    "fluent profile block": re.compile(r"\bwith img\b"),
    "frozen bake flag": re.compile(r"\bbake\([^)]*\bfrozen=True\b"),
    "internal source class": re.compile(r"\b(?:Http|Git|Local)Source\("),
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
        r"\bRecipe (?:file|has|factory|factories|attribute|defines)\b|\bbinds a Recipe\b"
    ),
    "variant for a profile (profile)": re.compile(
        r"\b[Uu]nknown variant|\bDeclared variants\b|\bin variant\b|\bvariant=(?:\{|\s*$)"
        r"|\bInherits: |\bvariants\.[a-z]"
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
    "BootStep": {"priority": "after=/before= readiness handles"},
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
def test_cli_docs_cite_only_the_twelve_commands(path: Path) -> None:
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
        ("Unknown variant(s): staging.", "variant for a profile (profile)"),
        ("Duplicate user name 'a' in variant 'dev'.", "variant for a profile (profile)"),
        ("  variant=", "variant for a profile (profile)"),
        ("variants.dev.files changed since the lock", "variant for a profile (profile)"),
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
def test_the_twelve_commands_pass(text: str) -> None:
    assert _hits(text) == []


@pytest.mark.parametrize(
    "source",
    [
        'error(hint="Call bake(frozen=True) first.")',
        'error(hint=f"img.apply({theirs}(), {mine}())")',
        "error(hint='pin it with HttpSource(sha256=...)')",
        "MESSAGE = 'see `tundravm lint RECIPE`'",
        "HINT = 'run tundravm init --base debian/sid'",
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
            "from tundravm.components.taiko import Raiko",
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
        "from tundravm.components import Component, DevAccess",
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
    )
    assert _renamed(source) == []
