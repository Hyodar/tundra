"""The attested TLS ports' keys: one key pair per port, generated at boot, on tmpfs only.

An ``AttestedTLS(port, upstream=)`` ingress lowers to its relay and to :func:`unit`, a root
oneshot the relay requires, which runs :data:`SCRIPT_PATH` with the port: an ECDSA P-256
key and a self-signed certificate made with ``openssl`` under :data:`DIRECTORY`/<port>
(the key root:root 0600, the certificate world-readable), fresh on every boot, and one
console line, :data:`CONSOLE` with the port and the certificate's SHA-256 fingerprint. The
relay reads both as credentials (``LoadCredential=``), and the attested endpoint reads the
certificate to bind its key into every quote (``tools/pkg/endpoint``), so a client that
verified the quote can check the certificate it sees is the bound one
(``tundravm attest --tls PORT``, ``tools/cmd/attested-client``).
"""

from __future__ import annotations

DIRECTORY = "/run/tundravm/attested-tls"
"""Where each port's ``key.pem`` and ``cert.pem`` live, under a directory named after it."""
SCRIPT_PATH = "/usr/lib/tundravm/attested-tls-key"
PACKAGE = "openssl"
CONSOLE = "tundravm-attested-tls: "
"""The console line's prefix; ``:PORT sha256:HEX`` (the certificate's fingerprint) follows."""

SCRIPT = f"""\
#!/bin/sh
# tundravm attested TLS key: a fresh P-256 key and self-signed certificate for the port
# given, every boot, on tmpfs only. The key is root:root 0600; the relay reads it through
# LoadCredential=, and the attested endpoint reads the certificate to bind its key.
set -eu
port=$1
dir={DIRECTORY}/$port
fingerprint() {{
    openssl x509 -in "$dir/cert.pem" -outform DER | sha256sum | cut -d ' ' -f 1
}}
mkdir -p -m 0711 /run/tundravm {DIRECTORY} "$dir"
if [ -s "$dir/key.pem" ] && [ -s "$dir/cert.pem" ]; then
    echo "{CONSOLE}:$port sha256:$(fingerprint)"
    exit 0
fi
umask 077
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \\
    -subj "/CN=tundravm attested TLS :$port" \\
    -keyout "$dir/key.pem.tmp" -out "$dir/cert.pem.tmp" 2>/dev/null
chmod 0644 "$dir/cert.pem.tmp"
mv "$dir/key.pem.tmp" "$dir/key.pem"
mv "$dir/cert.pem.tmp" "$dir/cert.pem"
echo "{CONSOLE}:$port sha256:$(fingerprint)"
"""
"""``/usr/lib/tundravm/attested-tls-key``: ``/bin/sh``, coreutils and ``openssl``."""


def unit(port: int) -> str:
    """The key unit of the attested TLS listener on *port*."""
    return f"attested-tls-{port}-key.service"


def certificate(port: int) -> str:
    """The PEM certificate of the listener on *port*, which the attested endpoint binds."""
    return f"{DIRECTORY}/{port}/cert.pem"


def key(port: int) -> str:
    """The PEM private key of the listener on *port*, which only its relay reads."""
    return f"{DIRECTORY}/{port}/key.pem"


__all__ = [
    "CONSOLE",
    "DIRECTORY",
    "PACKAGE",
    "SCRIPT",
    "SCRIPT_PATH",
    "certificate",
    "key",
    "unit",
]
