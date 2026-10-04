"""Public package entrypoint for the TDX VM SDK."""

__version__ = "0.1.0"

from .check import Diagnostic
from .diff import FileChange, TreeDiff
from .errors import (
    BackendExecutionError,
    DeploymentError,
    LintError,
    LockfileError,
    MeasurementError,
    PolicyError,
    ReproducibilityError,
    StateError,
    TdxError,
    ValidationError,
)
from .image import Applicable, Image
from .measure.model import Measurements
from .models import (
    BakeRequest,
    BakeResult,
    CompileResult,
    DebloatConfig,
    Kernel,
    ProfileState,
    RecipeState,
    SecretSchema,
    SecretSpec,
    SecretTarget,
)
from .policy import Policy
from .profile import Profile
from .recipe import load_recipe
from .source import (
    CargoBuild,
    DotnetBuild,
    GitSource,
    GoBuild,
    HttpSource,
    ScriptBuild,
    SourceBuild,
)

__all__ = [
    "Applicable",
    "BackendExecutionError",
    "BakeRequest",
    "BakeResult",
    "CargoBuild",
    "CompileResult",
    "DebloatConfig",
    "DeploymentError",
    "Diagnostic",
    "DotnetBuild",
    "FileChange",
    "GitSource",
    "GoBuild",
    "HttpSource",
    "Image",
    "Kernel",
    "LintError",
    "LockfileError",
    "MeasurementError",
    "Measurements",
    "PolicyError",
    "Policy",
    "Profile",
    "ProfileState",
    "RecipeState",
    "ReproducibilityError",
    "ScriptBuild",
    "SecretSchema",
    "SecretSpec",
    "SecretTarget",
    "SourceBuild",
    "StateError",
    "TdxError",
    "TreeDiff",
    "ValidationError",
    "__version__",
    "load_recipe",
]
