from tundravm.declarative import (
    Debian,
    Fragment,
    Package,
    Recipe,
    Variant,
    lock,
)


def test_lock_records_dependency_and_recipe_metadata() -> None:
    recipe = Recipe(
        "lockfile",
        items=(Package("curl"),),
        platform=Debian("bookworm", boot=None),
        variants=(
            Variant("default", outputs=("qemu",)),
            Variant("dev", add=Fragment("dev", items=(Package("jq"),))),
        ),
    )

    lockfile = lock(recipe).lockfile

    assert lockfile.version == 5
    assert lockfile.recipe["distribution"]["base"] == "debian/bookworm"
    assert "dialect" not in lockfile.recipe["compiler"]
    assert lockfile.recipe["profiles"]["default"]["outputs"] == ["qemu"]
    assert lockfile.recipe["profiles"]["dev"]["outputs"] == ["qemu"]
    assert lockfile.dependencies["default"] == ["curl"]
    assert lockfile.dependencies["dev"] == ["curl", "jq"]
    assert lockfile.recipe_digest
