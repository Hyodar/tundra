"""The public surface: ``tundravm.__all__`` is frozen and every exported name imports."""

from __future__ import annotations

import importlib

import pytest

import tundravm
from tundravm import declarative, image

# Changing this list changes the public API: update it deliberately, in review.
EXPECTED_TOP_LEVEL = [
    "ArtifactError",
    "AttestationError",
    "BackendExecutionError",
    "Build",
    "Calendar",
    "CargoBinary",
    "Component",
    "ComponentReady",
    "Credential",
    "Debian",
    "DeploymentError",
    "DevAccess",
    "Diagnostic",
    "DropIn",
    "Egress",
    "Endpoint",
    "Every",
    "File",
    "Git",
    "GoBinary",
    "Group",
    "Hardening",
    "Http",
    "HttpProbe",
    "IMAGE_OBJECT_NAMES",
    "Image",
    "Install",
    "Kernel",
    "LintError",
    "Listen",
    "LockfileError",
    "MeasurementError",
    "Mkosi",
    "Network",
    "Notify",
    "Package",
    "PolicyError",
    "Profile",
    "ReproducibilityError",
    "Resolved",
    "Resources",
    "Service",
    "ServiceReady",
    "ServiceRef",
    "Shutdown",
    "SourceError",
    "StateError",
    "StaticAddress",
    "TCP",
    "TcpProbe",
    "TdxError",
    "Tree",
    "UDP",
    "User",
    "ValidationError",
    "__version__",
    "bake",
    "compile",
    "doctor",
    "load",
    "lower",
]

REUSED_FROM_DECLARATIVE = [
    "Build",
    "Calendar",
    "CargoBinary",
    "Diagnostic",
    "DropIn",
    "Every",
    "Git",
    "GoBinary",
    "Group",
    "Hardening",
    "Http",
    "Install",
    "Listen",
    "Resolved",
    "StaticAddress",
    "User",
]
"""Names the new surface keeps rather than re-spells: the same objects, under the same name.

Everything else a recipe writes is a new value in :mod:`tundravm.image`; the internal
``Recipe``, ``Variant``, ``Fragment`` and the 43-field ``Service`` are not exported at all.
"""

NEW_IN_IMAGE = [
    "Component",
    "Debian",
    "DevAccess",
    "Egress",
    "Endpoint",
    "File",
    "Image",
    "Kernel",
    "Mkosi",
    "Network",
    "Notify",
    "Package",
    "Profile",
    "Resources",
    "Service",
    "Shutdown",
    "TCP",
    "Tree",
    "UDP",
]


def test_top_level_all_is_frozen() -> None:
    assert sorted(tundravm.__all__) == sorted(EXPECTED_TOP_LEVEL)
    assert len(tundravm.__all__) == len(set(tundravm.__all__))


@pytest.mark.parametrize(
    "module",
    ["tundravm", "tundravm.image", "tundravm.declarative", "tundravm.declarative.utils"],
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


def test_reused_names_are_the_declarative_objects() -> None:
    for name in REUSED_FROM_DECLARATIVE:
        assert getattr(tundravm, name) is getattr(declarative, name, None) or getattr(
            tundravm, name
        ) is getattr(declarative.utils, name), name


def test_the_new_values_come_from_the_image_package() -> None:
    for name in NEW_IN_IMAGE:
        value = getattr(tundravm, name)
        assert value is getattr(image, name), name
        assert value.__module__.startswith("tundravm.image"), name


def test_the_internal_model_is_not_exported() -> None:
    gone = ["Recipe", "Variant", "Fragment", "Task", "Unit", "Disk", "Key", "Secrets", "Firewall"]
    for name in gone:
        assert name not in tundravm.__all__, name
        assert not hasattr(tundravm, name), name
        assert hasattr(declarative, name), f"{name} must stay importable from the internals"
