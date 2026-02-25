from pathlib import Path

from tundravm import Image


def test_image_default_build_dir() -> None:
    image = Image()
    assert image.build_dir == Path("build")
    assert image.state.default_profile == "default"
    assert "default" in image.state.profiles
