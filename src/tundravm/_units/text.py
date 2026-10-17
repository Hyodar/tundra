"""A lossless model of systemd unit text: ``UnitText.parse(text).text == text`` for any input.

Every physical line is kept verbatim (comments, blank lines, continuation lines, odd
spacing), so lint can point at lines and a parsed unit renders back byte for byte.
Directive values follow systemd.syntax(7): a trailing backslash joins the next line with a
space, and comment lines inside a continuation are skipped.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Directive:
    """``Key=value``; *lines* are the raw lines it spans, *line* the 1-based first one."""

    key: str
    value: str
    lines: tuple[str, ...]
    line: int


@dataclass(frozen=True, slots=True)
class Other:
    """A blank line, a comment, or a line systemd would reject (no ``=``)."""

    raw: str
    line: int

    @property
    def lines(self) -> tuple[str, ...]:
        return (self.raw,)

    @property
    def is_junk(self) -> bool:
        stripped = self.raw.strip()
        return bool(stripped) and not stripped.startswith(("#", ";"))


Entry = Directive | Other


@dataclass(frozen=True, slots=True)
class Section:
    """``[name]`` and its entries; *header* is the raw header line."""

    name: str
    header: str
    line: int
    entries: tuple[Entry, ...]

    @property
    def directives(self) -> tuple[Directive, ...]:
        return tuple(e for e in self.entries if isinstance(e, Directive))

    def values(self, key: str) -> tuple[str, ...]:
        """Every value of *key*, in order; an empty assignment resets the list."""
        found: list[str] = []
        for directive in self.directives:
            if directive.key == key:
                if directive.value:
                    found.append(directive.value)
                else:
                    found.clear()
        return tuple(found)


@dataclass(frozen=True, slots=True)
class UnitText:
    """Parsed unit text: lines before the first section, the sections, the final newline."""

    preamble: tuple[Entry, ...]
    sections: tuple[Section, ...]
    trailing_newline: bool

    @classmethod
    def parse(cls, text: str) -> UnitText:
        raw = text.split("\n")
        trailing = len(raw) > 1 and raw[-1] == ""
        if trailing:
            raw.pop()
        preamble: list[Entry] = []
        sections: list[Section] = []
        current: list[Entry] = preamble
        header: tuple[str, str, int] | None = None
        index = 0
        while index < len(raw):
            line, number = raw[index], index + 1
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                if header is not None:
                    sections.append(Section(header[0], header[1], header[2], tuple(current)))
                header, current = (stripped[1:-1], line, number), []
                index += 1
                continue
            if not stripped or stripped.startswith(("#", ";")) or "=" not in stripped:
                current.append(Other(line, number))
                index += 1
                continue
            spanned, value = _logical(raw, index)
            key, _, first = value.partition("=")
            current.append(Directive(key.strip(), first.strip(), tuple(raw[index:spanned]), number))
            index = spanned
        if header is not None:
            sections.append(Section(header[0], header[1], header[2], tuple(current)))
        return cls(tuple(preamble), tuple(sections), trailing)

    @property
    def text(self) -> str:
        lines = [line for entry in self.preamble for line in entry.lines]
        for section in self.sections:
            lines.append(section.header)
            lines.extend(line for entry in section.entries for line in entry.lines)
        return "\n".join(lines) + ("\n" if self.trailing_newline else "")

    def section(self, name: str) -> Section | None:
        """The last ``[name]`` section (``None`` when absent)."""
        found = [s for s in self.sections if s.name == name]
        return found[-1] if found else None

    def values(self, section: str, key: str) -> tuple[str, ...]:
        """Every value of *key* across every ``[section]``, in order, honoring resets."""
        found: list[str] = []
        for directive in self.directives(section):
            if directive.key == key:
                if directive.value:
                    found.append(directive.value)
                else:
                    found.clear()
        return tuple(found)

    def directives(self, section: str | None = None) -> Iterator[Directive]:
        """Every directive, in file order, optionally only those of *section*."""
        for block in self.sections:
            if section is None or block.name == section:
                yield from block.directives


def _logical(raw: list[str], start: int) -> tuple[int, str]:
    """The index after the directive starting at *start*, and its joined logical line."""
    value = ""
    index = start
    while index < len(raw):
        stripped = raw[index].strip()
        index += 1
        if index - 1 > start and stripped.startswith(("#", ";")):
            continue
        if stripped.endswith("\\") and index < len(raw):
            value += stripped[:-1].rstrip() + " "
            continue
        value += stripped
        break
    return index, value
