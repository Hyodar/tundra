"""End-to-end example covering the major SDK API surfaces."""

from pathlib import Path

from tdx import Image, Kernel, SecretSchema, SecretTarget
from tdx.modules import Init, Tdxs


def build_full_api_recipe() -> None:
    img = Image(
        build_dir=Path("build"),
        base="debian/bookworm",
        arch="x86_64",
        target="x86_64",
        reproducible=True,
    )

    img.kernel = Kernel.tdx_kernel("6.8")

    img.repository(
        "https://deb.debian.org/debian-security",
        name="debian-security",
        suite="bookworm-security",
        components=["main"],
        priority=10,
    )

    img.install("ca-certificates", "curl", "jq")
    img.output_targets("qemu")
    img.debloat(
        enabled=True,
        systemd_units_keep_extra=["systemd-resolved.service"],
    )

    img.file("/etc/motd", content="TDX VM\n")
    img.template(
        "/etc/app/runtime.env",
        template="NETWORK={network}\nRPC_PORT={rpc_port}\n",
        vars={"network": "mainnet", "rpc_port": 8545},
    )

    img.user("app", system=True, home="/var/lib/app", uid=1000, groups=["tdx"])
    img.service(
        "app.service",
        exec=["/usr/local/bin/app", "--config", "/etc/app/runtime.env"],
        user="app",
        after=["network-online.target", "secrets-ready.target"],
        requires=["secrets-ready.target"],
        restart="always",
        extra_unit={"Service": {"MemoryMax": "4G"}},
        security_profile="strict",
    )

    img.partition("data", size="8G", mount="/var/lib/app", fs="ext4")
    img.prepare("pip", "install", "pyyaml")
    img.run("sysctl", "--system")  # default phase is postinst
    img.sync("git", "submodule", "update", "--init")

    jwt_secret = img.secret(
        "jwt_secret",
        required=True,
        schema=SecretSchema(kind="string", min_length=64, max_length=64),
        targets=(
            SecretTarget.file("/run/tdx-secrets/jwt.hex", owner="app", mode="0440"),
            SecretTarget.env("JWT_SECRET", scope="global"),
        ),
    )

    init = Init(secrets=(jwt_secret,), handoff="systemd")
    init.enable_disk_encryption(device="/dev/vda3", mapper_name="cryptroot")
    init.add_ssh_authorized_key("ssh-ed25519 AAAATEST full-api")
    delivery = init.secrets_delivery("http_post", completion="all_required", reject_unknown=True)

    init.apply(img)
    Tdxs(issuer_type="dcap").apply(img)

    with img.profile("azure"):
        img.output_targets("azure")
        img.install("waagent")

    with img.profile("gcp"):
        img.output_targets("gcp")
        img.install("google-guest-agent")

    with img.profile("dev"):
        img.ssh(enabled=True)
        img.install("strace", "gdb", "vim")
        img.debloat(enabled=False)

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
