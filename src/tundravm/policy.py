"""Policy configuration and enforcement helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from tundravm.errors import PolicyError

MutableRefPolicy = Literal["warn", "error", "allow"]
NetworkMode = Literal["online", "offline"]
StorageSafety = Literal["warn", "error"]


@dataclass(frozen=True, slots=True)
class Policy:
    """What a recipe's lint and bake enforce.

    ``network_mode="offline"`` keeps ``lock`` and ``fetch`` off the network and
    bakes with no network in the build sandbox (``bake --offline``).
    ``storage_safety="error"`` makes ``disk-auto-format`` an error.
    """

    require_frozen_lock: bool = False
    mutable_ref_policy: MutableRefPolicy = "warn"
    network_mode: NetworkMode = "online"
    storage_safety: StorageSafety = "warn"


def ensure_bake_policy(*, policy: Policy, frozen: bool) -> None:
    if policy.require_frozen_lock and not frozen:
        raise PolicyError(
            "Frozen lock mode is required by policy.",
            hint=(
                "Run `tundravm lock RECIPE` and bake against it (`tundravm bake --lockfile "
                "PATH`, or bake(recipe, lock=...)), or set "
                "Policy(require_frozen_lock=False)."
            ),
            context={"operation": "bake"},
        )
