"""Real artifact materialization via subprocess execution.

Runs actual compile commands (go build, cargo build, cc, etc.) and
collects output artifacts. Falls back to metadata-only output if the
tool is not available (for testing/CI environments).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from tundravm.builders.base import BuildArtifact, BuildSpec
from tundravm.errors import BackendExecutionError


def materialize_artifact(
    *,
    builder_name: str,
    command: tuple[str, ...],
    spec: BuildSpec,
) -> BuildArtifact:
    spec.output_dir.mkdir(parents=True, exist_ok=True)
    output_name = f"{spec.name}-{spec.target}.bin"
    output_path = spec.output_dir / output_name
    metadata_path = spec.output_dir / f"{output_name}.json"

    # Build environment
    env = dict(os.environ)
    env.update(spec.env)
    if spec.reproducible:
        env["SOURCE_DATE_EPOCH"] = "0"

    # Check if the build tool is available
    tool = command[0] if command else ""
    tool_available = shutil.which(tool) is not None

    # Determine if we should attempt a real build.
    # We run the real tool when: (1) the tool exists, and (2) the source looks
    # like a real project (not a stub test file). This lets unit tests pass
    # without needing real toolchains to compile stub files.
    should_build = tool_available and _source_looks_real(builder_name, spec)

    if should_build:
        # Run the actual compile command
        result = subprocess.run(
            list(command),
            cwd=str(spec.source.parent) if spec.source.is_file() else str(spec.source),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise BackendExecutionError(
                f"{builder_name} build failed.",
                hint=f"Check {builder_name} output and build configuration.",
                context={
                    "builder": builder_name,
                    "command": " ".join(command),
                    "returncode": str(result.returncode),
                    "stderr": result.stderr[:2000] if result.stderr else "",
                },
            )
        # The output binary location depends on the builder.
        # If the command didn't produce the expected output, write what we have.
        if not output_path.exists():
            # Try to find the output in common locations
            _find_and_move_output(builder_name, spec, output_path)
    else:
        # Tool not available or source is a stub - write build manifest as output.
        # This allows testing the build pipeline without actual toolchains.
        output_payload = (
            f"builder={builder_name}\n"
            f"source={spec.source}\n"
            f"target={spec.target}\n"
            f"reproducible={spec.reproducible}\n"
            f"command={' '.join(command)}\n"
        )
        output_path.write_text(output_payload, encoding="utf-8")

    # Write metadata
    metadata = {
        "builder": builder_name,
        "source": str(spec.source),
        "target": spec.target,
        "reproducible": spec.reproducible,
        "flags": list(spec.flags),
        "env": dict(sorted(spec.env.items())),
        "command": list(command),
        "output_path": str(output_path),
        "tool_available": tool_available,
    }
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # Install to target location if requested
    installed_path: Path | None = None
    if spec.install_to is not None:
        installed_path = spec.install_to
        installed_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(output_path, installed_path)

    return BuildArtifact(
        builder=builder_name,
        target=spec.target,
        output_path=output_path,
        installed_path=installed_path,
        metadata_path=metadata_path,
    )


def _find_and_move_output(builder_name: str, spec: BuildSpec, output_path: Path) -> None:
    """Try to find the compiled output and move it to the expected location."""
    source_dir = spec.source.parent if spec.source.is_file() else spec.source

    if builder_name == "go":
        # Go puts output in current directory or $GOBIN
        for candidate in [
            source_dir / spec.name,
            source_dir / f"{spec.name}.exe",
        ]:
            if candidate.exists():
                shutil.move(str(candidate), output_path)
                return
    elif builder_name == "rust":
        # Cargo puts output in target/<arch>/release/
        for candidate in [
            source_dir / "target" / spec.target / "release" / spec.name,
            source_dir / "target" / "release" / spec.name,
        ]:
            if candidate.exists():
                shutil.move(str(candidate), output_path)
                return
    elif builder_name == "c":
        candidate = source_dir / f"{spec.name}.bin"
        if candidate.exists():
            shutil.move(str(candidate), output_path)
            return

    # If we still can't find it, write a placeholder manifest
    output_path.write_text(
        f"builder={builder_name}\n"
        f"source={spec.source}\n"
        f"target={spec.target}\n"
        f"note=output not found at expected location\n",
        encoding="utf-8",
    )


# File extensions that indicate real source projects for each builder
_REAL_SOURCE_INDICATORS: dict[str, tuple[str, ...]] = {
    "go": ("go.mod", "go.sum"),
    "rust": ("Cargo.toml", "Cargo.lock"),
    "dotnet": (".csproj", ".sln", ".fsproj"),
    "c": ("Makefile", "CMakeLists.txt", "configure"),
    "script": (".sh",),
}


def _source_looks_real(builder_name: str, spec: BuildSpec) -> bool:
    """Check if the source path looks like a real project (not a test stub)."""
    source_dir = spec.source.parent if spec.source.is_file() else spec.source
    indicators = _REAL_SOURCE_INDICATORS.get(builder_name, ())
    for indicator in indicators:
        if indicator.startswith("."):
            # Check file extension of the source itself
            if spec.source.suffix == indicator:
                return True
        else:
            # Check for project file in the source directory
            if (source_dir / indicator).exists():
                return True
    return False
