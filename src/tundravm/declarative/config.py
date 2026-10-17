"""Configuration files rendered from data: :class:`ConfigFile`.

Each constructor renders *data* to text deterministically and returns an
ordinary :class:`File`, so a recipe, its digest and the compiled tree are the
same as for the hand-written file with the same bytes.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Literal

from tundravm.errors import ValidationError

from .model import File

type Stage = Literal["skeleton", "extra"]

_YAML_PLAIN = re.compile(r"[A-Za-z0-9_/.][A-Za-z0-9_/.@%+:-]*")
_YAML_NUMERIC = re.compile(r"[-+]?\.?[0-9][0-9a-fA-FxXoO_.:eE+-]*")
_YAML_WORDS = frozenset("y n yes no true false on off null ~ .inf -.inf +.inf .nan".split())
_TOML_BARE = re.compile(r"[A-Za-z0-9_-]+")
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ENV_BARE = re.compile(r"[A-Za-z0-9_./:@%+,-]+")
_INI_KEY = re.compile(r"[^\s=\[\]#;][^=\n]*")


def _fail(kind: str, path: str, message: str, hint: str | None = None) -> ValidationError:
    return ValidationError(f"ConfigFile.{kind}({path!r}): {message}", hint=hint)


def _float(value: float, kind: str, path: str) -> str:
    if not math.isfinite(value):
        raise _fail(kind, path, f"{value!r} is not a finite number.")
    text = repr(value)
    mantissa, _, exponent = text.partition("e")
    if exponent and "." not in mantissa:
        text = f"{mantissa}.0e{exponent}"
    return text


def _keys(mapping: Mapping[object, object], kind: str, path: str, sort: bool) -> list[str]:
    keys: list[str] = []
    for key in mapping:
        if not isinstance(key, str) or not key:
            raise _fail(kind, path, f"key {key!r} must be a non-empty string.")
        keys.append(key)
    return sorted(keys) if sort else keys


def _unsupported(kind: str, path: str, value: object) -> ValidationError:
    return _fail(
        kind,
        path,
        f"cannot render {value!r} ({type(value).__name__}).",
        hint="Use str, int, float, bool, None, lists or tuples, and mappings with str keys.",
    )


# ── YAML (a block-style subset) ─────────────────────────────────────────


def _yaml_scalar(value: object, kind: str, path: str) -> str | None:
    """*value* as a YAML scalar, or ``None`` when it is a collection."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _float(value, kind, path)
    if isinstance(value, str):
        plain = (
            _YAML_PLAIN.fullmatch(value)
            and not value.endswith(":")
            and value.lower() not in _YAML_WORDS
            and not _YAML_NUMERIC.fullmatch(value)
        )
        return value if plain else json.dumps(value, ensure_ascii=False)
    if isinstance(value, Mapping):
        return None if value else "{}"
    if isinstance(value, (list, tuple)):
        if any(isinstance(item, (Mapping, list, tuple)) for item in value):
            return None
        return "[" + ", ".join(str(_yaml_scalar(item, kind, path)) for item in value) + "]"
    raise _unsupported(kind, path, value)


def _yaml_lines(value: object, indent: int, sort: bool, path: str) -> list[str]:
    pad = " " * indent
    lines: list[str] = []
    if isinstance(value, Mapping):
        for key in _keys(value, "yaml", path, sort):
            child = value[key]
            label = f"{pad}{_yaml_scalar(key, 'yaml', path)}:"
            scalar = _yaml_scalar(child, "yaml", path)
            if scalar is not None:
                lines.append(f"{label} {scalar}")
            else:
                lines.append(label)
                lines.extend(_yaml_lines(child, indent + 2, sort, path))
        return lines
    for child in value:  # type: ignore[attr-defined]
        scalar = _yaml_scalar(child, "yaml", path)
        if scalar is not None:
            lines.append(f"{pad}- {scalar}")
            continue
        nested = _yaml_lines(child, indent + 2, sort, path)
        lines.append(f"{pad}- {nested[0][indent + 2 :]}")
        lines.extend(nested[1:])
    return lines


# ── TOML ────────────────────────────────────────────────────────────────


def _toml_key(key: str) -> str:
    return key if _TOML_BARE.fullmatch(key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value: object, path: str, sort: bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _float(value, "toml", path)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Mapping):
        pairs = (
            f"{_toml_key(k)} = {_toml_value(value[k], path, sort)}"
            for k in _keys(value, "toml", path, sort)
        )
        return "{ " + ", ".join(pairs) + " }" if value else "{}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item, path, sort) for item in value) + "]"
    if value is None:
        raise _fail("toml", path, "TOML has no null value.", hint="Leave the key out instead.")
    raise _unsupported("toml", path, value)


def _is_table_array(value: object) -> bool:
    return (
        isinstance(value, (list, tuple))
        and bool(value)
        and all(isinstance(item, Mapping) for item in value)
    )


def _toml_table(
    table: Mapping[object, object], header: list[str], path: str, sort: bool, array: bool
) -> list[str]:
    keys = _keys(table, "toml", path, sort)
    plain = [k for k in keys if not isinstance(table[k], Mapping) and not _is_table_array(table[k])]
    nested = [k for k in keys if k not in plain]
    lines: list[str] = []
    if header and (array or plain or not nested):
        name = ".".join(_toml_key(part) for part in header)
        lines.append(f"[[{name}]]" if array else f"[{name}]")
    lines.extend(f"{_toml_key(k)} = {_toml_value(table[k], path, sort)}" for k in plain)
    for key in nested:
        child = table[key]
        rows = list(child) if _is_table_array(child) else [child]  # type: ignore[call-overload]
        for row in rows:
            block = _toml_table(row, [*header, key], path, sort, _is_table_array(child))
            if lines and block:
                lines.append("")
            lines.extend(block)
    return lines


# ── INI and environment files ───────────────────────────────────────────


def _word(value: object, kind: str, path: str) -> str:
    """A scalar as one line of text: ``true``/``false``, digits, or the string itself."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _float(value, kind, path)
    if isinstance(value, str) and "\n" not in value:
        return value
    raise _unsupported(kind, path, value)


def _env_value(value: object, path: str) -> str:
    text = _word(value, "env", path)
    if _ENV_BARE.fullmatch(text):
        return text
    escaped = re.sub(r'(["\\$`])', r"\\\1", text)
    return f'"{escaped}"'


class ConfigFile:
    """Configuration files rendered from data, each an ordinary :class:`File`.

    ``ConfigFile.yaml(path, data)`` (and ``toml``, ``json``, ``ini``, ``env``)
    renders *data* and returns ``File.text(path, text, mode=..., ...)``: the
    same declaration, digest and tree as writing those bytes by hand. *data*
    holds ``str``, ``int``, ``float`` (finite), ``bool``, ``None``, lists or
    tuples, and mappings with ``str`` keys; anything else is refused. Mapping
    keys are written sorted, so the bytes do not depend on how a mapping was
    built; ``sort_keys=False`` keeps insertion order for a reader that cares.
    Lists keep their order. Floats are written as ``repr`` (``0.5``,
    ``1.0e-05``), booleans ``true``/``false``, and every file ends in a newline.
    """

    __slots__ = ()

    @staticmethod
    def _file(
        kind: str,
        path: str,
        data: object,
        render: Callable[[object, str], list[str]],
        mode: int,
        stage: Stage,
        allow_secret: bool,
    ) -> File:
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValidationError(
                f"ConfigFile.{kind}: path {path!r} must be an absolute path.",
                hint=f"Pass the path in the image, e.g. ConfigFile.{kind}('/etc/app/config', ...).",
            )
        lines = render(data, path)
        return File.text(
            path, "\n".join(lines) + "\n", mode=mode, stage=stage, allow_secret=allow_secret
        )

    @classmethod
    def yaml(
        cls,
        path: str,
        data: Mapping[str, object] | Sequence[object],
        *,
        mode: int = 0o644,
        stage: Stage = "extra",
        allow_secret: bool = False,
        sort_keys: bool = True,
    ) -> File:
        """*data* (a mapping or a list) as block-style YAML, two-space indents.

        Strings are plain unless YAML 1.1 or 1.2 would read them as another type
        (``"yes"``, ``"8080"``, ``"1e3"``, ``"127.0.0.1:9100"``) or they hold
        other characters; then they are JSON-quoted. A list of scalars is one
        flow-style line (``["127.0.0.1:9100"]``), a list holding collections a
        block sequence indented under its key; empty ones are ``{}`` and ``[]``.
        """

        def render(value: object, at: str) -> list[str]:
            if not isinstance(value, (Mapping, list, tuple)):
                raise _fail("yaml", at, "data must be a mapping or a list.")
            if not value:
                return ["{}" if isinstance(value, Mapping) else "[]"]
            return _yaml_lines(value, 0, sort_keys, at)

        return cls._file("yaml", path, data, render, mode, stage, allow_secret)

    @classmethod
    def toml(
        cls,
        path: str,
        data: Mapping[str, object],
        *,
        mode: int = 0o644,
        stage: Stage = "extra",
        allow_secret: bool = False,
        sort_keys: bool = True,
    ) -> File:
        """*data* as TOML: a table's keys, then its sub-tables and arrays of tables.

        A list of mappings is an array of tables (``[[name]]``); a mapping inside
        a list is an inline table. TOML has no null, so ``None`` is refused.
        """

        def render(value: object, at: str) -> list[str]:
            if not isinstance(value, Mapping):
                raise _fail("toml", at, "data must be a mapping.")
            return _toml_table(value, [], at, sort_keys, array=False)

        return cls._file("toml", path, data, render, mode, stage, allow_secret)

    @classmethod
    def json(
        cls,
        path: str,
        data: Mapping[str, object] | Sequence[object],
        *,
        mode: int = 0o644,
        stage: Stage = "extra",
        allow_secret: bool = False,
        sort_keys: bool = True,
    ) -> File:
        """*data* as JSON with two-space indents and UTF-8 text (``json.dumps``)."""

        def render(value: object, at: str) -> list[str]:
            _validate(value, "json", at)
            return [json.dumps(value, indent=2, sort_keys=sort_keys, ensure_ascii=False)]

        return cls._file("json", path, data, render, mode, stage, allow_secret)

    @classmethod
    def ini(
        cls,
        path: str,
        sections: Mapping[str, Mapping[str, object]],
        *,
        mode: int = 0o644,
        stage: Stage = "extra",
        allow_secret: bool = False,
        sort_keys: bool = True,
    ) -> File:
        """*sections* as ``[section]`` blocks of ``key = value`` lines, a blank line apart.

        A list value repeats its key once per item, as systemd units do;
        ``None`` leaves the key out. Values are one line each.
        """

        def render(value: object, at: str) -> list[str]:
            if not isinstance(value, Mapping):
                raise _fail("ini", at, "sections must be a mapping of mappings.")
            lines: list[str] = []
            for section in _keys(value, "ini", at, sort_keys):
                body = value[section]
                if not isinstance(body, Mapping) or "]" in section or "\n" in section:
                    raise _fail("ini", at, f"section {section!r} must map keys to values.")
                lines.extend(("", f"[{section}]") if lines else (f"[{section}]",))
                for key in _keys(body, "ini", at, sort_keys):
                    if not _INI_KEY.fullmatch(key) or key != key.strip():
                        raise _fail("ini", at, f"invalid key {key!r} in [{section}].")
                    items = body[key] if isinstance(body[key], (list, tuple)) else (body[key],)
                    lines.extend(f"{key} = {_word(v, 'ini', at)}" for v in items if v is not None)
            return lines

        return cls._file("ini", path, sections, render, mode, stage, allow_secret)

    @classmethod
    def env(
        cls,
        path: str,
        values: Mapping[str, object],
        *,
        mode: int = 0o644,
        stage: Stage = "extra",
        allow_secret: bool = False,
        sort_keys: bool = True,
    ) -> File:
        """*values* as ``KEY=value`` lines, for ``EnvironmentFile=`` or ``/etc/default``.

        A value of letters, digits and ``_./:@%+,-`` is written bare; others
        are double-quoted with ``"``, ``\\``, ``$`` and backticks escaped.
        """

        def render(value: object, at: str) -> list[str]:
            if not isinstance(value, Mapping):
                raise _fail("env", at, "values must be a mapping.")
            for key in value:
                if not isinstance(key, str) or not _ENV_KEY.fullmatch(key):
                    raise _fail("env", at, f"invalid variable name {key!r}.")
            return [f"{k}={_env_value(value[k], at)}" for k in _keys(value, "env", at, sort_keys)]

        return cls._file("env", path, values, render, mode, stage, allow_secret)


def _validate(value: object, kind: str, path: str) -> None:
    if value is None or isinstance(value, (str, int, bool)):
        return
    if isinstance(value, float):
        _float(value, kind, path)
    elif isinstance(value, Mapping):
        _keys(value, kind, path, sort=False)
        for child in value.values():
            _validate(child, kind, path)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _validate(child, kind, path)
    else:
        raise _unsupported(kind, path, value)


__all__ = ["ConfigFile"]
