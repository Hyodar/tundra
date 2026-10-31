"""``show``: the resolved image or an existing build, and ``show --diff`` between two builds.

For an image file :func:`image_report` is the dry run (every profile's packages, files,
units, sources, kernel, what each declaration came from), :func:`image_units` the unit
files its tree holds and :func:`image_tree` the tree's files. For a build directory
:func:`build_report` is its manifest read back with its provenance (inputs, toolchain,
observations) and every guarantee the build does not give; ``--units``, ``--tree`` and
``--measurements`` read the build's own ``tree/`` and ``measurements.json``.

:func:`diff_builds` compares two builds by what an operator approves: packages (mkosi's
manifest), files and units (the trees), the boot payload (the UKI and the root hash), the
measurements per target and boot path, and the policy coverage (which registers each
side cannot predict).
"""

from __future__ import annotations

import json
import stat
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import manifest as mf
from ._describe import describe, render
from ._location import display_location
from .builds import Build
from .declarative._lowered import Lowered
from .declarative.lifecycle import Lock, compile_image
from .declarative.model import Recipe
from .declarative.resolve import describe as describe_identity
from .declarative.resolve import provenance as resolved_provenance
from .diff import diff_trees

UNIT_DIRS = ("usr/lib/systemd/system/", "etc/systemd/system/")


# ── an image ─────────────────────────────────────────────────────────────


def image_report(
    img: Lowered, profiles: Sequence[str], described: Mapping[str, dict[str, object]] | None = None
) -> dict[str, object]:
    """The dry-run description of *profiles* (*described* holds the ones already made): the
    recipe digest the lockfile records and one entry per profile."""
    given = described or {}
    return {
        "image": img.image_id,
        "digest": img.select(profiles).digest(),
        "variants": {name: given.get(name) or describe(img, profile=name) for name in profiles},
    }


def provenance(recipe: Recipe, profiles: Sequence[str]) -> dict[str, list[dict[str, object]]]:
    """Per profile, every declaration its resolution touched, with each step that touched it
    (declared, added, edited, removed), the fragment chain and the ``file:line``."""
    found: dict[str, list[dict[str, object]]] = {}
    declared = {variant.name for variant in recipe.variants}
    for name in profiles:
        if name not in declared:
            continue
        found[name] = [
            {
                "declaration": describe_identity(key),
                "origins": [
                    {
                        "action": origin.action,
                        "variant": origin.variant,
                        "fragments": [f for f, _ in origin.fragments],
                        "fields": list(origin.fields),
                        "location": display_location(origin.location),
                    }
                    for origin in origins
                ],
            }
            for key, origins in sorted(
                resolved_provenance(recipe, variant=name).items(),
                key=lambda kv: describe_identity(kv[0]),
            )
        ]
    return found


def render_image(report: Mapping[str, Any]) -> str:
    return "\n\n".join(render(d).rstrip() for d in report["variants"].values())


def image_units(img: Lowered, profiles: Sequence[str], locked: Lock | None) -> dict[str, str]:
    """Every unit file (and drop-in) the tree of *profiles* holds, by tree path."""
    tree = compile_image(img, profiles, locked=locked)
    return {
        entry.path: entry.content.decode("utf-8", errors="replace")
        for entry in tree.entries
        if entry.content is not None and entry.symlink is None and _is_unit(entry.path)
    }


def image_tree(
    img: Lowered, profiles: Sequence[str], locked: Lock | None
) -> list[dict[str, object]]:
    tree = compile_image(img, profiles, locked=locked)
    return [
        {
            "path": entry.path,
            "mode": f"{entry.mode:04o}",
            "size": None if entry.content is None else len(entry.content),
            "symlink": entry.symlink,
        }
        for entry in tree.entries
    ]


def _is_unit(path: str) -> bool:
    return any(f"/{d}" in f"/{path}" for d in UNIT_DIRS) and not path.endswith("/")


# ── a build ──────────────────────────────────────────────────────────────


def _json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def build_report(build: Build) -> dict[str, object]:
    """The manifest with the observations and the signature's presence (not its check)."""
    return {
        "path": str(build.path),
        "manifest": dict(build.manifest),
        "observations": _json(build.path / mf.OBSERVATIONS),
        "signed": (build.path / mf.SIGNATURE).is_file(),
    }


def render_build(build: Build) -> str:
    m = build.manifest
    lines = [
        f"build {build.id}  {m['image']} {m['profile']} ({m['kind']})",
        f"  path          {build.path}",
        f"  targets       {', '.join(m['targets'])}",
        f"  rootfs        {m.get('rootfs')}",
    ]
    uki = m.get("uki") or {}
    if "sha256" in uki:
        lines.append(f"  uki           payload.efi {str(uki['sha256'])[:16]}")
    if m.get("root_hash"):
        lines.append(f"  root hash     {m['root_hash']}")
    lines.append(f"  recipe        {str(m.get('recipe_digest'))[:16]}")
    lock = m.get("lock") or {}
    tree = m.get("tree") or {}
    lines.append(f"  lock          {lock.get('path')} {str(lock.get('sha256'))[:16]}")
    lines.append(f"  tree          {str(tree.get('digest', ''))[:23]} ({tree.get('files')} files)")
    for key in ("packages", "sbom", "summary"):
        part = m.get(key) or {}
        shown = (
            part.get("path") if part.get("state") != "missing" else f"missing: {part.get('reason')}"
        )
        lines.append(f"  {key:<13} {shown}")
    lines.append("  artifacts")
    for name, record in sorted((m.get("artifacts") or {}).items()):
        if name.startswith(f"{mf.INPUTS}/"):
            continue
        lines.append(
            f"    {name:<22} {str(record.get('sha256', ''))[:16]}  {record.get('size', '')}"
        )
    lines.append("  measurements")
    for name, state in sorted(((m.get("measurements") or {}).get("records") or {}).items()):
        lines.append(f"    {name:<22} {state}")
    toolchain = m.get("toolchain") or {}
    lines.append(
        f"  toolchain     tundravm {toolchain.get('tundravm')}, backend {toolchain.get('backend')}"
    )
    observed = _json(build.path / mf.OBSERVATIONS) or {}
    if observed:
        runner = observed.get("runner") or {}
        lines.append(f"  built         {observed.get('finished_at')} on {runner.get('hostname')}")
        reproduction = observed.get("reproduction")
        state = reproduction.get("state") if isinstance(reproduction, dict) else "not attempted"
        lines.append(f"  reproduction  {state}")
    signed = (build.path / mf.SIGNATURE).is_file()
    shown = "yes (manifest.sig; `tundravm verify` checks it)" if signed else "no"
    lines.append(f"  signed        {shown}")
    unchecked = m.get("unchecked") or []
    if unchecked:
        lines.append("  unchecked")
        lines.extend(f"    - {item}" for item in unchecked)
    return "\n".join(lines)


def build_units(build: Build) -> dict[str, str]:
    root = build.path / mf.TREE
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_file() and not path.is_symlink() and _is_unit(rel):
            found[rel] = path.read_text(encoding="utf-8", errors="replace")
    return found


def build_tree(build: Build) -> list[dict[str, object]]:
    root = build.path / mf.TREE
    found: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        found.append(
            {
                "path": path.relative_to(root).as_posix(),
                "mode": f"{stat.S_IMODE(info.st_mode):04o}",
                "size": None if path.is_dir() or path.is_symlink() else info.st_size,
                "symlink": str(path.readlink()) if path.is_symlink() else None,
            }
        )
    return found


def build_measurements(build: Build) -> dict[str, Any]:
    return mf.read_json(build.path / mf.MEASUREMENTS, "measurements.json")


def render_measurements(payload: Mapping[str, Any]) -> str:
    lines: list[str] = []
    for record in mf.records_of(payload):
        head = f"{record['target']}/{record['boot']}  {record['artifact']}  {record['state']}"
        lines.append(head)
        if record["state"] != "predicted":
            lines.append(f"  {record.get('reason', '')}")
            continue
        lines.append(f"  tool     {record.get('tool')}")
        for name, values in sorted((record.get("registers") or {}).items()):
            shown = (
                values[0]
                if len(values) == 1 and isinstance(values[0], str)
                else f"{len(values)} accepted values"
            )
            lines.append(f"  {name:<8} {shown}")
        for name, why in sorted((record.get("unknowns") or {}).items()):
            lines.append(f"  {name:<8} unknown: {why}")
        lines.append(f"  RTMR3    {record.get('rtmr3')}")
    return "\n".join(lines)


def render_files(rows: Iterable[Mapping[str, object]]) -> str:
    lines = []
    for row in rows:
        link = f" -> {row['symlink']}" if row.get("symlink") else ""
        size = "" if row.get("size") is None else f"  {row['size']}"
        lines.append(f"{row['mode']}  {row['path']}{link}{size}")
    return "\n".join(lines)


def render_units(units: Mapping[str, str]) -> str:
    return "\n".join(
        f"# {path}\n{text.rstrip()}\n" for path, text in sorted(units.items())
    ).rstrip()


# ── show --diff ──────────────────────────────────────────────────────────


def _packages(build: Build) -> dict[str, str]:
    payload = _json(build.path / mf.PACKAGES) or {}
    found: dict[str, str] = {}
    for package in payload.get("packages", []):
        if isinstance(package, dict) and package.get("name"):
            found[str(package["name"])] = str(package.get("version", ""))
    return found


def _registers(build: Build) -> dict[str, dict[str, Any]]:
    payload = _json(build.path / mf.MEASUREMENTS) or {}
    return {f"{r['target']}/{r['boot']}": dict(r) for r in mf.records_of(payload)}


def diff_builds(old: Build, new: Build) -> dict[str, object]:
    """What changed from *old* to *new*, section by section (empty sections included)."""
    a, b = _packages(old), _packages(new)
    packages = {
        "added": {n: b[n] for n in sorted(set(b) - set(a))},
        "removed": {n: a[n] for n in sorted(set(a) - set(b))},
        "changed": {n: [a[n], b[n]] for n in sorted(set(a) & set(b)) if a[n] != b[n]},
        "known": bool(a or b),
    }
    tree = diff_trees(old.path / mf.TREE, new.path / mf.TREE)
    files = [{"path": c.path, "status": c.status} for c in tree.changes]
    units = [f for f in files if _is_unit(str(f["path"]))]
    boot = {
        name: [
            (old.manifest.get(key) or {}).get("sha256") if key == "uki" else old.manifest.get(key),
            (new.manifest.get(key) or {}).get("sha256") if key == "uki" else new.manifest.get(key),
        ]
        for name, key in (("uki", "uki"), ("root_hash", "root_hash"), ("cmdline", "uki"))
    }
    boot["cmdline"] = [
        (old.manifest.get("uki") or {}).get("cmdline"),
        (new.manifest.get("uki") or {}).get("cmdline"),
    ]
    ra, rb = _registers(old), _registers(new)
    measurements: dict[str, object] = {}
    coverage: dict[str, object] = {}
    for name in sorted({*ra, *rb}):
        left, right = ra.get(name, {}), rb.get(name, {})
        regs = sorted({*(left.get("registers") or {}), *(right.get("registers") or {})})
        changed = {
            reg: [(left.get("registers") or {}).get(reg), (right.get("registers") or {}).get(reg)]
            for reg in regs
            if (left.get("registers") or {}).get(reg) != (right.get("registers") or {}).get(reg)
        }
        if changed or left.get("state") != right.get("state"):
            measurements[name] = {
                "state": [left.get("state"), right.get("state")],
                "registers": changed,
            }
        unknown_a, unknown_b = set(left.get("unknowns") or {}), set(right.get("unknowns") or {})
        if unknown_a != unknown_b:
            coverage[name] = {
                "now_unknown": sorted(unknown_b - unknown_a),
                "now_predicted": sorted(unknown_a - unknown_b),
            }
    return {
        "old": {"id": old.id, "path": str(old.path)},
        "new": {"id": new.id, "path": str(new.path)},
        "packages": packages,
        "files": files,
        "units": units,
        "boot": {k: v for k, v in boot.items() if v[0] != v[1]},
        "measurements": measurements,
        "coverage": coverage,
    }


def render_diff(diff: Mapping[str, Any]) -> str:
    lines = [f"{diff['old']['id']} -> {diff['new']['id']}"]
    packages = diff["packages"]
    if not packages["known"]:
        lines.append("packages: unknown (neither build has a package manifest)")
    else:
        count = sum(len(packages[k]) for k in ("added", "removed", "changed"))
        lines.append(f"packages: {count} changed")
        lines += [f"  + {n} {v}" for n, v in packages["added"].items()]
        lines += [f"  - {n} {v}" for n, v in packages["removed"].items()]
        lines += [f"  ~ {n} {a} -> {b}" for n, (a, b) in packages["changed"].items()]
    codes = {"added": "+", "removed": "-", "modified": "~", "mode": "m"}
    lines.append(f"files: {len(diff['files'])} changed")
    lines += [f"  {codes[f['status']]} {f['path']}" for f in diff["files"]]
    lines.append(f"units: {len(diff['units'])} changed")
    lines += [f"  {codes[f['status']]} {f['path']}" for f in diff["units"]]
    lines.append(f"boot payload: {'unchanged' if not diff['boot'] else ', '.join(diff['boot'])}")
    for key, (a, b) in diff["boot"].items():
        lines.append(f"  {key}: {a} -> {b}")
    lines.append(
        f"measurements: {'unchanged' if not diff['measurements'] else len(diff['measurements'])}"
    )
    for name, change in diff["measurements"].items():
        lines.append(f"  {name}: {change['state'][0]} -> {change['state'][1]}")
        for reg, (a, b) in change["registers"].items():
            lines.append(f"    {reg}: {a} -> {b}")
    lines.append(
        f"policy coverage: {'unchanged' if not diff['coverage'] else len(diff['coverage'])}"
    )
    for name, change in diff["coverage"].items():
        if change["now_unknown"]:
            lines.append(f"  {name}: no longer predicted: {', '.join(change['now_unknown'])}")
        if change["now_predicted"]:
            lines.append(f"  {name}: now predicted: {', '.join(change['now_predicted'])}")
    return "\n".join(lines)


__all__ = [
    "build_measurements",
    "build_report",
    "build_tree",
    "build_units",
    "diff_builds",
    "image_report",
    "image_tree",
    "image_units",
    "render_build",
    "render_diff",
    "render_files",
    "render_image",
    "render_measurements",
    "render_units",
]
