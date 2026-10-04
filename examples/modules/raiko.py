"""Raiko, the TDX prover: built from source with cargo, run as ``raiko`` in group ``tdx``."""

from __future__ import annotations

from tundravm.declarative import Build, File, Fragment, Git, Install, Package, Unit, User

RAIKO_SOURCE = Git("https://github.com/NethermindEth/raiko.git", "feat/tdx")

RAIKO_BUILD_PACKAGES = (
    "build-essential",
    "pkg-config",
    "git",
    "clang",
    "libssl-dev",
    "libelf-dev",
)

RAIKO_CARGO_ENV = (
    (
        "RUSTFLAGS",
        "-C target-cpu=generic -C link-arg=-Wl,--build-id=none "
        "-C symbol-mangling-version=v0 -L /usr/lib/x86_64-linux-gnu",
    ),
    ("CARGO_HOME", "/build/.cargo"),
    ("CARGO_PROFILE_RELEASE_LTO", "thin"),
    ("CARGO_PROFILE_RELEASE_CODEGEN_UNITS", "1"),
    ("CARGO_PROFILE_RELEASE_PANIC", "abort"),
    ("CARGO_PROFILE_RELEASE_INCREMENTAL", "false"),
    ("CARGO_PROFILE_RELEASE_OPT_LEVEL", "3"),
    ("CARGO_TERM_COLOR", "never"),
)

RAIKO_UNIT = """\
[Unit]
Description=Raiko
After=tdxs.service
Requires=tdxs.service

[Service]
User=raiko
Group=tdx
Restart=on-failure
ExecStart=/usr/bin/raiko

[Install]
WantedBy=default.target
"""

RAIKO_ENV = """\
RAIKO_CONFIG=/etc/raiko/config.json
RAIKO_CHAIN_SPEC=/etc/raiko/chain-spec.json"""


def raiko(*, source: Git = RAIKO_SOURCE) -> Fragment:
    """Raiko from *source*: ``raiko-host`` (``tdx`` feature) installed as ``/usr/bin/raiko``.

    Requires ``tdxs()``: the unit orders after ``tdxs.service`` and the user's
    primary group is ``tdx``.
    """
    return Fragment(
        "raiko",
        requires=("tdxs",),
        items=(
            *(Package(name, role="build") for name in RAIKO_BUILD_PACKAGES),
            Build(
                "raiko",
                source,
                script="cargo fetch && cargo build --release --frozen --features tdx "
                "--package raiko-host",
                install=(Install("target/release/raiko-host", "/usr/bin/raiko"),),
                env=RAIKO_CARGO_ENV,
                cache_key=f"raiko-{source.ref}",
            ),
            User("raiko", home="/home/raiko", primary_group="tdx"),
            Unit("raiko.service", RAIKO_UNIT, enabled=True, after_init=True),
            File("/etc/raiko/env", RAIKO_ENV),
        ),
    )
