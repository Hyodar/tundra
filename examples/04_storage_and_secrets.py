"""Storage and secrets: a TPM-sealed key, an encrypted disk and secrets checked at boot.

Teaches the runtime-init steps the compiler emits, a dependency graph: ``keys``
creates the ``Key`` and seals it to the TPM, ``disks`` (after ``keys``) opens (or
formats) the ``Disk`` with it and mounts it, ``secrets`` (after ``disks``, since
``store=disk``) runs ``secret-delivery``, which waits for an SSH key POSTed over HTTP
and writes it to the SSH directory (``store`` is recorded in its config, but the
binary does not keep the key on the disk yet). ``key.ready``, ``disk.ready`` and
``secrets.ready`` name those steps. That SSH key is all the built-in ``tundra-tools``
provider delivers: the ``Secret`` entries (``Schema``, ``SecretFile``/``SecretEnv``
targets) are recorded in the image's secrets manifest, and ``ExternalDelivery``
declares what writes them at boot (lint ``secret-target-unsupported`` without it).
Its ``unit=`` adds a ``delivery-app-secret-fetch`` step that starts that unit and
waits for it: the app ``needs`` the disk and ``delivery.ready``, so its unit waits
for exactly those step units (``external-delivery-unwired`` warns when nothing can).
``RuntimeTools`` names the source of the helper binaries those steps run; ``Partition``
is a plain partition baked into the image instead. A ``Disk`` that may be formatted
names a stable device (a ``/dev/disk/by-id/`` link: QEMU's ``serial=`` makes
``virtio-SERIAL``); without one lint refuses it (``disk-device-guessed``), since
``disk-setup`` would pick the largest whole ``/dev/sd*`` disk, boot disk included.

    tundravm inspect examples/04_storage_and_secrets.py
    tundravm lint examples/04_storage_and_secrets.py   # source-unpinned
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
from tundravm.declarative.utils import TUNDRA_TOOLS, Network

key = Key("key_persistent", output="/run/keys/persistent")
disk = Disk(
    "disk_persistent",
    mount="/persistent",
    device="/dev/disk/by-id/virtio-tundra-data",
    format="on_initialize",  # the first boot formats it; later boots open it
    key=key,
    mapper="cryptpersistent",
)

# tundra-tools delivers only the SSH key: a oneshot unit of yours (ship it, e.g. with a
# Unit(...)) fetches these from your secret store and writes the files and
# /run/secrets/app.env; delivery.ready waits until that unit has finished.
delivery = ExternalDelivery(
    "app-secret-fetch", supports=("file", "env"), unit="app-secret-fetch.service"
)
secrets = Secrets(
    store=disk,
    delivery=delivery,
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
        # DHCP: the SSH key step waits for a POST on port 8080, so an interface must be up.
        Network(),
        # Ship /usr/bin/app with a Build(...) (see 05_source_builds.py) or a Package(...).
        # The service runs as "app" (its name; declared here for the home on the disk),
        # sandboxed by Hardening.strict(): it writes only where write_paths/logs say, and
        # reads the token systemd hands it as a credential, not from /run/secrets.
        User("app", home="/persistent/data"),
        Service(
            "app",
            "/usr/bin/app --token-file ${CREDENTIALS_DIRECTORY}/api-token",
            working_dir="/persistent/data",
            write_paths=("/persistent/data",),
            logs="app",
            credentials={"api-token": "/run/secrets/api-token"},
            needs=[disk.ready, delivery.ready],
        ),
    ),
)

backend = InProcessBackend()  # simulated artifacts; LimaMkosiBackend() bakes a real image
