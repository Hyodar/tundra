"""Storage and secrets: volumes that unlock at boot, secrets delivered after attestation.

Teaches:

- ``Volume(name, mount=, unlock=)``: a LUKS2 volume found by its attachment name
  (``tundravm-<name>``, a QEMU serial or a GCP device name), formatted only while it is
  uninitialised. ``unlock`` is the trust decision: ``OperatorKey()`` waits for a
  passphrase an operator sends after attesting the instance (``tundravm secrets unlock``);
  ``TpmSeal(platform=, pcrs=, recovery=)`` seals a random key to a vTPM and states which
  one. A key sealed to one platform's vTPM opens nowhere else, so ``TpmSeal`` is refused
  for any target that boots with another one: this image targets Azure only.
- ``volume.directory("x")``: a directory on the volume a service keeps its state in. The
  service waits until the volume is open and the directory exists, owned by it.
- ``Secret(name)``: a value delivered after attestation (``tundravm secrets deliver``) and
  never written into the image. A service binds it with ``credentials=[...]``, waits for
  it when it is required, and reads it at ``secret.path`` (``${CREDENTIALS_DIRECTORY}/
  <name>``), a systemd credential. ``required=False`` starts without it (an empty
  credential until it arrives); ``rotation="restart"`` restarts the service when it is
  delivered again, ``"none"`` leaves it running.
- ``/etc/tundravm/runtime.json``: what the image records for the tools that deliver.

    tundravm inspect examples/04_storage_and_secrets.py
    tundravm lint examples/04_storage_and_secrets.py   # source-unpinned
    tundravm compile examples/04_storage_and_secrets.py --tree build/storage/mkosi
"""

from tundravm import Image, OperatorKey, Package, Secret, Service, TpmSeal, Volume

# The data an operator protects: it opens only once they have attested this instance.
data = Volume("data", mount="/persistent", unlock=OperatorKey())
# A cache the instance reopens unattended: its key is sealed to Azure's paravisor vTPM.
# Today's key-gen keeps it in the vTPM's NV storage; pcrs= is recorded for gate 3 to bind.
cache = Volume(
    "cache",
    mount="/var/cache/app",
    unlock=TpmSeal(platform="azure", pcrs=[7], recovery="none"),
)

token = Secret("api-token")
flags = Secret("feature-flags", required=False, rotation="none")

image = Image(
    "storage-and-secrets",
    packages=[Package("ca-certificates")],
    services=[
        # Ship /usr/bin/app with a GoBinary (see 05_source_builds.py) or a Package.
        Service(
            "app",
            ["/usr/bin/app", "--token-file", token.path, "--flags-file", flags.path],
            state=data.directory("app"),
            credentials=[token, flags],
        ),
        Service(
            "indexer",
            ["/usr/bin/app", "index", "--cache", cache.directory("index").path],
            state=cache.directory("index", mode=0o700),
        ),
    ],
    volumes=[data, cache],
    secrets=[token, flags],
    targets=("azure",),
)
