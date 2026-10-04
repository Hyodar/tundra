"""Policy configuration and enforcement helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from tundravm.errors import PolicyError

MutableRefPolicy = Literal["warn", "error", "allow"]
NetworkMode = Literal["online", "offline"]


@dataclass(frozen=True, slots=True)
class Policy:
    require_frozen_lock: bool = False
    mutable_ref_policy: MutableRefPolicy = "warn"
    require_integrity: bool = True
    network_mode: NetworkMode = "online"


def ensure_bake_policy(*, policy: Policy, frozen: bool) -> None:
    if policy.require_frozen_lock and not frozen:
        raise PolicyError(
            "Frozen lock mode is required by policy.",
            hint=(
                "Run `tundravm lock RECIPE` and bake against it (`tundravm bake --lockfile "
                "PATH`, or bake(recipe, locked=...)), or set "
                "Policy(require_frozen_lock=False)."
            ),
            context={"operation": "bake"},
        )
