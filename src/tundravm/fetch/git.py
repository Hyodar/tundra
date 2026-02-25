"""Git fetch with immutable resolution, tree verification, and cache support."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tundravm.errors import PolicyError, ReproducibilityError, ValidationError
from tundravm.policy import Policy, ensure_network_allowed, mutable_ref_policy_from

MutableRefPolicy = Literal["warn", "error", "allow"]

COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class MutableRefWarning(UserWarning):
    """Warning raised when fetching a mutable git ref."""


@dataclass(frozen=True, slots=True)
class GitFetchResult:
    path: Path
    commit: str
    tree_hash: str
    mutable_ref: bool


def fetch_git(
    repo: str,
    *,
    ref: str,
    tree_hash: str | None,
    cache_dir: str | Path,
    mutable_ref_policy: MutableRefPolicy | None = None,
    policy: Policy | None = None,
) -> GitFetchResult:
    """Fetch git content, verify tree hash, and cache by commit/tree identity."""
    if policy is not None:
        ensure_network_allowed(policy=policy, operation="fetch_git")
    if not ref:
        raise ValidationError("fetch_git() requires a ref.")
    if mutable_ref_policy is None:
        mutable_ref_policy = mutable_ref_policy_from(policy) if policy is not None else "warn"
    expected_tree_hash = tree_hash or ""
    if not expected_tree_hash and (policy is None or policy.require_integrity):
        raise ValidationError("fetch_git() requires a tree_hash when integrity policy is enabled.")

    mutable_ref = not COMMIT_PATTERN.fullmatch(ref)
    _enforce_mutable_ref_policy(ref=ref, policy=mutable_ref_policy, mutable_ref=mutable_ref)

    resolved_commit = _resolve_commit(repo=repo, ref=ref)
    cache_root = Path(cache_dir)
    cache_root.mkdir(parents=True, exist_ok=True)
    checkout_path = (
        cache_root / f"{resolved_commit}-{expected_tree_hash}" if expected_tree_hash else None
    )

    if checkout_path is not None and checkout_path.exists():
        _verify_cached_checkout(
            checkout_path=checkout_path,
            tree_hash=expected_tree_hash,
            commit=resolved_commit,
        )
        return GitFetchResult(
            path=checkout_path,
            commit=resolved_commit,
            tree_hash=expected_tree_hash,
            mutable_ref=mutable_ref,
        )

    temp_root = Path(tempfile.mkdtemp(prefix="tdx-git-", dir=str(cache_root)))
    try:
        _run_git(["clone", "--quiet", repo, str(temp_root)])
        _run_git(["checkout", "--quiet", resolved_commit], cwd=temp_root)
        actual_tree = _run_git(["rev-parse", "HEAD^{tree}"], cwd=temp_root)
        if expected_tree_hash and actual_tree != expected_tree_hash:
            raise ReproducibilityError(
                "Git tree hash mismatch.",
                hint="Pin the expected tree hash to the resolved immutable revision.",
                context={
                    "operation": "fetch_git",
                    "repo": repo,
                    "ref": ref,
                    "commit": resolved_commit,
                    "expected": expected_tree_hash,
                    "actual": actual_tree,
                },
            )
        resolved_tree_hash = expected_tree_hash or actual_tree
        final_checkout_path = cache_root / f"{resolved_commit}-{resolved_tree_hash}"
        if final_checkout_path.exists():
            _verify_cached_checkout(
                checkout_path=final_checkout_path,
                tree_hash=resolved_tree_hash,
                commit=resolved_commit,
            )
        else:
            shutil.move(str(temp_root), final_checkout_path)
    finally:
        if temp_root.exists():
            shutil.rmtree(temp_root, ignore_errors=True)

    resolved_tree_hash = expected_tree_hash or _run_git(
        ["rev-parse", "HEAD^{tree}"],
        cwd=final_checkout_path,
    )
    return GitFetchResult(
        path=final_checkout_path,
        commit=resolved_commit,
        tree_hash=resolved_tree_hash,
        mutable_ref=mutable_ref,
    )


def _enforce_mutable_ref_policy(*, ref: str, policy: MutableRefPolicy, mutable_ref: bool) -> None:
    if not mutable_ref:
        return
    if policy == "allow":
        return
    if policy == "warn":
        warnings.warn(
            f"Mutable git ref `{ref}` was requested; result is not inherently reproducible.",
            MutableRefWarning,
            stacklevel=2,
        )
        return
    if policy == "error":
        raise PolicyError(
            "Mutable git refs are not allowed by policy.",
            hint="Use a full 40-char commit SHA or relax mutable_ref_policy.",
            context={"operation": "fetch_git", "ref": ref, "policy": policy},
        )
    raise ValidationError(f"Unsupported mutable_ref_policy value: {policy}")


def _resolve_commit(*, repo: str, ref: str) -> str:
    if COMMIT_PATTERN.fullmatch(ref):
        return ref
    output = _run_git(["ls-remote", repo, ref])
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        raise ValidationError(
            "Unable to resolve git ref.",
            hint="Ensure the repository and ref are valid and reachable.",
            context={"operation": "fetch_git", "repo": repo, "ref": ref},
        )
    return lines[0].split()[0]


def _verify_cached_checkout(*, checkout_path: Path, tree_hash: str, commit: str) -> None:
    cached_commit = _run_git(["rev-parse", "HEAD"], cwd=checkout_path)
    cached_tree = _run_git(["rev-parse", "HEAD^{tree}"], cwd=checkout_path)
    if cached_commit != commit or cached_tree != tree_hash:
        raise ReproducibilityError(
            "Cached git checkout does not match expected commit/tree.",
            hint="Delete cache entry and refetch immutable source.",
            context={
                "operation": "fetch_git",
                "path": str(checkout_path),
                "expected_commit": commit,
                "actual_commit": cached_commit,
                "expected_tree": tree_hash,
                "actual_tree": cached_tree,
            },
        )


def _run_git(argv: list[str], cwd: Path | None = None) -> str:
    command = ["git", *argv]
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise ValidationError(
            "Git command failed.",
            hint="Inspect repository/ref inputs and git installation.",
            context={
                "operation": "fetch_git",
                "argv": " ".join(command),
                "stderr": completed.stderr.strip(),
            },
        )
    return completed.stdout.strip()
