# Concepts

## Recipe, compiled tree, baked artifact

An `Image` is a **recipe**: an in-memory record of packages, files, users, services, hooks, and init scripts. Nothing touches disk until you ask.

```python
from tundravm import Image

img = Image(base="debian/bookworm")
img.install("curl")
img.service("app", command="/usr/bin/app")
```

`img.compile("build/mkosi")` turns the recipe into a **compiled mkosi tree**. It is a plain directory you can read, diff, commit, or build by hand with `mkosi build`. The same recipe always produces the same tree. Each profile directory is recreated on compile, so a file dropped from the recipe disappears from the tree. `compile()` returns a `CompileResult` with the `path`, the `profiles` emitted, and the recipe `digest`.

`img.bake()` lints the recipe (`img.check()`; error-level findings stop it), compiles into `<build_dir>/mkosi/`, then hands each profile to the configured backend, which runs mkosi and writes the **baked artifact** (qcow2, VHD, or tar.gz) into `<build_dir>/<profile>/` next to a `report.json`. `bake()` returns a `BakeResult`; `result.artifact_for(profile="default", target="qemu")` gives the `ArtifactRef`. The result is also saved as `<build_dir>/bake-result.json`, which `img.last_bake()` loads in a later process.

## Profiles

Every declaration goes to the active profile set. With no block open, that is the default profile (`Image.default_profile`, `"default"`). `img.profile(name)` returns a `Profile`: use it in a `with` block, or declare on it directly.

```python
with img.profile("azure"):
    img.output_targets("azure")
    img.install("waagent")

gcp = img.profile("gcp")
gcp.install("google-guest-agent").output_targets("gcp")

with img.profiles("azure", "gcp"):
    img.install("cloud-init")          # both profiles

with img.all_profiles():
    results = img.bake()               # every profile declared so far
```

A profile extends the default profile. Its image is the default's packages, files, users, services, hooks, init scripts and modules plus its own declarations. When both declare the same file path, unit, user, partition or repository name, the profile wins. `output_targets` and `debloat` fall back to the default's values when the profile does not set them. `img.profile("standalone", extends=None)` starts from an empty image instead; only init-script fragments, which live on `img.init`, still apply to it. `tundravm explain --profile NAME` shows the merged result with an `Extends:` line.

`compile()`, `lock()`, and `bake()` operate on the active set. Compiled trees have one directory per profile (`<path>/<profile>/mkosi.conf`, `mkosi.extra/`, `mkosi.skeleton/`, `scripts/`). Bake outputs land in `<output_dir>/<profile>/`. With `emit_mode="native_profiles"` you get a single root `mkosi.conf` plus `mkosi.profiles/<name>/` overrides instead.

`measure()` and `deploy()` need a single profile. Pass `profile="azure"` when more than one is active, or call `img.profile("azure").measure(...)`.

## Build phases

Hooks attach to mkosi phases. `PHASE_ORDER` in `tundravm.compiler` fixes the order:

| # | Phase | Runs | Use |
| --- | --- | --- | --- |
| 1 | `sync` | host, before packages | fetch inputs, apt sources |
| 2 | `skeleton` | host | files needed before the package manager |
| 3 | `prepare` | host | pre-build setup |
| 4 | `build` | host, `mkosi-chroot` available | compile binaries into `$DESTDIR` |
| 5 | `extra` | host | extra trees |
| 6 | `postinst` | host, `mkosi-chroot` available | users, enablement, config |
| 7 | `finalize` | host, `$BUILDROOT` | last edits before the image is packed |
| 8 | `postoutput` | host | act on the written disk image |
| 9 | `clean` | host | `mkosi clean` |
| 10 | `repart` | host | partition layout |
| 11 | `boot` | guest | systemd oneshot at first boot |

`img.run(cmd)` defaults to `postinst`. `img.hook(phase, cmd, after_phase=...)` validates that `after_phase` comes earlier. Each phase becomes one script under `scripts/NN-<phase>.sh` in the compiled tree, with hooks in declaration order.

## Runtime init ordering

Modules that must run at boot before application services set `init_priority` on the class and return a bash fragment from `init_script()`; `Module.apply()` registers it. Plain recipes call `img.add_init_script(script, priority=N)`. At `compile()` the SDK:

1. Sorts fragments by priority (lower first) and deduplicates identical ones.
2. Writes `/usr/bin/runtime-init` (`#!/bin/bash`, `set -euo pipefail`) and `/usr/lib/systemd/system/runtime-init.service` (oneshot, `After=network.target network-setup.service`).
3. Adds `After=runtime-init.service` and `Requires=runtime-init.service` to every other `img.service()` unit, and enables `runtime-init.service`.

Built-in priorities: `KeyGeneration` 10, `DiskEncryption` 20, `SecretDelivery` 30. The default for `add_init_script()` is 100. Modules that write their own unit files use `tundravm.modules.resolve.resolve_after()` to get the same `After=` entry. See [module-authoring.md](module-authoring.md).

## Lockfile and frozen bakes

`img.lock()` writes `build/tundravm.lock` (or the path you pass) containing the recipe payload for the active profiles, its SHA-256 `recipe_digest`, resolved dependencies, and fetch digests.

`img.bake(frozen=True)` recomputes the digest and refuses to build if it differs from the lockfile: `LockfileError` (`E_LOCKFILE`), listing the drifted recipe sections. `img.lock_status()` and `tundravm lock --check` show the same drift without baking. This is the CI mode. Combine with `Policy(require_frozen_lock=True)` so a non-frozen `bake()` fails with `E_POLICY` instead of silently building. See [policy.md](policy.md) and [reproducibility.md](reproducibility.md).

```python
img.lock()
img.bake(frozen=True)
```

## Backends

The backend decides where mkosi runs. `compile()` and `lock()` never need one; `bake()` does.

| Backend | Runs mkosi | Notes |
| --- | --- | --- |
| `LimaMkosiBackend(cpus=, memory=, disk=)` | inside a Lima VM with Nix | default; macOS and Linux |
| `NixMkosiBackend()` | `nix develop` on the host | Linux with Nix |
| `LocalLinuxBackend()` | `mkosi` on the host via `sudo`/`unshare` | Linux, mkosi >= 25 |
| `InProcessBackend()` | nothing real | tests; produces placeholder artifacts |

```python
from tundravm.backends import LimaMkosiBackend
img = Image(backend=LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB"))
```

A backend implements `prepare(request)`, `execute(request)`, `cleanup(request)` over a `BakeRequest`, plus `requirements()`, the host tools `tundravm doctor` probes.

## Measurements

`img.measure(backend=...)` derives expected attestation values from the artifacts in `img.last_bake()` and returns a `Measurements` object.

| Backend | Values | Source |
| --- | --- | --- |
| `"rtmr"` | RTMR registers | `measured-boot` or `dstack-mr` if on `PATH`; otherwise a deterministic SHA-256 derivation from artifact digests |
| `"azure"` | `PCR0`, `PCR1`, `PCR7` | SHA-256 over artifact digests, profile name, targets |
| `"gcp"` | `PCR0`, `PCR4`, `PCR8` | same scheme, GCP prefix |

```python
m = img.measure(backend="rtmr")
m.to_json("build/default/measurements.json")
check = m.verify(expected)        # VerificationResult(ok, mismatches)
```

## Deploy adapters

`img.deploy(target=..., profile=None, parameters=None, memory=None, cpus=None)` picks the adapter for the target and passes the baked artifact plus string parameters. It returns a `DeployResult(target, deployment_id, endpoint, metadata)`.

| Target | Adapter | Parameters (defaults) |
| --- | --- | --- |
| `qemu` | `QemuDeployAdapter` | `memory` (`2G`), `cpus` (`2`), `ssh_port` (`2222`), `tdx` (`false`), `daemonize` (`true`) |
| `azure` | `AzureDeployAdapter` | `resource_group` (`tdx-vms`), `location` (`eastus`), `vm_size` (`Standard_DC2s_v3`), `storage_account` |
| `gcp` | `GcpDeployAdapter` | `project`, `zone` (`us-central1-a`), `machine_type` (`n2d-standard-2`), `bucket` |

```python
img.deploy(target="qemu", memory="4G", cpus=4, parameters={"tdx": "true"})
```

With no bake result at all, `measure()` and `deploy()` raise `StateError` (`E_STATE`). The deploy target must be in that profile's `output_targets` and baked, otherwise `DeploymentError` (`E_DEPLOYMENT`).
