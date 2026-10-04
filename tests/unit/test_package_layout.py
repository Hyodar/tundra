import importlib

CORE_MODULES = [
    "tundravm._image",
    "tundravm.compiler",
    "tundravm.backends",
    "tundravm.lockfile",
    "tundravm.measure",
    "tundravm.deploy",
    "tundravm.policy",
    "tundravm.observability",
    "tundravm._modules",
    "tundravm.cli",
    "tundravm.recipe",
    "tundravm.check",
    "tundravm.diff",
    "tundravm.explain",
    "tundravm.declarative",
    "tundravm.testing",
]


def test_core_package_layout_modules_importable() -> None:
    for module_name in CORE_MODULES:
        module = importlib.import_module(module_name)
        assert module is not None
