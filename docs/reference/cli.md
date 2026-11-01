# Command reference

Generated from the `tundravm` argument parser by `uv run python -m tundravm._reference docs/reference` (Python 3.12). Do not edit it by hand: `tests/docs/test_reference.py` fails when it is stale.

| Command | Does |
|---|---|
| [`new`](#tundravm-new) | Create a project: an image file, its tests, pyproject.toml. |
| [`check`](#tundravm-check) | Check the image: declarations, lint, lock drift, tree drift. |
| [`lock`](#tundravm-lock) | Resolve or check the lock: snapshot, sources, toolchain. |
| [`build`](#tundravm-build) | Build into an immutable build directory, with its manifest. |
| [`show`](#tundravm-show) | Show the resolved image or a build; --diff two builds. |
| [`run`](#tundravm-run) | Run a build locally as a named instance; status\|logs\|stop\|ssh\|mode. |
| [`deploy`](#tundravm-deploy) | Deploy a build to Azure or GCP; status\|logs\|mode\|upgrade\|rollback. |
| [`attest`](#tundravm-attest) | Verify an instance's evidence against a policy; delivers nothing. |
| [`secrets`](#tundravm-secrets) | Deliver credentials or unlock a volume after a fresh attestation. |
| [`verify`](#tundravm-verify) | Check a build directory against its manifest. |
| [`convert`](#tundravm-convert) | Turn an mkosi tree or systemd unit files into source. |
| [`doctor`](#tundravm-doctor) | Report what this host can build, run, deploy and verify. |

## tundravm

```text
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

IMAGE defaults to $TUNDRAVM_IMAGE, else [tool.tundravm] image in the nearest pyproject.toml; the
build root to $TUNDRAVM_BUILD, else its `build` key (default: build); the backend to
$TUNDRAVM_BACKEND, else its `backend` key (default: local on Linux x86-64, lima elsewhere).
```

## tundravm new

Create a project: an image file, its tests, pyproject.toml.

```text
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
```

## tundravm check

Check the image: declarations, lint, lock drift, tree drift.

```text
usage: tundravm check [-h] [--attr NAME] [--pythonpath DIR] [--json] [--traceback] [--profile P]
                      [--tree DIR] [--explain CODE [CODE ...]]
                      [IMAGE]

Everything short of a build: resolution and declaration errors, every lint rule,
drift from BUILD/tundravm.lock when it exists, the toolchain against the lock's,
and with --tree a committed mkosi tree against what the image compiles to.
Exit 1 on an error (or a drifted lock or tree); warnings are reported only.
In GitHub Actions the findings are workflow annotations. --explain CODE says what
a finding's code checks and how to fix it (docs/reference/lint.md lists them all).

positional arguments:
  IMAGE                 The image file (default: $TUNDRAVM_IMAGE, else [tool.tundravm] image).

options:
  -h, --help            show this help message and exit
  --json                Print a JSON report.
  --traceback           Raise errors with the Python traceback.
  --profile P           Check this profile (repeatable; default: every profile).
  --tree DIR            A committed tree to compare.
  --explain CODE [CODE ...]
                        Print what each finding code checks and how to fix it; checks nothing.

loading the image file:
  --attr NAME           The name the image (or a zero-argument factory) is bound to in the file.
  --pythonpath DIR      Extra import directory for the file (repeatable); the working directory is
                        one.
```

## tundravm lock

Resolve or check the lock: snapshot, sources, toolchain.

```text
usage: tundravm lock [-h] [--attr NAME] [--pythonpath DIR] [--json] [--traceback]
                     [--update [INPUT ...] | --check]
                     [IMAGE]

Write BUILD/tundravm.lock: the image's section digests, a pin per source, the
toolchain. Pins already locked are kept while their source is unchanged; --update
resolves the named inputs again (every one when none is named; `toolchain` records
the running toolchain). --check never writes and never touches the network: it
prints the drift and exits 1 when the lock is stale. A build never changes the lock.

positional arguments:
  IMAGE                 The image file (default: $TUNDRAVM_IMAGE, else [tool.tundravm] image).

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
```

## tundravm build

Build into an immutable build directory, with its manifest.

```text
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
  IMAGE             The image file (default: $TUNDRAVM_IMAGE, else [tool.tundravm] image).

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
```

## tundravm show

Show the resolved image or a build; --diff two builds.

```text
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
  --profile P     Profiles of an image (repeatable; default: all).
  --dev           The latest development build (the image's dev profile is --profile dev).
  --attr NAME     The image's name in its file.
```

## tundravm run

Run a build locally as a named instance; status|logs|stop|ssh|mode.

```text
usage: tundravm run [-h] [--json] [--traceback] [--mode {simulated,tdx}] [--dev] [--detach]
                    [--memory MEMORY] [--cpus CPUS] [--identity KEY]
                    [BUILD|ACTION] [INSTANCE]

tundravm run [BUILD] --mode simulated|tdx [--dev] [--detach]
    boot qemu.raw through OVMF from its ESP on a snapshot (the build is never
    written), forward every TCP port the image's firewall accepts or its services
    declare, and the management port :8745, from 127.0.0.1, and write
    .tundravm/instances/<image>-<n>/.
    simulated: no TDX (KVM if usable, else TCG), verdicts are `simulated`; a
      production build boots fail-closed (tdx-evidence.service refuses, no service
      starts, nothing is delivered), a --dev build runs its services.
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
```

## tundravm deploy

Deploy a build to Azure or GCP; status|logs|mode|upgrade|rollback.

```text
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
```

## tundravm attest

Verify an instance's evidence against a policy; delivers nothing.

```text
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
```

## tundravm secrets

Deliver credentials or unlock a volume after a fresh attestation.

```text
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
```

### tundravm secrets deliver

Deliver NAME=@FILE credentials.

```text
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
```

### tundravm secrets unlock

Unlock an OperatorKey() volume.

```text
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
```

## tundravm verify

Check a build directory against its manifest.

```text
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
```

## tundravm convert

Turn an mkosi tree or systemd unit files into source.

```text
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
```

## tundravm doctor

Report what this host can build, run, deploy and verify.

```text
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

## Shell completion

There is no `completion` command: `python -m tundravm.completion SHELL` prints a script generated from this parser.

| Shell | Install |
|---|---|
| bash | add `source <(python -m tundravm.completion bash)` to ~/.bashrc, or save it as ~/.local/share/bash-completion/completions/tundravm |
| zsh | add `source <(python -m tundravm.completion zsh)` to ~/.zshrc after compinit, or save the output as _tundravm in a directory on $fpath |
| fish | save the output as ~/.config/fish/completions/tundravm.fish |
