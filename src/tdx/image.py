"""Core image object for SDK recipe declarations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Self

from .backends.inprocess import InProcessBackend
from .backends.lima import LimaBackend
from .backends.local_linux import LocalLinuxBackend
from .cache import BuildCacheInput, BuildCacheStore, cache_key
from .compiler import (
    DEFAULT_TDX_INIT_SCRIPT,
    PHASE_ORDER,
    EmitConfig,
    MkosiEmission,
    emit_mkosi_tree,
)
from .deploy import get_adapter
from .errors import DeploymentError, LockfileError, MeasurementError, ValidationError
from .lockfile import build_lockfile, read_lockfile, recipe_digest, write_lockfile
from .measure import Measurements, derive_measurements
from .models import (
    DEFAULT_DEBLOAT_MASK,
    DEFAULT_DEBLOAT_REMOVE,
    Arch,
    ArtifactRef,
    BakeRequest,
    BakeResult,
    CommandSpec,
    DebloatConfig,
    DeployRequest,
    DeployResult,
    FileEntry,
    HookSpec,
    Kernel,
    OutputTarget,
    PartitionSpec,
    Phase,
    ProfileBuildResult,
    ProfileState,
    RecipeState,
    RepositorySpec,
    RestartPolicy,
    SecretSchema,
    SecretSpec,
    SecretTarget,
    SecurityProfile,
    ServiceSpec,
    TemplateEntry,
    UserSpec,
)
from .observability import StructuredLogger
from .policy import Policy, ensure_bake_policy


@dataclass(slots=True)
class Image:
    """Represents an image recipe root."""

    DEFAULT_TDX_INIT = DEFAULT_TDX_INIT_SCRIPT

    build_dir: Path = field(default_factory=lambda: Path("build"))
    base: str = "debian/bookworm"
    arch: Arch = "x86_64"
    default_profile: str = "default"
    target: Arch = "x86_64"
    backend: str = "lima"
    reproducible: bool = True
    policy: Policy = field(default_factory=Policy)
    logger: StructuredLogger = field(default_factory=StructuredLogger)
    kernel: Kernel | None = field(default=None)
    with_network: bool = True
    clean_package_metadata: bool = True
    manifest_format: str = "json"
    compress_output: str | None = None
    output_directory: str | None = None
    seed: str | None = None
    sandbox_trees: tuple[str, ...] = ()
    package_cache_directory: str | None = None
    init_script: str | None = None
    generate_version_script: bool = False
    generate_cloud_postoutput: bool = True
    environment: dict[str, str] | None = None
    environment_passthrough: tuple[str, ...] | None = None
    emit_mode: Literal["per_directory", "native_profiles"] = "per_directory"
    _backend_override: object | None = field(init=False, default=None, repr=False)
    _state: RecipeState = field(init=False, repr=False)
    _active_profiles: tuple[str, ...] = field(init=False, repr=False)
    _last_bake_result: BakeResult | None = field(init=False, default=None, repr=False)
    _last_compile_digest: str | None = field(init=False, default=None, repr=False)
    _last_compile_path: Path | None = field(init=False, default=None, repr=False)
    _last_compile_emission: MkosiEmission | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        self._state = RecipeState.initialize(
            base=self.base,
            arch=self.arch,
            default_profile=self.default_profile,
        )
        self._active_profiles = (self.default_profile,)
        if self.reproducible:
            self.strip_image_version()

    @property
    def state(self) -> RecipeState:
        return self._state

    def set_policy(self, policy: Policy) -> Self:
        self.policy = policy
        return self

    @contextmanager
    def profile(self, name: str) -> Iterator[Self]:
        with self.profiles(name):
            yield self

    @contextmanager
    def profiles(self, *names: str) -> Iterator[Self]:
        selected = self._normalize_profile_names(names)
        previous_profiles = self._active_profiles
        for profile_name in selected:
            self._state.ensure_profile(profile_name)
        self._active_profiles = selected
        try:
            yield self
        finally:
            self._active_profiles = previous_profiles

    @contextmanager
    def all_profiles(self) -> Iterator[Self]:
        with self.profiles(*tuple(sorted(self._state.profiles))):
            yield self

    def install(self, *packages: str) -> Self:
        if not packages:
            raise ValidationError("install() requires at least one package.")
        for package in packages:
            if not package:
                raise ValidationError("Package names must be non-empty.")
        for profile in self._iter_active_profiles():
            profile.packages.update(packages)
        return self

    def build_install(self, *packages: str) -> Self:
        """Declare packages required at build time (removed after build)."""
        if not packages:
            raise ValidationError("build_install() requires at least one package.")
        for package in packages:
            if not package:
                raise ValidationError("Package names must be non-empty.")
        for profile in self._iter_active_profiles():
            profile.build_packages.update(packages)
        return self

    def build_source(self, host_path: str, target: str = "") -> Self:
        """Mount a host directory into the build environment (mkosi BuildSources)."""
        if not host_path:
            raise ValidationError("build_source() requires a non-empty host path.")
        for profile in self._iter_active_profiles():
            profile.build_sources.append((host_path, target))
        return self

    def repository(
        self,
        url: str,
        *,
        name: str | None = None,
        suite: str | None = None,
        components: tuple[str, ...] | list[str] = (),
        keyring: str | None = None,
        priority: int = 100,
    ) -> Self:
        if not url:
            raise ValidationError("repository() requires a non-empty URL.")
        repo_name = name or url.split("/")[-1] or url
        entry = RepositorySpec(
            name=repo_name,
            url=url,
            suite=suite,
            components=tuple(components),
            keyring=keyring,
            priority=priority,
        )
        for profile in self._iter_active_profiles():
            profile.repositories.append(entry)
        return self

    def file(
        self,
        path: str,
        *,
        content: str | None = None,
        src: str | Path | None = None,
        mode: str = "0644",
    ) -> Self:
        if not path:
            raise ValidationError("file() requires a destination path.")
        if (content is None) == (src is None):
            raise ValidationError("file() requires exactly one of content= or src=.")
        if content is not None:
            resolved_content = content
        else:
            if src is None:
                raise ValidationError("file() requires src when content is not provided.")
            resolved_content = Path(src).read_text(encoding="utf-8")
        for profile in self._iter_active_profiles():
            profile.files.append(FileEntry(path=path, content=resolved_content, mode=mode))
        return self

    def template(
        self,
        dest: str | None = None,
        *,
        src: str | Path | None = None,
        template: str | None = None,
        vars: Mapping[str, str | int | float] | None = None,
        variables: Mapping[str, str] | None = None,
        mode: str = "0644",
        # Legacy positional: template(path, template=..., variables=...)
        path: str | None = None,
    ) -> Self:
        # Resolve destination: dest= or legacy path=
        resolved_dest = dest or path
        if not resolved_dest:
            raise ValidationError("template() requires a destination path (dest= parameter).")

        # Resolve template content: src= file or inline template=
        if src is not None and template is not None:
            raise ValidationError("template() requires exactly one of src= or template=, not both.")
        if src is not None:
            template_content = Path(src).read_text(encoding="utf-8")
        elif template is not None:
            template_content = template
        else:
            raise ValidationError("template() requires either src= or template= parameter.")

        # Resolve variables: vars= (SPEC style) or variables= (legacy)
        resolved_vars: dict[str, str] = {}
        if vars is not None:
            resolved_vars = {k: str(v) for k, v in sorted(vars.items())}
        elif variables is not None:
            resolved_vars = dict(sorted(variables.items()))

        try:
            rendered = template_content.format_map(resolved_vars)
        except KeyError as exc:
            raise ValidationError(
                "template() variables are missing required placeholders.",
                hint="Provide all placeholder keys used in the template string.",
                context={"path": resolved_dest, "missing_key": str(exc)},
            ) from exc
        entry = TemplateEntry(
            path=resolved_dest,
            template=template_content,
            variables=resolved_vars,
            rendered=rendered,
            mode=mode,
        )
        for profile in self._iter_active_profiles():
            profile.templates.append(entry)
        return self

    def user(
        self,
        name: str,
        *,
        system: bool = False,
        home: str | None = None,
        shell: str = "/usr/sbin/nologin",
        uid: int | None = None,
        gid: int | None = None,
        groups: tuple[str, ...] | list[str] = (),
    ) -> Self:
        if not name:
            raise ValidationError("user() requires a non-empty user name.")
        entry = UserSpec(
            name=name,
            system=system,
            home=home,
            shell=shell,
            uid=uid,
            gid=gid,
            groups=tuple(groups),
        )
        for profile in self._iter_active_profiles():
            existing_names = {u.name for u in profile.users}
            if name in existing_names:
                raise ValidationError(
                    f"Duplicate user name '{name}' in profile '{profile.name}'.",
                    hint="User names must be unique within a profile.",
                    context={"user": name, "profile": profile.name},
                )
            profile.users.append(entry)
        return self

    def service(
        self,
        name: str,
        *,
        exec: tuple[str, ...] | list[str] | str = (),
        user: str | None = None,
        after: tuple[str, ...] | list[str] = (),
        requires: tuple[str, ...] | list[str] = (),
        wants: tuple[str, ...] | list[str] = (),
        restart: RestartPolicy = "no",
        enabled: bool = True,
        extra_unit: Mapping[str, Mapping[str, str]] | None = None,
        security_profile: SecurityProfile = "default",
    ) -> Self:
        if not name:
            raise ValidationError("service() requires a non-empty service name.")
        exec_argv: tuple[str, ...]
        if isinstance(exec, str):
            exec_argv = tuple(exec.split()) if exec else ()
        else:
            exec_argv = tuple(exec)
        entry = ServiceSpec(
            name=name,
            exec=exec_argv,
            user=user,
            after=tuple(after),
            requires=tuple(requires),
            wants=tuple(dict.fromkeys(wants)),
            restart=restart,
            enabled=enabled,
            extra_unit=dict(extra_unit) if extra_unit else {},
            security_profile=security_profile,
        )
        for profile in self._iter_active_profiles():
            existing_names = {s.name for s in profile.services}
            if name in existing_names:
                raise ValidationError(
                    f"Duplicate service name '{name}' in profile '{profile.name}'.",
                    hint="Service names must be unique within a profile.",
                    context={"service": name, "profile": profile.name},
                )
            profile.services.append(entry)
        return self

    def partition(self, name: str, *, size: str, mount: str, fs: str = "ext4") -> Self:
        if not name:
            raise ValidationError("partition() requires a non-empty name.")
        if not size or not mount:
            raise ValidationError("partition() requires both size and mount values.")
        entry = PartitionSpec(name=name, size=size, mount=mount, fs=fs)
        for profile in self._iter_active_profiles():
            profile.partitions.append(entry)
        return self

    def secret(
        self,
        name: str,
        *,
        required: bool = True,
        schema: SecretSchema | None = None,
        targets: tuple[SecretTarget, ...] = (),
    ) -> SecretSpec:
        if not name:
            raise ValidationError("secret() requires a non-empty secret name.")
        if not targets:
            raise ValidationError("secret() requires at least one delivery target.")
        entry = SecretSpec(name=name, required=required, schema=schema, targets=targets)
        for profile in self._iter_active_profiles():
            profile.secrets.append(entry)
        return entry

    def output_targets(self, *targets: OutputTarget) -> Self:
        if not targets:
            raise ValidationError("output_targets() requires at least one target.")
        deduped = tuple(dict.fromkeys(targets))
        for profile in self._iter_active_profiles():
            profile.output_targets = deduped
        return self

    def debloat(
        self,
        *,
        enabled: bool = True,
        # Rich debloat parameters matching SPEC / DESIGN docs
        paths_remove: tuple[str, ...] | None = None,
        paths_skip: tuple[str, ...] | list[str] = (),
        paths_remove_extra: tuple[str, ...] | list[str] = (),
        paths_skip_for_profiles: dict[str, tuple[str, ...]] | None = None,
        systemd_minimize: bool = True,
        systemd_units_keep: tuple[str, ...] | None = None,
        systemd_units_keep_extra: tuple[str, ...] | list[str] = (),
        systemd_bins_keep: tuple[str, ...] | None = None,
        # Legacy parameters (backward compat)
        remove: tuple[str, ...] | None = None,
        mask: tuple[str, ...] | None = None,
    ) -> Self:
        _defaults = DebloatConfig()
        if not enabled:
            config = DebloatConfig(enabled=False)
        else:
            # Convert dict to frozen tuple-of-tuples for the frozen dataclass
            profile_skips: tuple[tuple[str, tuple[str, ...]], ...] = ()
            if paths_skip_for_profiles:
                profile_skips = tuple(
                    (k, v) for k, v in sorted(paths_skip_for_profiles.items())
                )
            config = DebloatConfig(
                enabled=True,
                paths_remove=paths_remove or _defaults.paths_remove,
                paths_skip=tuple(paths_skip),
                paths_remove_extra=tuple(paths_remove_extra),
                paths_skip_for_profiles=profile_skips,
                systemd_minimize=systemd_minimize,
                systemd_units_keep=systemd_units_keep or _defaults.systemd_units_keep,
                systemd_units_keep_extra=tuple(systemd_units_keep_extra),
                systemd_bins_keep=systemd_bins_keep or _defaults.systemd_bins_keep,
            )

        # Also maintain legacy fields for backward compat
        remove_items = tuple(sorted(dict.fromkeys(remove or DEFAULT_DEBLOAT_REMOVE)))
        mask_items = tuple(sorted(dict.fromkeys(mask or DEFAULT_DEBLOAT_MASK)))

        for profile in self._iter_active_profiles():
            profile.debloat = config
            profile.debloat_enabled = enabled
            profile.debloat_remove = remove_items if enabled else ()
            profile.debloat_mask = mask_items if enabled else ()
        return self

    def explain_debloat(self, *, profile: str | None = None) -> dict[str, object]:
        selected_profile = self._resolve_operation_profile(profile)
        profile_state = self._state.ensure_profile(selected_profile)
        config = profile_state.debloat
        return {
            "profile": selected_profile,
            "enabled": config.enabled,
            "paths_remove": list(config.effective_paths_remove),
            "paths_skip": list(config.paths_skip),
            "systemd_minimize": config.systemd_minimize,
            "systemd_units_keep": list(config.effective_units_keep),
            "systemd_bins_keep": list(config.systemd_bins_keep),
            # Legacy keys for backward compat
            "remove": list(profile_state.debloat_remove),
            "mask": list(profile_state.debloat_mask),
        }

    # --- Lifecycle convenience methods ---

    def sync(self, *argv: str, env: Mapping[str, str] | None = None, shell: bool = False) -> Self:
        """Register a sync-phase command (runs before build)."""
        if not argv:
            raise ValidationError("sync() requires a command.")
        return self.hook("sync", *argv, env=env, shell=shell)

    def skeleton(
        self,
        path: str,
        *,
        content: str | None = None,
        src: str | Path | None = None,
        mode: str = "0644",
    ) -> Self:
        """Place a file in the image before the package manager runs.

        This maps to mkosi.skeleton/ and is useful for custom apt sources,
        resolv.conf for build DNS, or directory structure that packages expect.
        """
        if not path:
            raise ValidationError("skeleton() requires a destination path.")
        if (content is None) == (src is None):
            raise ValidationError("skeleton() requires exactly one of content= or src=.")
        if content is not None:
            resolved_content = content
        else:
            if src is None:
                raise ValidationError("skeleton() requires src when content is not provided.")
            resolved_content = Path(src).read_text(encoding="utf-8")
        for profile in self._iter_active_profiles():
            profile.skeleton_files.append(
                FileEntry(path=path, content=resolved_content, mode=mode)
            )
        return self

    def prepare(
        self,
        *argv: str,
        env: Mapping[str, str] | None = None,
        shell: bool = False,
    ) -> Self:
        """Register a prepare-phase command (runs after base packages, before build)."""
        if not argv:
            raise ValidationError("prepare() requires a command.")
        return self.hook("prepare", *argv, env=env, shell=shell)

    def finalize(
        self,
        *argv: str,
        env: Mapping[str, str] | None = None,
        shell: bool = False,
    ) -> Self:
        """Register a finalize-phase command (runs on HOST with $BUILDROOT)."""
        if not argv:
            raise ValidationError("finalize() requires a command.")
        return self.hook("finalize", *argv, env=env, shell=shell)

    def postoutput(
        self,
        *argv: str,
        env: Mapping[str, str] | None = None,
        shell: bool = False,
    ) -> Self:
        """Register a postoutput-phase command (runs after disk image is written)."""
        if not argv:
            raise ValidationError("postoutput() requires a command.")
        return self.hook("postoutput", *argv, env=env, shell=shell)

    def clean(
        self,
        *argv: str,
        env: Mapping[str, str] | None = None,
        shell: bool = False,
    ) -> Self:
        """Register a clean-phase command (runs on `mkosi clean`)."""
        if not argv:
            raise ValidationError("clean() requires a command.")
        return self.hook("clean", *argv, env=env, shell=shell)

    def on_boot(
        self,
        *argv: str,
        env: Mapping[str, str] | None = None,
        shell: bool = False,
    ) -> Self:
        """Register a boot-time command (runs when VM boots, systemd oneshot)."""
        if not argv:
            raise ValidationError("on_boot() requires a command.")
        return self.hook("boot", *argv, env=env, shell=shell)

    def strip_image_version(self, *, enabled: bool = True) -> Self:
        """Strip IMAGE_VERSION from /etc/os-release for reproducible attestation."""
        if not enabled:
            # Remove any existing finalize hooks that match the strip command
            for profile in self._iter_active_profiles():
                if "finalize" in profile.phases:
                    profile.phases["finalize"] = [
                        cmd
                        for cmd in profile.phases["finalize"]
                        if not (
                            cmd.argv[0] == "bash"
                            and len(cmd.argv) >= 3
                            and "IMAGE_VERSION" in cmd.argv[2]
                        )
                    ]
                    profile.hooks = [
                        h
                        for h in profile.hooks
                        if not (
                            h.phase == "finalize"
                            and h.command.argv[0] == "bash"
                            and len(h.command.argv) >= 3
                            and "IMAGE_VERSION" in h.command.argv[2]
                        )
                    ]
            return self
        script = """sed -i '/^IMAGE_VERSION=/d' "$BUILDROOT/etc/os-release\""""
        self.hook("finalize", "bash", "-c", script)
        return self

    def efi_stub(self, *, snapshot_url: str, package_version: str) -> Self:
        """Pin systemd-boot-efi from a specific Debian snapshot for reproducible EFI stub."""
        if not snapshot_url:
            raise ValidationError("efi_stub() requires a non-empty snapshot_url.")
        if not package_version:
            raise ValidationError("efi_stub() requires a non-empty package_version.")
        script = (
            f'EFI_SNAPSHOT_URL="{snapshot_url}"\n'
            f'EFI_PACKAGE_VERSION="{package_version}"\n'
            'DEB_URL="${EFI_SNAPSHOT_URL}/pool/main/s/systemd/'
            'systemd-boot-efi_${EFI_PACKAGE_VERSION}_amd64.deb"\n'
            'WORK_DIR=$(mktemp -d)\n'
            'curl -sSfL -o "$WORK_DIR/systemd-boot-efi.deb" "$DEB_URL"\n'
            'cp "$WORK_DIR/systemd-boot-efi.deb" "$BUILDROOT/tmp/"\n'
            'mkosi-chroot dpkg -i /tmp/systemd-boot-efi.deb\n'
            'cp "$BUILDROOT/usr/lib/systemd/boot/efi/systemd-bootx64.efi" '
            '"$BUILDROOT/usr/lib/systemd/boot/efi/linuxx64.efi.stub" 2>/dev/null || true\n'
            'rm -rf "$WORK_DIR" "$BUILDROOT/tmp/systemd-boot-efi.deb"'
        )
        self.run("bash", "-c", script, phase="postinst")
        return self

    def backports(self, *, mirror: str | None = None, release: str | None = None) -> Self:
        """Generate Debian backports sources dynamically at sync time.

        Registers a sync phase hook matching upstream add-backports.sh behavior.
        """
        lines: list[str] = []
        if mirror is not None:
            lines.append(f'MIRROR="{mirror}"')
        else:
            lines.append('MIRROR=$(jq -r .Mirror /work/config.json)')
            lines.append('if [ "$MIRROR" = "null" ]; then')
            lines.append('    MIRROR="http://deb.debian.org/debian"')
            lines.append("fi")

        if release is not None:
            lines.append(f'RELEASE="{release}"')

        lines.append(
            'cat > "$SRCDIR/mkosi.builddir/debian-backports.sources" <<EOF\n'
            "Types: deb deb-src\n"
            "URIs: $MIRROR\n"
            "Suites: ${RELEASE}-backports\n"
            "Components: main\n"
            "Enabled: yes\n"
            "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\n"
            "\n"
            "Types: deb deb-src\n"
            "URIs: $MIRROR\n"
            "Suites: sid\n"
            "Components: main\n"
            "Enabled: yes\n"
            "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\n"
            "EOF"
        )

        script = "\n".join(lines)
        self.hook("sync", "bash", "-c", script)

        # Auto-add sandbox_trees entry for the generated file
        backports_entry = (
            "mkosi.builddir/debian-backports.sources"
            ":/etc/apt/sources.list.d/debian-backports.sources"
        )
        if backports_entry not in self.sandbox_trees:
            self.sandbox_trees = (*self.sandbox_trees, backports_entry)

        return self

    def ssh(self, *, enabled: bool = True, key_delivery: str = "http") -> Self:
        """Enable or disable SSH access (typically for dev profiles)."""
        if enabled:
            self.install("dropbear")
        return self

    def run(
        self,
        *argv: str,
        phase: Phase = "postinst",
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        shell: bool = False,
    ) -> Self:
        return self.hook(
            phase,
            *argv,
            env=env,
            cwd=cwd,
            shell=shell,
        )

    def hook(
        self,
        phase: Phase,
        *argv: str,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        shell: bool = False,
        after_phase: Phase | None = None,
    ) -> Self:
        if not argv:
            raise ValidationError("hook() requires a command argv.")
        self._validate_phase_order(phase=phase, after_phase=after_phase)
        env_data = dict(env or {})
        for profile in self._iter_active_profiles():
            command = CommandSpec(
                argv=tuple(argv),
                env=dict(env_data),
                cwd=cwd,
                shell=shell,
            )
            profile.phases.setdefault(phase, []).append(command)
            profile.hooks.append(HookSpec(phase=phase, command=command, after_phase=after_phase))
        return self

    def lock(self, path: str | Path | None = None) -> Path:
        lock_path = self._normalize_path(path, fallback=self._default_lock_path())
        payload = self._recipe_payload(profile_names=self._active_profiles)
        lock = build_lockfile(recipe=payload)
        return write_lockfile(lock, lock_path)

    def compile(self, path: str | Path, *, force: bool = False) -> Path:
        destination = self._normalize_path(path)
        digest = recipe_digest(self._recipe_payload(profile_names=self._active_profiles))
        if (
            not force
            and self._last_compile_digest == digest
            and self._last_compile_path == destination
            and destination.exists()
        ):
            return destination
        self._last_compile_emission = emit_mkosi_tree(
            recipe=self._state,
            destination=destination,
            profile_names=self._active_profiles,
            base=self.base,
            config=self._emit_config(),
        )
        self._last_compile_digest = digest
        self._last_compile_path = destination
        return destination

    def emit_mkosi(self, path: str | Path) -> Path:
        """Deprecated: use compile() instead."""
        return self.compile(path)

    def bake(
        self,
        output_dir: str | Path | None = None,
        *,
        frozen: bool = False,
        force: bool = False,
    ) -> BakeResult:
        ensure_bake_policy(policy=self.policy, frozen=frozen)
        if frozen:
            self._assert_frozen_lock(profile_names=self._active_profiles)
        destination = self._normalize_path(output_dir, fallback=self.build_dir)
        destination.mkdir(parents=True, exist_ok=True)
        recipe_lock_digest = recipe_digest(
            self._recipe_payload(profile_names=self._active_profiles),
        )
        lock_digest = self._compute_lock_digest(recipe_lock_digest)

        # Compile the mkosi tree (skips if unchanged)
        emission_root = destination / "mkosi"
        self.compile(emission_root, force=force)
        emission = self._last_compile_emission
        assert emission is not None

        # Get the build backend
        backend = self._get_backend()

        profiles_result: dict[str, ProfileBuildResult] = {}
        for profile_name in self._sorted_active_profile_names():
            profile = self._state.profiles[profile_name]
            profile_dir = destination / profile_name
            profile_dir.mkdir(parents=True, exist_ok=True)

            self.logger.log(
                operation="bake_profile_start",
                profile=profile_name,
                phase="build",
                module="image",
                builder=self.backend,
                message=f"Starting profile bake via {self.backend} backend.",
            )

            # Build via the real backend
            request = BakeRequest(
                profile=profile_name,
                build_dir=destination,
                emit_dir=emission_root,
                output_targets=profile.output_targets,
            )

            backend.prepare(request)
            try:
                backend_result = backend.execute(request)
            finally:
                backend.cleanup(request)

            # Merge backend artifacts into profile result
            profile_result = backend_result.profiles.get(
                profile_name, ProfileBuildResult(profile=profile_name)
            )

            # If the backend didn't find typed artifacts for all targets,
            # check if the output files exist with expected names
            for target in profile.output_targets:
                if target not in profile_result.artifacts:
                    artifact_path = profile_dir / self._artifact_filename(target)
                    if artifact_path.exists():
                        profile_result.artifacts[target] = ArtifactRef(
                            target=target, path=artifact_path,
                        )

            # Generate build report
            script_checksums = self._script_checksums(
                emission.script_paths.get(profile_name, {})
            )
            artifact_digests = {
                target: hashlib.sha256(Path(artifact.path).read_bytes()).hexdigest()
                for target, artifact in sorted(profile_result.artifacts.items())
                if Path(artifact.path).exists()
            }
            profile_logs = self.logger.records_for_profile(profile_name)

            report_path = profile_dir / "report.json"
            report_payload = {
                "profile": profile_name,
                "lock_digest": lock_digest,
                "backend": self.backend,
                "debloat": self.explain_debloat(profile=profile_name),
                "artifact_digests": artifact_digests,
                "emitted_scripts": script_checksums,
                "artifacts": {
                    target: str(artifact.path)
                    for target, artifact in profile_result.artifacts.items()
                },
                "logs": profile_logs,
            }
            report_path.write_text(
                json.dumps(report_payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            profile_result.report_path = report_path
            profiles_result[profile_name] = profile_result

            self.logger.log(
                operation="bake_profile_complete",
                profile=profile_name,
                phase="build",
                module="image",
                builder=self.backend,
                message="Completed profile bake.",
            )

        bake_result = BakeResult(profiles=profiles_result)
        self._last_bake_result = bake_result
        return bake_result

    def measure(
        self,
        *,
        backend: Literal["rtmr", "azure", "gcp"],
        profile: str | None = None,
    ) -> Measurements:
        selected_profile = self._resolve_operation_profile(profile)
        if self._last_bake_result is None:
            raise MeasurementError(
                "No baked artifacts are available for measurement.",
                hint="Run bake() before measure().",
                context={"operation": "measure", "profile": selected_profile, "backend": backend},
            )
        profile_result = self._last_bake_result.profiles.get(selected_profile)
        if profile_result is None:
            raise MeasurementError(
                "Profile has no baked artifacts for measurement.",
                hint="Bake the selected profile before measure().",
                context={"operation": "measure", "profile": selected_profile, "backend": backend},
            )
        return derive_measurements(
            backend=backend,
            profile=selected_profile,
            profile_result=profile_result,
        )

    def deploy(
        self,
        *,
        target: OutputTarget,
        profile: str | None = None,
        parameters: Mapping[str, str] | None = None,
        # Common deploy parameters (passed through to adapter)
        memory: str | None = None,
        cpus: int | None = None,
        **kwargs: str,
    ) -> DeployResult:
        selected_profile = self._resolve_operation_profile(profile)
        if self._last_bake_result is None:
            raise DeploymentError(
                "No baked artifacts are available for deployment.",
                hint="Run bake() before deploy().",
                context={"operation": "deploy", "profile": selected_profile, "target": target},
            )

        artifact = self._last_bake_result.artifact_for(profile=selected_profile, target=target)
        if artifact is None:
            raise DeploymentError(
                "Requested deploy target artifact was not baked.",
                hint="Add the target via output_targets(...) and rerun bake().",
                context={"operation": "deploy", "profile": selected_profile, "target": target},
            )

        params = dict(parameters or {})
        params.update(kwargs)
        if memory is not None:
            params["memory"] = memory
        if cpus is not None:
            params["cpus"] = str(cpus)

        request = DeployRequest(
            profile=selected_profile,
            target=target,
            artifact_path=artifact.path,
            parameters=params,
        )
        return get_adapter(target).deploy(request)

    def _get_backend(self) -> LocalLinuxBackend | LimaBackend | InProcessBackend:
        """Select the appropriate build backend based on Image.backend setting."""
        if self._backend_override is not None:
            return self._backend_override  # type: ignore[return-value]
        if self.backend == "local_linux":
            return LocalLinuxBackend()
        if self.backend == "inprocess":
            return InProcessBackend()
        return LimaBackend()

    def set_backend(self, backend: object) -> Self:
        """Override the build backend (useful for testing or custom backends)."""
        self._backend_override = backend
        return self

    def _emit_config(self) -> EmitConfig:
        """Build an EmitConfig from the Image's settings."""
        emit_kwargs: dict[str, object] = {
            "base": self.base,
            "arch": self.arch,
            "reproducible": self.reproducible,
            "kernel": self.kernel,
            "with_network": self.with_network,
            "clean_package_metadata": self.clean_package_metadata,
            "manifest_format": self.manifest_format,
            "sandbox_trees": self.sandbox_trees,
            "package_cache_directory": self.package_cache_directory,
            "init_script": self.init_script,
            "generate_version_script": self.generate_version_script,
            "generate_cloud_postoutput": self.generate_cloud_postoutput,
            "emit_mode": self.emit_mode,
            "environment": self.environment,
            "environment_passthrough": self.environment_passthrough,
        }
        if self.compress_output is not None:
            emit_kwargs["compress_output"] = self.compress_output
        if self.output_directory is not None:
            emit_kwargs["output_directory"] = self.output_directory
        if self.seed is not None:
            emit_kwargs["seed"] = self.seed
        return EmitConfig(**emit_kwargs)  # type: ignore[arg-type]

    def _artifact_filename(self, target: OutputTarget) -> str:
        mapping: dict[OutputTarget, str] = {
            "qemu": "disk.qcow2",
            "azure": "disk.vhd",
            "gcp": "disk.raw.tar.gz",
        }
        return mapping[target]

    def _normalize_path(self, path: str | Path | None, *, fallback: Path | None = None) -> Path:
        if path is None:
            if fallback is None:
                raise ValidationError("A path value is required.")
            return fallback
        return Path(path)

    def _normalize_profile_names(self, names: tuple[str, ...]) -> tuple[str, ...]:
        if not names:
            raise ValidationError("At least one profile name is required.")
        normalized: list[str] = []
        for name in names:
            if not name:
                raise ValidationError("Profile names must be non-empty.")
            if name not in normalized:
                normalized.append(name)
        return tuple(normalized)

    def _validate_phase_order(self, *, phase: Phase, after_phase: Phase | None) -> None:
        if after_phase is None:
            return
        phase_index = PHASE_ORDER.index(phase)
        after_index = PHASE_ORDER.index(after_phase)
        if after_index >= phase_index:
            raise ValidationError(
                "Invalid phase hook dependency order.",
                hint="after_phase must be earlier than the hook phase.",
                context={"phase": phase, "after_phase": after_phase},
            )

    def _sorted_active_profile_names(self) -> list[str]:
        return sorted(self._active_profiles)

    def _default_lock_path(self) -> Path:
        return self.build_dir / "tdx.lock"

    def _compute_lock_digest(self, fallback_digest: str) -> str:
        lock_path = self._default_lock_path()
        if not lock_path.exists():
            return fallback_digest
        return hashlib.sha256(lock_path.read_bytes()).hexdigest()

    def _cache_store(self) -> BuildCacheStore:
        return BuildCacheStore(self.build_dir / ".cache" / "conversion")

    def _resolve_operation_profile(self, profile: str | None) -> str:
        if profile is not None:
            return profile
        if len(self._active_profiles) == 1:
            return self._active_profiles[0]
        raise ValidationError(
            "Operation requires an explicit profile when multiple profiles are active.",
            hint="Pass profile='name' to the operation.",
            context={"operation": "resolve_profile"},
        )

    def _iter_active_profiles(self) -> list[ProfileState]:
        profiles: list[ProfileState] = []
        for profile_name in self._active_profiles:
            profiles.append(self._state.ensure_profile(profile_name))
        return profiles

    def _recipe_payload(self, *, profile_names: tuple[str, ...]) -> dict[str, object]:
        profiles_data: dict[str, dict[str, object]] = {}
        for profile_name in sorted(profile_names):
            profile = self._state.profiles[profile_name]
            phases = {
                phase: [
                    {
                        "argv": list(command.argv),
                        "env": dict(command.env),
                        "cwd": command.cwd,
                        "shell": command.shell,
                    }
                    for command in commands
                ]
                for phase, commands in sorted(profile.phases.items())
            }
            repositories = [
                {
                    "name": repository.name,
                    "url": repository.url,
                    "suite": repository.suite,
                    "components": list(repository.components),
                    "keyring": repository.keyring,
                    "priority": repository.priority,
                }
                for repository in sorted(
                    profile.repositories,
                    key=lambda item: (item.priority, item.name, item.url),
                )
            ]
            files = [
                {
                    "path": file_entry.path,
                    "mode": file_entry.mode,
                    "sha256": hashlib.sha256(file_entry.content.encode()).hexdigest(),
                }
                for file_entry in sorted(profile.files, key=lambda item: item.path)
            ]
            templates = [
                {
                    "path": tmpl.path,
                    "mode": tmpl.mode,
                    "sha256": hashlib.sha256(tmpl.rendered.encode()).hexdigest(),
                    "variables": dict(sorted(tmpl.variables.items())),
                }
                for tmpl in sorted(profile.templates, key=lambda item: item.path)
            ]
            users = [
                {
                    "name": u.name,
                    "system": u.system,
                    "home": u.home,
                    "uid": u.uid,
                    "gid": u.gid,
                    "shell": u.shell,
                    "groups": list(u.groups),
                }
                for u in sorted(profile.users, key=lambda item: item.name)
            ]
            services = [
                {
                    "name": svc.name,
                    "exec": list(svc.exec),
                    "user": svc.user,
                    "after": list(svc.after),
                    "requires": list(svc.requires),
                    "wants": list(svc.wants),
                    "restart": svc.restart,
                    "enabled": svc.enabled,
                    "security_profile": svc.security_profile,
                }
                for svc in sorted(profile.services, key=lambda item: item.name)
            ]
            partitions = [
                {
                    "name": partition.name,
                    "size": partition.size,
                    "mount": partition.mount,
                    "fs": partition.fs,
                }
                for partition in sorted(profile.partitions, key=lambda item: item.name)
            ]
            hooks = [
                {
                    "phase": hook.phase,
                    "after_phase": hook.after_phase,
                    "argv": list(hook.command.argv),
                }
                for hook in sorted(
                    profile.hooks,
                    key=lambda item: (
                        PHASE_ORDER.index(item.phase),
                        item.after_phase or "",
                        item.command.argv,
                    ),
                )
            ]
            secrets = [
                {
                    "name": secret.name,
                    "required": secret.required,
                    "schema": None
                    if secret.schema is None
                    else {
                        "kind": secret.schema.kind,
                        "min_length": secret.schema.min_length,
                        "max_length": secret.schema.max_length,
                        "pattern": secret.schema.pattern,
                        "enum": list(secret.schema.enum),
                    },
                    "targets": [
                        {
                            "kind": t.kind,
                            "location": t.location,
                            "mode": t.mode,
                            "scope": t.scope,
                        }
                        for t in secret.targets
                    ],
                }
                for secret in sorted(profile.secrets, key=lambda item: item.name)
            ]
            skeleton_files = [
                {
                    "path": file_entry.path,
                    "mode": file_entry.mode,
                    "sha256": hashlib.sha256(file_entry.content.encode()).hexdigest(),
                }
                for file_entry in sorted(profile.skeleton_files, key=lambda item: item.path)
            ]
            profiles_data[profile_name] = {
                "packages": sorted(profile.packages),
                "build_packages": sorted(profile.build_packages),
                "build_sources": profile.build_sources,
                "output_targets": list(profile.output_targets),
                "phases": phases,
                "repositories": repositories,
                "files": files,
                "skeleton_files": skeleton_files,
                "templates": templates,
                "users": users,
                "services": services,
                "partitions": partitions,
                "hooks": hooks,
                "secrets": secrets,
                "debloat": {
                    "enabled": profile.debloat_enabled,
                    "remove": list(profile.debloat_remove),
                    "mask": list(profile.debloat_mask),
                },
            }

        return {
            "base": self._state.base,
            "arch": self._state.arch,
            "default_profile": self._state.default_profile,
            "profiles": profiles_data,
        }

    def _script_checksums(self, scripts: dict[Phase, Path]) -> dict[str, str]:
        checksums: dict[str, str] = {}
        for phase, path in sorted(scripts.items(), key=lambda item: PHASE_ORDER.index(item[0])):
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            checksums[f"{phase}:{path.name}"] = checksum
        return checksums

    def _assert_frozen_lock(self, *, profile_names: tuple[str, ...]) -> None:
        lock_path = self._default_lock_path()
        lock = read_lockfile(lock_path)
        current_recipe = self._recipe_payload(profile_names=profile_names)
        current_digest = recipe_digest(current_recipe)
        if lock.recipe_digest != current_digest:
            raise LockfileError(
                "Frozen bake lockfile is stale for current recipe state.",
                hint="Re-run img.lock() and commit the updated lockfile.",
                context={
                    "operation": "bake",
                    "mode": "frozen",
                    "expected": current_digest,
                    "actual": lock.recipe_digest,
                    "path": str(lock_path),
                },
            )

    def _convert_artifact(
        self,
        *,
        source_artifact: Path,
        profile_name: str,
        target: OutputTarget,
        dependencies: tuple[str, ...],
    ) -> tuple[ArtifactRef, bool]:
        artifact_path = source_artifact.parent / self._artifact_filename(target)
        source_hash = hashlib.sha256(source_artifact.read_bytes()).hexdigest()
        inputs = BuildCacheInput(
            source_hash=source_hash,
            source_tree=source_hash,
            toolchain="converter-v1",
            flags=(f"target={target}",),
            dependencies=dependencies,
            env={},
            target=target,
        )
        key = cache_key(inputs)
        cache_store = self._cache_store()
        cached_payload = cache_store.load(key=key, expected_inputs=inputs)
        if cached_payload is not None:
            artifact_path.write_bytes(cached_payload)
            return ArtifactRef(target=target, path=artifact_path), True

        payload = (
            "tdxvm converted artifact:\n"
            f"profile={profile_name}\n"
            f"target={target}\n"
            f"source={source_artifact.name}\n"
        ).encode()
        artifact_path.write_bytes(payload)
        cache_store.save(inputs=inputs, artifact=payload)
        return ArtifactRef(target=target, path=artifact_path), False
