import json
from pathlib import Path

import pytest

from tundravm.builders import CBuilder, DotNetBuilder, GoBuilder, RustBuilder, ScriptBuilder
from tundravm.builders.base import Builder, BuildSpec


@pytest.mark.parametrize(
    ("builder", "expected_name"),
    [
        (GoBuilder(), "go"),
        (RustBuilder(), "rust"),
        (DotNetBuilder(), "dotnet"),
        (CBuilder(), "c"),
        (ScriptBuilder(), "script"),
    ],
)
def test_builders_produce_installable_artifacts(
    tmp_path: Path,
    builder: Builder,
    expected_name: str,
) -> None:
    source = tmp_path / "src.txt"
    source.write_text("source\n", encoding="utf-8")
    install_to = tmp_path / "install" / f"{expected_name}.bin"

    spec = BuildSpec(
        name=f"{expected_name}-app",
        source=source,
        target="x86_64",
        output_dir=tmp_path / "out",
        install_to=install_to,
        reproducible=True,
        flags=("--verbose",),
        env={"FOO": "bar"},
    )

    artifact = builder.build(spec)
    assert artifact.metadata_path is not None
    metadata = json.loads(artifact.metadata_path.read_text(encoding="utf-8"))

    assert artifact.builder == expected_name
    assert artifact.target == "x86_64"
    assert artifact.output_path.exists()
    assert artifact.installed_path == install_to
    assert install_to.exists()
    assert install_to.read_bytes() == artifact.output_path.read_bytes()
    assert metadata["reproducible"] is True
    assert metadata["target"] == "x86_64"


def test_reproducibility_flag_influences_go_builder_command(tmp_path: Path) -> None:
    source = tmp_path / "main.go"
    source.write_text("package main\n", encoding="utf-8")
    builder = GoBuilder()

    reproducible_artifact = builder.build(
        BuildSpec(
            name="repro",
            source=source,
            target="x86_64",
            output_dir=tmp_path / "out-repro",
            reproducible=True,
        ),
    )
    non_repro_artifact = builder.build(
        BuildSpec(
            name="non-repro",
            source=source,
            target="x86_64",
            output_dir=tmp_path / "out-non-repro",
            reproducible=False,
        ),
    )

    assert "-trimpath" in reproducible_artifact.output_path.read_text(encoding="utf-8")
    assert "-trimpath" not in non_repro_artifact.output_path.read_text(encoding="utf-8")
