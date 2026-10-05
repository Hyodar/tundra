"""Storage and secrets: a TPM-sealed key, an encrypted disk and secrets checked at boot.

Teaches the runtime-init chain the compiler emits, in order: ``keys`` creates the
``Key`` and seals it to the TPM, ``disks`` opens (or formats) the ``Disk`` with it and
mounts it, ``secrets`` runs ``secret-delivery``, which waits for an SSH key POSTed over
HTTP, writes it to the SSH directory and keeps it in a LUKS token on the ``store`` disk.
The ``Secret`` entries (``Schema``, ``SecretFile``/``SecretEnv`` targets) are recorded in
the image's secrets manifest only: the runtime tool does not validate or deliver them yet.
``RuntimeTools`` names the source of the helper binaries those steps run; ``Partition``
is a plain partition baked into the image instead. ``Disk`` without ``device=`` lets
``disk-setup`` pick the largest whole ``/dev/sd*`` disk, so lint warns
``disk-auto-format``.

    tundravm inspect examples/04_storage_and_secrets.py
    tundravm lint examples/04_storage_and_secrets.py   # disk-auto-format, source-unpinned
    tundravm compile examples/04_storage_and_secrets.py --out build/storage/mkosi
"""

from tundravm.backends import InProcessBackend
from tundravm.declarative import (
    Disk,
    Fragment,
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

BOOT = ("linux-image-amd64", "systemd", "systemd-sysv", "udev", "kmod", "systemd-boot-efi")

key = Key("key_persistent", output="/run/keys/persistent")
disk = Disk("disk_persistent", mount="/persistent", key=key, mapper="cryptpersistent")

secrets = Secrets(
    store=disk,
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
    base="debian/bookworm",
    common=Fragment(
        "base",
        items=(
            *(Package(name) for name in (*BOOT, "cryptsetup", "ca-certificates")),
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
    ),
)

backend = InProcessBackend()  # simulated artifacts; LimaMkosiBackend() bakes a real image
