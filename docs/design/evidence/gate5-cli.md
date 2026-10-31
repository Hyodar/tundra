# Gate 5 evidence: CLI and build records

On the branch of this gate, rebased onto `79dd67a` (the compiler's shared-payload split and
fixed ESP). Plan section 8's gate 5 row asks for the twelve commands, immutable build
directories and signed manifests, `verify`, and instance and deployment receipts, with a
signed manifest verified on a second machine as the evidence. Everything below was run on
this host (`hetzner`, Linux x86-64, mkosi 27.1 through sudo: no unprivileged user
namespaces, no QEMU, no TDX); paths are shortened: `$A` is the build root of the real build,
`$B` the copy the second checkout verified, `$KEYS` where the signing key lives.

## The twelve commands

`tundravm --help`, and every command's help, as the parser prints them (shell completion is
generated from this parser by `python -m tundravm.completion bash|zsh|fish`; there is no
`completion` command):

```
$ tundravm --help
usage: tundravm [-h] [--version] COMMAND ...

Build, run and verify confidential VM images declared as one Image.

positional arguments:
  COMMAND
    new       Create a project: an image file, its tests, pyproject.toml.
    check     Check the image: declarations, lint, lock drift, tree drift.
    lock      Resolve or check the lock: snapshot, sources, toolchain.
    build     Build into an immutable build directory, with its manifest.
    show      Show the resolved image or a build; --diff two builds.
    run       Run a build locally as a named instance; status|logs|stop|ssh|mode.
    deploy    Deploy a build to Azure or GCP; status|logs|mode|upgrade|rollback.
    attest    Verify an instance's evidence against a policy; delivers nothing.
    secrets   Deliver credentials or unlock a volume after a fresh attestation.
    verify    Check a build directory against its manifest.
    convert   Turn an mkosi tree or systemd unit files into source.
    doctor    Report what this host can build, run, deploy and verify.

options:
  -h, --help  show this help message and exit
  --version   show program's version number and exit

IMAGE defaults to [tool.tundravm] image in the nearest pyproject.toml, or $TUNDRAVM_IMAGE; the
build root to its `build` key or $TUNDRAVM_BUILD (default: build); the backend to
$TUNDRAVM_BACKEND or its `backend` key (default: local on Linux x86-64, lima elsewhere).

$ tundravm new --help
usage: tundravm new [-h] [--traceback] [--template {service,prover}] [--ci] NAME

Write NAME/NAME.py from a template, tests/test_NAME.py, pyproject.toml with
[tool.tundravm] image = "NAME.py", README.md and .gitignore; --ci adds a GitHub
workflow that runs check, lock --check and build. The prover template also makes
policies/, where the operator-approved verifier policies go.

positional arguments:
  NAME                  Project directory and image name.

options:
  -h, --help            show this help message and exit
  --traceback           Raise errors with the Python traceback.
  --template {service,prover}
                        service: one sandboxed web service and a dev profile (default); prover: a
                        service behind attested TLS, its state on an operator-unlocked volume, a
                        secret.
  --ci                  Also write .github/workflows/tundravm.yml.

$ tundravm check --help
usage: tundravm check [-h] [--attr NAME] [--pythonpath DIR] [--json] [--traceback] [--profile P]
                      [--tree DIR]
                      [IMAGE]

Everything short of a build: resolution and declaration errors, every lint rule,
drift from BUILD/tundravm.lock when it exists, the toolchain against the lock's,
and with --tree a committed mkosi tree against what the image compiles to.
Exit 1 on an error (or a drifted lock or tree); warnings are reported only.
In GitHub Actions the findings are workflow annotations.

positional arguments:
  IMAGE             The image file (default: [tool.tundravm] image, or $TUNDRAVM_IMAGE).

options:
  -h, --help        show this help message and exit
  --json            Print a JSON report.
  --traceback       Raise errors with the Python traceback.
  --profile P       Check this profile (repeatable; default: every profile).
  --tree DIR        A committed tree to compare.

loading the image file:
  --attr NAME       The name the image (or a zero-argument factory) is bound to in the file.
  --pythonpath DIR  Extra import directory for the file (repeatable); the working directory is
                    one.

$ tundravm lock --help
usage: tundravm lock [-h] [--attr NAME] [--pythonpath DIR] [--json] [--traceback]
                     [--update [INPUT ...] | --check]
                     [IMAGE]

Write BUILD/tundravm.lock: the image's section digests, a pin per source, the
toolchain. Pins already locked are kept while their source is unchanged; --update
resolves the named inputs again (every one when none is named; `toolchain` records
the running toolchain). --check never writes and never touches the network: it
prints the drift and exits 1 when the lock is stale. A build never changes the lock.

positional arguments:
  IMAGE                 The image file (default: [tool.tundravm] image, or $TUNDRAVM_IMAGE).

options:
  -h, --help            show this help message and exit
  --json                Print a JSON report.
  --traceback           Raise errors with the Python traceback.
  --update [INPUT ...]  Resolve these inputs again (no name: all of them).
  --check               Report drift; exit 1 when stale.

loading the image file:
  --attr NAME           The name the image (or a zero-argument factory) is bound to in the file.
  --pythonpath DIR      Extra import directory for the file (repeatable); the working directory is
                        one.

$ tundravm build --help
usage: tundravm build [-h] [--attr NAME] [--pythonpath DIR] [--traceback] [--profile P | --dev]
                      [--target T[,T]] [--reproduce] [--tree-only DIR] [--offline] [--sign KEY]
                      [--clean-cache] [-v]
                      [IMAGE]

Build one profile into BUILD/<image>/production/<id>/ (development/ for a
non-production profile): payload.efi, qemu.raw and each --target's disk,
measurements.json, sbom.spdx.json, inputs/, tree/, manifest.json (the identity;
<id> is its digest), manifest.sig with --sign, observations.json. The directory
appears atomically and is never written again; latest-production points at the
newest. A stale lock is refused; a missing one is written first. --tree-only
writes the standalone mkosi project instead and builds nothing.

positional arguments:
  IMAGE             The image file (default: [tool.tundravm] image, or $TUNDRAVM_IMAGE).

options:
  -h, --help        show this help message and exit
  --traceback       Raise errors with the Python traceback.
  --profile P       The profile (default: production).
  --dev             The dev profile (DevAccess): non-production.
  --target T[,T]    Build these of the image's targets= (default: all of them); qemu is always
                    built.
  --reproduce       Build again in an independent workspace and record the comparison; exit 1 when
                    an artifact differs.
  --tree-only DIR   Only write the mkosi tree to DIR.
  --offline         No network: build only what the lock pins.
  --sign KEY        Sign the manifest with this SSH key.
  --clean-cache     Drop the image's workspace first (fetched sources, mkosi's tools tree and
                    cache).
  -v, --verbose     Echo mkosi's output.

loading the image file:
  --attr NAME       The name the image (or a zero-argument factory) is bound to in the file.
  --pythonpath DIR  Extra import directory for the file (repeatable); the working directory is
                    one.

$ tundravm show --help
usage: tundravm show [-h] [--json] [--traceback]
                     [--units | --tree | --measurements | --diff OLD NEW] [--profile P] [--dev]
                     [--attr NAME]
                     [BUILD]

Without BUILD: the image as it resolves (every profile's packages, files, units,
sources, kernel, and where each declaration came from). With BUILD (a build
directory, an id prefix, or `latest`): its manifest, provenance and unchecked
guarantees. --units, --tree and --measurements narrow it. --diff OLD NEW compares
two builds: packages, files, units, boot payload, measurements, policy coverage.

positional arguments:
  BUILD           A build, or an image file.

options:
  -h, --help      show this help message and exit
  --json          Print a JSON report.
  --traceback     Raise errors with the Python traceback.
  --units         The unit files.
  --tree          The tree's files.
  --measurements  The measurement records (a build).
  --diff OLD NEW  Compare two builds.
  --profile P     Profiles of an image (default: all).
  --dev           The latest development build.
  --attr NAME     The image's name in its file.

$ tundravm run --help
usage: tundravm run [-h] [--json] [--traceback] [--mode {simulated,tdx}] [--dev] [--detach]
                    [--memory MEMORY] [--cpus CPUS] [--identity KEY]
                    [BUILD|ACTION] [INSTANCE]

tundravm run [BUILD] --mode simulated|tdx [--dev] [--detach]
    boot qemu.raw through OVMF from its ESP on a snapshot (the build is never
    written), forward every TCP port the image's firewall accepts or its services
    declare, and the management port :8745, from 127.0.0.1, and write
    .tundravm/instances/<image>-<n>/.
    simulated: no TDX (KVM if usable, else TCG), verdicts are `simulated`.
    tdx: TDVF and a tdx-guest object; refused unless `doctor --for tdx` is ready.
tundravm run status INSTANCE   the process, control-plane and application readiness
tundravm run logs INSTANCE     the serial console
tundravm run stop INSTANCE     quit QEMU
tundravm run ssh INSTANCE      a shell, only on a development build that runs SSH
tundravm run mode INSTANCE     the security mode and what it means for evidence

positional arguments:
  BUILD|ACTION          A build, or an action.
  INSTANCE              The instance of an action.

options:
  -h, --help            show this help message and exit
  --json                Print a JSON report.
  --traceback           Raise errors with the Python traceback.
  --mode {simulated,tdx}
                        Security mode (required to start).
  --dev                 The latest development build (default: production).
  --detach              Return once QEMU runs (default: show the console).
  --memory MEMORY       Guest memory (default: 2G).
  --cpus CPUS           Guest CPUs (default: 2).
  --identity KEY        SSH key for `run ssh`.

$ tundravm deploy --help
usage: tundravm deploy [-h] [--json] [--traceback] [--build BUILD]
                       [--volume NAME=create:SIZE|existing:ID] [--policy FILE] [--identity KEY]
                       [--validator ENDPOINT]
                       azure|gcp|ACTION [ARG ...]

tundravm deploy azure|gcp --build BUILD [--volume NAME=create:SIZE|existing:ID]
    publish the build's disk and start a TDX VM from it, with a receipt; every
    volume the image declares needs --volume (GCP attaches it as tundravm-NAME;
    Azure is refused: its disks have no name the image can find). Settings come
    from [tool.tundravm.gcp] (project, bucket, zone, machine_type) and
    [tool.tundravm.azure] (storage_account, resource_group, location, vm_size,
    gallery), or TUNDRAVM_GCP_* / TUNDRAVM_AZURE_*. BUILD is never guessed.
tundravm deploy status|logs|mode INSTANCE
tundravm deploy upgrade INSTANCE --build BUILD --policy FILE [NAME=@FILE...]
    the replacement workflow (GCP): launch the candidate, attest it and deliver its
    credentials, stop the old one, move the volumes, unlock, wait for readiness
tundravm deploy rollback INSTANCE --policy FILE [NAME=@FILE...]
    the same back to the instance it replaced

positional arguments:
  azure|gcp|ACTION      Where to deploy, or an action.
  ARG                   INSTANCE, then NAME=@FILE credentials.

options:
  -h, --help            show this help message and exit
  --json                Print a JSON report.
  --traceback           Raise errors with the Python traceback.
  --build BUILD         The build to deploy (required).
  --volume NAME=create:SIZE|existing:ID
                        A disk for the image's volume NAME (repeatable).
  --policy FILE         The verifier policy (upgrade, rollback).
  --identity KEY        The operator's SSH key.
  --validator ENDPOINT  A tdxs validator.

$ tundravm attest --help
usage: tundravm attest [-h] [--json] [--traceback] --policy FILE [--output REPORT]
                       [--identity KEY] [--issuer ENDPOINT] [--validator ENDPOINT]
                       INSTANCE

Ask the instance's tdxs issuer for a quote bound to a fresh nonce and check MRTD and
RTMR0..2 against FILE: an operator-approved tundravm.policy/2 file, or a build's
measurements.json (the record for the instance's platform). With --validator a tdxs
validator judges the quote's signature and collateral; only then can it be
`trusted`. Verdicts: trusted, partial, measurements-match, measurements-mismatch,
simulated (accepted for a simulated instance), untrusted. Exit 0 when it passes.

Channel: unverified first contact: the channel is the bootstrap SSH session, not bound
to the quote (gate 3 adds the attested endpoint). The first contact POSTs the
operator's SSH public key to :8745; the quote then comes over that SSH session.
--issuer names an endpoint the operator relays instead.

positional arguments:
  INSTANCE

options:
  -h, --help            show this help message and exit
  --json                Print a JSON report.
  --traceback           Raise errors with the Python traceback.
  --policy FILE         The verifier policy.
  --output REPORT       Write the record (tundravm.attestation/2).
  --identity KEY        The operator's SSH key (default: ~/.ssh/id_ed25519).
  --issuer ENDPOINT     unix:PATH or tcp://HOST:PORT instead of the SSH relay.
  --validator ENDPOINT  A tdxs validator (unix:PATH or tcp://HOST:PORT).

$ tundravm secrets --help
usage: tundravm secrets [-h] [--traceback] deliver|unlock ...

tundravm secrets deliver INSTANCE --policy FILE NAME=@FILE...
    attest again, then write each value to /run/tundravm/credentials/NAME (0600);
    a production build needs `trusted`.
tundravm secrets unlock INSTANCE VOLUME
    attest again with the policy of the last `attest`, then hand the passphrase
    (typed, or piped on stdin) to the OperatorKey() volume.

Channel: unverified first contact: the channel is the bootstrap SSH session, not bound
to the quote (gate 3 adds the attested endpoint). Values travel on the SSH command's
stdin only: never on argv, in a log, a tree, a manifest or a receipt.

positional arguments:
  deliver|unlock
    deliver       Deliver NAME=@FILE credentials.
    unlock        Unlock an OperatorKey() volume.

options:
  -h, --help      show this help message and exit
  --traceback     Raise errors with the Python traceback.

$ tundravm secrets deliver --help
usage: tundravm secrets deliver [-h] [--traceback] --policy FILE [--identity KEY]
                                [--validator ENDPOINT]
                                INSTANCE NAME=@FILE [NAME=@FILE ...]

Deliver NAME=@FILE credentials. Channel: unverified first contact: the channel is the bootstrap
SSH session, not bound to the quote (gate 3 adds the attested endpoint).

positional arguments:
  INSTANCE
  NAME=@FILE            A secret and the file holding it.

options:
  -h, --help            show this help message and exit
  --traceback           Raise errors with the Python traceback.
  --policy FILE         The verifier policy.
  --identity KEY        The operator's SSH key.
  --validator ENDPOINT  A tdxs validator.

$ tundravm secrets unlock --help
usage: tundravm secrets unlock [-h] [--traceback] [--identity KEY] [--validator ENDPOINT]
                               INSTANCE VOLUME

Unlock an OperatorKey() volume. Channel: unverified first contact: the channel is the bootstrap
SSH session, not bound to the quote (gate 3 adds the attested endpoint).

positional arguments:
  INSTANCE
  VOLUME

options:
  -h, --help            show this help message and exit
  --traceback           Raise errors with the Python traceback.
  --identity KEY        The operator's SSH key.
  --validator ENDPOINT  A tdxs validator.

$ tundravm verify --help
usage: tundravm verify [-h] [--json] [--traceback] [--require-signature] [--allowed-signers FILE]
                       [--rebuild] [--attr NAME] [--pythonpath DIR]
                       BUILD

Offline: the manifest is canonical and its id is its digest and the directory's
name; every artifact, input and the tree hash to what it records, and nothing else
is there; the UKI and its root hash are the recorded ones. Each check is ok, failed,
missing or unchecked. A signature is checked with --allowed-signers (an OpenSSH
allowed-signers file, namespace tundravm-manifest); --require-signature fails
without a good one. --rebuild builds the recorded inputs again in an independent
workspace and compares (it runs the project's code). Exit 1 when a check fails.

positional arguments:
  BUILD                 A build directory, an id prefix, or `latest`.

options:
  -h, --help            show this help message and exit
  --json                Print a JSON report.
  --traceback           Raise errors with the Python traceback.
  --require-signature   Fail without a valid signature.
  --allowed-signers FILE
                        Who may sign.
  --rebuild             Rebuild independently and compare.
  --attr NAME           --rebuild: the image's name in its file.
  --pythonpath DIR      --rebuild: extra import directory for the image file (repeatable).

$ tundravm convert --help
usage: tundravm convert [-h] [--traceback] --from {mkosi,systemd} [--allow-scripts]
                        [--mode {exact,semantic}]
                        PATH

--from systemd: unit files (a file, or a directory) as typed declarations; units no
typed shape renders stay text, with what blocked them. --from mkosi: the tree
resolved by mkosi itself, as an Image file; every setting it cannot carry is
reported. Parsing a tree runs its executable mkosi.version or mkosi.configure:
refused unless --allow-scripts.

positional arguments:
  PATH

options:
  -h, --help            show this help message and exit
  --traceback           Raise errors with the Python traceback.
  --from {mkosi,systemd}
                        What PATH is.
  --allow-scripts       Let mkosi run the tree's scripts while parsing.
  --mode {exact,semantic}
                        systemd: exact renders the file byte for byte (default); semantic the same
                        directives.

$ tundravm doctor --help
usage: tundravm doctor [-h] [--json] [--traceback] [--for {build,run,tdx,azure,gcp}]

One row per capability: ready, blocked (with the fix), unavailable or unsupported.
--for limits it to one: build, run (simulated), tdx, azure, gcp. Exit 1 when a
build row (or, with --for, a row of that area) is blocked.

options:
  -h, --help            show this help message and exit
  --json                Print a JSON report.
  --traceback           Raise errors with the Python traceback.
  --for {build,run,tdx,azure,gcp}
                        One capability.
```

## What each removed verb's guarantee moved to

| Removed | Now |
|---|---|
| `init` | `new NAME [--template service\|prover] [--ci]` |
| `inspect`, `explore`, `preview`, `explain` (`inspect --why`) | `show [IMAGE]` (text; `--json` with every profile's description and the provenance of each declaration: action, fragment chain, `file:line`), `--units`, `--tree` |
| `status` | `show BUILD` (manifest, provenance, unchecked guarantees), `run status`, `deploy status` |
| `diff`, `changes` | `check --tree DIR` (tree drift against what the image compiles to) and `show --diff OLD NEW` (packages, files, units, boot payload, measurements, policy coverage between two builds) |
| `lint`, `compile --check`, `ci` | `check [--profile P] [--tree DIR]`: declarations, every lint rule, lock drift, toolchain drift, tree drift |
| `compile` | `build --tree-only DIR` |
| `fetch` | `build` fetches into its workspace; `lock --update` re-resolves; `build --offline` builds only what the lock pins |
| `bake`, `bake --verify-reproducible` | `build`, `build --reproduce` (an independent workspace: its own sources, mkosi state and tree; the comparison in `observations.json`) |
| `measure`, `measure --export-policy` | `build` writes `measurements.json`: per target and boot path, the prediction, the unknown registers with their reason, RTMR3 outside the policy, and a `tundravm.policy/2` `policy` per record that `attest --policy` takes directly or as the operator's approved copy |
| `sbom` | `build` writes `sbom.spdx.json`, bound to the lock and mkosi's package manifest (`packages.json`) |
| `evidence`, `release`, `witness`, `verify` (bundles) | `manifest.json` (canonical bytes, artifact index, explicit `missing`/`unchecked`), `manifest.sig` (`build --sign KEY`), `verify BUILD [--require-signature] [--allowed-signers FILE] [--rebuild]` |
| `deploy qemu` | `run [BUILD] --mode simulated\|tdx`, `run status\|logs\|stop\|ssh\|mode` |
| `deploy azure\|gcp --param` | `deploy azure\|gcp --build BUILD [--volume ...]` with `[tool.tundravm.azure\|gcp]` or `TUNDRAVM_AZURE_*`/`TUNDRAVM_GCP_*`; `deploy status\|logs\|mode\|upgrade\|rollback` |
| `attest --issuer --policy --record` | `attest INSTANCE --policy FILE [--output REPORT]` (`--issuer` stays for an operator's own relay, `--validator` for a tdxs validator) |
| `clean` | `build --clean-cache` |
| `config` | the resolution rule in every `--help` epilog: argument, `TUNDRAVM_IMAGE`/`TUNDRAVM_BUILD`/`TUNDRAVM_BACKEND`, `[tool.tundravm]`, default |
| `completion` | `python -m tundravm.completion SHELL` |
| `units convert` | `convert PATH --from systemd [--mode exact\|semantic]` |
| `units render` | `show IMAGE --units` |
| `import` | `convert PATH --from mkosi [--allow-scripts]`, through the mkosi adapter (`_mkosi.summary`, i.e. `parse_config`) |

Deleted modules: `evidence.py`, `release.py`, `witness.py`, `verify.py`, `changes.py`
(their bundle schemas had no consumer; what still means something is in the manifest),
`explore.py`, `preview.py`, `watch.py`, `status.py`, `clean.py`. New: `builds.py`,
`manifest.py`, `instances.py`, `bootstrap.py`, `deployments.py`, `show.py`, `convert.py`.

## A build directory

`TUNDRAVM_BACKEND=local tundravm build tests/fixtures/native-tree/recipe.py --profile default
--reproduce --sign $KEYS/signing-key` (a real mkosi build, 3m03s with the second, independent
build):

```
lock written: $A/tundravm.lock (commit it)
built $A/fixture/production/c1745dfd651e15f3
  measurements.json    655a1a793fbe4ec2
  packages.json        ce7b4b051558880e
  payload.efi          4c69cd5caeec3b27
  qemu.raw             c8349a43fa442481
  sbom.spdx.json       98aad9471a58ed95
  unchecked: RTMR3: outside the policy: no event schema is defined for RTMR3
reproduction: matched
```

The directory (`latest-production -> production/c1745dfd651e15f3`), every file read-only, the
tree's 33 files under `tree/` left out of the listing:

```
444          17  production/c1745dfd651e15f3/inputs/mkosi-argv.json
444          20  production/c1745dfd651e15f3/inputs/sources.json
444         314  production/c1745dfd651e15f3/manifest.sig
444         482  production/c1745dfd651e15f3/inputs/toolchain.json
444        1030  production/c1745dfd651e15f3/observations.json
444        3681  production/c1745dfd651e15f3/manifest.json
444        4766  production/c1745dfd651e15f3/measurements.json
444       16572  production/c1745dfd651e15f3/packages.json
444       20395  production/c1745dfd651e15f3/inputs/summary.json
444       79176  production/c1745dfd651e15f3/inputs/tundravm.lock
444       82528  production/c1745dfd651e15f3/sbom.spdx.json
444    68019200  production/c1745dfd651e15f3/payload.efi
444   135286784  production/c1745dfd651e15f3/qemu.raw
```

### manifest.json

The canonical bytes, as written and signed (`id` is the first 16 hex digits of the sha256 of
this document without `id`; `tree.digest` covers paths, modes without write bits, contents
and symlink targets; `lock.project_sha256` is the committed lock it was checked against,
`lock.sha256` the one it was built against, equal here because the build targets what the
image declares):

```json
{
  "artifacts": {
    "inputs/mkosi-argv.json": {
      "sha256": "e0719c17dc1acd70239ac9ef3706bcaf185a1173d89e59a20636135558c10903",
      "size": 17
    },
    "inputs/sources.json": {
      "sha256": "6c3b9f0390341dfd18635526c002291b374ba88078d4f87034c71c820e21474e",
      "size": 20
    },
    "inputs/summary.json": {
      "sha256": "278d196d2aa0e61c937c71c01676bf6e4049bc27255000043d016c34f9f6a692",
      "size": 20395
    },
    "inputs/toolchain.json": {
      "sha256": "1cbea4c303907cb03fd6dd806485bcb58c8ab9bf0d9446387fabf5e3f1e858ca",
      "size": 482
    },
    "inputs/tundravm.lock": {
      "sha256": "0edc13a01192d932b05d8ed18bee739a4ab8ef15aa035b2f63a1748b33f31183",
      "size": 79176
    },
    "measurements.json": {
      "sha256": "655a1a793fbe4ec2ae1d5961626e82a4128deee664a8bf7877123eb1c98f2278",
      "size": 4766
    },
    "packages.json": {
      "sha256": "ce7b4b051558880ea661054c120d6434c613d2e679095d353181985a3c05b62d",
      "size": 16572
    },
    "payload.efi": {
      "sha256": "4c69cd5caeec3b276d3f72c4993f3a3ade70f2ae0759666cdfb8b278a64bf1c5",
      "size": 68019200
    },
    "qemu.raw": {
      "sha256": "c8349a43fa4424818c0bd2cddade0a90db3e0f4b33491be272e861e42e36c61a",
      "size": 135286784
    },
    "sbom.spdx.json": {
      "sha256": "98aad9471a58ed958fd72ae9ee9b6652b2f7dc6339fb346ca09039c80e5deae6",
      "size": 82528
    }
  },
  "id": "c1745dfd651e15f3",
  "image": "fixture",
  "kind": "production",
  "lock": {
    "path": "inputs/tundravm.lock",
    "project_sha256": "0edc13a01192d932b05d8ed18bee739a4ab8ef15aa035b2f63a1748b33f31183",
    "recipe_digest": "079f2d0c5d2dec8116b8d2d00dd737f02268ffb374f0a499b7e438a05bfc00c9",
    "sha256": "0edc13a01192d932b05d8ed18bee739a4ab8ef15aa035b2f63a1748b33f31183"
  },
  "measurements": {
    "path": "measurements.json",
    "records": {
      "qemu/disk": "predicted",
      "qemu/uki": "predicted"
    },
    "sha256": "655a1a793fbe4ec2ae1d5961626e82a4128deee664a8bf7877123eb1c98f2278"
  },
  "packages": {
    "count": 113,
    "path": "packages.json",
    "sha256": "ce7b4b051558880ea661054c120d6434c613d2e679095d353181985a3c05b62d"
  },
  "ports": [
    8080
  ],
  "profile": "default",
  "recipe_digest": "079f2d0c5d2dec8116b8d2d00dd737f02268ffb374f0a499b7e438a05bfc00c9",
  "root_hash": null,
  "rootfs": "initramfs",
  "sbom": {
    "bound": true,
    "notes": [],
    "path": "sbom.spdx.json",
    "sha256": "98aad9471a58ed958fd72ae9ee9b6652b2f7dc6339fb346ca09039c80e5deae6"
  },
  "schema": "tundravm.manifest/1",
  "simulated": false,
  "sources": [],
  "summary": {
    "path": "inputs/summary.json",
    "sha256": "278d196d2aa0e61c937c71c01676bf6e4049bc27255000043d016c34f9f6a692"
  },
  "targets": [
    "qemu"
  ],
  "toolchain": {
    "backend": "local_linux",
    "locked": {
      "measure": {
        "gcp-firmware-table": "dstack-mr-gcp@b16e08b",
        "tundravm-measure": "1.0+attest-12f1e29.dstack-mr-gcp-b16e08b"
      },
      "mkosi": {
        "rev": "ecf7f5702bb2052b7a69cab3fe7d37bc3b9ad77b",
        "version": "27.1"
      },
      "tools_tree": {
        "distribution": "debian",
        "release": "trixie",
        "snapshot": "20251113T083151Z"
      },
      "tundravm": "0.1.0+d7e34d248eb3"
    },
    "tundravm": "0.1.0"
  },
  "tree": {
    "digest": "sha256:dfb7ef9fd1f0608698ac70e542dfd5e1985191894125bbc7e19b083f7d21f496",
    "files": 33,
    "path": "tree"
  },
  "uki": {
    "cmdline": "",
    "path": "payload.efi",
    "sha256": "4c69cd5caeec3b276d3f72c4993f3a3ade70f2ae0759666cdfb8b278a64bf1c5",
    "size": 68019200
  },
  "unchecked": [
    "RTMR3: outside the policy: no event schema is defined for RTMR3"
  ]
}
```

### observations.json

Not identity: when, where, and how the reproduction went (`workspace: .reproduce` is kept
because a sudo build leaves root-owned files in it; the next `--reproduce` names the
`sudo rm -rf` that removes it):

```json
{
  "duration_s": 183.406,
  "finished_at": "2026-10-31T00:48:23+00:00",
  "id": "c1745dfd651e15f3",
  "lock_written": true,
  "reproduction": {
    "artifacts": {
      "payload.efi": {
        "first": "4c69cd5caeec3b276d3f72c4993f3a3ade70f2ae0759666cdfb8b278a64bf1c5",
        "second": "4c69cd5caeec3b276d3f72c4993f3a3ade70f2ae0759666cdfb8b278a64bf1c5"
      },
      "qemu.raw": {
        "first": "c8349a43fa4424818c0bd2cddade0a90db3e0f4b33491be272e861e42e36c61a",
        "second": "c8349a43fa4424818c0bd2cddade0a90db3e0f4b33491be272e861e42e36c61a"
      }
    },
    "differ": [],
    "finished_at": "2026-10-31T00:48:21+00:00",
    "started_at": "2026-10-31T00:46:51+00:00",
    "state": "matched",
    "workspace": ".reproduce"
  },
  "runner": {
    "backend": "local_linux",
    "ci": null,
    "hostname": "hetzner",
    "platform": "Linux-6.8.0-117-generic-x86_64-with-glibc2.39",
    "python": "3.12.3",
    "user": "franco"
  },
  "schema": "tundravm.observations/1",
  "started_at": "2026-10-31T00:45:19+00:00"
}
```

### measurements.json

Two records for the QEMU target, one per boot path (`uki`: `payload.efi` through
`-kernel`; `disk`: `qemu.raw` through its ESP, which is how `run` boots it). The disk record
without its `policy` (whose keys are `artifact`, `boot_payload`, `registers`, `schema`, `schema_version`, `scheme`, `tool`, `tool_version`, `unverifiable`):

```json
{
  "artifact": "qemu.raw",
  "artifact_sha256": "c8349a43fa4424818c0bd2cddade0a90db3e0f4b33491be272e861e42e36c61a",
  "boot": "disk",
  "registers": {
    "RTMR1": [
      "5ed69d287b77be9cb228709328c8fcade6cfdfd88c284ce52b7b05fe4bc8885234b7cd5709415b0617cfedb2fd0ed791"
    ],
    "RTMR2": [
      "a91da4762d3927eb630e7fcf52d44bc212da9c7a03324e7120d57e5f14d325bd09b1ace9d4da3ffc565afa7b7754c187"
    ]
  },
  "rtmr3": "outside the policy: no event schema is defined for RTMR3",
  "scheme": "qemu",
  "state": "predicted",
  "target": "qemu",
  "tool": "tundravm-measure 1.0+attest-12f1e29.dstack-mr-gcp-b16e08b",
  "uki": {
    "path": "payload.efi",
    "sha256": "4c69cd5caeec3b276d3f72c4993f3a3ade70f2ae0759666cdfb8b278a64bf1c5"
  },
  "unknowns": {
    "MRTD": "QEMU: depends on the host's TDVF/OVMF build, its ACPI tables and the VM's memory layout; take it from a quote of a host you trust",
    "RTMR0": "QEMU: depends on the host's TDVF/OVMF build, its ACPI tables and the VM's memory layout; take it from a quote of a host you trust"
  }
}
```

## A signed manifest verified from a second checkout

The build directory copied (`cp -a`) to `$B`, the branch cloned afresh into `$CHECKOUT2` with
its own `uv sync`, and `verify` run from there against an allowed-signers file holding the
signing key's public half; then against another key's, then with one byte of `qemu.raw`
changed:

```
$ git -C checkout2 log --oneline -1
adcf166 ci: build, verify and run the fixture with the new commands
$ cat allowed_signers
gate5@tundravm namespaces="tundravm-manifest" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPZmJc9Wgu8VkivDOzZsHFq1O1j4sMMzclxbca0MAjyo
$ tundravm verify elsewhere/c1745dfd651e15f3 --allowed-signers allowed_signers --require-signature
build c1745dfd651e15f3 ($B/c1745dfd651e15f3)
  manifest      ok  tundravm.manifest/1
  id            ok  c1745dfd651e15f3
  artifacts     ok  10 files match
  tree          ok  sha256:dfb7ef9fd1f0
  uki           ok  4c69cd5caeec
  lock          ok  inputs/tundravm.lock
  sbom          ok  sbom.spdx.json
  packages      ok  packages.json
  summary       ok  inputs/summary.json
  measurements  ok  qemu/disk, qemu/uki
  reproduction  ok  an independent build matched every artifact
  signature     ok  signed by gate5@tundravm
  unchecked: RTMR3: outside the policy: no event schema is defined for RTMR3
verdict: pass
exit=0
$ tundravm verify elsewhere/c1745dfd651e15f3 --allowed-signers other_signers --require-signature
  signature     failed  no principal in $KEYS/other_signers holds the signing key
  unchecked: RTMR3: outside the policy: no event schema is defined for RTMR3
verdict: fail
exit=1
$ (one byte of qemu.raw changed) tundravm verify elsewhere/c1745dfd651e15f3 --allowed-signers allowed_signers --require-signature
  artifacts     failed  qemu.raw differs (sha256 3155fdb1ee93)
  signature     ok  signed by gate5@tundravm
verdict: fail
exit=1
```

`tests/cli/test_manifest_verify.py` asserts the same round trip (and a copy verified
elsewhere) with a key `ssh-keygen` makes, and each tampering: a changed artifact, an
unexpected file, an edited tree file, a changed tree mode, a replaced symlink, an edited or
non-canonical manifest, a renamed directory.

## Instances and deployments

Unit-tested with a fake QEMU, a fake SSH and issuer, and a fake `gcloud`/`az`; none of it
ran against a real VM here (no QEMU on this host, no cloud credentials):

- `run --mode simulated` writes `.tundravm/instances/<image>-<n>/instance.json`
  (`tundravm.instance/1`: the build's path, id and kind, the mode, the QEMU argv with
  `snapshot=on` on the build's own `qemu.raw`, the forwarded ports, the volume disks),
  `events.jsonl` and `console.log`; nothing is written into the build directory
  (`tests/cli/test_run.py`). `--mode tdx` is refused unless `doctor --for tdx` is ready, and
  boots TDVF with a `tdx-guest` object and no SMBIOS console words.
- `attest` POSTs the operator's public key to `:8745` once, relays tdxs over the bootstrap SSH
  session, checks the quote against a `tundravm.policy/2` file or the build's
  `measurements.json`, records the verdict, delivers nothing, and prints `unverified first
  contact`; `secrets deliver` attests again and needs `trusted` for a production build,
  `simulated` for a simulated instance; values travel on the SSH command's stdin only, never
  in argv or a receipt; `secrets unlock` re-attests with the last approved policy before the
  passphrase is even asked for (`tests/cli/test_secrets.py`).
- `deploy gcp --build BUILD --volume data=create:100GiB` attaches the disk as device
  `tundravm-data` (what the image's `/dev/disk/by-id/*-tundravm-data` matches) and records the
  VM, its address and the firewall rule the management port needs; Azure with a volume is
  refused (its data disks are named by LUN). `deploy upgrade` runs plan 6.8's order on GCP and
  refuses Azure and a change of volumes; a candidate that fails attestation leaves the old
  instance running with its volumes (`tests/cli/test_deploy.py`, `test_upgrade.py`).

## What CI must prove

`.github/workflows/ci.yml` now calls only the new commands:

- `test`: `tundravm check examples/surge-tdx-prover/image.py --tree
  examples/surge-tdx-prover/mkosi` (what `lint` and `compile --check` did).
- `fixture-boot`: a real `build` of the fixture into a build directory, `verify latest`,
  `show latest --measurements`, the harness's two boots from that directory, then `run
  latest --mode simulated --detach` on the runner (KVM when it exposes `/dev/kvm`, TCG
  otherwise) until the host fetches the fixture's page through the forwarded port the
  receipt names, `run status`, `run mode`, and `run stop`. That run is the first boot of an
  instance through `tundravm run`; it has not happened on this host.
- `verity`, `verity-cloud`: `build --attr verity_recipe --profile ...` and `verify latest`;
  the integration tests read the flat build directories (`payload.root.raw` and
  `payload.root-verity.raw` are part of them for the verity layout).
- `reproduce`: `build --profile azure --reproduce` and `--profile gcp --reproduce` (exit 1 on
  any difference), `verify latest`, the manifests and observations uploaded.

Not proven anywhere yet: a `deploy` to GCP or Azure, an `attest` against a real quote, a
delivery or unlock on a real instance (gate 3's attested endpoint, a TDX host and cloud
credentials).
