import importlib

CORE_MODULES = [
    "tundravm.image",
    "tundravm.cache",
    "tundravm.ir",
    "tundravm.compiler",
    "tundravm.backends",
    "tundravm.builders",
    "tundravm.fetch",
    "tundravm.lockfile",
    "tundravm.measure",
    "tundravm.deploy",
    "tundravm.policy",
    "tundravm.observability",
    "tundravm.modules",
]


def test_core_package_layout_modules_importable() -> None:
    for module_name in CORE_MODULES:
        module = importlib.import_module(module_name)
        assert module is not None
