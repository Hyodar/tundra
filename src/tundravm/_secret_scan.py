"""Heuristics for credentials baked into image files and service environments.

The ``secret-in-file`` and ``secret-in-env`` lint rules (:mod:`tundravm.check`)
call :func:`scan_file` and :func:`scan_env`. A :class:`Finding` names what was
found and where, never the value. The guards against false positives: public
keys, certificates and a bare PEM header without a key body do not match;
documentation values (``EXAMPLE``, ``changeme``, ``xxxx``, low-variety strings)
are skipped; a ``secrets.yaml`` entry counts only under a sensitive key or a
``value``/``default``/``data`` key; the generic ``token|secret|password = value``
assignment is only
looked for in config-like files, outside comments, and needs a token-like value
(16+ characters mixing letter cases, digits or base64 symbols; not a path, not a
``$VAR`` or ``{{ template }}`` reference).
"""

from __future__ import annotations

import posixpath
import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass

SCAN_LIMIT = 1 << 20
"""Bytes of a file the scan reads; the rest is not looked at."""

UNIT_DIRECTORIES = ("/usr/lib/systemd/system/", "/lib/systemd/system/", "/etc/systemd/system/")

_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:(?:RSA|EC|DSA|OPENSSH|ENCRYPTED|PGP) )?PRIVATE KEY(?: BLOCK)?-----\r?\n"
    r"(?:[\w-]+: [^\n]*\n)*\r?\n?[A-Za-z0-9+/=]{16,}"
)
"""A PEM private key header followed by its body: a script that only names one does not match."""
_AWS_KEY_ID = re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])")
_GITHUB_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,})(?![A-Za-z0-9_])"
)
_STRONG = (
    (_PRIVATE_KEY, "a private key"),
    (_AWS_KEY_ID, "an AWS access key id"),
    (_GITHUB_TOKEN, "a GitHub token"),
)
_SENSITIVE = r"[\w.-]*?(?:token|secret|password|passwd|api[_-]?key|access[_-]?key)"
_ASSIGNMENT = re.compile(
    rf"(?:^|[\s\"'{{,;])(?P<key>{_SENSITIVE})[\"']?\s*[:=]\s*[\"']?"
    r"(?P<value>[A-Za-z0-9/+=_.-]{16,})(?=[\"'\s,;}]|$)",
    re.IGNORECASE,
)
_SENSITIVE_KEY = re.compile(rf"^{_SENSITIVE}$", re.IGNORECASE)
_YAML_ENTRY = re.compile(r"^\s*(?:-\s+)?(?P<key>[\w.-]+):\s+(?P<value>[^\s#].*?)\s*$")
_SECRETS_YAML = re.compile(r"(?:^|[-_.])secrets?\.ya?ml$")
_CONFIG_SUFFIXES = frozenset(
    {".conf", ".cfg", ".cnf", ".ini", ".env", ".yaml", ".yml", ".toml", ".json", ".properties"}
)
_CONFIG_NAMES = frozenset(
    {".env", ".netrc", ".npmrc", ".pypirc", ".git-credentials", "credentials", "environment"}
)
_PLACEHOLDERS = (
    "example",
    "changeme",
    "change_me",
    "change-me",
    "placeholder",
    "redacted",
    "dummy",
    "xxxx",
    "your",
    "replace",
    "notreal",
    "fake",
)
_VALUE_KEYS = frozenset({"value", "default", "data", "plaintext", "literal"})
"""Keys of a secrets YAML file that hold a secret's value rather than its configuration."""
_YAML_NON_LITERALS = frozenset({"null", "~", '""', "''", "|", ">", "|-", ">-", "{}", "[]"})
_COMMENT = ("#", ";", "//")


@dataclass(frozen=True, slots=True)
class Finding:
    """What looks like a credential (``kind``), on which 1-based ``line`` (0: unknown)."""

    kind: str
    line: int = 0

    def where(self) -> str:
        return f"{self.kind} (line {self.line})" if self.line else self.kind


def is_unit_path(path: str) -> bool:
    """Whether *path* is a systemd unit file the image ships."""
    return path.startswith(UNIT_DIRECTORIES)


def config_like(path: str) -> bool:
    """Whether *path* reads as configuration: under ``/etc``, or a config suffix or name."""
    name = posixpath.basename(path)
    suffix = posixpath.splitext(name)[1].lower()
    return path.startswith("/etc/") or suffix in _CONFIG_SUFFIXES or name in _CONFIG_NAMES


def _text(content: str | bytes) -> tuple[str, bool]:
    """The scanned text of *content* and whether it is text (UTF-8) at all."""
    if isinstance(content, str):
        return content[:SCAN_LIMIT], True
    head = content[:SCAN_LIMIT]
    try:
        return head.decode("utf-8"), True
    except UnicodeDecodeError:
        return head.decode("latin-1"), False


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def placeholder(value: str) -> bool:
    """Whether *value* reads as documentation rather than a credential."""
    lowered = value.lower()
    return len(set(value)) < 6 or any(word in lowered for word in _PLACEHOLDERS)


def token_like(value: str) -> bool:
    """A literal that looks generated: 16+ characters, mixed classes or base64, not a path."""
    if len(value) < 16 or value.startswith("/") or placeholder(value):
        return False
    classes = sum(
        (
            any(c.islower() for c in value),
            any(c.isupper() for c in value),
            any(c.isdigit() for c in value),
        )
    )
    return classes >= 2 or any(c in "+/=" for c in value)


def _strong(text: str) -> Iterator[Finding]:
    for pattern, kind in _STRONG:
        for match in pattern.finditer(text):
            if not placeholder(match.group(0).split("_", 1)[-1]):
                yield Finding(kind, _line_of(text, match.start()))
                break


def _assignments(text: str) -> Iterator[Finding]:
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith(_COMMENT):
            continue
        for match in _ASSIGNMENT.finditer(line):
            if token_like(match.group("value")):
                yield Finding(f"a literal value for {match.group('key')!r}", number)
                break


def _secrets_yaml(text: str) -> Iterator[Finding]:
    if re.search(r"^sops:", text, re.M):
        return
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        match = _YAML_ENTRY.match(line)
        if match is None:
            continue
        key, value = match.group("key"), match.group("value").strip("\"'")
        if (
            not (_SENSITIVE_KEY.match(key) or key.lower() in _VALUE_KEYS)
            or match.group("value") in _YAML_NON_LITERALS
            or len(value) < 8
            or value.startswith(("/", "!", "*", "&", "{{", "${", "%{", "ENC["))
            or placeholder(value)
        ):
            continue
        yield Finding(f"a literal secrets file value for {match.group('key')!r}", number)
        break


def scan_file(path: str, content: str | bytes) -> tuple[Finding, ...]:
    """What in the file *content* at *path* looks like a credential, first match per kind."""
    text, is_text = _text(content)
    found = list(_strong(text))
    if not is_text:
        return tuple(found)
    if _SECRETS_YAML.search(posixpath.basename(path)):
        found.extend(_secrets_yaml(text))
    elif config_like(path):
        found.extend(_assignments(text))
    return tuple(sorted(found, key=lambda f: f.line))


def scan_env(key: str, value: str) -> Finding | None:
    """A credential in the environment variable *key* = *value*, else ``None``."""
    for found in _strong(value):
        return Finding(found.kind)
    if _SENSITIVE_KEY.match(key) and token_like(value):
        return Finding(f"a literal value for {key!r}")
    return None


def scan_unit(content: str | bytes) -> tuple[Finding, ...]:
    """Credentials in a unit file: anywhere for the strong patterns, else in ``Environment=``."""
    text, _ = _text(content)
    found = list(_strong(text))
    for number, line in enumerate(text.splitlines(), 1):
        key, _, assignments = line.strip().partition("=")
        if key.strip() != "Environment":
            continue
        try:
            words = shlex.split(assignments)
        except ValueError:
            words = assignments.split()
        for word in words:
            name, sep, value = word.partition("=")
            finding = scan_env(name, value) if sep else None
            if finding is not None:
                found.append(Finding(finding.kind, number))
                break
    return tuple(sorted(dict.fromkeys(found), key=lambda f: f.line))
