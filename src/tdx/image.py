"""Core image object placeholder for the SDK scaffold."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Image:
    """Represents an image recipe root."""

    build_dir: Path = Path("build")
