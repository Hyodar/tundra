"""``show``: the resolved image or an existing build, and ``show --diff`` between two builds.

For an image file :func:`image_report` is the dry run (the image and its file, then each
profile's packages, files, units, sources, kernel, what each declaration came from, the
default profile first and the others as declared), :func:`image_units` the unit
files its tree holds and :func:`image_tree` the tree's files. For a build directory
:func:`build_report` is its manifest read back with its provenance (inputs, toolchain,
observations) and every guarantee the build does not give, led by :func:`report`, what
``build`` prints at its end; ``--units``, ``--tree`` and ``--measurements`` read the build's
own ``tree/`` and ``measurements.json``.

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
from ._reporting import format_duration, format_size
from .backends.base import ARTIFACT_NAMES, BOOT_PAYLOAD_NAME
from .builds import Build
from .compiler.emit_mkosi import ESP_BYTES, UKI_CAPACITY
from .declarative._lowered import Lowered
from .declarative.lifecycle import Lock, compile_image
from .declarative.lower import declared_firewall
from .declarative.model import Recipe
from .declarative.resolve import COMMON_LEVEL
from .declarative.resolve import describe as describe_identity
from .declarative.resolve import provenance as resolved_provenance
from .diff import diff_trees

UNIT_DIRS = ("usr/lib/systemd/system/", "etc/systemd/system/")


# ── an image ─────────────────────────────────────────────────────────────


def image_report(
    img: Lowered,
    profiles: Sequence[str],
    described: Mapping[str, dict[str, object]] | None = None,
    *,
    file: Path | None = None,
) -> dict[str, object]:
    """The dry-run description of *profiles* (*described* holds the ones already made): the
    image, the *file* it is declared in, the digest the lockfile records and one entry per
    profile, in the order given."""
    given = described or {}
    return {
        "image": img.image_id,
        "file": None if file is None else _shown(file),
        "digest": img.select(profiles).digest(),
        "profiles": {name: given.get(name) or describe(img, profile=name) for name in profiles},
    }


def provenance(recipe: Recipe, profiles: Sequence[str]) -> dict[str, list[dict[str, object]]]:
    """Per profile, every declaration its resolution touched, with each step that touched it
    (declared, added, edited, removed), the profile (``image`` for the image's own
    declarations), the component chain and the ``file:line``."""
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
                        "profile": "image" if origin.variant == COMMON_LEVEL else origin.variant,
                        "components": [f for f, _ in origin.fragments],
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
    """The image and its file on the first line, then one block per profile."""
    head = "  ".join(str(part) for part in (report["image"], report.get("file")) if part)
    blocks = "\n\n".join(render(d).rstrip() for d in report["profiles"].values())
    return f"{head}\n{blocks}"


def ordered_profiles(declared: Sequence[str], given: Sequence[str]) -> tuple[str, ...]:
    """*given* (every profile when empty), the default one first and the rest as declared."""
    return tuple(name for name in declared if not given or name in given)


def _shown(path: Path) -> str:
    """*path* relative to the working directory when it is under it."""
    try:
        return path.resolve().relative_to(Path.cwd()).as_posix()
    except ValueError:
        return str(path)


def image_units(img: Lowered, profiles: Sequence[str], locked: Lock | None) -> dict[str, str]:
    """Every unit file (and drop-in) the tree of *profiles* holds, by tree path."""
    tree = compile_image(img, profiles, locked=locked)
    return {
        entry.path: entry.content.decode("utf-8", errors="replace")
        for entry in tree.entries
        if entry.content is not None and entry.symlink is None and _is_unit(entry.path)
    }


def image_egress(recipe: Recipe, profile: str) -> dict[str, list[str]] | None:
    """What *profile*'s output chain accepts, by who asked for it (``image``, a component,
    a cloud); ``None`` when it does not filter."""
    variant = next((v for v in recipe.variants if v.name == profile), None)
    firewall = None if variant is None else declared_firewall(recipe, variant)
    if firewall is None or firewall.egress != "filter":
        return None
    found: dict[str, list[str]] = {}
    for rule in firewall.egress_rules:
        for who, name in rule.origins:
            found.setdefault(who, []).append(f"{rule} ({name})" if name else str(rule))
    return found


def render_egress(egress: Mapping[str, Sequence[str]] | None) -> str:
    if egress is None:
        return "# egress: not filtered (no Egress)"
    width = max(map(len, egress), default=0)
    rows = (f"{who:<{width}}  {', '.join(rules)}" for who, rules in egress.items())
    return "\n".join(["# egress", *rows])


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
    """The manifest with the observations, the signature's presence (not its check) and the
    build :func:`report`."""
    return {
        "path": str(build.path),
        "manifest": dict(build.manifest),
        "observations": _json(build.path / mf.OBSERVATIONS),
        "signed": (build.path / mf.SIGNATURE).is_file(),
        "report": report(build),
    }


REPORT_PHASES = ("checkouts", "prefetch", "mkosi", "measurements")
"""The phases the report times, in the order a build runs them; ``total`` follows."""


def report(build: Build) -> dict[str, Any]:
    """What *build* fetched, made and took, read from its manifest and ``observations.json``.

    ``artifacts``: the UKI and each target's disk with its size and sha256, the UKI with the
    room the fixed ESP leaves it. ``sources``: each locked source with its ref, pin and the
    Go or Rust toolchain its prefetch recorded, and whether this build fetched its checkout
    and dependencies or found them (``-`` where the build directory does not say). ``time``:
    seconds per phase and in total (``None`` for a phase that did not run).
    ``authenticode_sha384``: the UKI's digest RTMR1's event log records. ``measurements``:
    per record of ``measurements.json`` (target and boot path) one row per predicted MRTD or
    RTMR, a register with several accepted values (a firmware table) as their count, the
    platform registers paired by firmware on one row, and a record without a prediction
    as its state.
    """
    m = build.manifest
    observed = _json(build.path / mf.OBSERVATIONS) or {}
    found = _mapping(observed.get("sources"))
    index = m.get("artifacts") or {}
    names = [
        BOOT_PAYLOAD_NAME,
        *(ARTIFACT_NAMES[t] for t in m.get("targets", ()) if t in ARTIFACT_NAMES),
    ]
    artifacts: list[dict[str, Any]] = []
    for name in names:
        record = index.get(name)
        if not isinstance(record, dict) or "size" not in record:
            continue
        row: dict[str, Any] = {"name": name, "size": record["size"], "sha256": record["sha256"]}
        if name == BOOT_PAYLOAD_NAME:
            row["esp"] = {"size": ESP_BYTES, "left": UKI_CAPACITY - int(record["size"])}
        artifacts.append(row)
    sources = []
    for source in m.get("sources") or []:
        if not isinstance(source, dict):
            continue
        state = found.get(str(source.get("name"))) or {}
        sources.append(
            {
                "name": source.get("name"),
                "kind": source.get("kind"),
                "ref": source.get("ref"),
                "pin": source.get("pin"),
                "toolchain": _toolchain(source),
                "checkout": state.get("checkout"),
                "deps": _deps(state.get("deps")),
            }
        )
    phases = _mapping(observed.get("phases"))
    time = {name: phases.get(name) for name in REPORT_PHASES}
    time["total"] = observed.get("duration_s")
    measured = _json(build.path / mf.MEASUREMENTS) or {}
    return {
        "artifacts": artifacts,
        "sources": sources,
        "time": time,
        "authenticode_sha384": _mapping(m.get("uki")).get("authenticode_sha384"),
        "measurements": [row for r in mf.records_of(measured) for row in _measured(r)],
    }


TDX_REGISTERS = ("MRTD", "RTMR0", "RTMR1", "RTMR2")
"""The registers a record predicts; RTMR3 is outside every policy."""


def _measured(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The report rows of one ``measurements.json`` record."""
    base = {"target": record.get("target"), "boot": record.get("boot")}
    if record.get("state") != "predicted":
        reason = str(record.get("reason") or "").splitlines()[:1]
        note = f"{record.get('state')}: {reason[0]}" if reason else str(record.get("state"))
        return [{**base, "register": None, "value": None, "note": note}]
    registers = _mapping(record.get("registers"))
    single = [r for r in TDX_REGISTERS if len(registers.get(r) or ()) == 1]
    several = [r for r in TDX_REGISTERS if len(registers.get(r) or ()) > 1]
    rows = []
    if several:
        counts = "/".join(str(len(registers[r])) for r in several)
        note = f"{counts} accepted values, paired by firmware (show --measurements)"
        rows.append({**base, "register": "/".join(several), "value": None, "note": note})
    for name in single:
        (value,) = registers[name]
        shown = value.get("value") if isinstance(value, dict) else value
        rows.append({**base, "register": name, "value": str(shown), "note": None})
    if not rows:
        note = "no TDX register predicted (show --measurements)"
        rows.append({**base, "register": None, "value": None, "note": note})
    return rows


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _toolchain(source: Mapping[str, Any]) -> str | None:
    """The Go or Rust toolchain a source's prefetch recorded: the release (``go1.25.0``,
    ``1.91.0``), ``local``/``debian`` when its source pins none, ``missing`` without one."""
    for key in ("go_toolchain", "rust_toolchain"):
        record = source.get(key)
        if isinstance(record, dict):
            if record.get("state") == "missing":
                return "missing"
            return str(record.get("channel") or record.get("name"))
    return None


def _deps(outcome: object) -> str | None:
    """A dependency prefetch's outcome (``go: prefetched``, ``cargo: kept``, ``skipped (no
    host go)``...) as the report's ``fetched``, ``cached``, ``skipped``, ``incomplete`` or
    ``failed``."""
    if not isinstance(outcome, str):
        return None
    if outcome.startswith(("skipped", "failed")):
        return outcome.split(" ", 1)[0]
    if ": incomplete" in outcome:
        return "incomplete"
    return "cached" if outcome.endswith(": kept") else "fetched"


def render_report(found: Mapping[str, Any]) -> list[str]:
    """The :func:`report` as text: one row per artifact, the sources table, the time row,
    the UKI's Authenticode digest and the measurement rows (target, boot path, register,
    value)."""
    lines = []
    for row in found["artifacts"]:
        esp = row.get("esp")
        room = (
            f"  ESP {format_size(esp['size'])}, {format_size(max(esp['left'], 0))} left"
            if esp
            else ""
        )
        size = format_size(int(row["size"]))
        lines.append(f"  {row['name']:<12} {size:>10}  sha256 {str(row['sha256'])[:16]}{room}")
    columns = ("name", "kind", "ref", "pin", "toolchain", "checkout", "deps")
    rows = [
        [f"  {source['name']}", *(_cell(source, key) for key in columns[1:])]
        for source in found["sources"]
    ]
    if rows:
        table = [["source", *columns[1:]], *rows]
        widths = [max(len(cells[i]) for cells in table) for i in range(len(columns))]
        for cells in table:
            text = "  ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True))
            lines.append(text.rstrip())
    time = found["time"]
    cells = [
        f"{name} {'-' if time.get(name) is None else format_duration(float(time[name]))}"
        for name in (*REPORT_PHASES, "total")
    ]
    lines.append("time  " + "  ".join(cells))
    authenticode = found.get("authenticode_sha384")
    lines.append(
        f"measured  payload.efi authenticode sha384 {authenticode}" if authenticode else "measured"
    )
    for row in found.get("measurements") or ():
        value = row["value"] or row["note"]
        lines.append(f"  {row['target']:<6} {row['boot']:<5} {row['register'] or '-':<10} {value}")
    return lines


def _cell(source: Mapping[str, Any], key: str) -> str:
    """A sources-table cell: the pin, and a ref that is the pin itself (a commit), short."""
    value = source.get(key)
    if not value:
        return "-"
    short = key == "pin" or (key == "ref" and value == source.get("pin"))
    return str(value)[:12] if short else str(value)


def render_build(build: Build) -> str:
    m = build.manifest
    lines = [
        f"build {build.id}  {m['image']} {m['profile']} ({m['kind']})",
        f"  path          {build.path}",
        *render_report(report(build)),
        f"  targets       {', '.join(m['targets'])}",
        f"  rootfs        {m.get('rootfs')}"
        + (f", at most {m['rootfs_max_size']}" if m.get("rootfs_max_size") else ""),
    ]
    if m.get("root_hash"):
        lines.append(f"  root hash     {m['root_hash']}")
    lines.append(f"  digest        {str(m.get('recipe_digest'))[:16]}")
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
    probe = m.get("probe") or []
    if probe:
        lines.append(
            f"  boot probe    {len(probe)} checks (`tundravm run probe INSTANCE` reads them)"
        )
        width = max(len(str(row["check"])) for row in probe)
        lines.extend(f"    {row['check']:<{width}}  {row['what']}" for row in probe)
    else:
        lines.append("  boot probe    none: a production build carries none")
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
    "image_egress",
    "image_report",
    "image_tree",
    "image_units",
    "ordered_profiles",
    "render_build",
    "render_diff",
    "render_egress",
    "render_files",
    "render_image",
    "render_measurements",
    "render_report",
    "render_units",
    "report",
]
