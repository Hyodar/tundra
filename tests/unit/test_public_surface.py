"""The public surface: ``tundravm.__all__`` is frozen and every exported name imports."""

from __future__ import annotations

import importlib

import pytest

import tundravm
from tundravm import declarative

# Changing this list changes the public API: update it deliberately, in review.
EXPECTED_TOP_LEVEL = [
    "Artifact",
    "ArtifactError",
    "Attestation",
    "Azure",
    "Backend",
    "BackendExecutionError",
    "Build",
    "Cargo",
    "Debloat",
    "Declaration",
    "Deployment",
    "DeploymentError",
    "Diagnostic",
    "Directory",
    "Disk",
    "Dotnet",
    "Entry",
    "Evidence",
    "FetchedSource",
    "File",
    "Fragment",
    "Gcp",
    "Git",
    "Go",
    "Group",
    "Hook",
    "Http",
    "Init",
    "Install",
    "Kernel",
    "Key",
    "LintError",
    "Lock",
    "LockfileError",
    "MeasurementError",
    "Measurements",
    "Mkosi",
    "Package",
    "Partition",
    "Pin",
    "Policy",
    "PolicyError",
    "Qemu",
    "Recipe",
    "Repository",
    "ReproducibilityError",
    "Resolved",
    "RuntimeTools",
    "Sbom",
    "Schema",
    "Secret",
    "SecretEnv",
    "SecretFile",
    "Secrets",
    "Service",
    "Setting",
    "SourceError",
    "StateError",
    "TdxError",
    "Template",
    "Tree",
    "Unit",
    "User",
    "ValidationError",
    "Variant",
    "Why",
    "__version__",
    "attest",
    "bake",
    "compile",
    "doctor",
    "evidence",
    "explain_why",
    "fetch",
    "lint",
    "load",
    "load_recipe",
    "lock",
    "lock_status",
    "lower",
    "read_artifacts",
    "read_lock",
    "resolve",
    "resolve_all",
    "sbom",
    "verify_artifact",
    "write_lock",
]


def test_top_level_all_is_frozen() -> None:
    assert sorted(tundravm.__all__) == EXPECTED_TOP_LEVEL
    assert len(tundravm.__all__) == len(set(tundravm.__all__))


@pytest.mark.parametrize(
    "module", ["tundravm", "tundravm.declarative", "tundravm.declarative.utils"]
)
def test_every_exported_name_imports(module: str) -> None:
    imported = importlib.import_module(module)
    exported = imported.__all__
    assert exported, module
    assert len(exported) == len(set(exported)), module
    missing = [name for name in exported if not hasattr(imported, name)]
    assert missing == [], f"{module}.__all__ names it does not define: {missing}"
    namespace: dict[str, object] = {}
    exec(f"from {module} import *", namespace)
    assert set(exported) <= set(namespace), module


def test_top_level_names_are_the_declarative_ones() -> None:
    for name in set(tundravm.__all__) & set(declarative.__all__):
        assert getattr(tundravm, name) is getattr(declarative, name), name
