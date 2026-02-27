"""Init — minimal runtime-init script builder.

Collects bash script fragments (registered via ``image.add_init_script()``),
sorts them by priority, and generates ``/usr/bin/runtime-init`` plus
``runtime-init.service``.  Image owns an Init instance and applies it
automatically during ``compile()``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from textwrap import dedent

from tundravm.errors import ValidationError
from tundravm.models import FileEntry, InitScriptEntry, ProfileState


@dataclass(slots=True)
class Init:
    """Default runtime-init implementation.

    Collects bash script fragments, sorts by priority, and generates:
    - ``/usr/bin/runtime-init`` (executable shell script)
    - ``/usr/lib/systemd/system/runtime-init.service`` (oneshot unit)
    """

    _scripts: list[InitScriptEntry] = field(default_factory=list)

    @property
    def service_name(self) -> str:
        return "runtime-init.service"

    @property
    def has_scripts(self) -> bool:
        return bool(self._scripts)

    def add_script(self, script: str, *, priority: int = 100) -> None:
        """Register a bash fragment with the given priority (lower runs first)."""
        if not script:
            raise ValidationError("add_script() requires non-empty script content.")
        self._scripts.append(InitScriptEntry(script=script, priority=priority))

    def apply(self, profile: ProfileState) -> None:
        """Generate runtime-init script + service unit into *profile*.files."""
        merged_scripts = list(self._scripts) + list(profile.init_scripts)
        if not merged_scripts:
            return
        deduped: list[InitScriptEntry] = []
        seen: set[tuple[int, str]] = set()
        for entry in merged_scripts:
            key = (entry.priority, entry.script)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(entry)
        sorted_scripts = sorted(deduped, key=lambda e: e.priority)

        parts = [
            dedent("""\
            #!/bin/bash
            set -euo pipefail
        """)
        ]
        for entry in sorted_scripts:
            parts.append(entry.script)
        script_content = "\n".join(parts)

        # Remove previous runtime-init entries to stay idempotent
        init_paths = {
            "/usr/bin/runtime-init",
            "/usr/lib/systemd/system/runtime-init.service",
        }
        profile.files = [f for f in profile.files if f.path not in init_paths]

        profile.files.append(
            FileEntry(
                path="/usr/bin/runtime-init",
                content=script_content,
                mode="0755",
            )
        )
        profile.files.append(
            FileEntry(
                path="/usr/lib/systemd/system/runtime-init.service",
                content=self._render_service_unit(),
                mode="0644",
            )
        )

    def _render_service_unit(self) -> str:
        return dedent("""\
            [Unit]
            Description=Runtime Init
            After=network.target network-setup.service
            Requires=network-setup.service

            [Service]
            Type=oneshot
            ExecStart=/usr/bin/runtime-init
            RemainAfterExit=yes

            [Install]
            WantedBy=minimal.target
        """)
