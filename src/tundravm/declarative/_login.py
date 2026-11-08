"""A login's commands as argparse spells them, and the Go program tundravm writes for them.

``CommandParam`` is ``add_argument``, ``Command`` a subparser whose ``runs`` is the argv
template over its params, and a ``ParamType`` says how a value is checked in the program,
admitted by sudo and spelled in the help. :func:`program` renders the login's
``main.go`` and ``dev.go`` against the runtime ``tools/logincli/cli.go``; the sudoers lines
of a ``sudo=True`` command are every argv it can produce (:meth:`Command.sudoers`).
"""

from __future__ import annotations

import ast
import builtins
import itertools
import json
import math
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tundravm.errors import ValidationError

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable

_POSITIONAL = re.compile(r"[a-z][a-z0-9_]{0,31}")
_OPTION = re.compile(r"--[a-z][a-z0-9-]{0,31}")
_CHOICE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}")
_MODE_NAME = re.compile(r"[a-z][a-z0-9]{0,23}")
_MODE_RESERVED = frozenset({"tdx", "simulated"})
_HELP_WORDS = frozenset({"help", "-h", "--help", "h"})
_INT_MAX = 999_999_999
_TEMPLATE = re.compile(r"\{\{([^{}]*)\}\}")
_FLAG_WORD = re.compile(r"\{\{#([^{}]*)\}\}(.*)\{\{/([^{}]*)\}\}", re.DOTALL)
_SUDO_REFUSED = frozenset("*?[]\\\"'")
_SUDO_ESCAPED = re.compile(r"([,:=#])")
_PATTERN_LITERAL = re.compile(r"[A-Za-z0-9/_.+-]*")
_PATTERN_REFUSED = frozenset(" \t\n\\,:=\"'")
_PATTERN_BRACKET = re.compile(r"\[\]?[^]]*\]")
_GO_LITERAL = r'[0-9]+|"(?:[^"\\\x00-\x1f\x7f]|\\[^\x00-\x1f\x7f])*"'
_CHECK_CALL = re.compile(rf"(?:Int|OneOf|Match)\(value(?:, (?:{_GO_LITERAL}))*\)")
_MATCH_CALL = re.compile(rf"Match\(value, ({_GO_LITERAL})\)")
_SAFE_ESCAPES = frozenset("dws")
SUDO_LINES = 64
"""The most sudoers lines one ``sudo=True`` command may take (``login-sudo-lines``)."""


class ParamType(ABC):
    """The domain of a :class:`CommandParam`: how the generated program checks a value, what
    a root command's sudoers line admits, and how the help spells it.

    ``type=int`` (with ``min=`` and ``max=``), ``type=str`` (with ``choices=``) and
    ``type=bool`` pick the built-in ones; pass an instance of a subclass for another
    domain. :meth:`check` is compiled into the login's program, so it is one call of the
    runtime's ``Int``, ``OneOf`` or ``Match`` with literal arguments (anything else is
    refused), and a :meth:`pattern` looser than :meth:`check` lets sudo admit what the
    program refuses: keep the two the same set.
    """

    __slots__ = ()

    @abstractmethod
    def usage(self) -> str:
        """The domain as the help prints it: ``1-10000``, ``production|maintenance``."""

    @abstractmethod
    def check(self) -> str:
        """A Go expression over the string ``value`` yielding an ``error``:
        ``Match(value, "^[0-9a-f]{64}$")``."""

    @abstractmethod
    def pattern(self) -> str | None:
        """The anchored regular expression a root command's sudoers line admits the value
        with (``^[0-9a-f]{64}$``), or ``None`` for a type sudo cannot take."""


@dataclass(frozen=True, slots=True)
class _Int(ParamType):
    low: int
    high: int

    def usage(self) -> str:
        return f"{self.low}-{self.high}"

    def check(self) -> str:
        return f"Int(value, {self.low}, {self.high})"

    def pattern(self) -> str:
        return "^[0-9]+$"


@dataclass(frozen=True, slots=True)
class _Choices(ParamType):
    values: tuple[str, ...]

    def usage(self) -> str:
        return "|".join(self.values)

    def check(self) -> str:
        return f"OneOf(value, {', '.join(map(_go, self.values))})"

    def pattern(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class _Flag(ParamType):
    def usage(self) -> str:
        return ""

    def check(self) -> str:
        return ""

    def pattern(self) -> None:
        return None


def _go(text: str) -> str:
    """*text* as a Go string literal."""
    return json.dumps(text, ensure_ascii=False)


def _param_hint(name: str) -> str:
    if name.startswith("-"):
        return f'CommandParam("{name}", type=int, min=1, max=1000, default=100)'
    return f'CommandParam("{name}", choices=["a", "b"])'


@dataclass(frozen=True, slots=True, init=False, repr=False)
class CommandParam:
    """A param of a :class:`Command`, as ``add_argument`` takes it:
    ``CommandParam("--lines", type=int, min=1, max=10000, default=200, help="lines from the
    end")``.

    A *name* without dashes is positional: required, typed in declaration order. ``--name``
    is an option, typed as ``--name VALUE`` or ``--name=VALUE`` anywhere after the command,
    and takes *default* when not given (required, but for a flag). ``runs`` and the help
    call it by its name without the dashes, ``-`` read as ``_`` (``{{lines}}``).
    ``type=int`` needs *min* and *max* (0 to 999999999); ``type=str`` needs *choices*, as
    there are no free strings; ``type=bool`` is a flag, an option without a value or a
    default, which ``{{#name}}word{{/name}}`` turns into a word. Any other domain is a
    :class:`ParamType` instance. *help* is its line in the command's help.
    """

    name: str
    type: ParamType
    kind: str
    default: str | None
    help: str | None

    def __init__(
        self,
        name: str,
        type: builtins.type | ParamType = str,  # noqa: A002 - add_argument's keyword
        *,
        choices: Sequence[str] | None = None,
        min: int | None = None,  # noqa: A002 - add_argument's keyword
        max: int | None = None,  # noqa: A002 - add_argument's keyword
        default: object = None,
        help: str | None = None,  # noqa: A002 - add_argument's keyword
    ) -> None:
        self._check_name(name)
        object.__setattr__(self, "name", name)
        domain, kind = self._domain(type, choices, min, max)
        object.__setattr__(self, "type", domain)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "default", self._default(default))
        if help is not None and (not isinstance(help, str) or not help.strip() or "\n" in help):
            raise ValidationError(
                f"CommandParam {name}: help={help!r} is not one line of text.",
                hint=f'CommandParam("{name}", ..., help="lines from the end").',
            )
        object.__setattr__(self, "help", help)

    @staticmethod
    def _check_name(name: object) -> None:
        if isinstance(name, str) and name.lstrip("-") in _HELP_WORDS:
            raise ValidationError(
                f"CommandParam: {name!r} is the program's own help, which every command takes.",
                hint='Give the param another name: CommandParam("--verbose", type=bool).',
            )
        if isinstance(name, str) and (_POSITIONAL.fullmatch(name) or _OPTION.fullmatch(name)):
            return
        bare = name.lstrip("-").lower() if isinstance(name, str) else "lines"
        raise ValidationError(
            f"CommandParam: {name!r} is neither a positional name nor a --option.",
            hint=f'CommandParam("{bare or "lines"}", ...) is positional, '
            f'CommandParam("--{bare or "lines"}", ...) an option: lower-case letters, digits, '
            '"_" or "-".',
        )

    def _domain(
        self, kind: object, choices: object, low: object, high: object
    ) -> tuple[ParamType, str]:
        name = self.name
        if isinstance(kind, ParamType):
            if choices is not None or low is not None or high is not None:
                raise ValidationError(
                    f"CommandParam {name}: type={kind!r} brings its own domain, so choices=, "
                    "min= and max= say nothing.",
                    hint=f'CommandParam("{name}", type={kind!r}).',
                )
            self._refuse_option_like(kind)
            return kind, repr(kind)
        if kind is int:
            if choices is not None or low is None or high is None:
                raise ValidationError(
                    f"CommandParam {name}: type=int takes min= and max=, so what a login may "
                    "pass is bounded.",
                    hint=f'CommandParam("{name}", type=int, min=1, max=1000'
                    + (", default=100)." if self.option else ")."),
                )
            bounds = (low, high)
            if any(isinstance(b, bool) or not isinstance(b, int) for b in bounds) or not (
                0 <= low <= high <= _INT_MAX  # type: ignore[operator]
            ):
                raise ValidationError(
                    f"CommandParam {name}: min={low!r}, max={high!r} are not two whole numbers "
                    f"from 0 to {_INT_MAX}, min first.",
                    hint=f'CommandParam("{name}", type=int, min=1, max=1000).',
                )
            return _Int(low, high), "int"  # type: ignore[arg-type]
        if kind is str:
            return self._choices(choices, low, high), "str"
        if kind is bool:
            if not self.option:
                raise ValidationError(
                    f"CommandParam {name}: a flag (type=bool) is an option, given or not.",
                    hint=f'CommandParam("--{name}", type=bool).',
                )
            if choices is not None or low is not None or high is not None:
                raise ValidationError(
                    f"CommandParam {name}: a flag (type=bool) takes no value, so no choices=, "
                    "min= or max=.",
                    hint=f'CommandParam("{name}", type=bool).',
                )
            return _Flag(), "bool"
        raise ValidationError(
            f"CommandParam {name}: type={kind!r} is not int, str, bool or a ParamType.",
            hint=f'CommandParam("{name}", type=int, min=1, max=1000), choices=[...] for str, '
            "type=bool for a flag, or an instance of a ParamType subclass.",
        )

    def _refuse_option_like(self, kind: ParamType) -> None:
        """Refuse a type whose ``pattern()`` or ``Match`` check may admit a value starting with
        ``-``, which the program it fills would read as an option."""
        check = kind.check()
        matched = _MATCH_CALL.fullmatch(check) if isinstance(check, str) else None
        regexes = [kind.pattern(), _go_text(matched.group(1)) if matched else None]
        for regex in (r for r in regexes if isinstance(r, str)):
            if _option_like(regex):
                raise ValidationError(
                    f"CommandParam {self.name}: type={kind!r} admits a value starting with "
                    f"'-' ({regex!r}), which the program would read as an option "
                    "(login-param-option-like).",
                    hint="Anchor the pattern so a value cannot start with -: "
                    "'^[0-9a-f][0-9a-f-]*$'.",
                    context={"rule": "login-param-option-like"},
                )

    def _choices(self, choices: object, low: object, high: object) -> _Choices:
        name = self.name
        if low is not None or high is not None:
            raise ValidationError(
                f"CommandParam {name}: min= and max= bound an int, and this one is a str.",
                hint=f'CommandParam("{name}", type=int, min=1, max=1000).',
            )
        if choices is None:
            raise ValidationError(
                f"CommandParam {name}: a str takes choices=, as a login passes no free string.",
                hint=f'CommandParam("{name}", choices=["production", "maintenance"]).',
            )
        values = tuple(choices) if isinstance(choices, (list, tuple)) else ()
        bad = [v for v in values if not isinstance(v, str) or not _CHOICE.fullmatch(v)]
        if not values or bad or len(set(values)) != len(values):
            said = (
                "offer no value"
                if not values
                else f"offer {', '.join(map(repr, bad)) or 'a value twice'}"
            )
            raise ValidationError(
                f"CommandParam {name}: choices= {said}.",
                hint=f'List distinct words, none starting with "-": CommandParam("{name}", '
                'choices=["a", "b"]).',
            )
        return _Choices(values)

    def _default(self, default: object) -> str | None:
        name = self.name
        if isinstance(self.type, _Flag):
            if default is not None:
                raise ValidationError(
                    f"CommandParam {name}: a flag is given or not, so it has no default.",
                    hint=f'CommandParam("{name}", type=bool).',
                )
            return None
        if not self.option:
            if default is not None:
                raise ValidationError(
                    f"CommandParam {name}: a positional param is required, so it has no default.",
                    hint=f'Make it an option: CommandParam("--{name}", ..., default={default!r}).',
                )
            return None
        if default is None:
            raise ValidationError(
                f"CommandParam {name}: an option the client may leave out needs default=.",
                hint=f'CommandParam("{name}", type=int, min={self.type.low}, '
                f"max={self.type.high}, default={self.type.low})."
                if isinstance(self.type, _Int)
                else f'CommandParam("{name}", ..., default="...").',
            )
        if isinstance(self.type, _Int):
            if (
                isinstance(default, bool)
                or not isinstance(default, int)
                or not (self.type.low <= default <= self.type.high)
            ):
                raise ValidationError(
                    f"CommandParam {name}: default={default!r} is not a whole number from "
                    f"{self.type.low} to {self.type.high}.",
                    hint=f'CommandParam("{name}", type=int, min={self.type.low}, '
                    f"max={self.type.high}, default={self.type.low}).",
                )
            return str(default)
        if isinstance(self.type, _Choices) and default not in self.type.values:
            raise ValidationError(
                f"CommandParam {name}: default={default!r} is not one of its choices.",
                hint=f'CommandParam("{name}", choices=[...], default="{self.type.values[0]}").',
            )
        if not isinstance(default, str) or not default or any(c.isspace() for c in default):
            raise ValidationError(
                f"CommandParam {name}: default={default!r} is not one word.",
                hint=f'CommandParam("{name}", ..., default="...").',
            )
        return default

    @property
    def option(self) -> bool:
        """Whether it is an option (``--name``) rather than positional."""
        return self.name.startswith("--")

    @property
    def flag(self) -> bool:
        """Whether it is a flag (``type=bool``)."""
        return isinstance(self.type, _Flag)

    @property
    def dest(self) -> str:
        """Its name in ``runs`` and the program: without the dashes, ``-`` as ``_``."""
        return self.name.removeprefix("--").replace("-", "_")

    def usage(self, bracketed: bool) -> str:
        """The param as the client types it: ``<mode>``, ``--lines N``, ``[--follow]``."""
        if not self.option:
            return f"<{self.dest}>"
        if self.flag:
            spelled = self.name
        elif re.fullmatch(r"[0-9]+-[0-9]+", self.type.usage()):
            spelled = f"{self.name} N"
        else:
            spelled = f"{self.name} {self.dest.upper()}"
        return f"[{spelled}]" if bracketed else spelled

    def __repr__(self) -> str:
        words = [json.dumps(self.name)]
        if self.kind == "int":
            assert isinstance(self.type, _Int)
            words += ["type=int", f"min={self.type.low}", f"max={self.type.high}"]
        elif self.kind == "bool":
            words.append("type=bool")
        elif self.kind == "str":
            assert isinstance(self.type, _Choices)
            words.append(f"choices={json.dumps(list(self.type.values))}")
        else:
            words.append(f"type={self.kind}")
        if self.default is not None:
            words.append(
                f"default={self.default if self.kind == 'int' else json.dumps(self.default)}"
            )
        if self.help is not None:
            words.append(f"help={json.dumps(self.help)}")
        return f"CommandParam({', '.join(words)})"

    def payload(self) -> dict[str, object]:
        """What ``show --json`` says of it."""
        return {
            "name": self.dest,
            "option": self.name if self.option else None,
            "type": self.kind,
            "usage": self.type.usage(),
            "default": self.default,
            "help": self.help,
        }


@dataclass(frozen=True, slots=True)
class _Word:
    """A word of ``runs``: literal text and ``{{name}}`` holes, or a flag's word."""

    parts: tuple[tuple[bool, str], ...]
    """``(is a param, text)`` in order: the literal text, or the param's name."""
    flag: str | None = None
    """The flag whose ``{{#flag}}...{{/flag}}`` wraps the word, else ``None``."""

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(text for named, text in self.parts if named)

    def literal(self) -> str | None:
        """The word when it holds no param and no flag."""
        if self.flag is None and not self.names:
            return "".join(text for _, text in self.parts)
        return None

    def go(self) -> str:
        """The word as a Go expression over ``v Values``."""
        if self.flag is not None:
            return f"v.If({_go(self.flag)}, {_go(self.parts[0][1])})"
        pieces = [f"v.Get({_go(text)})" if named else _go(text) for named, text in self.parts]
        return "+".join(pieces)

    def shown(self) -> str:
        """The word as ``runs:`` prints it: ``<name>`` for a value, ``[word]`` for a flag's."""
        if self.flag is not None:
            return f"[{self.parts[0][1]}]"
        return "".join(f"<{text}>" if named else text for named, text in self.parts)


def _parse(where: str, word: object) -> _Word:
    """*word* of ``runs=`` as its parts, or a refusal naming the spelling."""
    if not isinstance(word, str) or not word or "\0" in word:
        raise ValidationError(
            f"{where}: runs= holds {word!r}, which is not a word.",
            hint='Each word is a non-empty str: a literal, a path, "{{lines}}" or '
            '"{{#follow}}-f{{/follow}}".',
        )
    if any(c.isspace() for c in word):
        raise ValidationError(
            f"{where}: runs= word {word!r} holds a space, and a word is never split.",
            hint=f"Give each word its own string: {', '.join(map(json.dumps, word.split()))}.",
        )
    if "{{#" in word or "{{/" in word:
        found = _FLAG_WORD.fullmatch(word)
        inner = found.group(2) if found else ""
        if (
            found is None
            or found.group(1) != found.group(3)
            or not inner
            or "{{" in inner
            or "}}" in inner
        ):
            raise ValidationError(
                f"{where}: runs= word {word!r} is not one flag's word.",
                hint='Wrap a whole literal word in its flag: "{{#follow}}-f{{/follow}}".',
            )
        return _Word(((False, inner),), flag=found.group(1))
    parts: list[tuple[bool, str]] = []
    at = 0
    for found in _TEMPLATE.finditer(word):
        if found.start() > at:
            parts.append((False, word[at : found.start()]))
        parts.append((True, found.group(1)))
        at = found.end()
    if at < len(word):
        parts.append((False, word[at:]))
    rest = "".join(text for named, text in parts if not named)
    if "{{" in rest or "}}" in rest:
        raise ValidationError(
            f"{where}: runs= word {word!r} holds a brace no template closes.",
            hint='Name a param as "{{lines}}", or "--lines={{lines}}" inside a word.',
        )
    return _Word(tuple(parts))


@dataclass(frozen=True, slots=True, init=False, repr=False)
class Command:
    """A command of a :class:`Login`, as an argparse subparser:
    ``Command(params=[CommandParam("mode", choices=["a", "b"])], runs=["/usr/bin/switch",
    "{{mode}}"], sudo=True, help="switch the mode")``.

    *runs* is the argv, its first word the program's absolute path; each word is a literal,
    a path (``directory / "file"``), a word with ``{{name}}`` holes each param's value
    fills (``"--lines={{lines}}"`` stays one word), or a flag's whole word wrapped in
    ``{{#flag}}...{{/flag}}``, which exists only when the client gives the flag. A word is
    never split and nothing passes through a shell; every param is in *runs*, and every
    ``{{name}}`` names a param (``login-param-unused``, ``login-param-unknown``).
    *sudo* runs it as root through ``sudo -n``, which ``/etc/sudoers.d/<login>`` admits for
    exactly the argvs it can produce: each flag given and not, one line per choice, a
    type's pattern for any other value (named once), at most 64 lines
    (``login-sudo-lines``). *modes* offers it in those ``Firewall(modes=)`` modes only;
    *dev* makes it exist in development builds only, the profiles whose ``production=`` is
    ``False``. *help* is its line in the listing.
    """

    params: tuple[CommandParam, ...]
    runs: tuple[str, ...]
    sudo: bool
    modes: tuple[str, ...]
    dev: bool
    help: str | None
    words: tuple[_Word, ...] = field(compare=False)

    def __init__(
        self,
        params: Sequence[CommandParam] = (),
        runs: Sequence[str] | None = None,
        *,
        sudo: bool = False,
        modes: Sequence[str] = (),
        dev: bool = False,
        help: str | None = None,  # noqa: A002 - argparse's keyword
    ) -> None:
        self._refuse_positional(params, runs, sudo)
        if isinstance(runs, str) or not isinstance(runs, Sequence):
            raise ValidationError(
                f"Command: runs={runs!r} is not a list of words.",
                hint=f"Command(runs=[{json.dumps(runs) if isinstance(runs, str) else '...'}]).",
            )
        if isinstance(params, CommandParam) or not isinstance(params, Sequence):
            raise ValidationError(
                "Command: params= takes a list of CommandParam.",
                hint='params=[CommandParam("mode", choices=["a", "b"])].',
            )
        object.__setattr__(self, "params", tuple(params))
        object.__setattr__(self, "runs", tuple(runs or ()))
        object.__setattr__(self, "sudo", sudo)
        object.__setattr__(self, "modes", modes)
        object.__setattr__(self, "dev", dev)
        object.__setattr__(self, "help", help)
        for keyword, value in (("sudo", sudo), ("dev", dev)):
            if not isinstance(value, bool):
                raise ValidationError(
                    f"Command: {keyword}= must be True or False.", hint=f"{keyword}=True."
                )
        if help is not None and (not isinstance(help, str) or not help.strip() or "\n" in help):
            raise ValidationError(
                f"Command: help={help!r} is not one line of text.",
                hint='Command(..., help="reboot the VM").',
            )
        self._check_modes()
        self._check_params()
        object.__setattr__(self, "words", tuple(_parse(self._where, w) for w in self.runs))
        self._check_words()
        if self.sudo:
            self._check_sudo()

    @staticmethod
    def _refuse_positional(params: object, runs: object, sudo: object) -> None:
        """Refuse ``Command("/usr/bin/x", ...)``, the argv as positional words."""
        argv = [params] if isinstance(params, str) else None
        if argv is None and isinstance(params, (list, tuple)):
            if params and all(isinstance(p, str) for p in params):
                argv = list(params)
        if argv is None:
            if runs is None:
                raise ValidationError(
                    "Command() runs nothing: runs= is the argv.",
                    hint='Command(runs=["/usr/bin/app-status"]).',
                )
            return
        if isinstance(runs, str):
            argv.append(runs)
        words = ", ".join(json.dumps(word) for word in argv)
        raise ValidationError(
            "Command: the argv is runs=, a list of words, and params= the params it takes.",
            hint=f"Command(runs=[{words}]{', sudo=True' if sudo is True else ''}).",
        )

    @property
    def _where(self) -> str:
        first = self.runs[0] if self.runs and isinstance(self.runs[0], str) else "?"
        return f"Command {first}"

    def _check_modes(self) -> None:
        modes = self.modes
        if isinstance(modes, str) or not isinstance(modes, Sequence):
            raise ValidationError(
                f"Command: modes={modes!r} is not a list of mode names.",
                hint='modes=["maintenance"].',
            )
        object.__setattr__(self, "modes", tuple(modes))
        for mode in self.modes:
            if not isinstance(mode, str) or not _MODE_NAME.fullmatch(mode):
                raise ValidationError(
                    f"Command: modes= names {mode!r}, not a firewall mode's name.",
                    hint='Name a mode of Firewall(modes=): modes=["maintenance"].',
                )
            if mode in _MODE_RESERVED:
                raise ValidationError(
                    f"Command: modes= names {mode!r}, a word of the security mode (run mode), "
                    "not a firewall mode.",
                    hint='Name a mode of Firewall(modes=): modes=["maintenance"].',
                )
        if len(set(self.modes)) != len(self.modes):
            raise ValidationError("Command: modes= names a mode twice.", hint="Name each once.")

    def _check_params(self) -> None:
        if not all(isinstance(p, CommandParam) for p in self.params):
            raise ValidationError(
                f"{self._where}: params= takes CommandParam values.",
                hint='params=[CommandParam("--lines", type=int, min=1, max=1000, default=100)].',
            )
        names = [p.dest for p in self.params]
        twice = sorted({n for n in names if names.count(n) > 1})
        if twice:
            raise ValidationError(
                f"{self._where}: params= names {', '.join(twice)} twice.",
                hint="Give each CommandParam its own name.",
            )

    def _check_words(self) -> None:
        where = self._where
        if not self.runs:
            raise ValidationError(
                "Command: runs= is empty, so it runs nothing.",
                hint='Command(runs=["/usr/bin/app-status"]).',
            )
        first = self.words[0].literal()
        if first is None or not first.startswith("/"):
            raise ValidationError(
                f"Command: runs= starts with {self.runs[0]!r}, not the program's absolute path, "
                "and nothing is run by name.",
                hint='Start with the program\'s path: runs=["/usr/bin/...", ...].',
            )
        params = {p.dest: p for p in self.params}
        used: set[str] = set()
        for word in self.words:
            for name in (*word.names, *([word.flag] if word.flag else [])):
                param = params.get(name)
                if param is None:
                    raise ValidationError(
                        f"{where}: runs= names {{{{{name}}}}}, which no param declares "
                        "(login-param-unknown).",
                        hint=f"Declare it: params=[{_param_hint(f'--{name}')}], or name one of "
                        f"{', '.join(params) or 'its params'}.",
                        context={"rule": "login-param-unknown"},
                    )
                if param.flag and word.flag is None:
                    raise ValidationError(
                        f"{where}: {param.name} is a flag, so it has no value for {{{{{name}}}}}.",
                        hint="Wrap the word it adds: "
                        f'"{{{{#{name}}}}}{param.name}{{{{/{name}}}}}".',
                    )
                if not param.flag and word.flag is not None:
                    raise ValidationError(
                        f"{where}: {param.name} takes a value, so it is no flag to wrap a word.",
                        hint=f'Write its value: "{{{{{name}}}}}".',
                    )
                used.add(name)
        unused = [p for p in self.params if p.dest not in used]
        if unused:
            param = unused[0]
            spelled = (
                f'"{{{{#{param.dest}}}}}{param.name}{{{{/{param.dest}}}}}"'
                if param.flag
                else f'"{{{{{param.dest}}}}}"'
            )
            raise ValidationError(
                f"{where}: param {param.name} appears nowhere in runs=, so what the client "
                "types would be dropped (login-param-unused).",
                hint=f"Put it in the argv: runs=[..., {spelled}], or drop the param.",
                context={"rule": "login-param-unused"},
            )

    def _check_sudo(self) -> None:
        """Refuse a root command sudoers would read as more than it says."""
        where = f"{self._where} (sudo=True)"
        params = {p.dest: p for p in self.params}
        patterned = any(
            params[name].type.pattern() is not None for word in self.words for name in word.names
        )
        named = [n for w in self.words for n in w.names]
        for at, word in enumerate(self.words):
            literal = "".join(text for is_name, text in word.parts if not is_name)
            refused = "".join(sorted(_SUDO_REFUSED & set(literal)))
            if refused:
                raise ValidationError(
                    f"{where}: {self.runs[at]!r} holds {refused!r}, which sudoers reads as a "
                    "wildcard or a quote, so the line would allow more than it says.",
                    hint="Write each word literally, one argv word per word.",
                )
            if patterned and at and not _PATTERN_LITERAL.fullmatch(literal):
                raise ValidationError(
                    f"{where}: {self.runs[at]!r} becomes part of a sudoers pattern, which takes "
                    "only letters, digits and /_.+- beside a value.",
                    hint='Spell the option apart from its value: "--lines", "{{lines}}".',
                )
            for name in word.names:
                kind = params[name].type
                pattern = kind.pattern()
                if pattern is not None and named.count(name) > 1:
                    raise ValidationError(
                        f"{where}: runs= names {{{{{name}}}}} twice, and a sudoers pattern cannot "
                        "say both are the same value, so the line would admit two different ones.",
                        hint=f"Name {{{{{name}}}}} once in a root command's runs=.",
                    )
                if pattern is None and not isinstance(kind, _Choices):
                    raise ValidationError(
                        f"{where}: {params[name].name}'s type {params[name].kind} has no "
                        "pattern, so no sudoers line can admit its value.",
                        hint="Return the anchored regular expression its check admits from "
                        "the type's pattern(): '^[0-9a-f]{64}$'.",
                    )
                if pattern is not None and not _sudo_pattern(pattern):
                    raise ValidationError(
                        f"{where}: {params[name].name}'s pattern {pattern!r} is not an anchored "
                        "regular expression of one word sudoers reads as written.",
                        hint="Anchor it (^...$), match no space (no . and no [^...]) and leave out "
                        "quotes, \\ , : and =: '^[0-9a-f]{64}$'.",
                    )
        count = math.prod(len(v) for p in self.params if (v := _spelled(p)) is not None)
        if count > SUDO_LINES:
            raise ValidationError(
                f"{where} produces {count} argvs, one sudoers line each (login-sudo-lines).",
                hint=f"Keep it to {SUDO_LINES}: fewer flags or choices, or split the command.",
                context={"rule": "login-sudo-lines"},
            )

    def sudoers(self) -> list[str]:
        """The sudoers commands admitting every argv it can produce: each flag given and not,
        one line per choice value, a param one value in every word that names it, a type's
        pattern where it has one (then the line is one anchored regular expression over the
        arguments)."""
        params = {p.dest: p for p in self.params}
        spelled = {n: v for n in self._named() if (v := _spelled(params[n])) is not None}
        lines: list[list[tuple[bool, str]]] = []
        for values in itertools.product(*spelled.values()):
            given = dict(zip(spelled, values, strict=True))
            line: list[tuple[bool, str]] = []
            for word in self.words:
                if word.flag is not None:
                    if given[word.flag]:
                        line.append((False, word.parts[0][1]))
                    continue
                parts: list[tuple[bool, str]] = []
                for named, text in word.parts:
                    if not named:
                        parts.append((False, text))
                    elif text in given:
                        parts.append((False, str(given[text])))
                    else:
                        core = (params[text].type.pattern() or "^$")[1:-1]
                        parts.append((True, f"({core})" if "|" in core else core))
                line.append(_joined(parts))
            lines.append(line)
        found = []
        for line in lines:
            path, rest = line[0][1], line[1:]
            if any(pattern for pattern, _ in rest):
                spelt = [
                    text if pattern else re.sub(r"([.+])", r"[\1]", text) for pattern, text in rest
                ]
                found.append(f"{_sudo_word(path)} ^{' '.join(spelt)}$")
                continue
            words = [_sudo_word(text) for _, text in line]
            if rest and words[1].startswith("^"):
                words[1] = f"\\{words[1]}"
            found.append(" ".join(words if rest else [*words, '""']))
        return found

    def _named(self) -> list[str]:
        """The params *runs* names, in the order it first names each."""
        found = [n for w in self.words for n in (*w.names, *([w.flag] if w.flag else []))]
        return list(dict.fromkeys(found))

    def usage(self, name: str) -> str:
        """The command as the client types it: ``logs [--lines N] [--follow]``."""
        return " ".join([name, *(p.usage(True) for p in self.params)])

    def notes(self, root: bool) -> list[str]:
        """What the listing says beside its help: ``as root``, ``maintenance only``."""
        found = ["as root"] if root and self.sudo else []
        if self.modes:
            found.append(" or ".join(self.modes) + " only")
        return found

    def shown(self) -> str:
        """The argv as the help's ``runs:`` line prints it: ``<lines>`` for a value,
        ``[-f]`` for a flag's word, ``(as root)`` after a root command's."""
        line = " ".join(word.shown() for word in self.words)
        return f"{line} (as root)" if self.sudo else line

    def __repr__(self) -> str:
        words = []
        if self.params:
            words.append(f"params=[{', '.join(map(repr, self.params))}]")
        words.append(f"runs={json.dumps(list(self.runs))}")
        if self.sudo:
            words.append("sudo=True")
        if self.modes:
            words.append(f"modes={json.dumps(list(self.modes))}")
        if self.dev:
            words.append("dev=True")
        if self.help is not None:
            words.append(f"help={json.dumps(self.help)}")
        return f"Command({', '.join(words)})"

    def payload(self, name: str, profiles: Sequence[str]) -> dict[str, object]:
        """What ``show --json`` says of it, *profiles* the ones it exists in."""
        return {
            "name": name,
            "help": self.help,
            "params": [p.payload() for p in self.params],
            "runs": list(self.runs),
            "sudo": self.sudo,
            "modes": list(self.modes),
            "profiles": list(profiles),
        }


def _spelled(param: CommandParam) -> tuple[bool, ...] | tuple[str, ...] | None:
    """What the sudoers lines spell *param* as, one line each: a flag given and not, each
    choice; ``None`` for a value its type's pattern admits."""
    if param.flag:
        return (True, False)
    return param.type.values if isinstance(param.type, _Choices) else None


def _sudo_word(text: str) -> str:
    """*text* as sudoers reads it literally: ``,``, ``:``, ``=`` and ``#`` (a comment to the
    end of the line otherwise) escaped."""
    return _SUDO_ESCAPED.sub(r"\\\1", text)


def _joined(parts: Sequence[tuple[bool, str]]) -> tuple[bool, str]:
    """One sudoers word of *parts*: a pattern when any part is, else the literal."""
    if any(pattern for pattern, _ in parts):
        return True, "".join(
            text if pattern else re.sub(r"([.+])", r"[\1]", text) for pattern, text in parts
        )
    return False, "".join(text for _, text in parts)


def _go_text(literal: str) -> str:
    """The text of a Go string *literal*, ``-`` for one Python cannot read (always refused)."""
    try:
        text = ast.literal_eval(literal)
    except (SyntaxError, ValueError):
        return "-"
    return text if isinstance(text, str) else "-"


def _option_like(regex: str) -> bool:
    """Whether *regex* may match a value starting with ``-``: unless every alternative
    begins, after its ``^``, with a literal character or a class that leaves ``-`` out,
    and no quantifier lets that first atom be absent."""
    for alternative in _alternatives(regex):
        rest = alternative.lstrip("^")
        if not rest or rest == "$":
            continue
        atom, after = _first_atom(rest)
        if atom is None or _admits_dash(atom) or after.startswith(("?", "*", "{0,", "{0}")):
            return True
    return False


def _alternatives(regex: str) -> list[str]:
    """*regex* split at each ``|`` outside brackets and parentheses."""
    found, depth, start, at = [], 0, 0, 0
    while at < len(regex):
        char = regex[at]
        if char == "\\":
            at += 2
            continue
        if char == "[":
            at = _bracket_end(regex, at)
            continue
        depth += {"(": 1, ")": -1}.get(char, 0)
        if char == "|" and depth == 0:
            found.append(regex[start:at])
            start = at + 1
        at += 1
    return [*found, regex[start:]]


def _bracket_end(regex: str, at: int) -> int:
    """The index just past the class opening at *at*."""
    at += 1
    at += regex[at : at + 1] == "^"
    at += regex[at : at + 1] == "]"
    while at < len(regex) and regex[at] != "]":
        at += 2 if regex[at] == "\\" else 1
    return at + 1


def _first_atom(rest: str) -> tuple[str | None, str]:
    """The first atom of *rest* and what follows it, ``None`` for a group or ``.``."""
    if rest[0] in "(.":
        return None, ""
    end = _bracket_end(rest, 0) if rest[0] == "[" else 2 if rest[0] == "\\" else 1
    return rest[:end], rest[end:]


def _admits_dash(atom: str) -> bool:
    """Whether the atom (a character, an escape or a class) matches ``-``."""
    if atom.startswith("\\"):
        escaped = atom[1:]
        return escaped not in _SAFE_ESCAPES and (escaped.isalnum() or escaped == "-")
    if not atom.startswith("["):
        return atom == "-"
    body = atom[1:-1]
    if body.startswith("^") or "[:" in body:
        return True
    at = 0
    while at < len(body):
        low = body[at : at + 2] if body[at] == "\\" else body[at]
        at += len(low)
        if body[at : at + 1] == "-" and at + 1 < len(body):
            high = body[at + 1 : at + 3] if body[at + 1] == "\\" else body[at + 1]
            at += 1 + len(high)
            if len(low) > 1 or len(high) > 1 or ord(low) <= ord("-") <= ord(high):
                return True
            continue
        if low in ("-", "\\-"):
            return True
    return False


def _sudo_pattern(pattern: object) -> bool:
    """Whether *pattern* is anchored, holds nothing sudoers would read otherwise and matches
    no space: sudo matches the arguments joined by spaces, so a pattern that could match one
    would admit an argv of more words than the program passes."""
    if not isinstance(pattern, str) or len(pattern) < 3:
        return False
    if not (pattern.startswith("^") and pattern.endswith("$")):
        return False
    core = pattern[1:-1]
    if _PATTERN_REFUSED & set(core) or "^" in core or "$" in core:
        return False
    if not all("!" <= c <= "~" for c in core) or "." in _PATTERN_BRACKET.sub("", core):
        return False
    try:
        re.compile(pattern)
    except re.error:
        return False
    return True


def table(rows: Sequence[tuple[str, str]]) -> list[str]:
    """*rows* as the program prints two columns: indented two spaces, the second aligned."""
    width = max((len(left) for left, _ in rows), default=0)
    return [
        f"  {left}{' ' * (width - len(left))}  {right}" if right else f"  {left}"
        for left, right in rows
    ]


def annotated(help: str | None, notes: Sequence[str]) -> str:  # noqa: A002 - the field's name
    """*help* with *notes* in parentheses after it, as the program prints them."""
    if not notes:
        return help or ""
    return f"{help or ''} ({', '.join(notes)})".strip()


GENERATED = "// Code generated by tundravm from {source}; do not edit.\n"
"""The first line of a login's ``main.go``, naming the file that declares the login."""


def program(
    login: str,
    help: str | None,
    commands: Mapping[str, Command],
    source: str,  # noqa: A002
) -> tuple[str, str | None]:
    """``(main.go, dev.go)`` of *login*'s program: every command but a ``dev=True`` one in
    ``main.go``, those in ``dev.go`` (``//go:build dev``, ``None`` without one), each
    sorted by name. *source* is the file that declares the login, escaped as a JSON string
    is so it stays on the comment's line."""
    shipped = [(n, c) for n, c in sorted(commands.items()) if not c.dev]
    developed = [(n, c) for n, c in sorted(commands.items()) if c.dev]
    named = GENERATED.format(source=json.dumps(source)[1:-1])
    lines = [named + "package main", "", "var program = Program{"]
    lines.append(f"\tLogin: {_go(login)},")
    if help is not None:
        lines.append(f"\tHelp:  {_go(help)},")
    lines.append("\tCommands: []Command{")
    for name, command in shipped:
        lines += _listed(name, command)
    lines += ["\t},", "}", "", "func main() { Main(program) }", ""]
    main = "\n".join(lines)
    if not developed:
        return main, None
    dev = ["//go:build dev", "", "package main", "", "func init() {"]
    for at, (name, command) in enumerate(developed):
        opening = (
            "\tprogram.Commands = append(program.Commands, Command{" if not at else "\t}, Command{"
        )
        dev.append(opening)
        dev += _fields(name, command, "\t\t")
    dev += ["\t})", "}", ""]
    return main, "\n".join(dev)


def _head(name: str, command: Command) -> str:
    fields = [f"Name: {_go(name)}"]
    if command.help is not None:
        fields.append(f"Help: {_go(command.help)}")
    if command.sudo:
        fields.append("Sudo: true")
    if command.modes:
        fields.append(f"Modes: []string{{{', '.join(map(_go, command.modes))}}}")
    return ", ".join(fields)


_ONE_LINE = 100
"""gofmt keeps a function literal on one line while its signature and body fit in this."""
_CHECK = "func(value string) error"
_RUN = "func(v Values) []string"


def _func(indent: str, lead: str, signature: str, statement: str, close: str) -> list[str]:
    """``lead`` and a function literal at *indent*, as gofmt lays it out: on one line when
    it fits, else its statement on a line of its own; *close* ends the last line."""
    if len(signature) + len(statement) <= _ONE_LINE:
        return [f"{indent}{lead}{signature} {{ {statement} }}{close}"]
    return [f"{indent}{lead}{signature} {{", f"{indent}\t{statement}", f"{indent}}}{close}"]


def _param(param: CommandParam) -> tuple[str, str | None]:
    """``(head fields, check statement)`` of a param's literal."""
    fields = [f"Name: {_go(param.dest)}"]
    if param.option:
        fields.append(f"Option: {_go(param.name)}")
    if param.flag:
        fields.append("Flag: true")
    elif param.type.usage():
        fields.append(f"Usage: {_go(param.type.usage())}")
    if param.default is not None:
        fields.append(f"Default: {_go(param.default)}")
    if param.help is not None:
        fields.append(f"Help: {_go(param.help)}")
    return ", ".join(fields), None if param.flag else f"return {_checked(param)}"


def _checked(param: CommandParam) -> str:
    """The param's check, refused unless it is one call of the runtime's ``Int``, ``OneOf``
    or ``Match`` on ``value`` with literal arguments, so no other Go reaches the program."""
    check = param.type.check()
    if not isinstance(check, str) or not _CHECK_CALL.fullmatch(check):
        raise ValidationError(
            f"CommandParam {param.name}: type={param.kind}'s check() is {check!r}, not one call "
            "of the runtime's Int, OneOf or Match on value with literal arguments.",
            hint='Return Match(value, "^[0-9a-f]{64}$"), Int(value, 1, 100) or OneOf(value, '
            '"a", "b"), each string a Go literal (json.dumps(text)).',
        )
    return check


def _params(params: Sequence[CommandParam], indent: str) -> list[str]:
    """``Params: []Param{...},`` at *indent*: one param written inline, more one per line."""
    if len(params) == 1:
        head, check = _param(params[0])
        if check is None:
            return [f"{indent}Params: []Param{{{{{head}}}}},"]
        opening = f"{indent}Params: []Param{{{{{head},"
        return [opening, *_func(f"{indent}\t", "Check: ", _CHECK, check, "}},")]
    lines = [f"{indent}Params: []Param{{"]
    for param in params:
        head, check = _param(param)
        if check is None:
            lines.append(f"{indent}\t{{{head}}},")
        else:
            lines += [
                f"{indent}\t{{{head},",
                *_func(f"{indent}\t\t", "Check: ", _CHECK, check, "},"),
            ]
    return [*lines, f"{indent}}},"]


def _run(command: Command) -> str:
    return f"return Words({', '.join(word.go() for word in command.words)})"


def _listed(name: str, command: Command) -> list[str]:
    """One command in ``main.go``'s list: inline with at most one param, else one field
    per line."""
    if len(command.params) > 1:
        return [
            "\t\t{",
            f"\t\t\t{_head(name, command)},",
            *_params(command.params, "\t\t\t"),
            f"\t\t\tRun: {_RUN} {{",
            f"\t\t\t\t{_run(command)}",
            "\t\t\t},",
            "\t\t},",
        ]
    entries = [] if not command.params else [("Params", _params(command.params, "\t\t\t"), True)]
    entries.append(("Run", _func("\t\t\t", "Run: ", _RUN, _run(command), "},"), True))
    return [f"\t\t{{{_head(name, command)},", *_aligned("\t\t\t", entries)]


def _fields(name: str, command: Command, indent: str) -> list[str]:
    """A ``dev.go`` command's fields at *indent*."""
    head = _head(name, command)
    entries = [("Name", [f"{indent}{head},"], ", " not in head)]
    if command.params:
        entries.append(("Params", _params(command.params, indent), True))
    entries.append(("Run", _func(indent, "Run: ", _RUN, _run(command), ","), True))
    return _aligned(indent, entries)


def _aligned(indent: str, entries: Sequence[tuple[str, list[str], bool]]) -> list[str]:
    """The lines of a literal's *entries* (``(key, lines, alone on its line)`` each), the
    values of a run of one-line entries aligned as gofmt aligns them: each but the last of
    the run the only pair on its line."""
    found: list[str] = []
    at = 0
    while at < len(entries):
        last = at
        while (
            last + 1 < len(entries)
            and len(entries[last][1]) == 1
            and entries[last][2]
            and len(entries[last + 1][1]) == 1
        ):
            last += 1
        run = entries[at : last + 1]
        if len(run) == 1:
            found += run[0][1]
        else:
            width = max(len(key) for key, _, _ in run)
            for key, (line,), _ in run:
                spaced = f"{indent}{key}:{' ' * (width - len(key) + 1)}"
                found.append(line.replace(f"{indent}{key}: ", spaced, 1))
        at = last + 1
    return found


RUNTIME_FILES = {"cli.go": "logincli/cli.go", "go.mod": "go.mod"}
"""The files a login's program takes from ``tools/``, by the name the installed package
ships each under in :data:`PACKAGED_RUNTIME`: the runtime, and the ``go.mod`` whose ``go``
line it takes. ``pyproject.toml`` force-includes them."""
PACKAGED_RUNTIME = "_logincli"
"""The directory of the installed package holding :data:`RUNTIME_FILES`."""


def runtime() -> tuple[str, str]:
    """``(cli.go, go)``: the runtime and the ``go`` line of the tools' ``go.mod``, read from
    the checkout tundravm runs from, else from the copy the installed package ships."""
    from importlib.resources import files

    from tundravm._modules.base import tools_checkout

    tools = tools_checkout()
    cli: Traversable
    mod: Traversable
    if tools is not None:
        cli, mod = (tools / RUNTIME_FILES[name] for name in ("cli.go", "go.mod"))
    else:
        packaged = files("tundravm").joinpath(PACKAGED_RUNTIME)
        cli, mod = (packaged.joinpath(name) for name in ("cli.go", "go.mod"))
    try:
        runtime, module = cli.read_text(encoding="utf-8"), mod.read_text(encoding="utf-8")
    except OSError:
        raise ValidationError(
            f"This tundravm install lacks the login runtime ({cli}), so it cannot write a "
            "Login's program.",
            hint="Reinstall tundravm (uv tool install --reinstall git+...), or run it from a "
            "checkout of its repository.",
        ) from None
    go = next(line.split()[1] for line in module.splitlines() if line.startswith("go "))
    return runtime, go
