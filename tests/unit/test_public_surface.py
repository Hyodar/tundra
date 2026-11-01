"""The public surface: one frozen ``__all__`` per namespace, and one home per name."""

from __future__ import annotations

import contextlib
import importlib
import inspect
import pkgutil
import re
from pathlib import Path

import pytest

import tundravm
from tundravm import _reference, declarative

# Changing one of these lists changes the public API: update it deliberately, in review.
ROOT = [
    "AptRepository",
    "Debian",
    "File",
    "Group",
    "Image",
    "Kernel",
    "Mkosi",
    "Package",
    "Profile",
    "RootFS",
    "Service",
    "Tree",
    "User",
    "__version__",
    "build",
    "compile",
    "doctor",
    "load",
    "lower",
]
PORTS = ["TCP", "TLS", "UDP", "AttestedTLS"]
SYSTEMD = [
    "Calendar",
    "DropIn",
    "Every",
    "Hardening",
    "HttpProbe",
    "Listen",
    "Notify",
    "Resources",
    "Shutdown",
    "TcpProbe",
]
COMPONENTS = [
    "EVIDENCE",
    "Component",
    "ComponentReady",
    "DevAccess",
    "Diagnostic",
    "Egress",
    "Endpoint",
    "Firewall",
    "JournalExport",
    "Lighthouse",
    "Nethermind",
    "Network",
    "Observability",
    "PrometheusRemoteWrite",
    "Proxy",
    "Raiko",
    "Resolved",
    "Rule",
    "ServiceReady",
    "ServiceRef",
    "SshAccess",
    "StaticAddress",
    "TaikoClient",
    "Tdxs",
    "TeeBase",
]
STORAGE = ["BrokerKey", "OperatorKey", "TpmSeal", "Volume", "VolumeDirectory"]
SECRETS = ["Credential", "Generated", "InstanceKey", "Secret"]
SOURCES = ["Build", "CargoBinary", "Git", "GoBinary", "Http", "Install", "Local"]
ERRORS = [
    "ArtifactError",
    "AttestationError",
    "BackendExecutionError",
    "DeploymentError",
    "ErrorCode",
    "LintError",
    "LockfileError",
    "MeasurementError",
    "PolicyError",
    "ReproducibilityError",
    "SourceError",
    "StateError",
    "TdxError",
    "ValidationError",
]

TESTING = [
    "UPDATE_GOLDEN_ENV",
    "BootResult",
    "BootUnavailable",
    "ConsoleScan",
    "FakeCall",
    "FakeFetch",
    "FakeMkosi",
    "FakeSources",
    "HttpResponse",
    "PROBE_DONE",
    "ProbeReport",
    "ImageFile",
    "InitStep",
    "Lintable",
    "PlannedImage",
    "Profiles",
    "UnitView",
    "assert_clean",
    "assert_diagnostic",
    "assert_probe",
    "assert_tree",
    "boot",
    "boot_unavailable",
    "build_unavailable",
    "image_file",
    "plan",
    "read_lock",
    "read_probe",
    "run_cli",
    "scan_console",
]

NAMESPACES = {
    "tundravm": ROOT,
    "tundravm.ports": PORTS,
    "tundravm.systemd": SYSTEMD,
    "tundravm.components": COMPONENTS,
    "tundravm.storage": STORAGE,
    "tundravm.secrets": SECRETS,
    "tundravm.sources": SOURCES,
    "tundravm.errors": ERRORS,
    "tundravm.testing": TESTING,
}
"""Every public namespace and its names."""

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
    "Local",
    "Resolved",
    "StaticAddress",
    "User",
]
"""Names the public surface keeps rather than re-spells: the internal model's own objects.

Everything else an image file writes is a value of the private ``tundravm._image``; the
internal ``Recipe``, ``Variant``, ``Fragment`` and the 43-field ``Service`` are not exported.
"""


def _home(name: str) -> object:
    (module,) = [module for module, names in NAMESPACES.items() if name in names]
    return getattr(importlib.import_module(module), name)


def test_the_reference_covers_every_namespace() -> None:
    assert _reference.MODULES == tuple(NAMESPACES)


def test_root_all_is_frozen() -> None:
    assert sorted(tundravm.__all__) == sorted(ROOT)
    assert len(tundravm.__all__) == len(set(tundravm.__all__))


def test_ports_all_is_frozen() -> None:
    assert sorted(importlib.import_module("tundravm.ports").__all__) == sorted(PORTS)


def test_systemd_all_is_frozen() -> None:
    assert sorted(importlib.import_module("tundravm.systemd").__all__) == sorted(SYSTEMD)


def test_components_all_is_frozen() -> None:
    assert sorted(importlib.import_module("tundravm.components").__all__) == sorted(COMPONENTS)


def test_storage_all_is_frozen() -> None:
    assert sorted(importlib.import_module("tundravm.storage").__all__) == sorted(STORAGE)


def test_secrets_all_is_frozen() -> None:
    assert sorted(importlib.import_module("tundravm.secrets").__all__) == sorted(SECRETS)


def test_sources_all_is_frozen() -> None:
    assert sorted(importlib.import_module("tundravm.sources").__all__) == sorted(SOURCES)


def test_errors_all_is_frozen() -> None:
    assert sorted(importlib.import_module("tundravm.errors").__all__) == sorted(ERRORS)


def test_testing_all_is_frozen() -> None:
    assert sorted(importlib.import_module("tundravm.testing").__all__) == sorted(TESTING)


@pytest.mark.parametrize(
    "module",
    [*NAMESPACES, "tundravm.declarative", "tundravm.declarative.utils"],
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


def test_every_public_object_has_one_home() -> None:
    homes: dict[int, list[str]] = {}
    for module in _reference.MODULES:
        imported = importlib.import_module(module)
        for name in imported.__all__:
            value = getattr(imported, name)
            if isinstance(value, type) or callable(value):
                homes.setdefault(id(value), []).append(f"{module}.{name}")
    assert [paths for paths in homes.values() if len(paths) > 1] == []


def test_the_root_does_not_spell_a_namespaced_name() -> None:
    for module, names in NAMESPACES.items():
        if module == "tundravm":
            continue
        for name in names:
            assert not hasattr(tundravm, name), f"tundravm.{name} is a second home of {module}"


def test_no_private_module_looks_like_a_namespace() -> None:
    public = {name.removeprefix("tundravm.") for name in _reference.MODULES}
    # The internal modules a reader could take for a namespace of the SDK are private.
    package = Path(tundravm.__file__ or "").parent
    for gone in ("image", "observability", "platforms"):
        assert gone not in public
        # A stale ``__pycache__`` directory would import as a namespace package, so the
        # check is for source files, not for the import failing.
        assert not (package / f"{gone}.py").exists(), gone
        assert not list((package / gone).glob("*.py")), gone
    components = importlib.import_module("tundravm.components")
    submodules = [m.name for m in pkgutil.iter_modules(components.__path__)]
    assert submodules and all(name.startswith("_") for name in submodules), submodules


def test_reused_names_are_the_declarative_objects() -> None:
    for name in REUSED_FROM_DECLARATIVE:
        value = _home(name)
        assert value is getattr(declarative, name, None) or value is getattr(
            declarative.utils, name
        ), name


def test_the_component_library_is_components() -> None:
    components = importlib.import_module("tundravm.components")
    shipped = [
        "DevAccess",
        "Firewall",
        "Lighthouse",
        "Nethermind",
        "Network",
        "Observability",
        "Raiko",
        "SshAccess",
        "TaikoClient",
        "Tdxs",
        "TeeBase",
    ]
    for name in shipped:
        value = getattr(components, name)
        assert issubclass(value, components.Component), name
        assert value.__module__.startswith("tundravm.components._"), name


def test_the_internal_model_is_not_exported() -> None:
    gone = ["Recipe", "Variant", "Fragment", "Task", "Unit", "Disk", "Key", "Secrets"]
    exported = {name for names in NAMESPACES.values() for name in names}
    for name in gone:
        assert name not in exported, name
        assert not hasattr(tundravm, name), name
        assert hasattr(declarative, name), f"{name} must stay importable from the internals"


def test_the_public_surface_says_profile() -> None:
    """No public signature or docstring, nor what compile and build return, says variant."""
    lifecycle = declarative.lifecycle
    values = [
        getattr(importlib.import_module(module), name)
        for module in _reference.MODULES
        for name in importlib.import_module(module).__all__
    ]
    found = []
    for value in [*values, lifecycle.Tree, lifecycle.Artifact]:
        members = [value, *vars(value).values()] if isinstance(value, type) else [value]
        for member in filter(callable, members):
            text = inspect.getdoc(member) or ""
            with contextlib.suppress(TypeError, ValueError):
                text += str(inspect.signature(member))
            if re.search(r"(?<!in)variant", text, re.IGNORECASE):
                found.append(getattr(member, "__qualname__", repr(member)))
    assert found == []


def test_the_api_reference_names_no_internal_model() -> None:
    """The reference speaks the public model: no recipe, fragment or variant."""
    found = re.findall(
        r".{0,30}\b(?:[Rr]ecipes?|[Ff]ragments?|[Vv]ariants?)\b.{0,30}", _reference.api_markdown()
    )
    assert found == []
