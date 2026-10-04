"""Attestation: the tdxs quote service, expected peer measurements and a strict policy.

Teaches ``Tdxs()``: it builds the tundra-tools attestation service from source and
runs it socket-activated on ``/var/tdxs.sock`` as user ``tdxs`` in group ``tdx``.
``issuer`` makes this VM produce quotes; ``validator`` makes it check peers' quotes
against ``expected_measurements``, the MRTD/RTMR values ``tundravm measure`` derives
from the image you trust. Services that talk to the socket join the ``tdx`` group.
``Policy(require_frozen_lock=True)`` refuses to bake without a lockfile.

    tundravm lock examples/06_attestation.py      # pin tundra-tools (network)
    tundravm bake examples/06_attestation.py --variant attester --out build/attestation
    tundravm measure build/attestation --allow-placeholder   # simulated artifact
"""

from tundravm.backends import InProcessBackend
from tundravm.declarative import Fragment, Package, Policy, Recipe, Service, User, Variant
from tundravm.declarative.utils import Tdxs

BOOT = ("linux-image-amd64", "systemd", "systemd-sysv", "udev", "kmod", "systemd-boot-efi")

# What a trusted attester measures to: replace with `tundravm measure --json` output.
PEER_MEASUREMENTS = (
    ("mrtd", "0" * 96),
    ("rtmr1", "0" * 96),
    ("rtmr2", "0" * 96),
)

# The app asks tdxs for quotes over the socket, so it joins the tdx group Tdxs declares.
app = Fragment(
    "app",
    items=(
        User("app", home="/var/lib/app", groups=("tdx",)),
        Service(
            "app",
            "/usr/bin/app --tdxs-socket /var/tdxs.sock",
            user="app",
            after=("tdxs.socket",),
            requires=("tdxs.socket",),
            restart="on-failure",
            wanted_by="minimal.target",
        ),
    ),
)

recipe = Recipe(
    name="attestation",
    base="debian/bookworm",
    policy=Policy(require_frozen_lock=True),
    common=Fragment(
        "base",
        items=(*(Package(name) for name in (*BOOT, "ca-certificates")), app),
    ),
    variants=(
        # Proves what it runs: issues TDX quotes, checks nothing.
        Variant("attester", target="qemu", add=Tdxs(issuer="tdx")),
        # Also checks peers: their quotes must match PEER_MEASUREMENTS and not be revoked.
        Variant(
            "verifier",
            target="qemu",
            add=Tdxs(
                issuer="tdx",
                validator="tdx",
                expected_measurements=PEER_MEASUREMENTS,
                check_revocations=True,
                get_collateral=True,
            ),
        ),
        # Azure attests through its own quote path; the validator also checks the IMDS token.
        Variant(
            "azure-verifier",
            target="azure",
            add=Tdxs(
                issuer="azure",
                validator="azure",
                expected_measurements=PEER_MEASUREMENTS,
                verify_imds=True,
            ),
        ),
    ),
)

backend = InProcessBackend()  # simulated artifacts; LimaMkosiBackend() bakes real images
