import importlib

CORE_MODULES = [
    "tdx.image",
    "tdx.ir",
    "tdx.compiler",
    "tdx.backends",
    "tdx.builders",
    "tdx.fetch",
    "tdx.lockfile",
    "tdx.measure",
    "tdx.deploy",
    "tdx.modules",
]


def test_core_package_layout_modules_importable() -> None:
    for module_name in CORE_MODULES:
        module = importlib.import_module(module_name)
        assert module is not None
