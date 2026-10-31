# CLI

`tundravm` is the operator's loop in twelve commands:

| Command | Contract |
|---|---|
| `new` | a project: an image file, its tests, `pyproject.toml`, optional CI |
| `check` | declarations, lint, lock drift, optional committed-tree drift; no build |
| `lock` | resolve or check the pins: section digests, sources, toolchain |
| `build` | an immutable build directory: artifacts, measurements, SBOM, manifest |
| `show` | the resolved image or an existing build; `--diff` between two builds |
| `run` | a named local instance with an explicit security mode; `status`, `logs`, `stop`, `ssh`, `mode` |
| `deploy` | an explicit build on Azure or GCP with a receipt; `status`, `logs`, `mode`, `upgrade`, `rollback` |
| `attest` | fresh evidence checked against a policy; never delivers anything |
| `secrets` | `deliver` credentials or `unlock` a volume after a fresh attestation |
| `verify` | a build directory checked against its manifest, optionally rebuilt |
| `convert` | an mkosi tree or systemd unit files as source |
| `doctor` | what this host can build, run, deploy and verify |

`tundravm` with no arguments prints the help and a quickstart; `tundravm --version` prints
the version; every command takes `-h`/`--help` and `--traceback` (raise errors with the
Python traceback instead of one line).

## Project configuration

Three values, each resolved the same way: an explicit argument, then the environment, then
the `[tool.tundravm]` table of the nearest `pyproject.toml` (walking up from the working
directory), then the default.

| Key | Environment | Default | Meaning |
|---|---|---|---|
| `image` | `TUNDRAVM_IMAGE` | none | the image file (`IMAGE` on `check`, `lock`, `build`, `show`) |
| `build` | `TUNDRAVM_BUILD` | `build` | the build root: `BUILD/tundravm.lock` and `BUILD/<image>/...` |
| `backend` | `TUNDRAVM_BACKEND` | `local` on Linux x86-64, else `lima` | `local`, `lima` or `inprocess` |

```toml
[tool.tundravm]
image = "prover.py"
build = "build"

[tool.tundravm.gcp]
project = "my-project"
bucket = "my-images"
zone = "us-central1-a"
```

Paths in the table are relative to the `pyproject.toml`, in the environment to the working
directory. `[tool.tundravm.gcp]` and `[tool.tundravm.azure]` hold `deploy`'s cloud settings;
`TUNDRAVM_GCP_<KEY>` and `TUNDRAVM_AZURE_<KEY>` override them. The keys of earlier versions
are refused with what replaced them: `recipe` (now `image`), `out` (`build`), `tree` (pass
`check --tree DIR`), `lockfile` (always `BUILD/tundravm.lock`), `require` and
`strict_toolchain`. Instances live in `.tundravm/instances/` beside the `pyproject.toml`
(the working directory without one).

The image file binds an `Image` (or a `Recipe`) to `image`; `--attr NAME` picks another
name or a zero-argument factory, `--pythonpath DIR` adds an import directory (repeatable).
A file that fails to import is an `E_VALIDATION` error naming `file:line`.

## Exit codes and output

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | a failed check: `check` errors or drift, `lock --check` drift, a failing `verify`, an `attest` that does not pass, a `build --reproduce` whose rebuild differs, a blocked `doctor` row |
| 2 | an SDK error (`E_*` code) or a usage error (unknown commands and flags get a "did you mean") |
| 130 | interrupted |

An SDK error prints `error [E_CODE]: message` on stderr, then `Hint: ...` and one
`  key: value` line per context entry. With `--json` the error is one line instead:
`{"schema":"tundravm.error/1","code":...,"message":...,"hint":...,"context":{...}}`.
Every `--json` report is an object with `"schema": "tundravm.<command>/1"` (`tundravm.check/1`,
`tundravm.show/1`, `tundravm.run/1`, ...). In GitHub Actions (`GITHUB_ACTIONS=true`),
`check` and `lock --check` print workflow annotations.

## new

```
tundravm new NAME [--template service|prover] [--ci]
```

Writes `NAME/NAME.py` from the template, `NAME/tests/test_NAME.py` (check, compile and
golden-tree tests), `NAME/pyproject.toml` with `[tool.tundravm] image = "NAME.py"`, a
`README.md` and a `.gitignore` that ignores build directories and `.tundravm/` but keeps
`build/tundravm.lock`. Then it checks the new image and prints the next commands. `NAME` is
lowercase letters, digits, `-` and `_`; an existing non-empty directory is refused.

- `--template service` (default): one sandboxed web service and a `dev` profile.
  `--template prover`: a service behind `AttestedTLS`, its state on an `OperatorKey()`
  volume, a secret delivered after attestation, and `policies/` for the verifier policies.
- `--ci`: also `.github/workflows/tundravm.yml`: a `check` job (`show` to the job summary,
  `check --tree mkosi`, `lock --check`) and an opt-in `build` job that runs when the
  repository variable `TUNDRAVM_BACKEND` is set (`build --reproduce`, `verify latest`, the
  build directory uploaded).

`pyproject.toml` depends on tundravm from git, pinned to the commit of the tundravm that
ran `new` (read from its install metadata; when unknown it follows `master` and says so).

```console
$ tundravm new demo && cd demo
$ uv sync
$ uv run tundravm build
```

## check

```
tundravm check [IMAGE] [--profile P]... [--tree DIR] [--json]
```

Everything short of a build: resolution and declaration errors, every lint rule, drift from
`BUILD/tundravm.lock` when it exists (a missing lock is a note: the first `build` writes
it), the running toolchain against the lock's, and with `--tree DIR` the committed mkosi
tree against what the image compiles to (each changed file is a `tree-drift` error).
Errors exit 1; warnings and infos are reported only.

- `--profile P`: check this profile (repeatable; default: every profile).
- `--tree DIR`: compare a committed tree; rewrite it with `build --tree-only DIR`.
- `--json`: `{"diagnostics": [...], "summary": {...}, "tree": {...} | null, "passed": bool}`.
- `--attr NAME`, `--pythonpath DIR`: how the image file is loaded.

```console
$ tundravm check examples/surge-tdx-prover/image.py --tree examples/surge-tdx-prover/mkosi
0 errors, 14 warnings, 0 infos
tree examples/surge-tdx-prover/mkosi is up to date
```

## lock

```
tundravm lock [IMAGE] [--update [INPUT...] | --check] [--json]
```

Writes `BUILD/tundravm.lock`: a digest per section of the resolved image (every profile), a
pin per source (a commit for `Git`, a sha256 for `Http`) and the build toolchain (mkosi,
the tools tree's distribution, release and snapshot, the measurement tools). Pins already
in the lock are kept while their source is unchanged.

- `--update INPUT...`: resolve these inputs again; with no name, every one; `toolchain`
  records the running toolchain.
- `--check`: never writes and never touches the network: prints the drift
  (`~ variants.production.packages: +htop`) and exits 1 when the lock is stale.
- `--json`: `{"path", "clean", "drift"}` with `--check`, else `{"path", "recipe_digest", "updated"}`.

A build never changes the lock: a stale lock is refused by `build`, and only `lock` moves
it. Locking tries every source before failing, and writes nothing unless all resolve.

```console
$ tundravm lock --update raiko
locked build/tundravm.lock
$ tundravm lock --check
```

## build

```
tundravm build [IMAGE] [--profile P | --dev] [--target T[,T]] [--reproduce]
               [--tree-only DIR] [--offline] [--sign KEY] [--clean-cache] [-v]
```

Builds one profile into `BUILD/<image>/production/<build-id>/` (`development/` for a profile
that is not a production one, such as `dev`), published atomically and never written again;
see [Build directories](#build-directories). The lock must be current: a stale one is
refused with the drift, a missing one is written first (`lock written: ...`). Fetching the
pinned sources is part of the build; they and mkosi's tools tree and cache stay in
`BUILD/<image>/.work/` for the next build. Building an identity that is already published
leaves that directory untouched (`already built`).

- `--profile P`: the profile (default: the image's default, `production` for an `Image`).
  `--dev`: the `dev` profile (`DevAccess`), a development build.
- `--target T[,T]`: which of the image's `targets=` to build (`qemu`, `azure`, `gcp`;
  repeatable or comma-separated; default: all of them); `qemu` is always built. A target the
  image does not declare is refused: its cloud glue is part of the payload. The order does
  not change the build id.
- `--reproduce`: build again in an independent workspace (`BUILD/<image>/.reproduce/`, its
  own sources, state and tree) and record the comparison in `observations.json`; exit 1 when
  an artifact differs (the second workspace is kept) or the second build fails.
- `--tree-only DIR`: write the standalone mkosi project (every profile, or `--profile`/`--dev`)
  and print the `mkosi` command that builds each profile from it; nothing is built.
- `--offline`: no network in the build: only what the lock pins (refused without a lock).
- `--sign KEY`: sign `manifest.json` with an SSH private key (`manifest.sig`).
- `--clean-cache`: drop the image's workspace first (fetched sources, tools tree, cache).
- `-v`, `--verbose`: echo mkosi's output (progress goes to stderr either way).
- `--attr NAME`, `--pythonpath DIR`: how the image file is loaded.

```console
$ tundravm build --target gcp
built build/prover/production/3f1c9a07d2b84e65
  gcp.tar.gz           9e1f...
  payload.efi          41aa...
  qemu.raw             c07b...
$ tundravm build --tree-only mkosi
```

## show

```
tundravm show [BUILD|IMAGE] [--units | --tree | --measurements] [--json]
tundravm show --diff OLD NEW [--json]
```

Without a build: the image as it resolves, profile by profile (packages, files with modes,
units, sources and their pins, hooks, kernel, and the declarations each came from), from an
`IMAGE` argument or the project's image. With `BUILD` (a build directory, a build id or its
prefix, `latest`, `latest-production`, `latest-development`): its manifest read back with
its provenance, the observations, whether it is signed, and every guarantee it does not give.

- `--units`: the unit files and drop-ins (of the image's compiled tree, or the build's `tree/`).
- `--tree`: the tree's files with mode, size and symlink target.
- `--measurements`: the build's measurement records (a build only).
- `--diff OLD NEW`: what changed between two builds: packages (mkosi's manifest), files and
  units (the trees), the boot payload (UKI digest, root hash, command line), measurements per
  target and boot path, and policy coverage (registers newly predicted or no longer predicted).
- `--profile P`: profiles of an image (default: all). `--dev`: the latest development build.
- `--attr NAME`: the image's name in its file. `--json`: the same as JSON.

```console
$ tundravm show --units
$ tundravm show latest --measurements
$ tundravm show --diff 3f1c9a07 latest
```

## run

```
tundravm run [BUILD] --mode simulated|tdx [--dev] [--detach] [--memory M] [--cpus N]
tundravm run status|logs|stop|ssh|mode INSTANCE
```

Starts the build's `qemu.raw` through OVMF from its ESP (the disk boot path, the one the
measurements predict), on a snapshot so the build directory is never written, as instance
`<image>-<n>` with its receipt in `.tundravm/instances/<image>-<n>/`. Every TCP port the
image's firewall accepts or its services declare (`ports=`, recorded in the manifest's
`ports`), and the management port `:8745`, is forwarded from `127.0.0.1`
(the same port when it is free and unprivileged, else a free one; the receipt and the output
name each). Each `OperatorKey()` or other volume gets a sparse 16 GiB disk with serial
`tundravm-NAME`. `BUILD` defaults to `latest-production` (`latest-development` with `--dev`).

- `--mode simulated`: no TDX; KVM when `/dev/kvm` is usable, TCG otherwise; the console
  settings reach the kernel through SMBIOS. Evidence is `simulated`, never production. A
  production build's services wait for a TDX quote that never comes: run `--dev` builds.
- `--mode tdx`: TDVF and a `tdx-guest` object, nothing appended to the command line;
  refused unless `doctor --for tdx` is ready.
- `--detach`: return once QEMU runs; without it the console is shown until QEMU exits, and
  Ctrl-C stops the instance.
- `--memory M` (default `2G`), `--cpus N` (default `2`).
- `--identity KEY`: the SSH key `run ssh` uses.
- `--json`: for `status` and `mode`.

Actions on an instance: `status` (the process, then control-plane readiness, the management
port answering, and application readiness, every service port answering, as two states),
`logs` (the serial console), `stop` (`quit` on the QEMU monitor, then SIGTERM), `ssh` (a
shell, only on a development build whose image runs SSH; the host key is trusted on first
use, not verified through an attested endpoint) and `mode` (the security mode and what it
means for evidence).

```console
$ tundravm run --mode simulated --dev
instance demo-1: simulated (development build 5b5bf2fe7ff6b3ca)
  :8080  -> 127.0.0.1:8080  (service)
  :8745  -> 127.0.0.1:8745  (management)
$ curl http://127.0.0.1:8080/
$ tundravm run stop demo-1
```

## deploy

```
tundravm deploy azure|gcp --build BUILD [--volume NAME=create:SIZE|existing:ID]...
tundravm deploy status|logs|mode INSTANCE
tundravm deploy upgrade INSTANCE --build BUILD --policy FILE [NAME=@FILE...]
tundravm deploy rollback INSTANCE --policy FILE [NAME=@FILE...]
```

Publishes the build's `azure.vhd` or `gcp.tar.gz` and starts a TDX VM from it with
today's adapters (`az`, `gcloud` on `PATH`, logged in), then writes the instance receipt:
the VM, its address, the volumes and the firewall rule the image's ports and `:8745` need
(tundravm does not create cloud firewall rules). `--build` is required: a deployment never
guesses its build, and a development build is deployed only when named, with a warning.

- `--build BUILD`: the build to deploy (or, for `upgrade`, the candidate).
- `--volume NAME=create:SIZE|existing:ID`: a disk for the image's volume `NAME`
  (repeatable); every declared volume needs one. GCP attaches it as device `tundravm-NAME`,
  which the image finds as `/dev/disk/by-id/*-tundravm-NAME`; neither is deleted with the
  VM. Azure is refused for an image with volumes: its data disks are identified by LUN.
- `--policy FILE`: the verifier policy the candidate is attested against (`upgrade`, `rollback`).
- `--identity KEY`, `--validator ENDPOINT`: as for `attest`.
- `--json`: the receipt or the status as JSON.

`status` reports the cloud's VM state and the two readiness states; `logs` the VM's serial
console; `mode` the security mode. `upgrade` runs the one replacement workflow on GCP, each
step a receipt: launch the candidate without volumes; attest it and deliver the
`NAME=@FILE` credentials (control plane ready); stop the old VM (drain and fence); detach
every volume from it and attach it to the candidate; unlock the candidate's operator-key
volumes (passphrases are read once, first); wait for application readiness. Downtime runs
from the stop to readiness. It is refused on Azure, for a build whose volumes differ from
the instance's (names or unlock kinds), and when the candidate does not pass the release
predicate (the old VM keeps running with its volumes). `rollback` moves the volumes back to
the instance an upgrade replaced and starts it again, attested and unlocked the same way.
Retiring the old policy when the rollback window closes is the operator's step.

```console
$ tundravm deploy gcp --build build/prover/production/3f1c9a07d2b84e65 --volume data=create:100GiB
deployed 3f1c9a07d2b84e65 to gcp: instance prover-1
$ tundravm deploy upgrade prover-1 --build build/prover/production/8d0e... --policy policies/prover-gcp.json raiko-config=@raiko.json
```

## attest

```
tundravm attest INSTANCE --policy FILE [--output REPORT] [--json]
```

Asks the instance's tdxs issuer for a quote bound to a fresh nonce and checks MRTD and
RTMR0..RTMR2 against `FILE`: an operator-approved `tundravm.policy/2` file, or a build's
`measurements.json`, whose record for the instance's platform and disk boot path is used.
It delivers nothing. Verdicts: `trusted` (only with `--validator`, which judges the quote's
signature and collateral), `partial` (nothing disagreed, but a register went unchecked:
approve MRTD and RTMR0 in the policy's `platform_approval`), `measurements-match`,
`measurements-mismatch`, `simulated` (passes for a simulated instance) and `untrusted`.
Exit 0 when it passes. A passing attestation records the policy in the receipt (`secrets
unlock` reuses it) and makes the session's SSH host key the instance's `known_hosts`. An
azure policy is refused: Azure's evidence is vTPM-bound.

- `--policy FILE`: the verifier policy (required).
- `--output REPORT`: also write the `tundravm.attestation/2` record (document, nonce,
  registers, the policy's sha256, the validator's reply, the verdict, `observed_at`).
- `--identity KEY`: the operator's SSH key (default `$TUNDRAVM_SSH_KEY`, else
  `~/.ssh/id_ed25519`; its `.pub` beside it).
- `--issuer ENDPOINT`: `unix:PATH` or `tcp://HOST:PORT`, an issuer the operator relays,
  instead of the SSH relay.
- `--validator ENDPOINT`: a tdxs validator (`unix:PATH` or `tcp://HOST:PORT`).
- `--json`: the attestation as JSON, with the channel it came over.

The channel is today's bootstrap; see [The control-plane bootstrap](#the-control-plane-bootstrap).

```console
$ tundravm attest prover-1 --policy policies/prover-gcp.json --output attest-1.json
```

## secrets

```
tundravm secrets deliver INSTANCE --policy FILE NAME=@FILE...
tundravm secrets unlock INSTANCE VOLUME
```

`deliver` attests again (fresh nonce), applies the release predicate, then writes each value
to `/run/tundravm/credentials/NAME` (mode 0600, written to a temporary name and renamed),
where the unit that binds it reads it as a systemd credential. Every `NAME` must be a secret
the image declares. `unlock` attests again with the policy of the instance's last passing
`attest`, then writes the passphrase (typed without echo, or piped on stdin) into the
`OperatorKey()` volume's FIFO.

The release predicate: a production build on TDX needs `trusted`; a development build on TDX
accepts `trusted` or `measurements-match`; a simulated instance accepts `simulated`, and
nothing it receives is production. `partial` never releases.

- `deliver --policy FILE`: the verifier policy (required). `NAME=@FILE`: a secret and the
  file holding its value; the value never goes on the command line.
- `--identity KEY`, `--validator ENDPOINT`: as for `attest`.

```console
$ tundravm secrets unlock prover-1 data
$ tundravm secrets deliver prover-1 --policy policies/prover-gcp.json raiko-config=@raiko.json
```

## verify

```
tundravm verify BUILD [--require-signature] [--allowed-signers FILE] [--rebuild] [--json]
```

Checks a build directory against its manifest, offline; each check is `ok`, `failed`,
`missing` or `unchecked`, and the build passes when none failed:

| Check | Passes when |
|---|---|
| `manifest` | `manifest.json` is in canonical form (an edit shows) |
| `id` | the recorded id is the manifest's digest and the directory's name |
| `artifacts` | every indexed file exists with its sha256, and no other file is there |
| `tree` | `tree/` hashes to the recorded digest |
| `uki` | `payload.efi` is the recorded UKI and carries the recorded root hash |
| `lock`, `sbom`, `packages`, `summary` | the part is recorded (`missing` with the reason otherwise; an unbound SBOM is `unchecked`) |
| `measurements` | every target and boot path has a prediction (`unchecked` names the ones without) |
| `reproduction` | `observations.json` records a matching independent build (`unchecked` without one) |
| `signature` | `manifest.sig` verifies against `--allowed-signers` (`unchecked` without the file, `missing` without a signature) |
| `rebuild` | with `--rebuild`: an independent rebuild matches every artifact |

- `--require-signature`: fail unless the signature is `ok`.
- `--allowed-signers FILE`: an OpenSSH allowed-signers file naming who may sign.
- `--rebuild`: build the recorded profile and targets again against the recorded
  `inputs/tundravm.lock`, in a fresh build root, and compare. This runs the project's image
  file (the project's image, or `TUNDRAVM_IMAGE`).
- `--json`: `{"id", "passed", "checks": [...], "unchecked": [...], "signer", ...}`.

```console
$ tundravm verify build/prover/production/3f1c9a07d2b84e65 --require-signature --allowed-signers allowed_signers
```

## convert

```
tundravm convert PATH --from mkosi|systemd [--allow-scripts] [--mode exact|semantic]
```

`--from systemd`: unit files (one file, or every unit and `NAME.d/*.conf` drop-in of a
directory) as typed declarations, pairing `x.socket` and `x.timer` with `x.service`. A unit
no typed shape renders stays text, and a comment names what blocked it. `--mode exact`
(default) keeps a typed declaration only when it renders the file byte for byte, `semantic`
when it renders the same directives.

`--from mkosi`: the tree resolved by mkosi itself (includes, `mkosi.conf.d`, profiles and
subimages), as an `Image` file: `Distribution=debian` and `Release=` as `base=`,
`Packages=` as `packages=`, `ExtraTrees=` and `SkeletonTrees=` as `Tree(...)`, scripts as
`Mkosi(scripts=)`. Every other setting that differs from mkosi's default is printed as
`# unsupported: KEY=VALUE`, and the subimages as notes. Parsing runs a tree's executable
`mkosi.version` or `mkosi.configure`: such a tree is refused unless `--allow-scripts`.

```console
$ tundravm convert tests/fixtures/native-tree/tree-initramfs --from mkosi > fixture.py
$ tundravm convert units/ --from systemd --mode semantic
```

## doctor

```
tundravm doctor [--for build|run|tdx|azure|gcp] [--json]
```

One row per capability, each `ready`, `blocked` (with the fix), `unavailable` or
`unsupported`. `--for` limits it to one: `build` (this host's backend), `run` (QEMU and OVMF
for `run --mode simulated`), `tdx` (a TDX host and TDVF), `azure` and `gcp` (the CLI and an
account). Exit 1 when a build row (or, with `--for`, a row of that area) is blocked.

```console
$ tundravm doctor
build:x86-64     ready
run:simulated    ready (qemu 9.2, OVMF /usr/share/OVMF/OVMF_CODE_4M.fd, KVM)
run:tdx          unavailable: no Intel TDX host (kvm_intel tdx is not enabled)
```

## Build directories

```
BUILD/
  tundravm.lock                    the lock (committed)
  <image>/
    production/<build-id>/
      payload.efi                  the UKI, the payload that is measured
      qemu.raw                     the raw GPT disk OVMF boots through its ESP (always built)
      azure.vhd                    when the build targets azure
      gcp.tar.gz                   when the build targets gcp
      payload.*.raw                the verity root and hash partitions, when the root is verity
      packages.json                mkosi's package manifest
      measurements.json            per target and boot path: predictions, unknowns, the policy
      sbom.spdx.json               the SBOM, bound to the lock and the package manifest
      inputs/                      tundravm.lock, summary.json, sources.json, toolchain.json, mkosi-argv.json
      tree/                        the mkosi project it was built from
      manifest.json                the identity
      manifest.sig                 with --sign
      observations.json            timestamps, runner, reproduction attempts
    development/<build-id>/
    latest-production -> production/<build-id>
    latest-development -> development/<build-id>
    .work/                         the reused workspace (sources, mkosi tools tree and cache)
```

A build runs in `.work/`, assembles the directory in a scratch sibling and publishes it with
one `rename`; the files are made read-only. `inputs/summary.json` is `mkosi --force summary
--json` of `tree/`, with host-specific settings dropped and host paths written as `$TREE`,
`$WORK`, `$HOME`, `$MKOSI_RESOURCES`.

**`manifest.json`** (`tundravm.manifest/1`) is canonical bytes: sorted keys, two-space
indent, ASCII, one trailing newline. The build id is the first 16 hex digits of the sha256
of the canonical manifest without its `id`, so the same inputs and bytes give the same id.

| Key | Holds |
|---|---|
| `schema`, `id` | `tundravm.manifest/1`, the build id |
| `image`, `profile`, `kind` | the image name, the profile, `production` or `development` |
| `simulated` | built by the in-process backend: the artifacts are placeholders |
| `targets`, `ports`, `rootfs` | the targets built (in the image's `targets=` order, or `qemu`, `azure`, `gcp` when `--target` narrows them or `qemu` is added), the TCP ports the services declare (what `run` forwards besides the firewall's), `initramfs` or `verity` |
| `recipe_digest` | the digest the lock records for the resolved image |
| `artifacts` | the artifact index: every file outside `tree/`, `manifest.json`, `manifest.sig` and `observations.json`, with `sha256` and `size` |
| `uki` | `{path, sha256, size, cmdline}` of `payload.efi` |
| `root_hash` | the `roothash=` on the UKI's command line (verity), else `null` |
| `lock` | `{path: "inputs/tundravm.lock", sha256, recipe_digest}` |
| `tree` | `{path: "tree", digest, files}`: `sha256:` over each entry's type, mode (write bits left out), content digest or symlink target, in path order |
| `sources` | `[{name, kind, source, ref, pin}]` from the lock |
| `packages`, `sbom`, `summary` | `{path, sha256, ...}` (`sbom` also `bound` and `notes`), or `{state: "missing", reason}` |
| `measurements` | `{path, sha256, records: {"qemu/uki": "predicted", ...}}` |
| `toolchain` | tundravm, the backend, the lock's toolchain |
| `unchecked` | every guarantee the build does not give, in words |

**`measurements.json`** (`tundravm.measurements/1`) holds one record per target and boot path
(`qemu/uki`, `qemu/disk`, `gcp/disk`, `azure/disk`): `target`, `boot`, `artifact`,
`artifact_sha256` and `state`, which is `predicted` (with `scheme`, `tool`, `registers`,
`unknowns`, `rtmr3`, `uki` and `policy`, a `tundravm.policy/2` an operator can approve),
`unknown` or `unchecked` (with `reason`). RTMR3 is outside the policy: no event schema is
defined for it.

**`observations.json`** (`tundravm.observations/1`) is not identity: `started_at`,
`finished_at`, `duration_s`, `runner` (hostname, user, platform, python, backend, the CI
run), `lock_written`, and `reproduction`: `null`, or `{state: matched|different|error,
artifacts: {name: {first, second}}, differ, workspace, error}`.

## Instances and receipts

`.tundravm/instances/<instance-id>/` holds one instance:

- `instance.json` (`tundravm.instance/1`): `id`, `kind` (`run` or `deploy`), `platform`
  (`qemu`, `gcp`, `azure`), `mode` (`simulated` or `tdx`), `state`, the `build` (path, id,
  image, profile, kind, simulated), `ports` (guest to host), `volumes`, and per kind the QEMU
  `pid` and `command`, or the cloud VM, `address` and `firewall`; `attested` (the last
  passing policy), `bootstrap` (the operator key's fingerprint, `first_contact: unverified`),
  `previous`/`successor` across an upgrade.
- `events.jsonl`: one receipt per thing done to it (`launched`, `first-contact`, `attested`,
  `delivered`, `delivery-refused`, `unlocked`, `stopped`, `deployed`, `fenced`,
  `volume-attached`, `upgraded`, `rolled-back`, ...), with the time; names, never values.
- `console.log`, `known_hosts`, and a local VM's monitor socket, OVMF variables and volume disks.

## Signing and verifying a manifest

```console
$ ssh-keygen -t ed25519 -f release-key -N ''
$ tundravm build --sign release-key
$ echo "release@example namespaces=\"tundravm-manifest\" $(cut -d' ' -f1,2 release-key.pub)" > allowed_signers
$ tundravm verify latest --require-signature --allowed-signers allowed_signers
```

`manifest.sig` is `ssh-keygen -Y sign -n tundravm-manifest` over the bytes of
`manifest.json`, so it signs the whole identity and cannot be replayed as a git or file
signature. `verify` needs only the build directory and the allowed-signers file: copy the
directory anywhere and check it there.

## The control-plane bootstrap

Until the attested endpoint exists (plan gate 3), the way into an instance is the one an
image with a volume or a secret already runs: tundra-tools' `secret-delivery` on the
management port `:8745` takes the operator's SSH public key once (an HTTP `POST` of its
base64 body) and lets that key in as root. That first contact is **unverified**: nothing
binds the key exchange, the SSH host key or the session to a quote. `attest`, `secrets` and
every receipt say so.

- `attest` POSTs the key (once per instance), then asks tdxs for the quote over that SSH
  session (one `socat - UNIX-CONNECT:/var/tdxs.sock` per request).
- `secrets deliver` and `secrets unlock` attest again first, then write over the same
  session. Values travel on the SSH command's standard input only: never on argv, in a log,
  a tree, a manifest or a receipt.

## Shell completion

The scripts are generated from the parser:

```console
$ source <(python -m tundravm.completion bash)       # ~/.bashrc
$ source <(python -m tundravm.completion zsh)        # ~/.zshrc, after compinit
$ python -m tundravm.completion fish > ~/.config/fish/completions/tundravm.fish
```

## Installing tundravm in the project

tundravm is not on PyPI, so the generated `pyproject.toml` depends on it from git, pinned to
the commit of the tundravm that ran `new` (from its install metadata: a git install records
the commit; an editable checkout pins its `HEAD` when a remote branch contains it; otherwise
the dependency follows `master` and the comment says so). `uv sync` installs it and
`pytest`; commit `uv.lock`, and the generated workflow installs exactly it. `uv add
--editable PATH/TO/tundravm` works against a local checkout instead.

## What replaced the old commands

| Old command | Where its guarantee went |
|---|---|
| `init` | `new NAME [--template service\|prover] [--ci]` |
| `inspect` | `show [IMAGE]` (and `--units`, `--tree`, `--json`) |
| `lint` | `check` |
| `compile` | `build --tree-only DIR`; `compile --check` is `check --tree DIR` |
| `diff` | `check --tree DIR` (tree drift); `show --diff OLD NEW` (between builds) |
| `fetch` | `build` fetches; `lock --update` resolves; `build --offline` uses only the lock |
| `bake` | `build` (`--verify-reproducible` is `--reproduce`) |
| `measure` | `build` writes `measurements.json`, with a policy per record; `show --measurements` |
| `sbom` | `build` writes `sbom.spdx.json` |
| `evidence` | the build directory and its manifest; `verify` checks it |
| `changes` | `show --diff OLD NEW` |
| `release` | a signed build (`build --sign`); `verify --require-signature` |
| `witness` | `verify --rebuild` and `build --reproduce` |
| `explore`, `preview`, `status` | `show` |
| `watch` | `check` (run it on save) |
| `clean` | `build --clean-cache`; build directories are plain directories to delete |
| `config` | the [Project configuration](#project-configuration) table above; errors name the key |
| `ci` | `check --tree DIR` and `lock --check` |
| `completion` | `python -m tundravm.completion SHELL` |
| `units` | `convert PATH --from systemd` (`units convert`); `show --units` (`units render`) |
| `import` | `convert PATH --from mkosi` |
| `deploy qemu` | `run --mode simulated\|tdx` |
