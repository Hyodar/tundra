"""Init — minimal runtime-init script builder.

Lowering registers bash fragments per profile
(``ProfileState.init_scripts``); compiling hands each profile's merged
fragments to an Init, which orders them (:func:`runtime_init_order`) and
generates ``/usr/bin/runtime-init`` plus ``runtime-init.service``. The lockfile
records the same order.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from textwrap import dedent

from tundravm.models import FileEntry, InitScriptEntry, ProfileState


def runtime_init_order(entries: Iterable[InitScriptEntry]) -> list[InitScriptEntry]:
    """*entries* in the order ``/usr/bin/runtime-init`` runs them.

    Duplicate ``(priority, script)`` fragments run once, at the first one's place;
    lower priorities run first and equal ones keep their registration order
    (which ``Init.after`` decides).
    """
    unique = {(entry.priority, entry.script): entry for entry in entries}
    return sorted(unique.values(), key=lambda entry: entry.priority)


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
        sorted_scripts = runtime_init_order(profile.init_scripts if scripts is None else scripts)
        if not sorted_scripts:
            return

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
