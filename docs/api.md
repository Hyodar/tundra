# API Reference

Compact reference for `tundravm`. Signatures are taken from `src/tundravm/image.py`. All mutating `Image` methods return `Self` and can be chained. Declarations apply to the active profile set (see [Profiles](#profiles)).

```python
from tundravm import Image
```

## `Image`

### Construction and fields

`Image` is a dataclass. All fields are keyword arguments to the constructor and remain assignable afterwards (`img.kernel = Kernel.tdx_kernel("6.8")`).

| Field | Default | Meaning |
| --- | --- | --- |
| `build_dir` | `Path("build")` | Default bake output and lockfile directory; accepts `str` or `Path` |
| `base` | `"debian/bookworm"` | Distribution/release |
| `arch` | `"x86_64"` | `"x86_64"` or `"aarch64"` |
| `default_profile` | `"default"` | Name of the implicit profile |
| `backend` | `None` | `BuildBackend` used by `bake()`; required for `bake()` only |
| `reproducible` | `True` | Registers `strip_image_version()` on construction |
| `policy` | `Policy()` | Strictness settings; see `set_policy()` |
| `kernel` | `None` | `Kernel` spec (custom or TDX kernel build) |
| `with_network` | `True` | mkosi `WithNetwork=` |
| `clean_package_metadata` | `True` | mkosi `CleanPackageMetadata=` |
| `manifest_format` | `"json"` | mkosi `ManifestFormat=` |
| `compress_output` | `None` | mkosi `CompressOutput=` |
| `output_directory` | `None` | mkosi `OutputDirectory=` |
| `seed` | `None` | mkosi `Seed=` for partition UUIDs |
| `mirror` | `None` | Package mirror, e.g. a Debian snapshot URL |
| `tools_tree_mirror` | `None` | Mirror for the mkosi tools tree |
| `sandbox_trees` | `()` | `host:guest` sandbox tree entries |
| `package_cache_directory` | `None` | mkosi `PackageCacheDirectory=` |
| `init_script` | `None` | Override for the default TDX init script |
| `generate_version_script` | `False` | Emit `mkosi.version` |
| `generate_cloud_postoutput` | `True` | Emit Azure/GCP conversion postoutput scripts |
| `environment` | `None` | mkosi `Environment=` entries |
| `environment_passthrough` | `None` | mkosi `PassEnvironment=` |
| `emit_mode` | `"per_directory"` | `"per_directory"` (one dir per profile) or `"native_profiles"` (`mkosi.profiles/`) |
| `init` | `Init()` | Runtime-init builder; receives `add_init_script()` fragments |

`Image.DEFAULT_TDX_INIT` holds the built-in init script text.

### Profiles

| Method | Description |
| --- | --- |
| `state -> RecipeState` | Property. Raw recipe state; `state.profiles[name]` is a `ProfileState` |
| `profile_names -> tuple[str, ...]` | Property. Every declared profile, sorted |
| `profile(name, *, extends=<default profile>) -> Profile` | Declare profile `name` and return its handle (see below). A new profile extends the default profile; `extends=None` makes it standalone |
| `profiles(*names, extends=<default profile>) -> ContextManager[Self]` | Scope declarations to several profiles at once |
| `all_profiles() -> ContextManager[Self]` | Scope to every profile declared so far (sorted by name) |

A profile extends the default profile: it compiles to the default's packages, files, users, services, hooks, init scripts and modules plus its own declarations, and its own declaration wins on the same file path, unit, user, partition or repository name. `output_targets` and `debloat` fall back to the default when unset.

`Profile` (`from tundravm import Profile`) works two ways. As a context manager, `with img.profile("azure"):` scopes every `img.*` call inside the block to that profile (the `as` target is the `Image`). As an object, it carries the declaration API bound to that profile, and each call returns the `Profile`, so chains stay on it:

```python
azure = img.profile("azure")
azure.apply(AzurePlatform()).install("walinuxagent").output_targets("azure")
azure.service("agent", command="/usr/bin/agent", env={"LOG": "info"})
```

| Member | Description |
| --- | --- |
| `name`, `image` | Profile name and owning `Image` |
| `install`, `file`, `directory`, `template`, `user`, `service`, `apply`, `output_targets`, `debloat`, `run`, `hook`, `repository`, `partition`, `add_init_script` | Same signatures as on `Image`, run with only this profile active; return the `Profile` |
| any other `Image` method returning `Self` (`backports`, `skeleton`, `build_install`, `ssh`, ...) | Same, resolved dynamically (typed as `(...) -> Profile`) |
| `state -> ProfileState` | Property. This profile's state |
| `explain()`, `summary()`, `explain_debloat()`, `applied_modules()` | `Image` counterparts with `profile=name` (`applied_modules()` lists only the profile's own modules) |
| `check() -> list[Diagnostic]` | Diagnostics for this profile only |
| `compile(path, *, force=False)`, `lock(path=None)`, `diff(against)`, `bake(output_dir=None, *, frozen=False, force=False)` | Run with only this profile active |
| `measure(*, backend)`, `deploy(*, target, parameters=None, memory=None, cpus=None)` | `Image` counterparts with `profile=name` |

### Packages and repositories

| Method | Description |
| --- | --- |
| `install(*packages) -> Self` | Runtime packages |
| `build_install(*packages) -> Self` | Build-only packages, removed after build |
| `build_source(host_path, target="") -> Self` | Mount a host directory into the build (`BuildSources=`) |
| `repository(url, *, name=None, suite=None, components=(), keyring=None, priority=100) -> Self` | Extra apt repository |

### Files and templates

| Method | Description |
| --- | --- |
| `file(path, *, content=None, src=None, mode="0644") -> Self` | Place a file in the image (`mkosi.extra/`); exactly one of `content`/`src`. `content` may be `bytes`; a `src` that is not UTF-8 is copied as bytes |
| `directory(dest, *, src, mode=None, exclude=()) -> Self` | Place every file under host directory `src` at `dest`, keeping relative paths, in sorted order. Mode is `mode`, else `0755` for host-executable files and `0644` otherwise. `exclude` holds fnmatch globs on the path relative to `src` (`*` also matches `/`); a matching directory is skipped whole |
| `template(dest, *, src=None, template=None, variables=None, mode="0644") -> Self` | Render `{name}` placeholders with `variables`, then place the file |
| `skeleton(path, *, content=None, src=None, mode="0644") -> Self` | Place a file before the package manager runs (`mkosi.skeleton/`) |

### Users and services

| Method | Description |
| --- | --- |
| `user(name, *, system=False, home=None, shell="/usr/sbin/nologin", uid=None, gid=None, groups=()) -> Self` | Create a user; names unique per profile |
| `service(name, *, command=(), description=None, user=None, working_dir=None, env=None, env_file=None, exec_start_pre=(), after=(), requires=(), wants=(), restart="no", enabled=True, extra_unit=None, security_profile="default") -> Self` | Register a unit. With `command` the SDK generates the unit file; without it, only enablement is emitted. `description` sets `Description=` (default: the name). `exec_start_pre` adds one `ExecStartPre=` per command, `working_dir` sets `WorkingDirectory=`, `env_file` sets `EnvironmentFile=`, and `env` adds `Environment=` lines sorted by key, quoted when a value has spaces, quotes or backslashes. `restart`: `"always"`, `"on-failure"`, `"no"`. `security_profile`: `"strict"`, `"default"`, `"none"` |

### Partitions and outputs

| Method | Description |
| --- | --- |
| `partition(name, *, size, mount, fs="ext4") -> Self` | Extra data partition |
| `output_targets(*targets) -> Self` | Any of `"qemu"`, `"azure"`, `"gcp"`; default `("qemu",)` |

### Build phases and hooks

Phases run in this order: `sync`, `skeleton`, `prepare`, `build`, `extra`, `postinst`, `finalize`, `postoutput`, `clean`, `repart`, `boot`.

| Method | Description |
| --- | --- |
| `run(command, *, phase="postinst", env=None, cwd=None) -> Self` | Shell snippet in a phase; alias for `hook(phase, command)` |
| `hook(phase, command, *, env=None, cwd=None, after_phase=None) -> Self` | Shell snippet in a phase; `after_phase` must be an earlier phase |
| `sync(command, *, env=None) -> Self` | `sync` phase (before build) |
| `prepare(command, *, env=None) -> Self` | `prepare` phase (after base packages, before build) |
| `finalize(command, *, env=None) -> Self` | `finalize` phase (host, `$BUILDROOT`) |
| `postoutput(command, *, env=None) -> Self` | `postoutput` phase (after the disk image is written) |
| `clean(command, *, env=None) -> Self` | `clean` phase (`mkosi clean`) |
| `on_boot(command, *, env=None) -> Self` | `boot` phase: a systemd oneshot run at VM boot |

Hooks run on the host with mkosi variables (`$BUILDROOT`, `$DESTDIR`, `$BUILDDIR`). Use `mkosi-chroot <cmd>` to execute inside the image.

### Reproducibility helpers

| Method | Description |
| --- | --- |
| `strip_image_version(*, enabled=True) -> Self` | Finalize hook removing `IMAGE_VERSION` from `os-release`; on by default via `reproducible=True` |
| `efi_stub(*, snapshot_url, package_version) -> Self` | Postinst hook installing `systemd-boot-efi` from a Debian snapshot |
| `backports(*, mirror=None, release=None) -> Self` | Sync hook generating `debian-backports.sources`; adds the matching `sandbox_trees` entry |
| `debloat(*, enabled=True, paths_remove=None, paths_skip=(), paths_remove_extra=(), paths_skip_for_profiles=None, systemd_minimize=True, systemd_units_keep=None, systemd_units_keep_extra=(), systemd_bins_keep=None) -> Self` | Configure path removal and systemd minimization; `None` keeps the defaults from `DebloatConfig` |
| `explain_debloat(*, profile=None) -> dict[str, object]` | Effective debloat settings for one profile |

### Init

| Method | Description |
| --- | --- |
| `add_init_script(script, *, priority=100) -> Self` | Append a bash fragment to `/usr/bin/runtime-init`; lower priority runs first |
| `ssh() -> Self` | Install `dropbear` (dev profiles) |

### Introspection

| Method | Description |
| --- | --- |
| `apply(*modules) -> Self` | Call `module.apply(img)` for each argument in order; chainable. Accepts any `Applicable`; `Module` subclasses are recorded |
| `applied_modules(profile=None, *, inherited=False) -> tuple[Module, ...]` | `Module` instances applied to one profile, in apply order; `inherited=True` puts the extended profile's modules first |
| `explain(*, profile=None) -> dict[str, object]` | Dry-run description of the recipe for one profile |
| `summary(*, profile=None) -> str` | Human-readable form of `explain()` |
| `check(*, profiles=None) -> list[Diagnostic]` | Lint the active profiles (or `profiles`); see [Diagnostics](#diagnostics) |
| `diff(against) -> TreeDiff` | Compile the active profiles to a temp dir and diff against the tree at `against`; writes nothing |

The same views are available from the shell: `tundravm explain`, `check` and `diff`. See [cli.md](cli.md).

### Lifecycle

| Method | Description |
| --- | --- |
| `set_policy(policy) -> Self` | Replace the `Policy` |
| `lock(path=None) -> Path` | Write the lockfile for the active profiles; default `build_dir / "tundravm.lock"` |
| `lock_status(path=None) -> LockDrift` | Compare the lockfile with the recipe section by section; never writes. `LockfileError` if the lockfile is missing |
| `compile(path, *, force=False) -> CompileResult` | Emit the mkosi tree for the active profiles; skipped when digest and path are unchanged. Each profile directory is recreated, so files dropped from the recipe disappear |
| `emit_mkosi(path) -> CompileResult` | Deprecated alias of `compile()` |
| `bake(output_dir=None, *, frozen=False, force=False) -> BakeResult` | Run `check()` (error-level findings raise `ValidationError`), `compile()` into `<output_dir>/mkosi`, then build each active profile with the backend into `<output_dir>/<profile>/` and write `<output_dir>/bake-result.json`; `frozen=True` fails on a stale lockfile with `LockfileError` listing the drifted sections |
| `measure(*, backend, profile=None) -> Measurements` | Derive measurements from `last_bake()`; `backend` is `"rtmr"`, `"azure"` or `"gcp"` |
| `deploy(*, target, profile=None, parameters=None, memory=None, cpus=None) -> DeployResult` | Deploy an artifact from `last_bake()`; `parameters` are adapter-specific strings |
| `last_bake(build_dir=None) -> BakeResult` | The latest bake result, loaded from `<build_dir>/bake-result.json` when this process has not baked. With `build_dir` it reloads from that directory (for a bake made with `output_dir`). `StateError` if there is none |

`profile=None` is accepted only when exactly one profile is active.

## Public models (`from tundravm import ...`)

| Name | Description |
| --- | --- |
| `Image` | The recipe object above |
| `Profile` | Handle returned by `Image.profile(name)`; see [Profiles](#profiles) |
| `Applicable` | Protocol: anything with `apply(image: Image) -> None`; accepted by `Image.apply()` |
| `load_recipe(path, attr=None, extra_paths=()) -> Image` | Load an `Image` from a recipe file using the CLI resolution rules |
| `__version__` | Package version string |
| `Policy` | `require_frozen_lock`, `mutable_ref_policy` (`warn`/`error`/`allow`), `require_integrity`, `network_mode` (`online`/`offline`) |
| `Kernel` | Kernel spec; constructors `Kernel.generic(version)`, `Kernel.from_config(path)`, `Kernel.tdx_kernel(version, *, cmdline=None, config_file=None, source_repo=...)` |
| `DebloatConfig` | Frozen defaults for `debloat()`; `effective_paths_remove`, `effective_units_keep` |
| `SecretSchema` | `kind` (`string`/`json`), `min_length`, `max_length`, `pattern`, `enum` |
| `SecretTarget` | Where a secret lands; `SecretTarget.file(path, *, mode="0400", owner=None)`, `SecretTarget.env(name, *, scope="service")` |
| `SecretSpec` | `name`, `required`, `schema`, `targets` |
| `ProfileState` | Per-profile declarations: `packages`, `build_packages`, `files`, `services`, `users`, `phases`, `hooks`, `init_scripts`, `debloat`, `output_targets` |
| `RecipeState` | `base`, `arch`, `default_profile`, `profiles` |
| `CompileResult` | `path`, `profiles`, `digest`; behaves like a `Path` (`/`, `exists()`) |
| `BakeRequest` | `profile`, `build_dir`, `emit_dir`, `output_targets`; passed to backends |
| `BakeResult` | `profiles: dict[str, ProfileBuildResult]`, `lock_digest`, `backend`, `created_at`; `artifact_for(profile=, target=) -> ArtifactRef \| None`; `save(build_dir)`, `BakeResult.load(build_dir)` for `bake-result.json` |
| `Diagnostic` | One lint finding: `level` (`error`/`warning`/`info`), `code`, `message`, `hint`, `profile`, `subject`; `to_dict()` |
| `TreeDiff`, `FileChange` | Result of `Image.diff()`: the changed files and their unified diffs |
| `Measurements` | `backend`, `values`; `to_json(path=None)`, `to_cbor(path=None)`, `verify(expected) -> VerificationResult` |

Other useful imports:

- `tundravm.modules`: `KeyGeneration`, `DiskEncryption`, `SecretDelivery`, `Tdxs`, `Devtools`, `Init`, and the `Module` base class (`name`, `requires`, `init_priority`; `setup`, `install`, `init_script`, `check`; final `apply`). See [module-authoring.md](module-authoring.md).
- `tundravm.backends`: `LimaMkosiBackend`, `NixMkosiBackend`, `LocalLinuxBackend`, `InProcessBackend` (tests), `BuildBackend` (`name`, `requirements()`, `mount_plan()`, `prepare()`, `execute()`, `cleanup()`), `Requirement`.
- `tundravm.lockfile`: `LockDrift` (`changed`, `added`, `removed`, `is_clean`, `render()`), `compare_lock`, `section_digests`.
- `tundravm.testing`: test helpers and pytest fixtures; see [testing.md](testing.md).
- `tundravm.platforms`: `AzurePlatform`, `GcpPlatform`.
- `tundravm.build_cache`: `Build`, `Cache` for cached source builds in `build` hooks.

## Errors (`tundravm.errors`)

All errors derive from `TdxError(message, *, code, hint=None, context=None)` and expose `.code`, `.hint`, `.context`, `.to_dict()`.

| Class | Code | Raised when |
| --- | --- | --- |
| `ValidationError` | `E_VALIDATION` | Bad arguments, duplicate names, missing backend, unmet module `requires`, `bake()` with error-level lint findings |
| `LockfileError` | `E_LOCKFILE` | `bake(frozen=True)` with a missing or stale lockfile |
| `ReproducibilityError` | `E_REPRODUCIBILITY` | Artifact digests differ between equivalent builds |
| `BackendExecutionError` | `E_BACKEND_EXECUTION` | mkosi/backend failure, unsupported mkosi version |
| `MeasurementError` | `E_MEASUREMENT` | `measure()` for a profile the last bake did not build, or unknown backend |
| `DeploymentError` | `E_DEPLOYMENT` | `deploy()` without a baked artifact for the target, or a missing deploy tool |
| `StateError` | `E_STATE` | `measure()`/`deploy()`/`last_bake()` with no `bake-result.json` |
| `PolicyError` | `E_POLICY` | Policy violation (non-frozen bake, mutable ref, offline network) |

`ErrorCode` is a `StrEnum` of the codes above.

## Diagnostics

`Image.check()` and `tundravm check` return `Diagnostic`s. Codes are stable.

| Code | Level | Finding |
| --- | --- | --- |
| `service-user-missing` | error | A service runs as a user the profile never creates |
| `file-path-duplicate` | error | Two files at the same path |
| `file-path-relative` | error | A file path that is not absolute |
| `service-command-not-shipped` | warning | A service command that no package or file provides |
| `output-target-platform-mismatch` | warning | An `azure`/`gcp` target without its platform module |
| `profile-empty` | info | A profile with no declarations of its own |
| `init-priority-collision` | warning | Two init scripts at the same priority |
| `backend-missing` | warning | No backend, so `bake()` cannot run |
| `debloat-removes-needed-unit` | warning | Debloat masks a unit a declared service needs |
| `debloat-removes-declared-file` | warning | Debloat deletes a declared file |
| `secret-undelivered` | warning | A secret with no delivery target, or secrets declared with no delivery at boot |
| `disk-key-undefined` | error | `DiskEncryption` reads a key no `KeyGeneration` declares |
| `disk-key-path-mismatch` | warning | A disk reads a key path the key never writes |
| `key-pipe-outside-run` | info | A pipe-strategy key whose pipe is not under `/run` |
| `platform-target-missing` | warning | `AzurePlatform`/`GcpPlatform` applied without its output target |

`bake()` refuses recipes with any error-level finding.
