"""One page that explains a recipe: :func:`explore` and :class:`Exploration`.

:func:`explore` gathers, without baking or touching the network, what someone
needs to understand a recipe without reading its Python: the recipe settings,
the lint findings, the lockfile's state, the bake in a build directory (when
there is one), every declaration across the variants with its provenance, the
compiled tree of each variant with each file's content and the declarations
that produced it, and the hooks, runtime-init steps and source builds in the
order they run. :meth:`Exploration.to_dict` is that model as JSON
(``"schema": "tundravm.explore/5"``) and :meth:`Exploration.html` one
self-contained page: inline CSS and a small inline script (tabs, filters,
cross-link jumps) that reads as a static page without it.
"""

from __future__ import annotations

import hashlib
import html
import json
import posixpath
from collections.abc import Mapping, Sequence
from dataclasses import MISSING, dataclass, fields, is_dataclass
from pathlib import Path
from string import Template
from typing import Any

from ._location import display_location
from ._modules.init import runtime_init_order
from .compiler import PHASE_ORDER
from .declarative._lowered import Lowered
from .declarative.lifecycle import (
    MANIFEST_KEY,
    NO_LOCK,
    Entry,
    Lock,
    check_report,
    compile_image,
    image_lock_status,
    lock_status,
    read_artifacts,
    read_lock,
    recorded_reproducibility,
    reproducibility_of,
    using_lock,
    variant_names,
    variant_provenance,
)
from .declarative.lower import lower
from .declarative.model import BUILTIN_STEPS, BootStep, Declaration, Hook, Recipe
from .declarative.resolve import Identity, Origin, _run, identity, level_name
from .declarative.resolve import describe as describe_identity
from .errors import TdxError, ValidationError
from .explain import _changed_fields, _emitted_for, _fragment_chain
from .models import BAKE_RESULT_FILENAME

EXPLORE_SCHEMA = "tundravm.explore/5"
EXPLORE_FORMATS = ("html", "json")
PAGE_NAME = "explore.html"
"""``tundravm explore`` writes ``BUILD/explore.html`` unless ``--output`` says otherwise."""
TEXT_LIMIT = 64 * 1024
"""A file's or script's content shown in full up to this many bytes, then truncated."""
TEXT_BUDGET = 4 * 1024 * 1024
"""Content shown on one page in all; past it a file's content is left out, with a note."""
SHARED = "(shared)"
"""The tree group of paths outside every variant directory (``Mkosi(layout="native")``)."""
STATES = ("present", "added", "edited", "removed", "absent")
"""A declaration's state in one variant, as the matrix shows it."""
_SHORT = 120


@dataclass(frozen=True, slots=True)
class Exploration:
    """What :func:`explore` gathered: :attr:`model` is the JSON document without ``schema``."""

    model: Mapping[str, Any]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Exploration:
        """The exploration a :meth:`to_dict` (or ``explore --format json``) document holds."""
        if data.get("schema") != EXPLORE_SCHEMA:
            raise ValidationError(
                f"Not an exploration: schema is {data.get('schema')!r}.",
                hint=f"Pass what `tundravm explore --format json` prints ({EXPLORE_SCHEMA}).",
            )
        return cls({key: value for key, value in data.items() if key != "schema"})

    @property
    def variants(self) -> tuple[str, ...]:
        return tuple(str(v["name"]) for v in self.model["variants"])

    def to_dict(self) -> dict[str, Any]:
        """The model as ``explore --format json`` prints it, ``schema`` included."""
        loaded: dict[str, Any] = json.loads(json.dumps(self.model))
        return {"schema": EXPLORE_SCHEMA, **loaded}

    def json(self) -> str:
        """:meth:`to_dict` as JSON text, in the model's order (what the page renders from)."""
        return json.dumps(self.to_dict(), indent=2)

    def html(self) -> str:
        """One self-contained page: no external assets, every value escaped."""
        return render_html(self.to_dict())


def explore(
    recipe_or_lowered: Recipe | Lowered,
    *,
    variants: Sequence[str] | None = None,
    lock: Lock | Path | None = None,
    build: Path | None = None,
) -> Exploration:
    """Everything a reader needs to understand *variants* (default: all) of a recipe.

    *lock* (a :class:`Lock` or a lockfile path) pins the sources and is checked for
    drift; without it the recipe's own lockfile (``build/tundravm.lock``) is used
    when it exists, else the lock is ``missing``. *build* is a build directory
    whose ``bake-result.json`` gives the artifacts. A lowered recipe has no
    declarations, so its page leaves the matrix and the annotations out. Nothing
    is written and the network is never used.
    """
    recipe = recipe_or_lowered if isinstance(recipe_or_lowered, Recipe) else None
    notes: list[str] = []
    if recipe is not None:
        names = variant_names(recipe, variants)
    else:
        assert isinstance(recipe_or_lowered, Lowered)
        declared = recipe_or_lowered.profile_names
        names = tuple(variants) if variants is not None else declared
        unknown = [name for name in names if name not in declared]
        if unknown:
            raise ValidationError(
                f"Unknown variant(s): {', '.join(unknown)}.",
                hint=f"Declared variants: {', '.join(declared)}",
            )
        notes.append("a lowered recipe has no declarations: the matrix and annotations are empty")
    locked, lock_source = _selected_lock(recipe_or_lowered, lock)
    given = recipe_or_lowered if isinstance(recipe_or_lowered, Lowered) else None
    findings = check_report(
        recipe,
        given,
        variants=names,
        lock=NO_LOCK if locked is None else locked,
    )
    resolutions = _resolutions(recipe, names) if recipe is not None else {}
    model: dict[str, Any] = {
        "recipe": _recipe_entry(recipe, recipe_or_lowered, names),
        "lint": _lint_entry(findings),
        "lock": _lock_entry(locked, lock_source),
        "bake": _bake_entry(build, notes),
        "variants": _variant_entries(recipe, names, resolutions),
        "declarations": [],
        "trees": {},
        "scripts": {},
        "notes": notes,
    }
    try:
        whole = lower(recipe, variants=names) if recipe is not None else given
        assert whole is not None
        img = whole.select(names)
        model["recipe"]["digest"] = img.digest()
        if locked is not None:
            drift = (
                lock_status(recipe, locked, variants=names)
                if recipe is not None
                else image_lock_status(img, locked, names, partial=True)
            )
            model["lock"]["state"] = "drifted" if drift else "current"
            model["lock"]["drift"] = [_finding(d) for d in drift]
        tree = compile_image(img, names, locked=locked)
        with using_lock(img, locked) as pinned:
            model["scripts"] = {name: _scripts(pinned, name, resolutions) for name in names}
    except TdxError as exc:
        notes.append(f"the recipe did not compile, so the tree and scripts are left out: {exc}")
        model["notes"] = list(dict.fromkeys(notes))
        return Exploration(model)
    groups = _group(tree.entries, tree.variants or names)
    budget = [TEXT_BUDGET]
    trees = {
        group: [_file_record(rel, entry, budget) for rel, entry in entries]
        for group, entries in groups.items()
    }
    if budget[0] <= 0:
        notes.append(
            f"the page shows at most {TEXT_BUDGET // (1024 * 1024)} MiB of file content: "
            "later files' content is left out"
        )
    if resolutions:
        model["declarations"] = _declarations(names, resolutions, groups, trees)
    model["trees"] = {group: {"files": files} for group, files in trees.items()}
    model["notes"] = list(dict.fromkeys(notes))
    return Exploration(model)


# ── gathering ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Resolution:
    items: dict[Identity, Declaration]
    trace: dict[Identity, tuple[Origin, ...]]
    fragments: tuple[str, ...]
    outputs: tuple[str, ...]


def _resolutions(recipe: Recipe, names: Sequence[str]) -> dict[str, _Resolution]:
    found: dict[str, _Resolution] = {}
    for name in names:
        resolved, state = _run(recipe, name)
        found[name] = _Resolution(
            items={identity(item): item for item in resolved.items},
            trace={key: tuple(origins) for key, origins in state.trace.items()},
            fragments=resolved.fragments,
            outputs=tuple(resolved.outputs),
        )
    return found


def _selected_lock(
    subject: Recipe | Lowered, lock: Lock | Path | None
) -> tuple[Lock | None, str | None]:
    if isinstance(lock, Lock):
        return lock, None
    if lock is not None:
        path = Path(lock)
    else:
        try:
            path = (lower(subject) if isinstance(subject, Recipe) else subject).lock_path
        except TdxError:
            return None, None
    return (read_lock(path), str(path)) if path.is_file() else (None, str(path))


def _recipe_entry(
    recipe: Recipe | None, subject: Recipe | Lowered, names: Sequence[str]
) -> dict[str, Any]:
    if recipe is not None:
        return {
            "name": recipe.name,
            "base": recipe.platform.name,
            "arch": recipe.platform.arch,
            "snapshot": recipe.platform.snapshot,
            "mirror": recipe.platform.mirror,
            "dialect": recipe.mkosi.dialect,
            "layout": recipe.mkosi.layout,
            "digest": None,
            "variants": list(names),
        }
    assert isinstance(subject, Lowered)
    return {
        "name": None,
        "base": subject.base,
        "arch": subject.arch,
        "snapshot": subject.snapshot,
        "mirror": subject.mirror,
        "dialect": subject.mkosi.dialect,
        "layout": "native" if subject.mkosi.emit_mode == "native_profiles" else "directories",
        "digest": None,
        "variants": list(names),
    }


def _finding(diagnostic: Any) -> dict[str, Any]:
    return {
        "code": diagnostic.code,
        "severity": diagnostic.severity,
        "variant": diagnostic.variant,
        "location": diagnostic.location,
        "subject": diagnostic.subject,
        "message": diagnostic.message,
        "hint": diagnostic.hint,
    }


def _lint_entry(findings: Sequence[Any]) -> dict[str, Any]:
    severities = [d.severity for d in findings]
    return {
        "errors": severities.count("error"),
        "warnings": severities.count("warning"),
        "infos": severities.count("info"),
        "findings": [_finding(d) for d in findings],
    }


def _lock_entry(locked: Lock | None, source: str | None) -> dict[str, Any]:
    if locked is None:
        return {"state": "missing", "source": source, "drift": []}
    return {
        "state": "current",
        "source": source,
        "recipe_digest": locked.recipe_digest,
        "compiler_version": locked.compiler_version,
        "sections": dict(locked.sections),
        "pins": [
            {
                "identity": pin.identity,
                "kind": type(pin.source).__name__.lower(),
                "url": pin.source.url,
                "digest": pin.digest,
            }
            for pin in locked.pins
        ],
        "drift": [],
    }


def _bake_entry(build: Path | None, notes: list[str]) -> dict[str, Any] | None:
    if build is None:
        return None
    base = Path(build)
    manifest = base / BAKE_RESULT_FILENAME
    if not manifest.is_file():
        notes.append(f"no bake in {base}: run `tundravm bake` to list its artifacts here")
        return None
    try:
        artifacts = read_artifacts(base)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (TdxError, OSError, ValueError) as exc:
        notes.append(f"unreadable bake in {base}: {exc}")
        return None
    extra = payload.get(MANIFEST_KEY) or {}
    reproduced = recorded_reproducibility(extra)
    return {
        "build": str(base),
        "backend": payload.get("backend"),
        "simulated": bool(extra.get("simulated", False)),
        "reproducibility": None if reproduced is None else reproduced.get("state"),
        "recipe_digest": extra.get("recipe_digest"),
        "artifacts": [
            {
                "variant": a.variant,
                "target": a.target,
                "path": str(a.path),
                "sha256": a.sha256,
                "simulated": a.simulated,
                "reproducibility": reproducibility_of(
                    recorded_reproducibility(variant_provenance(payload, a.variant)),
                    f"{a.variant}/{a.target}",
                ),
            }
            for a in artifacts
        ],
    }


def _variant_entries(
    recipe: Recipe | None, names: Sequence[str], resolutions: Mapping[str, _Resolution]
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for name in names:
        found = resolutions.get(name)
        entries.append(
            {
                "name": name,
                "inherits": None if recipe is None else recipe.variant(name).inherits,
                "outputs": [] if found is None else list(found.outputs),
                "fragments": [] if found is None else list(found.fragments),
                "declarations": 0 if found is None else len(found.items),
            }
        )
    return entries


def _group(entries: Sequence[Entry], variants: Sequence[str]) -> dict[str, list[tuple[str, Entry]]]:
    """Tree entries by variant directory (paths relative to it); the rest under :data:`SHARED`."""
    tops = set(variants)
    groups: dict[str, list[tuple[str, Entry]]] = {name: [] for name in variants}
    for entry in entries:
        top, _, rest = entry.path.partition("/")
        if top in tops:
            if rest:
                groups[top].append((rest, entry))
        else:
            groups.setdefault(SHARED, []).append((entry.path, entry))
    return groups


def _file_record(rel: str, entry: Entry, budget: list[int]) -> dict[str, Any]:
    record: dict[str, Any] = {"path": rel, "mode": f"{entry.mode:04o}"}
    stage, _, rest = rel.partition("/")
    if stage in ("mkosi.extra", "mkosi.skeleton") and rest:
        record["image_path"] = f"/{rest}"
    if entry.symlink is not None:
        return {**record, "type": "symlink", "target": entry.symlink}
    if entry.content is None:
        return {**record, "type": "directory"}
    content = entry.content
    text: str | None
    try:
        text = None if b"\0" in content[:8192] else content.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    binary = text is None
    truncated = False
    if text is not None and budget[0] <= 0:
        text, truncated = None, True
    elif text is not None and len(content) > TEXT_LIMIT:
        text, truncated = content[:TEXT_LIMIT].decode("utf-8", errors="ignore"), True
    if text is not None:
        budget[0] -= len(text)
    return {
        **record,
        "type": "file",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "binary": binary,
        "truncated": truncated,
        "text": text,
        "declarations": [],
    }


def _state(origins: Sequence[Origin]) -> str:
    if not origins:
        return "absent"
    actions = [origin.action for origin in origins]
    if actions[-1] == "removed":
        return "removed"
    if "edited" in actions:
        return "edited"
    return "added" if actions[-1] == "added" else "present"


def _origin(origin: Origin) -> dict[str, str]:
    return {
        "action": origin.action,
        "level": origin.variant,
        "via": _fragment_chain(origin),
        "fields": ", ".join(origin.fields),
        "location": display_location(origin.location),
    }


def _declarations(
    names: Sequence[str],
    resolutions: Mapping[str, _Resolution],
    groups: Mapping[str, Sequence[tuple[str, Entry]]],
    trees: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    keys = list(
        dict.fromkeys(
            key for name in names for key in (*resolutions[name].items, *resolutions[name].trace)
        )
    )
    rows: dict[Identity, dict[str, Any]] = {}
    for key in keys:
        first = next((r.items[key] for r in resolutions.values() if key in r.items), None)
        rows[key] = {
            "declaration": describe_identity(key),
            "type": key[0],
            "key": list(key[1:]),
            "fields": {} if first is None else _summary(first),
            "variants": {},
        }
    records = {group: {f["path"]: f for f in files} for group, files in trees.items()}
    for name in names:
        found = resolutions[name]
        view = _variant_view(name, groups)
        for key in keys:
            item = found.items.get(key)
            first = next((r.items[key] for r in resolutions.values() if key in r.items), None)
            files: list[str] = []
            generated: list[str] = []
            if item is not None:
                hits, generated = _emitted_for(item, {rel: text for rel, (_, text) in view.items()})
                for hit in hits:
                    rel = hit if hit in view else hit.split(" (", 1)[0]
                    if rel in view and rel not in files:
                        files.append(rel)
                        group = view[rel][0]
                        record = records[group].get(rel)
                        if record is not None and "declarations" in record:
                            record["declarations"].append(describe_identity(key))
            changed = (
                list(_changed_fields(first, item))
                if item is not None and first is not None and item != first
                else []
            )
            rows[key]["variants"][name] = {
                "state": _state(found.trace.get(key, ())),
                "origins": [_origin(o) for o in found.trace.get(key, ())],
                "changed": changed,
                "files": [{"group": view[rel][0], "path": rel} for rel in files],
                "generated": list(dict.fromkeys(generated)),
            }
    return list(rows.values())


def _variant_view(
    name: str, groups: Mapping[str, Sequence[tuple[str, Entry]]]
) -> dict[str, tuple[str, str]]:
    """*name*'s tree as ``explain`` reads it: relative path -> (group, text)."""
    view: dict[str, tuple[str, str]] = {}
    for group in (name, SHARED):
        for rel, entry in groups.get(group, ()):
            if entry.content is not None and rel not in view:
                view[rel] = (group, entry.content.decode("utf-8", errors="replace"))
            elif entry.symlink is not None and rel not in view:
                view[rel] = (group, f"-> {entry.symlink}")
    return view


def _summary(item: Declaration) -> dict[str, str]:
    """*item*'s fields that differ from their defaults, each shortened to one line."""
    found: dict[str, str] = {}
    for spec in fields(item):
        if spec.name.startswith("_"):
            continue
        value = getattr(item, spec.name)
        default: object = spec.default
        if default is MISSING and spec.default_factory is not MISSING:
            default = spec.default_factory()
        if default is not MISSING and value == default:
            continue
        found[spec.name] = _short(value, mode=spec.name == "mode")
    return found


def _short(value: object, *, mode: bool = False) -> str:
    if mode and isinstance(value, int):
        return f"{value:04o}"
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, str):
        lines = value.splitlines()
        if len(lines) > 1:
            return f"{_clip(lines[0])} … ({len(lines)} lines)"
        return _clip(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return _clip(", ".join(_short(v) for v in value)) if value else "()"
    if is_dataclass(value) and not isinstance(value, type):
        inner = ", ".join(
            f"{f.name}={_short(getattr(value, f.name))}"
            for f in fields(value)
            if getattr(value, f.name) not in (None, (), "")
        )
        return _clip(f"{type(value).__name__}({inner})")
    return _clip(str(value))


def _clip(text: str) -> str:
    return text if len(text) <= _SHORT else f"{text[: _SHORT - 1]}…"


def _clip_script(script: str) -> tuple[str, bool]:
    data = script.encode("utf-8")
    if len(data) <= TEXT_LIMIT:
        return script, False
    return data[:TEXT_LIMIT].decode("utf-8", errors="ignore"), True


def _scripts(img: Lowered, name: str, resolutions: Mapping[str, _Resolution]) -> dict[str, Any]:
    """*name*'s hooks in stage order, its runtime-init steps in order and its sources."""
    pins = img.build_pins()
    profile = img.pinned_state(pins, variant=name).effective_profile(name)
    items = resolutions[name].items.values() if name in resolutions else ()
    hooks_by_script = {
        hook.script.strip(): describe_identity(identity(hook))
        for hook in items
        if isinstance(hook, Hook)
    }
    inits_by_script = {
        init.script.strip(): describe_identity(identity(init))
        for init in items
        if isinstance(init, BootStep)
    }
    hooks: list[dict[str, Any]] = []
    for phase in PHASE_ORDER:
        for hook in profile.hooks:
            if hook.phase != phase:
                continue
            script, truncated = _clip_script("\n".join(hook.command.argv))
            hooks.append(
                {
                    "phase": phase,
                    "declaration": hooks_by_script.get(script.strip()),
                    "script": script,
                    "truncated": truncated,
                }
            )
    init = [
        {
            "step": entry.name,
            "after": list(entry.after),
            "declaration": inits_by_script.get(entry.script.strip()),
            "builtin": entry.name
            if entry.name in BUILTIN_STEPS and entry.script.strip() not in inits_by_script
            else None,
            "script": _clip_script(entry.script)[0],
        }
        for entry in runtime_init_order(profile.init_scripts)
    ]
    sources = []
    for source_name, spec in sorted(profile.source_builds.items()):
        pin = img.keyed(name, spec).pin_from(pins)
        sources.append(
            {
                "name": source_name,
                "kind": spec.source.kind,
                "url": spec.source.url,
                "ref": spec.source.requested,
                "pin": pin,
                "build": spec.build.kind,
                "install": [install.dest for install in spec.install],
            }
        )
    kernel = img.kernel_for(name)
    kernel_source = img.kernel_source(name)
    return {
        "hooks": hooks,
        "init": init,
        "sources": sources,
        "kernel": None
        if kernel is None
        else {
            "version": kernel.version,
            "cmdline": kernel.cmdline,
            "tdx": kernel.tdx,
            "source": None if kernel_source is None else kernel_source.source.url,
            "ref": None if kernel_source is None else kernel_source.source.requested,
            "pin": None if kernel_source is None else kernel_source.pin_from(pins),
        },
    }


# ── rendering ────────────────────────────────────────────────────────────


_PAGE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Explore: $title</title>
<style>
body { font: 14px/1.5 system-ui, sans-serif; margin: 0 auto; max-width: 72rem;
  padding: 1rem; color: #1b1f24; background: #fff; }
h1, h2, h3, h4 { line-height: 1.25; }
h4 { margin: 1rem 0 .25rem; font-family: ui-monospace, monospace; font-size: 13px; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0 1rem; display: block;
  overflow-x: auto; }
th, td { border: 1px solid #d0d7de; padding: .3rem .5rem; text-align: left; vertical-align: top; }
th { background: #f6f8fa; }
code, pre { font-family: ui-monospace, monospace; font-size: 12px; }
code { word-break: break-all; }
pre { background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px; padding: .5rem;
  overflow-x: auto; white-space: pre; max-height: 32rem; }
nav { display: flex; flex-wrap: wrap; gap: .25rem; border-bottom: 1px solid #d0d7de;
  margin-bottom: 1rem; }
nav a { padding: .4rem .8rem; text-decoration: none; color: #0969da; border-radius: 6px 6px 0 0; }
nav a.on { background: #f6f8fa; border: 1px solid #d0d7de; border-bottom: none; color: #1b1f24; }
.js .tab { display: none; }
.js .tab.on { display: block; }
.tools { display: none; }
.js .tools { display: flex; }
.badge { display: inline-block; padding: 0 .4rem; border-radius: 4px; font-size: 12px; }
.present, .current, .matched, .verified { background: #dafbe1; }
.added { background: #ddf4ff; }
.edited, .warning, .drifted, .different { background: #fff8c5; }
.removed, .error, .missing { background: #ffebe9; }
.absent { color: #8c959f; }
.info { background: #eaeef2; }
.meta { color: #57606a; font-size: 12px; }
.by { font-size: 12px; }
details.file { border-bottom: 1px solid #eaeef2; padding: .15rem 0; }
details > summary { cursor: pointer; }
:target, .hit { outline: 2px solid #bf8700; outline-offset: 1px; }
.tools { flex-wrap: wrap; gap: 1rem; align-items: center; margin: .5rem 0; }
input[type=search] { padding: .3rem .5rem; min-width: 18rem; border: 1px solid #d0d7de;
  border-radius: 6px; }
.hidden { display: none; }
td.cell { text-align: center; white-space: nowrap; }
</style>
</head>
<body>
<h1>Explore: <code>$title</code></h1>
<p class="meta">$subtitle</p>
<nav>
<a href="#overview">Overview</a>
<a href="#variants">Variants</a>
<a href="#tree">Emitted tree</a>
<a href="#scripts">Hooks &amp; builds</a>
</nav>
<section class="tab" id="overview">
<h2>Overview</h2>
$overview
</section>
<section class="tab" id="variants">
<h2>Variants</h2>
$variants
</section>
<section class="tab" id="tree">
<h2>Emitted tree</h2>
$tree
</section>
<section class="tab" id="scripts">
<h2>Hooks &amp; builds</h2>
$scripts
</section>
<script>
$script
</script>
</body>
</html>
"""
)

_SCRIPT = """\
(function () {
  "use strict";
  var root = document.documentElement;
  root.classList.add("js");
  var tabs = Array.prototype.slice.call(document.querySelectorAll("section.tab"));
  var links = Array.prototype.slice.call(document.querySelectorAll("nav a"));
  function show(id) {
    var target = id ? document.getElementById(id) : null;
    var section = target ? target.closest("section.tab") : null;
    if (!section) { section = tabs[0]; target = null; }
    tabs.forEach(function (t) { t.classList.toggle("on", t === section); });
    links.forEach(function (a) {
      a.classList.toggle("on", a.getAttribute("href") === "#" + section.id);
    });
    if (target && target !== section) {
      for (var node = target; node; node = node.parentElement) {
        if (node.tagName === "DETAILS") { node.open = true; }
        if (node.classList) { node.classList.remove("hidden"); }
      }
      var inner = target.querySelector("details");
      if (inner) { inner.open = true; }
      target.scrollIntoView({ block: "start" });
    }
  }
  window.addEventListener("hashchange", function () { show(location.hash.slice(1)); });
  show(location.hash.slice(1));
  function all(selector) {
    return Array.prototype.slice.call(document.querySelectorAll(selector));
  }
  var files = all("details.file");
  var dirs = all("div.dir");
  var rows = all("tr.decl");
  var pathBox = document.getElementById("path-filter");
  var declBox = document.getElementById("decl-filter");
  var differs = document.getElementById("differs");
  function filterPaths() {
    var wanted = pathBox.value.toLowerCase();
    files.forEach(function (f) {
      f.classList.toggle("hidden", f.dataset.path.toLowerCase().indexOf(wanted) < 0);
    });
    dirs.forEach(function (d) {
      d.classList.toggle("hidden", !d.querySelector("details.file:not(.hidden)"));
    });
  }
  function filterRows() {
    var wanted = declBox.value.toLowerCase();
    var only = differs && differs.checked;
    rows.forEach(function (r) {
      var shown = r.dataset.text.toLowerCase().indexOf(wanted) >= 0;
      r.classList.toggle("hidden", !shown || (only && r.dataset.differs !== "1"));
    });
  }
  if (pathBox) { pathBox.addEventListener("input", filterPaths); }
  if (declBox) { declBox.addEventListener("input", filterRows); }
  if (differs) { differs.addEventListener("change", filterRows); }
})();
"""


def _e(value: object) -> str:
    return html.escape("-" if value is None else str(value), quote=True)


def _c(value: object) -> str:
    return f"<code>{_e(value)}</code>"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """A table of already-escaped cells."""
    head = "".join(f"<th>{_e(h)}</th>" for h in header)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _pairs(mapping: Mapping[str, object]) -> str:
    return _table(("key", "value"), [(_e(k), _c(v)) for k, v in mapping.items()])


def _badge(state: object, text: object = None) -> str:
    return f'<span class="badge {_e(state)}">{_e(state if text is None else text)}</span>'


def _origin_text(origin: Mapping[str, Any]) -> str:
    level = str(origin["level"])
    where = level_name(level)
    via = f" via {origin['via']}" if origin["via"] else ""
    edited = f"{origin['fields']} " if origin.get("fields") else ""
    at = f" at {origin['location']}" if origin.get("location") else ""
    return f"{edited}{origin['action']}{at} {where}{via}"


class _Anchors:
    """Stable ``id`` values: ``d<n>`` per declaration, ``f<n>`` per tree file."""

    def __init__(self, model: Mapping[str, Any]) -> None:
        self.declarations = {
            str(row["declaration"]): f"d{index}" for index, row in enumerate(model["declarations"])
        }
        self.files: dict[tuple[str, str], str] = {}
        for group, tree in model["trees"].items():
            for record in tree["files"]:
                self.files[(group, str(record["path"]))] = f"f{len(self.files)}"

    def declaration(self, name: str) -> str:
        anchor = self.declarations.get(name)
        return _c(name) if anchor is None else f'<a href="#{anchor}">{_c(name)}</a>'

    def file(self, group: str, path: str, label: str | None = None) -> str:
        anchor = self.files.get((group, path))
        text = _c(label or path)
        return text if anchor is None else f'<a href="#{anchor}">{text}</a>'


def render_html(data: Mapping[str, Any]) -> str:
    """The exploration *data* (:meth:`Exploration.to_dict`) as one self-contained page."""
    anchors = _Anchors(data)
    recipe = data["recipe"]
    title = str(recipe["name"] or "lowered recipe")
    subtitle = (
        f"{recipe['base']} · {recipe['arch']} · {len(recipe['variants'])} variant(s): "
        f"{', '.join(recipe['variants'])} · recipe digest {str(recipe['digest'] or '-')[:12]}"
    )
    return _PAGE.substitute(
        title=_e(title),
        subtitle=_e(subtitle),
        overview=_overview_html(data),
        variants=_variants_html(data, anchors),
        tree=_tree_html(data, anchors),
        scripts=_scripts_html(data, anchors),
        script=_SCRIPT,
    )


def _overview_html(data: Mapping[str, Any]) -> str:
    recipe = data["recipe"]
    lint = data["lint"]
    lock = data["lock"]
    parts = ["<h3>Recipe</h3>", _pairs({**recipe, "variants": ", ".join(recipe["variants"])})]
    parts.append(
        f"<h3>Lint</h3><p>{_badge('error', f'{lint["errors"]} error(s)')} "
        f"{_badge('warning', f'{lint["warnings"]} warning(s)')} "
        f"{_badge('info', f'{lint["infos"]} info')}</p>"
    )
    if lint["findings"]:
        parts.append(
            _table(
                ("severity", "code", "variant", "location", "subject", "message", "hint"),
                [
                    (
                        _badge(d["severity"]),
                        _c(d["code"]),
                        _c(d["variant"] or "common"),
                        _c(d["location"] or None),
                        _c(d["subject"] or None),
                        _e(d["message"]),
                        _e(d["hint"]),
                    )
                    for d in lint["findings"]
                ],
            )
        )
    parts.append(f"<h3>Lockfile</h3><p>{_badge(lock['state'])} {_c(lock['source'])}</p>")
    if lock["state"] != "missing":
        parts.append(
            _pairs(
                {
                    "recipe digest": lock["recipe_digest"],
                    "compiler version": lock["compiler_version"],
                }
            )
        )
        parts.append(
            _table(("section", "digest"), [(_c(k), _c(v)) for k, v in lock["sections"].items()])
        )
        if lock["pins"]:
            parts.append("<h4>Source pins</h4>")
            parts.append(
                _table(
                    ("source", "kind", "url", "pin"),
                    [
                        (_c(p["identity"]), _e(p["kind"]), _c(p["url"]), _c(p["digest"]))
                        for p in lock["pins"]
                    ],
                )
            )
    if lock["drift"]:
        parts.append(
            "<h4>Drift</h4><ul>"
            + "".join(
                f"<li>{_c(d['subject'] or d['code'])}: {_e(d['message'])}</li>"
                for d in lock["drift"]
            )
            + "</ul>"
        )
    bake = data["bake"]
    parts.append("<h3>Bake</h3>")
    if bake is None:
        parts.append("<p>No bake in the build directory.</p>")
    else:
        parts.append(
            _pairs(
                {
                    "build": bake["build"],
                    "backend": bake["backend"],
                    "simulated": bake["simulated"],
                    "reproducibility": bake["reproducibility"] or "not checked",
                    "recipe digest": bake["recipe_digest"],
                }
            )
        )
        parts.append(
            _table(
                ("variant", "target", "path", "sha256", "reproducibility", "simulated"),
                [
                    (
                        _c(a["variant"]),
                        _c(a["target"]),
                        _c(a["path"]),
                        _c(a["sha256"]),
                        _badge(
                            a["reproducibility"] or "absent", a["reproducibility"] or "not checked"
                        ),
                        _e(a["simulated"]),
                    )
                    for a in bake["artifacts"]
                ],
            )
        )
    if data["notes"]:
        parts.append(
            "<h3>Notes</h3><ul>" + "".join(f"<li>{_e(n)}</li>" for n in data["notes"]) + "</ul>"
        )
    return "\n".join(parts)


_MARKS = {"present": "●", "added": "+", "edited": "~", "removed": "−", "absent": "·"}


def _variants_html(data: Mapping[str, Any], anchors: _Anchors) -> str:
    variants = [str(v["name"]) for v in data["variants"]]
    parts = [
        _table(
            ("variant", "inherits", "outputs", "declarations", "fragments"),
            [
                (
                    _c(v["name"]),
                    _c(v["inherits"]),
                    _e(", ".join(v["outputs"])),
                    _e(v["declarations"]),
                    _e(", ".join(v["fragments"])),
                )
                for v in data["variants"]
            ],
        )
    ]
    rows = data["declarations"]
    if not rows:
        return "\n".join([*parts, "<p>No declarations (a lowered recipe).</p>"])
    legend = " ".join(_badge(state, f"{_MARKS[state]} {state}") for state in STATES)
    parts.append(
        '<div class="tools"><input type="search" id="decl-filter" '
        'placeholder="Filter declarations" aria-label="Filter declarations">'
        '<label><input type="checkbox" id="differs"> only declarations that differ</label>'
        f"</div><p>{legend}</p>"
    )
    head = "".join(f"<th>{_e(name)}</th>" for name in variants)
    body: list[str] = []
    for row in rows:
        name = str(row["declaration"])
        cells = row["variants"]
        states = [str(cells[v]["state"]) for v in variants]
        differs = len(set(states)) > 1 or any(cells[v]["changed"] for v in variants)
        cell_html = "".join(
            f'<td class="cell">{_badge(s, _MARKS[s])}'
            + (
                f'<br><span class="meta">{_e(", ".join(cells[v]["changed"]))}</span>'
                if cells[v]["changed"]
                else ""
            )
            + "</td>"
            for v, s in zip(variants, states, strict=True)
        )
        detail = _declaration_detail(row, variants, anchors)
        text = _e(f"{name} {' '.join(states)}")
        body.append(
            f'<tr class="decl" id="{anchors.declarations[name]}" data-differs="{int(differs)}" '
            f'data-text="{text}"><td><details><summary>{_c(name)}</summary>{detail}</details></td>'
            f"{cell_html}</tr>"
        )
    parts.append(
        f"<table><thead><tr><th>declaration</th>{head}</tr></thead><tbody>{''.join(body)}"
        "</tbody></table>"
    )
    return "\n".join(parts)


def _declaration_detail(row: Mapping[str, Any], variants: Sequence[str], anchors: _Anchors) -> str:
    fields_html = (
        "<ul>" + "".join(f"<li>{_e(k)}: {_c(v)}</li>" for k, v in row["fields"].items()) + "</ul>"
        if row["fields"]
        else ""
    )
    lines: list[str] = []
    for name in variants:
        cell = row["variants"][name]
        origins = "; ".join(_origin_text(o) for o in cell["origins"]) or "not in this variant"
        files = ", ".join(anchors.file(f["group"], f["path"]) for f in cell["files"])
        generated = "".join(f"<li>{_c(g)}</li>" for g in cell["generated"])
        lines.append(
            f"<li><strong>{_e(name)}</strong> {_badge(cell['state'])}: {_e(origins)}"
            + (f"<br>files: {files}" if files else "")
            + (f"<ul>{generated}</ul>" if generated else "")
            + "</li>"
        )
    return f'<p class="meta">{_e(row["type"])}</p>{fields_html}<ul>{"".join(lines)}</ul>'


def _tree_html(data: Mapping[str, Any], anchors: _Anchors) -> str:
    trees = data["trees"]
    if not trees:
        return "<p>The tree was not compiled (see the notes in the overview).</p>"
    declared = {str(row["declaration"]): row for row in data["declarations"]}
    parts = [
        '<div class="tools"><input type="search" id="path-filter" '
        'placeholder="Filter paths" aria-label="Filter paths"></div>'
    ]
    for group, tree in trees.items():
        files = tree["files"]
        parts.append(f'<h3>{_e(group)} <span class="meta">{len(files)} entries</span></h3>')
        by_dir: dict[str, list[Mapping[str, Any]]] = {}
        for record in files:
            by_dir.setdefault(posixpath.dirname(str(record["path"])), []).append(record)
        for directory, records in by_dir.items():
            parts.append(f'<div class="dir"><h4>{_e(directory or ".")}/</h4>')
            parts.extend(_file_html(group, r, anchors, declared) for r in records)
            parts.append("</div>")
    return "\n".join(parts)


def _file_html(
    group: str, record: Mapping[str, Any], anchors: _Anchors, declared: Mapping[str, Any]
) -> str:
    path = str(record["path"])
    anchor = anchors.files[(group, path)]
    kind = record["type"]
    meta = [str(record["mode"])]
    if kind == "file":
        meta += [f"{record['size']} B", f"sha256 {str(record['sha256'])[:12]}"]
    elif kind == "symlink":
        meta.append(f"-> {record['target']}")
    else:
        meta.append("directory")
    names = [str(n) for n in record.get("declarations", ())]
    by = ", ".join(anchors.declaration(n) for n in names[:6])
    if len(names) > 6:
        by += f" and {len(names) - 6} more"
    summary = (
        f'<summary>{_c(posixpath.basename(path))} <span class="meta">{_e(" · ".join(meta))}'
        f"</span>{f' <span class=by>declared by {by}</span>' if by else ''}</summary>"
    )
    why = "".join(
        f"<li>declared by {anchors.declaration(n)}: "
        f"{_e('; '.join(_origin_text(o) for o in _origins_of(declared.get(n), group)))}</li>"
        for n in names
    )
    body = [
        f'<p class="meta">{_c(path)}'
        + (f" → image path {_c(record['image_path'])}" if record.get("image_path") else "")
        + "</p>"
    ]
    if why:
        body.append(f"<ul>{why}</ul>")
    elif kind == "file":
        body.append('<p class="meta">generated by the compiler; no declaration names it</p>')
    if kind == "file":
        if record["binary"]:
            body.append(f'<p class="meta">binary file, {record["size"]} bytes: not shown</p>')
        elif record["text"] is None:
            body.append('<p class="meta">content left out: the page\'s content budget is spent</p>')
        else:
            if record["truncated"]:
                body.append(
                    f'<p class="meta">truncated: showing the first {TEXT_LIMIT // 1024} KiB '
                    f"of {record['size']} bytes</p>"
                )
            body.append(f"<pre>{_e(record['text'])}</pre>")
    return (
        f'<details class="file" id="{anchor}" data-path="{_e(path)}">{summary}'
        f"{''.join(body)}</details>"
    )


def _origins_of(row: Mapping[str, Any] | None, group: str) -> list[Mapping[str, Any]]:
    if row is None:
        return []
    cell = row["variants"].get(group)
    if cell is None:
        cell = next(iter(row["variants"].values()), None)
    return [] if cell is None else list(cell["origins"])


def _scripts_html(data: Mapping[str, Any], anchors: _Anchors) -> str:
    scripts = data["scripts"]
    if not scripts:
        return "<p>Not compiled (see the notes in the overview).</p>"
    parts: list[str] = []
    for name, entry in scripts.items():
        parts.append(f"<h3>{_e(name)}</h3><h4>Hooks, in stage order</h4>")
        if not entry["hooks"]:
            parts.append("<p>none</p>")
        for hook in entry["hooks"]:
            label = (
                anchors.declaration(hook["declaration"])
                if hook["declaration"]
                else '<span class="meta">generated by the compiler</span>'
            )
            more = '<p class="meta">truncated</p>' if hook["truncated"] else ""
            first = str(hook["script"]).strip().splitlines()[:1]
            parts.append(
                f"<details><summary>{_badge('info', hook['phase'])} {label} "
                f'<span class="meta">{_e(first[0][:100] if first else "")}</span></summary>'
                f"{more}<pre>{_e(hook['script'])}</pre></details>"
            )
        parts.append("<h4>Runtime init, in order</h4>")
        if not entry["init"]:
            parts.append("<p>none</p>")
        for step in entry["init"]:
            label = (
                anchors.declaration(step["declaration"])
                if step["declaration"]
                else _c(step["builtin"] or "generated")
            )
            parts.append(
                f"<details><summary>{_badge('info', step['step'])} {label}</summary>"
                f"<pre>{_e(step['script'])}</pre></details>"
            )
        parts.append("<h4>Source builds</h4>")
        if entry["sources"]:
            parts.append(
                _table(
                    ("name", "build", "kind", "url", "ref", "pin", "installs"),
                    [
                        (
                            _c(s["name"]),
                            _e(s["build"]),
                            _e(s["kind"]),
                            _c(s["url"]),
                            _c(s["ref"]),
                            _c(s["pin"] or "unpinned"),
                            _c(", ".join(s["install"])),
                        )
                        for s in entry["sources"]
                    ],
                )
            )
        else:
            parts.append("<p>none</p>")
        kernel = entry["kernel"]
        if kernel is not None:
            parts.append("<h4>Kernel</h4>" + _pairs(kernel))
    return "\n".join(parts)


__all__ = [
    "EXPLORE_FORMATS",
    "EXPLORE_SCHEMA",
    "PAGE_NAME",
    "TEXT_LIMIT",
    "Exploration",
    "explore",
    "render_html",
]
