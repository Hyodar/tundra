"""Storage and secrets: a TPM-sealed key, an encrypted disk and secrets checked at boot.

Teaches the runtime-init chain the compiler emits, in order: ``keys`` creates the
``Key`` and seals it to the TPM, ``disks`` opens (or formats) the ``Disk`` with it and
mounts it, ``secrets`` runs ``secret-delivery``, which waits for an SSH key POSTed over
HTTP and writes it to the SSH directory (``store`` is recorded in its config, but the
binary does not keep the key on the disk yet). That SSH key is all the built-in
``tundra-tools`` provider delivers: the ``Secret`` entries (``Schema``,
``SecretFile``/``SecretEnv`` targets) are recorded in the image's secrets manifest, and
``ExternalDelivery`` declares what writes them at boot (lint
``secret-target-unsupported`` without it).
``RuntimeTools`` names the source of the helper binaries those steps run; ``Partition``
is a plain partition baked into the image instead. ``Disk`` without ``device=`` lets
``disk-setup`` pick the largest whole ``/dev/sd*`` disk, so lint warns
``disk-auto-format``.

    tundravm inspect examples/04_storage_and_secrets.py
    tundravm lint examples/04_storage_and_secrets.py   # disk-auto-format, source-unpinned
    tundravm compile examples/04_storage_and_secrets.py --tree build/storage/mkosi
"""

from tundravm.backends import InProcessBackend
from tundravm.declarative import (
    Debian,
    Disk,
    ExternalDelivery,
    Key,
    Package,
    Partition,
    Recipe,
    RuntimeTools,
    Schema,
    Secret,
    SecretEnv,
    SecretFile,
    Secrets,
    Service,
    User,
)
from tundravm.declarative.utils import TUNDRA_TOOLS

key = Key("key_persistent", output="/run/keys/persistent")
disk = Disk("disk_persistent", mount="/persistent", key=key, mapper="cryptpersistent")

secrets = Secrets(
    store=disk,
    # tundra-tools delivers only the SSH key: a unit of yours, ordered before app.service,
    # fetches these from your secret store and writes the files and /run/secrets/app.env.
    delivery=ExternalDelivery("app-secret-fetch", supports=("file", "env")),
    entries=(
        # A file only the app reads, and the same value in the app's environment.
        Secret(
            "api_token",
            (SecretFile("/run/secrets/api-token", owner="app"), SecretEnv("API_TOKEN", "app")),
            schema=Schema(kind="string", min_length=10, pattern="^tok_"),
        ),
        Secret(
            "jwt_secret",
            (SecretFile("/run/secrets/jwt.hex", mode=0o440, owner="app"),),
            schema=Schema(kind="string", min_length=64, max_length=64),
        ),
        # Optional: boot continues without it.
        Secret(
            "feature_flags",
            (SecretFile("/run/secrets/flags.json", owner="app"),),
            required=False,
            schema=Schema(kind="json"),
        ),
    ),
)

recipe = Recipe(
    name="storage-and-secrets",
    platform=Debian("bookworm"),
    items=(
        Package("cryptsetup"),
        Package("ca-certificates"),
        Partition("logs", size="4G", mount="/var/log/app"),
        key,
        disk,
        secrets,
        RuntimeTools(TUNDRA_TOOLS),
        # Ship /usr/bin/app with a Build(...) (see 05_source_builds.py) or a Package(...).
        User("app", home="/persistent/data"),
        Service(
            "app",
            "/usr/bin/app --token-file /run/secrets/api-token",
            user="app",
            working_dir="/persistent/data",
            restart="on-failure",
            wanted_by="minimal.target",
        ),
    ),
)

backend = InProcessBackend()  # simulated artifacts; LimaMkosiBackend() bakes a real image
