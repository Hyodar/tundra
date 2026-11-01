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

    assert lockfile.version == 8
    assert lockfile.image["distribution"]["base"] == "debian/bookworm"
    assert "dialect" not in lockfile.image["compiler"]
    assert lockfile.image["profiles"]["default"]["outputs"] == ["qemu"]
    assert lockfile.image["profiles"]["dev"]["outputs"] == ["qemu"]
    assert lockfile.dependencies["default"] == ["curl", "openssl"]
    assert lockfile.dependencies["dev"] == ["curl", "jq", "openssl"]
    assert lockfile.image_digest
