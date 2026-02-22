"""Policy configuration and enforcement helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from tdx.errors import PolicyError

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
            hint="Call bake(frozen=True) or relax policy.require_frozen_lock.",
            context={"operation": "bake"},
        )


def ensure_network_allowed(*, policy: Policy, operation: str) -> None:
    if policy.network_mode == "offline":
        raise PolicyError(
            "Network operations are disabled by policy.",
            hint="Switch policy.network_mode to 'online' for this operation.",
            context={"operation": operation},
        )


def mutable_ref_policy_from(policy: Policy) -> MutableRefPolicy:
    return policy.mutable_ref_policy
