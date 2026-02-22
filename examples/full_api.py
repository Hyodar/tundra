"""End-to-end example covering the major SDK API surfaces."""

from pathlib import Path

from tdx import Image
from tdx.models import SecretSchema, SecretTarget
from tdx.modules import Init, Tdxs


def build_full_api_recipe() -> None:
    img = Image(build_dir=Path("build"), base="debian/bookworm", arch="x86_64")
    img.repository("debian-security", "https://deb.debian.org/debian-security", priority=10)
    img.install("ca-certificates", "curl", "jq")
    img.output_targets("qemu")
    img.debloat(enabled=True)

    img.file("/etc/motd", content="TDX VM\n")
    img.template(
        "/etc/app/runtime.env",
        template="NETWORK={network}\nRPC_PORT={rpc_port}\n",
        variables={"network": "mainnet", "rpc_port": "8545"},
    )
    img.user("app", uid=1000, gid=1000, shell="/usr/sbin/nologin")
    img.service("app.service", enabled=True, wants=("network-online.target",))
    img.partition("data", size="8G", mount="/var/lib/app", fs="ext4")
    img.run("echo", "prepare-phase", phase="prepare")
    img.run("sysctl", "--system")  # default phase is postinst

    jwt_secret = img.secret(
        "jwt_secret",
        required=True,
        schema=SecretSchema(kind="string", min_length=64, max_length=64),
        targets=(
            SecretTarget.file("/run/tdx-secrets/jwt.hex"),
            SecretTarget.env("JWT_SECRET", scope="global"),
        ),
    )

    init = Init(secrets=(jwt_secret,), handoff="systemd")
    init.enable_disk_encryption(device="/dev/vda3", mapper_name="cryptroot")
    init.add_ssh_authorized_key("ssh-ed25519 AAAATEST full-api")
    delivery = init.secrets_delivery("http_post", completion="all_required", reject_unknown=True)

    img.use(init, Tdxs.issuer())

    with img.profile("azure"):
        img.output_targets("azure")
        img.install("waagent")

    with img.profile("gcp"):
        img.output_targets("gcp")
        img.install("google-guest-agent")

    img.lock()
    img.bake(frozen=True)

    # Runtime step after attestation/boot: validate and materialize secret payload.
    validation = delivery.validate_payload({"jwt_secret": "a" * 64})
    if validation.ready:
        delivery.materialize_runtime("runtime")

    print(img.measure(backend="rtmr").to_json())
    print(img.deploy(target="qemu").deployment_id)


if __name__ == "__main__":
    build_full_api_recipe()
