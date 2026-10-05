"""Init — minimal runtime-init script builder.

Lowering registers bash fragments per profile
(``ProfileState.init_scripts``); compiling hands each profile's merged
fragments to an Init, which sorts them by priority and
generates ``/usr/bin/runtime-init`` plus ``runtime-init.service``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from textwrap import dedent

from tundravm.models import FileEntry, InitScriptEntry, ProfileState


@dataclass(slots=True)
class Init:
    """Default runtime-init generator.

    Holds no fragments itself; ``apply()`` renders the ones it is given into:
    - ``/usr/bin/runtime-init`` (executable shell script)
    - ``/usr/lib/systemd/system/runtime-init.service`` (oneshot unit)
    """

    @property
    def service_name(self) -> str:
        return "runtime-init.service"

    def apply(
        self,
        profile: ProfileState,
        *,
        scripts: Sequence[InitScriptEntry] | None = None,
        network_setup: bool = True,
    ) -> None:
        """Generate runtime-init script + service unit into *profile*.files.

        *scripts* replaces ``profile.init_scripts`` as the fragments to render, e.g.
        with the merged fragments of a profile that extends another. With
        *network_setup* the unit requires ``network-setup.service``; without it, it
        waits for ``network-online.target``.
        """
        merged_scripts = list(profile.init_scripts if scripts is None else scripts)
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
                content=self._render_service_unit(network_setup=network_setup),
                mode="0644",
            )
        )

    def _render_service_unit(self, *, network_setup: bool = True) -> str:
        network = (
            "After=network.target network-setup.service\nRequires=network-setup.service"
            if network_setup
            else "After=network-online.target\nWants=network-online.target"
        )
        return dedent("""\
            [Unit]
            Description=Runtime Init
            {network}

            [Service]
            Type=oneshot
            ExecStart=/usr/bin/runtime-init
            RemainAfterExit=yes

            [Install]
            WantedBy=minimal.target
        """).format(network=network)
