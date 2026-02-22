from pathlib import Path

from tdx import Image


def test_image_default_build_dir() -> None:
    image = Image()
    assert image.build_dir == Path("build")
