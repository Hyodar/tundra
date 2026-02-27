"""Shared helpers for configuring the upstream nethermind-tdx init binary."""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any

from tundravm.build_cache import Build, Cache
from tundravm.errors import PolicyError, ValidationError
from tundravm.fetch.git import COMMIT_PATTERN, MutableRefWarning
from tundravm.models import CommandSpec, HookSpec, ProfileState

if TYPE_CHECKING:
    from tundravm.image import Image

TDX_INIT_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

TDX_INIT_CONFIG_PATH = "/etc/tdx-init/config.yaml"


def ensure_tdx_init_build(image: Image, *, source_repo: str, source_ref: str) -> None:
    """Ensure /usr/bin/tdx-init is built exactly once per profile from source."""
    _enforce_source_ref_policy(image, ref=source_ref, component="tdx-init")
    source = (source_repo, source_ref)
    existing_source = image._tdx_init_source
    if existing_source is not None and existing_source != source:
        raise ValidationError(
            "Conflicting tdx-init source configuration.",
            hint="Use a single source_repo/source_branch across init modules in one image.",
            context={
                "existing_repo": existing_source[0],
                "existing_ref": existing_source[1],
                "requested_repo": source_repo,
                "requested_ref": source_ref,
            },
        )
    image._tdx_init_source = source
    image.build_install(*TDX_INIT_BUILD_PACKAGES)

    cache_key = f"tdx-init-{source_ref}"
    clone_dir = Build.build_path("tdx-init")
    chroot_dir = Build.chroot_path("tdx-init")
    cache = Cache.declare(
        cache_key,
        (
            Cache.file(
                src=Build.build_path("tdx-init/init/build/tdx-init"),
                dest=Build.dest_path("usr/bin/tdx-init"),
                name="tdx-init",
            ),
        ),
    )
    build_cmd = (
        f"# cache-key:{cache_key}\n"
        f"git clone --depth=1 -b {source_ref} {source_repo} \"{clone_dir}\" && "
        "mkosi-chroot bash -c '"
        f"cd {chroot_dir}/init && "
        "go build -trimpath -ldflags \"-s -w -buildid=\" -o ./build/tdx-init ./cmd/main.go"
        "'"
    )
    wrapped = cache.wrap(build_cmd)
    for profile in image._iter_active_profiles():
        if _has_tdx_init_build_hook(profile, cache_key=cache_key):
            continue
        spec = CommandSpec(argv=(wrapped,))
        profile.phases.setdefault("build", []).append(spec)
        profile.hooks.append(HookSpec(phase="build", command=spec))


def ensure_tdx_init_config(image: Image) -> dict[str, Any]:
    """Return mutable tdx-init config map persisted on the Image object."""
    existing = image._tdx_init_config
    if isinstance(existing, dict):
        return existing

    config: dict[str, Any] = {
        "ssh": {
            "strategy": "webserver",
            "strategy_config": {"server_url": "0.0.0.0:8080"},
            "dir": "/root/.ssh",
            "key_path": "/etc/root_key",
            "store_at": "disk_persistent",
        },
        "keys": {
            "key_persistent": {
                "strategy": "random",
                "tpm": True,
            }
        },
        "disks": {
            "disk_persistent": {
                "strategy": "largest",
                "format": "on_fail",
                "encryption_key": "key_persistent",
                "mount_at": "/persistent",
            }
        },
    }
    image._tdx_init_config = config
    return config


def write_tdx_init_config(image: Image, config: dict[str, Any]) -> None:
    """Render and place /etc/tdx-init/config.yaml into active profiles."""
    for profile in image._iter_active_profiles():
        profile.files = [entry for entry in profile.files if entry.path != TDX_INIT_CONFIG_PATH]
    image.file(TDX_INIT_CONFIG_PATH, content=_render_config_yaml(config))


def _render_config_yaml(config: dict[str, Any]) -> str:
    ssh = config.get("ssh", {})
    ssh_cfg = ssh.get("strategy_config", {})
    keys = config.get("keys", {})
    key_persistent = keys.get("key_persistent", {})
    disks = config.get("disks", {})
    disk_persistent = disks.get("disk_persistent", {})

    return (
        "\n"
        "ssh:\n"
        f'  strategy: "{ssh.get("strategy", "webserver")}"\n'
        "  strategy_config:\n"
        f'    server_url: "{ssh_cfg.get("server_url", "0.0.0.0:8080")}"\n'
        f'  dir: "{ssh.get("dir", "/root/.ssh")}"\n'
        f'  key_path: "{ssh.get("key_path", "/etc/root_key")}"\n'
        f'  store_at: "{ssh.get("store_at", "disk_persistent")}"\n'
        "\n"
        "keys:\n"
        "  key_persistent:\n"
        f'    strategy: "{key_persistent.get("strategy", "random")}"\n'
        f'    tpm: {_yaml_bool(bool(key_persistent.get("tpm", True)))}\n'
        "\n"
        "disks:\n"
        "  disk_persistent:\n"
        f'    strategy: "{disk_persistent.get("strategy", "largest")}"\n'
        f'    format: "{disk_persistent.get("format", "on_fail")}"\n'
        f'    encryption_key: "{disk_persistent.get("encryption_key", "key_persistent")}"\n'
        f'    mount_at: "{disk_persistent.get("mount_at", "/persistent")}"\n'
    )


def _yaml_bool(value: bool) -> str:
    return "true" if value else "false"


def _enforce_source_ref_policy(image: Image, *, ref: str, component: str) -> None:
    """Validate mutable refs against configured policy."""
    if COMMIT_PATTERN.fullmatch(ref):
        return

    policy = image.policy.mutable_ref_policy
    if policy == "allow":
        return
    if policy == "warn":
        warnings.warn(
            f"{component} uses mutable git ref `{ref}`; builds may be non-reproducible.",
            MutableRefWarning,
            stacklevel=3,
        )
        return
    if policy == "error":
        raise PolicyError(
            "Mutable git refs are not allowed by policy.",
            hint="Pin module source refs to full 40-char commit SHAs.",
            context={"component": component, "ref": ref},
        )


def _has_tdx_init_build_hook(profile: ProfileState, *, cache_key: str) -> bool:
    return any(cache_key in command.argv[0] for command in profile.phases.get("build", ()))
