"""The public surface: ``tundravm.__all__`` is frozen and every exported name imports."""

from __future__ import annotations

import importlib

import pytest

import tundravm
from tundravm import components, declarative, image

# Changing this list changes the public API: update it deliberately, in review.
EXPECTED_TOP_LEVEL = [
    "ArtifactError",
    "AttestationError",
    "AttestedTLS",
    "BackendExecutionError",
    "BrokerKey",
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
    "JournalExport",
    "Kernel",
    "LintError",
    "Listen",
    "LockfileError",
    "MeasurementError",
    "Mkosi",
    "Network",
    "Notify",
    "Observability",
    "OperatorKey",
    "Package",
    "PolicyError",
    "Profile",
    "PrometheusRemoteWrite",
    "ReproducibilityError",
    "Resolved",
    "Resources",
    "RootFS",
    "Secret",
    "Service",
    "ServiceReady",
    "ServiceRef",
    "Shutdown",
    "SourceError",
    "StateError",
    "StaticAddress",
    "TCP",
    "TLS",
    "TcpProbe",
    "TdxError",
    "TpmSeal",
    "Tree",
    "UDP",
    "User",
    "ValidationError",
    "Volume",
    "VolumeDirectory",
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
    "AttestedTLS",
    "BrokerKey",
    "Component",
    "Debian",
    "Egress",
    "Endpoint",
    "File",
    "Image",
    "Kernel",
    "Mkosi",
    "Notify",
    "OperatorKey",
    "Package",
    "Profile",
    "Resources",
    "RootFS",
    "Secret",
    "Service",
    "Shutdown",
    "TCP",
    "TLS",
    "TpmSeal",
    "Tree",
    "UDP",
    "Volume",
    "VolumeDirectory",
]

FROM_COMPONENTS = [
    "DevAccess",
    "JournalExport",
    "Network",
    "Observability",
    "PrometheusRemoteWrite",
]
"""Values an image slot takes that are components of the shipped library: the same objects
as :mod:`tundravm.components` exports (``network=``, ``dev=``, ``observability=``)."""


def test_top_level_all_is_frozen() -> None:
    assert sorted(tundravm.__all__) == sorted(EXPECTED_TOP_LEVEL)
    assert len(tundravm.__all__) == len(set(tundravm.__all__))


@pytest.mark.parametrize(
    "module",
    [
        "tundravm",
        "tundravm.image",
        "tundravm.components",
        "tundravm.declarative",
        "tundravm.declarative.utils",
    ],
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


def test_the_slot_components_come_from_the_component_library() -> None:
    for name in FROM_COMPONENTS:
        value = getattr(tundravm, name)
        assert value is getattr(components, name), name
        assert value.__module__.startswith("tundravm.components"), name


def test_the_component_library_is_components() -> None:
    assert sorted(components.__all__) == sorted(
        [
            "DevAccess",
            "EVIDENCE",
            "Firewall",
            "JournalExport",
            "Nethermind",
            "Network",
            "Observability",
            "PrometheusRemoteWrite",
            "Raiko",
            "SshAccess",
            "TaikoClient",
            "Tdxs",
            "TeeBase",
        ]
    )
    for name in components.__all__:
        value = getattr(components, name)
        if isinstance(value, type) and name not in ("JournalExport", "PrometheusRemoteWrite"):
            assert issubclass(value, tundravm.Component), name


def test_the_internal_model_is_not_exported() -> None:
    gone = ["Recipe", "Variant", "Fragment", "Task", "Unit", "Disk", "Key", "Secrets", "Firewall"]
    for name in gone:
        assert name not in tundravm.__all__, name
        assert not hasattr(tundravm, name), name
        assert hasattr(declarative, name), f"{name} must stay importable from the internals"
