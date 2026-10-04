# Changelog

## Unreleased

### Added

- `tundravm` command (also `python -m tundravm`) with `new`, `explain`, `check`, `digest`, `compile` (`--check`), `diff`, `lock` (`--check`, `--explain`), `bake`, `measure`, `deploy` and `doctor`. See `docs/cli.md`.
- `load_recipe()`: loads an `Image` from a Python file (`img`, `build()`, or the single `Image`/`build_*` in the file; `--attr` to choose). The file runs under a private `__name__`.
- `Image.apply(*modules)`: applies modules in order and chains. `Applicable` protocol for anything with `apply(image)`.
- `Image.explain()` and `Image.summary()`: dry-run description of one profile, including `Extends:` and `Modules:`.
- `Image.check()` linter returning `Diagnostic`s (level, code, profile, subject, message, hint). Rules: `service-user-missing`, `file-path-duplicate`, `file-path-relative`, `service-command-not-shipped`, `output-target-platform-mismatch`, `profile-empty`, `init-priority-collision`, `backend-missing`, `debloat-removes-needed-unit`, `debloat-removes-declared-file`, `secret-undelivered`, plus module checks `disk-key-undefined`, `disk-key-path-mismatch`, `key-pipe-outside-run`, `platform-target-missing`.
- `Image.diff(against)` returning a `TreeDiff`, `tundravm diff`, and `compile --check` as a CI drift gate.
- `Module` base class with `name`, `requires`, `init_priority` and `setup()`/`install()`/`init_script()`/`check()` hooks. `Image.applied_modules(profile=None, *, inherited=False)`.
- `bake()` writes `<out>/bake-result.json`. `BakeResult.save()`/`load()`, `Image.last_bake(build_dir=None)`, so `measure()` and `deploy()` work in a new process.
- `StateError` (`E_STATE`) for missing or unreadable persisted state.
- `measure --out` and `deploy --out` to follow a bake made with `--out`.
- `doctor` probes each backend's `Requirement`s (`limactl`, `nix`, `mkosi`, `sudo`/`unshare`).
- `Profile` objects: `img.profile(name)` returns a handle with the declaration API and profile-scoped `explain`/`summary`/`check`/`compile`/`lock`/`diff`/`bake`/`measure`/`deploy`. `Image.profile_names`.
- `img.profile(name, extends=None)` for a standalone profile.
- `Image.directory(dest, *, src, mode=None, exclude=())`: imports a host directory tree, keeping the executable bit.
- `service()` gains `description`, `working_dir`, `env`, `env_file` and `exec_start_pre`.
- Lockfile section digests (`base`, `arch`, `default_profile`, `init_scripts`, `profiles.<name>.<section>`). `Image.lock_status(path=None) -> LockDrift`. `tundravm.lockfile` exports `LockDrift`, `compare_lock`, `section_digests`.
- `tundravm.testing`: `compile_tree()`/`CompiledTree`, `assert_clean()`, `assert_diagnostic()`, `assert_tree_matches()` (golden trees, `TUNDRAVM_UPDATE_GOLDEN=1`), `bake_in_process()`, `FakeModule`, `recipe_file()`, `run_cli()`. A pytest plugin registers the `image`, `inprocess_image`, `compiled` and `run_cli` fixtures.
- `QemuDeployAdapter` accepts an injectable runner.
- Docs: concepts, tutorial, API, CLI, module authoring, testing.

### Changed

- `bake()` runs the linter before building.
- Frozen-bake errors list the drifted lockfile sections (up to 15) instead of a bare digest mismatch.
- Lockfile version 1 → 2. Version 1 files still load and still pass frozen bakes; `lock --check` reports every section as added until you re-lock.
- `explain` shows `Extends:` and `Modules:` lines and previews hooks by their first non-comment line.
- Init priorities live on the module classes: `KeyGeneration` 10, `DiskEncryption` 20, `SecretDelivery` 30. `Raiko` (example) declares `requires = (Tdxs,)`.
- Small examples expose `build() -> Image` and no longer bake on import. The surge recipe is rewritten on the new API and `python -m examples.surge-tdx-prover` delegates to the CLI. Its compiled tree is unchanged.

### Breaking

- Profiles extend the default profile. A profile's image is the default's packages, files, users, services, hooks, init scripts and modules plus its own additions; the profile wins on the same file path, unit, user, partition or repository. Previously each profile compiled to a standalone image. Pass `extends=None` for the old behaviour.
- The `Module` and `InitModule` protocols are removed. Modules subclass the `Module` base class. `apply()` is final; override `setup()`, `install()`, `init_script()` or `check()` instead. Init priorities are the `init_priority` class attribute, not an `apply()` argument.
- `img.profile(name)` returns a `Profile`, not a context manager yielding the `Image`. `with img.profile(name):` still works.
- `measure()` and `deploy()` with no bake result raise `StateError` (`E_STATE`) instead of `MeasurementError`/`DeploymentError`.
- `bake()` raises `ValidationError` for a recipe with error-level lint findings.
- `compile()` recreates each profile directory, so stale files are removed. Files at the tree root and other profiles' directories are kept.
- The `BuildBackend` protocol gains `requirements() -> tuple[Requirement, ...]`.
- `FileEntry.content` may be `bytes`. `file(src=...)` copies non-UTF-8 files as bytes.
- `Image.build_dir` is normalized to a `Path` (a `str` is accepted).

### Fixed

- `runtime-init.service` is registered once per profile, so compiling the default profile and then all profiles no longer fails with a duplicate service.
- A file removed from the recipe no longer lingers in the compiled tree and in `compile --check`.
- `examples/full_api.py`: a `Requires=` on a `secrets-ready.target` that nothing provides, and a disk reading a key path the key never writes.
