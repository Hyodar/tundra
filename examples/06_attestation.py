"""Attestation: the evidence service, a verifier of peers, and an attested TLS ingress.

Teaches:

- the evidence service: every image holds ``Tdxs()`` (tundra-tools' ``tdxs``,
  socket-activated on ``/var/tdxs.sock``), which produces the quote an attestation is
  checked against. Passing a ``Tdxs`` in ``components=`` replaces it: here one that also
  validates peers' quotes against the measurements of a policy file
  (``Tdxs.from_policy()``; a build's ``measurements.json`` holds one). The same
  component's ``source=`` points the build at a mirror or a vendored checkout of
  tundra-tools.
- a service that asks for quotes joins the ``tdx`` group and requires ``EVIDENCE``, the
  evidence service's readiness.
- ``AttestedTLS(443, upstream=...)``: TLS relayed to a service that listens on the
  loopback, the only way requests reach it; gate 3 binds its key to the quote.

The committed ``peer.policy.json`` came from a simulated (in-process) bake with
``allow_placeholder=True``: its ``note`` marks it as a placeholder, and the verifier below
accepts it only because it passes ``allow_placeholder=True``.

    tundravm lock examples/06_attestation.py      # pin tundra-tools (network)
    tundravm build examples/06_attestation.py --tree-only build/attestation/mkosi
    tundravm build examples/06_attestation.py
"""

from tundravm import AttestedTLS, Endpoint, Image, Package, Service, TcpProbe
from tundravm.components import EVIDENCE, Tdxs
from tundravm.declarative import Resources

# What a trusted peer measures to: a verifier policy (a measurements.json record's `policy`).
PEER_POLICY = Resources.from_module(__name__).file("peer.policy.json")

# The app asks tdxs for quotes over its socket and answers on the loopback only.
app = Service(
    "app",
    ["/usr/bin/app", "--tdxs-socket", "/var/tdxs.sock", "--listen", "127.0.0.1:8080"],
    requires=[EVIDENCE],
    readiness=TcpProbe(8080),
    groups=["tdx"],
    state="app",
)

image = Image(
    "attestation",
    packages=[Package("ca-certificates")],
    services=[app],
    components=[
        # Issues TDX quotes, and checks peers: their quotes must match PEER_POLICY and
        # not be revoked.
        Tdxs.from_policy(
            PEER_POLICY,
            check_revocations=True,
            get_collateral=True,
            allow_placeholder=True,  # the committed policy is a placeholder
        ),
    ],
    ingress=[AttestedTLS(443, upstream=Endpoint("127.0.0.1", 8080, ready=app.ready))],
    # Every target boots the same payload; azure only adds its fixed VHD.
    targets=("qemu", "azure"),
)
