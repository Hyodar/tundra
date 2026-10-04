"""Nethermind execution client: a deterministic ``dotnet publish``, run as ``nethermind-surge``."""

from __future__ import annotations

from tundravm.declarative import Build, File, Fragment, Git, Install, Package, Unit, User

NETHERMIND_SOURCE = Git("https://github.com/NethermindEth/nethermind.git", "1.32.3")
NETHERMIND_PROJECT = "src/Nethermind/Nethermind.Runner"
NETHERMIND_RUNTIME = "linux-x64"

NETHERMIND_BUILD_PACKAGES = ("dotnet-sdk-10.0", "dotnet-runtime-10.0", "build-essential", "git")

NETHERMIND_DOTNET_ENV = (
    ("DOTNET_CLI_TELEMETRY_OPTOUT", "1"),
    ("DOTNET_SKIP_FIRST_TIME_EXPERIENCE", "1"),
    ("DOTNET_NOLOGO", "1"),
    ("DOTNET_CLI_HOME", "/tmp/dotnet"),
    ("NUGET_PACKAGES", "/tmp/nuget"),
)

NETHERMIND_PUBLISH_PROPERTIES = (
    ("Deterministic", "true"),
    ("ContinuousIntegrationBuild", "true"),
    ("PublishSingleFile", "true"),
    ("BuildTimestamp", "0"),
    ("Commit", "0" * 40),
    ("PublishReadyToRun", "false"),
    ("DebugType", "none"),
    ("IncludeAllContentForSelfExtract", "true"),
    ("IncludePackageReferencesDuringMarkupCompilation", "true"),
    ("EmbedUntrackedSources", "true"),
    ("PublishRepositoryUrl", "true"),
)

NETHERMIND_UNIT = """\
[Unit]
Description=Nethermind Surge

[Service]
User=nethermind-surge
Group=eth
Restart=on-failure
LimitNOFILE=1048576
EnvironmentFile=/etc/nethermind-surge/env
ExecStart=/usr/bin/nethermind \\
--config /etc/nethermind-surge/config.json \\
--datadir /home/nethermind-surge/data \\
--JsonRpc.EngineHost 0.0.0.0 \\
--JsonRpc.EnginePort 8551

[Install]
WantedBy=default.target
"""

NETHERMIND_ENV = """\
NETHERMIND_CONFIG=/etc/nethermind-surge/config.json
NETHERMIND_DATADIR=/persistent/nethermind
NETHERMIND_JSONRPC_ENGINEHOST=127.0.0.1
NETHERMIND_JSONRPC_ENGINEPORT=8551
NETHERMIND_JSONRPC_HOST=127.0.0.1
NETHERMIND_JSONRPC_PORT=8545
NETHERMIND_JSONRPC_JWTSECRETFILE=/persistent/jwt/jwt.hex"""


def _publish_script(name: str, project: str, runtime: str) -> str:
    properties = " ".join(f"-p:{key}={value}" for key, value in NETHERMIND_PUBLISH_PROPERTIES)
    return (
        f"dotnet restore {project} --runtime {runtime} --disable-parallel --force && "
        f"dotnet publish {project} --configuration Release --runtime {runtime} "
        f"--self-contained true --output /build/{name}/publish {properties}"
    )


def nethermind(*, source: Git = NETHERMIND_SOURCE) -> Fragment:
    """Nethermind at *source*: the runner binary, ``NLog.config`` and ``plugins``.

    The ``eth`` group the user joins is the recipe's to declare.
    """
    etc = "/etc/nethermind-surge"
    return Fragment(
        "nethermind",
        items=(
            *(Package(name, role="build") for name in NETHERMIND_BUILD_PACKAGES),
            Build(
                "nethermind",
                source,
                script=_publish_script("nethermind", NETHERMIND_PROJECT, NETHERMIND_RUNTIME),
                install=(
                    Install("publish/nethermind", "/usr/bin/nethermind"),
                    Install("publish/NLog.config", f"{etc}/NLog.config", mode=0o644),
                    Install("publish/plugins", f"{etc}/plugins", mode=None, directory=True),
                ),
                env=NETHERMIND_DOTNET_ENV,
                cache_key=f"nethermind-{source.ref}-{NETHERMIND_RUNTIME}",
            ),
            User("nethermind-surge", home="/home/nethermind-surge", groups=("eth",)),
            Unit("nethermind-surge.service", NETHERMIND_UNIT, enabled=True, after_init=True),
            File(f"{etc}/env", NETHERMIND_ENV),
        ),
    )
