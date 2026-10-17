"""Attestation: the tdxs quote service, expected peer measurements and a strict policy.

Teaches ``Tdxs()``: it builds the tundra-tools attestation service from source and
runs it socket-activated on ``/var/tdxs.sock`` as user ``tdxs`` in group ``tdx``.
``issuer`` makes this VM produce quotes; ``validator`` makes it check peers' quotes
against ``expected_measurements``. ``Tdxs.from_policy()`` reads them from the policy
file ``tundravm measure --export-policy`` writes for the image you trust (RTMR0..2;
pass ``mrtd=`` to check MRTD too). Services that talk to the socket join the ``tdx``
group. ``Policy(require_frozen_lock=True)`` refuses to bake without a lockfile.

    tundravm lock examples/06_attestation.py      # pin tundra-tools (network)
    tundravm bake examples/06_attestation.py --variant attester --build build/attestation
    tundravm measure --build build/attestation --scheme rtmr \
        --export-policy examples/peer.policy.json

The committed ``peer.policy.json`` came from a simulated (in-process) bake with
``--allow-placeholder``: its ``note`` marks it as a placeholder, and the verifiers
below accept it only because they pass ``allow_placeholder=True``.
"""

from tundravm.backends import InProcessBackend
from tundravm.declarative import (
    Debian,
    Fragment,
    Package,
    Policy,
    Recipe,
    Resources,
    Service,
    User,
    Variant,
)
from tundravm.declarative.utils import Tdxs

# What a trusted attester measures to: `tundravm measure --export-policy` output.
PEER_POLICY = Resources.from_module(__name__).file("peer.policy.json")

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
    platform=Debian("bookworm"),
    policy=Policy(require_frozen_lock=True),
    items=(Package("ca-certificates"), app),
    variants=(
        # Proves what it runs: issues TDX quotes, checks nothing.
        Variant("attester", outputs=("qemu",), add=Tdxs(issuer="tdx")),
        # Also checks peers: their quotes must match PEER_POLICY and not be revoked.
        Variant(
            "verifier",
            outputs=("qemu",),
            add=Tdxs.from_policy(
                PEER_POLICY,
                issuer="tdx",
                check_revocations=True,
                get_collateral=True,
                allow_placeholder=True,  # the committed policy is a placeholder
            ),
        ),
        # Azure attests through its own quote path; the validator also checks the IMDS token.
        Variant(
            "azure-verifier",
            outputs=("azure",),
            add=Tdxs.from_policy(
                PEER_POLICY,
                issuer="azure",
                validator="azure",
                verify_imds=True,
                allow_placeholder=True,
            ),
        ),
    ),
)

backend = InProcessBackend()  # simulated artifacts; LimaMkosiBackend() bakes real images
