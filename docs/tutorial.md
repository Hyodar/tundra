# Tutorial

This walk-through starts from an empty directory and follows the project `tundravm init` writes: lock it, inspect the starter recipe and ask where one of its objects comes from, lint it (and see what happens without a kernel), add a variant, lock again and compile, change the recipe and see the diff and the lock drift, bake, prove the bake reproducible, measure and export a verifier policy, list what is in the image, deploy, then write a fragment of your own, test it next to the generated tests and run the CI gate. Everything runs on the in-process backend, which needs no build tools and writes simulated artifacts. Every transcript below is real output; exit codes are shown as `[exit N]`. The one command that needs a running TDX VM, attesting it, is shown but not run.

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), and the `tundravm` command (`uv sync` in this repository; tundravm is not on PyPI yet). Step 14 runs the project's tests with `uv run`, which installs tundravm into the project's own environment, so run `uv add --editable PATH/TO/tundravm` in the project first, as the last line of `init`'s next steps says.

## 1. Start a project

```console
$ mkdir node && cd node
$ tundravm init . --backend inprocess
created node.py
created tests/test_node.py
created pyproject.toml
created README.md
created .gitignore
lint node.py: no findings
next:
  1. tundravm lock node.py                 record recipe sections and pin source repositories/downloads in build/tundravm.lock
  2. tundravm compile node.py --out mkosi  write the mkosi tree; commit it
  3. uv run pytest tests                   run test_node.py against mkosi/
  4. tundravm ci node.py --out mkosi       the lint, tree and lockfile checks CI runs
  5. tundravm bake node.py --out build     build the image
  tundravm is not on PyPI yet: `uv add --editable PATH/TO/tundravm` uses a local checkout

checking the inprocess backend (tundravm doctor --backend inprocess):
tundravm 0.1.0
python 3.12.3
backend inprocess: available
  no external tools required
measurement tools:
  missing (optional) measured-boot — Real RTMR measurements need it. Install measured-boot or dstack-mr and make sure it is on PATH.
  missing (optional) dstack-mr — Real RTMR measurements need it. Install measured-boot or dstack-mr and make sure it is on PATH.
[exit 0]
```

The recipe is named after the directory (`--name` overrides it). `init` writes the `service` starter (`--template` picks another, `tundravm init --list-templates` lists them), a tests module for it, and `pyproject.toml` and `README.md` when they are absent. It lints the recipe, prints the next steps, and ends with `tundravm doctor --backend inprocess`, which needs no tools; the measurement tools are optional (see [Measure](#9-measure)). `--no-doctor` skips the probe. For tab completion of verbs and flags, see [CLI: Shell completion](cli.md#shell-completion).

`node.py` binds a `Recipe` to `recipe` and a backend to `backend`. Below its docstring and imports:

```python
APP_VERSION = "0.1.0"
APP_PORT = 8080


@dataclass(frozen=True, slots=True, kw_only=True)
class App(Composite):
    """The app: its account, config file, a first-boot step, the service and a lint rule.

    Ship ``/usr/bin/app`` with a ``Build(...)`` or a ``Package(...)``. Fragment name: ``app``.
    """

    version: str
    port: int

    def compose(self) -> Fragment:
        config = f"version={self.version}\nlisten=0.0.0.0:{self.port}\n"
        return Fragment(
            "app",
            items=(
                User("app", home="/var/lib/app"),
                File("/etc/app/app.conf", config),
                # Runs in runtime-init before the service starts (after_init=True).
                Init("app-state", "mkdir -p /var/lib/app && chown app:app /var/lib/app"),
                Service(
                    "app",
                    "/usr/bin/app --config /etc/app/app.conf",
                    description=f"app {self.version}",
                    user="app",
                    working_dir="/var/lib/app",
                    restart="on-failure",
                    wanted_by="minimal.target",
                ),
            ),
            checks=(self.check_port,),
        )

    def check_port(self, resolved: Resolved) -> tuple[Diagnostic, ...]:
        """Lint rule: the app runs as a regular user, so it cannot bind a port below 1024."""
        if self.port >= 1024:
            return ()
        message = f"app runs as user app and cannot bind port {self.port}"
        return (Diagnostic("app-privileged-port", message, variant=resolved.variant),)


backend = InProcessBackend()

recipe = Recipe(
    name="node",
    base="debian/trixie",
    common=Fragment(
        "base",
        items=(
            # Boot: the distribution kernel, systemd as init, udev and the UKI's EFI stub.
            Package("linux-image-amd64"),
            Package("systemd"),
            Package("systemd-sysv"),
            Package("udev"),
            Package("kmod"),
            Package("systemd-boot-efi"),
            Package("ca-certificates"),
            App(version=APP_VERSION, port=APP_PORT),
        ),
    ),
    variants=(
        Variant("default", target="qemu"),
        Variant("dev", parent="default", add=DevTools()),  # never ship it
    ),
)
```

- `common` holds what every variant shares: the packages that make the image bootable and the `App` fragment.
- `App` is a `Composite`, a `Fragment` whose configuration is its own dataclass fields. `compose()` returns its contents: an account, a config file, a first-boot `Init` step and a generated systemd `Service`. Its `checks` run during `lint`: `App(port=80)` reports `app-privileged-port`.
- The service runs `/usr/bin/app`, which the recipe does not ship yet. Add a `Build(...)` or a `Package(...)` for it before you bake a real image.
- `dev` inherits `default` and adds `DevTools()`: a serial console, root login and debugging tools. Never ship it.

The `.gitignore` block ignores `build/` but keeps `build/tundravm.lock`, which you commit.

`pyproject.toml` ends with the project's `[tool.tundravm]` table:

```toml
[tool.tundravm]
recipe = "node.py"
out = "build"
tree = "mkosi"
lockfile = "build/tundravm.lock"
backend = "inprocess"
```

Every command looks for it in the working directory and its parents, so in this project `node.py` may be left out: `tundravm lint` lints `node.py`, `tundravm compile` writes `mkosi/`, and `tundravm status` reports on the whole project. The steps below spell `node.py` and `--out mkosi` out so that each command reads on its own; a flag always wins over the table. `tundravm config` prints what applies:

```console
$ tundravm config
pyproject: /home/me/node/pyproject.toml
  recipe    node.py              pyproject
  out       build                pyproject
  tree      mkosi                pyproject
  lockfile  build/tundravm.lock  pyproject; does not exist; run `tundravm lock`
  backend   inprocess            pyproject
[exit 0]
```

The lockfile the table configures does not exist yet. Until `tundravm lock` writes it, `inspect`, `lint`, `compile`, `diff` and `fetch` print `` note: configured lockfile build/tundravm.lock does not exist; run `tundravm lock` to create it `` and run without pins, and `bake` refuses with `E_LOCKFILE` instead of baking unfrozen. Lock first:

```console
$ tundravm lock node.py
locked build/tundravm.lock
[exit 0]
```

`tundravm config` now prints `lockfile  build/tundravm.lock  pyproject`. Every later change to the recipe drifts from this lock until you lock again, which steps 5, 6 and 13 do.

## 2. Inspect

`inspect` is a dry run: what each variant will contain, without writing anything.

```console
$ tundravm inspect node.py --variant dev
Image: debian/trixie (x86_64)  variant=dev  reproducible=yes
Policy: mutable_ref_policy=warn network_mode=online require_frozen_lock=no storage_safety=warn
Parent: default
Fragments (3): base app devtools
Packages (20): apt bash-completion ca-certificates curl dnsutils iputils-ping kmod linux-image-amd64 net-tools netcat-openbsd openssh-server socat strace systemd systemd-boot-efi systemd-sysv tcpdump tcpflow udev vim
Files (2):
  /etc/app/app.conf                               0644  34B  sha256:681b227aa566
  /usr/lib/systemd/system/serial-console.service  0644  263B  sha256:016dd361c717
Users (1):
  app  system  home=/var/lib/app  shell=/usr/sbin/nologin
Services (1):
  app  /usr/bin/app --config /etc/app/app.conf  restart=on-failure  user=app  cwd=/var/lib/app  wanted_by=minimal.target
Hooks:
  postinst (2):
    mkosi-chroot systemctl enable serial-console.service
    ROOT_PASS=$(openssl passwd -6 "tdx")
  finalize (1):
    sed -i '/^IMAGE_VERSION=/d' "$BUILDROOT/usr/lib/os-release"
Runtime init: 1 (priorities: 100)
Debloat: enabled, 30 paths removed, systemd minimize=yes
Targets: qemu
[exit 0]
```

`Parent:` is the variant `dev` builds on and `Fragments:` the fragments it includes: `base` (the `common` fragment), `app` from `App`, and `devtools` from its own `add`. Without `--variant` every variant is shown. `--format json` adds the recipe digest the lockfile records; `--format markdown` renders tables for a CI job summary.

To ask where one object comes from, name it with `--why`: an image path, or `unit:`, `package:`, `hook:` or `init:` and a name.

```console
$ tundravm inspect node.py --variant dev --why unit:app
why unit:app (variant dev)
  Service(app.service)
    declared in common via base > app (App)
files (in dev/):
  mkosi.extra/usr/lib/systemd/system/app.service
generated:
  After=runtime-init.service (after_init=True)
  Requires=runtime-init.service (after_init=True)
  scripts/06-postinst.sh: mkosi-chroot systemctl enable app.service
  scripts/06-postinst.sh: minimal.target.wants/app.service link
[exit 0]
```

The service is declared in `common`, inside the `app` fragment that the `App` composite returns, nested in `base`. Its unit file lands in `dev/mkosi.extra/`, and the compiler added two things of its own: the ordering after `runtime-init.service` (the variant has a runtime-init step, `App`'s `Init`) and the enablement into `minimal.target`. A declaration a variant adds, replaces or removes lists that step too. `--why` compiles the variant into a scratch directory and writes nothing (see [CLI: Explaining one object](cli.md#explaining-one-object)).

## 3. Lint

```console
$ tundravm lint node.py
no findings
[exit 0]
```

Every variant here bakes a bootable UKI, and a UKI needs a kernel. Comment out `Package("linux-image-amd64")` in `common` and lint again:

```console
$ tundravm lint node.py
error kernel-missing [default]: variant builds a bootable UKI but installs no kernel, so mkosi stops with 'A kernel must be installed in the image to build a UKI'
    hint: Add Package("linux-image-amd64") for the distribution kernel, declare Kernel(...) to build one, or Setting("Content", "Bootable", ("no",)) for a non-bootable image.
error kernel-missing [dev]: variant builds a bootable UKI but installs no kernel, so mkosi stops with 'A kernel must be installed in the image to build a UKI'
    hint: Add Package("linux-image-amd64") for the distribution kernel, declare Kernel(...) to build one, or Setting("Content", "Bootable", ("no",)) for a non-bootable image.
2 errors, 0 warnings, 0 infos
[exit 1]

$ tundravm bake node.py
[tundravm] lint ...
[default] error kernel-missing: variant builds a bootable UKI but installs no kernel, so mkosi stops with 'A kernel must be installed in the image to build a UKI'
[dev] error kernel-missing: variant builds a bootable UKI but installs no kernel, so mkosi stops with 'A kernel must be installed in the image to build a UKI'
[tundravm] lint ... FAILED (0.0s)
[tundravm] failed after 0.0s
error [E_LINT]: Recipe has 2 error-level diagnostics.
Hint: Run `tundravm lint RECIPE` to see them.
  codes: kernel-missing, kernel-missing
[exit 2]
```

`bake` lints first, so the missing kernel stops it before mkosi starts instead of minutes into a real build; `ci` fails at its lint step too. `compile` and `inspect` still run. The hint lists the ways out: the distribution kernel package, a `Kernel(...)` built from source, or `Setting("Content", "Bootable", ("no",))` for a disk that is never booted. Restore the package:

```console
$ tundravm lint node.py
no findings
[exit 0]
```

Warnings do not fail `lint` unless you pass `--strict`.

A credential baked into the image is refused the same way, since every copy of the disk would carry it. Generate a key with `ssh-keygen -t ed25519 -N "" -f deploy.key`, ship it with `File("/etc/app/deploy.key", Path("deploy.key").read_text())` in `common` (and `from pathlib import Path`), and lint:

```console
$ tundravm lint node.py
error secret-in-file [default] /etc/app/deploy.key: the file holds a private key (line 1); it is baked into the image
    hint: Deliver it at boot with Secrets(...) and a SecretFile or SecretEnv target instead of baking it into the image, where every copy and every registry holding it exposes it; if it is not a secret, pass allow_secret=True to the declaration.
error secret-in-file [dev] /etc/app/deploy.key: the file holds a private key (line 1); it is baked into the image
    hint: Deliver it at boot with Secrets(...) and a SecretFile or SecretEnv target instead of baking it into the image, where every copy and every registry holding it exposes it; if it is not a secret, pass allow_secret=True to the declaration.
2 errors, 0 warnings, 0 infos
[exit 1]
```

The message names the kind of credential and its line, never the value. A real key goes in `Secrets` and reaches the VM at boot; `allow_secret=True` on the `File` is for a value that only looks like one (see [Concepts: Secrets never belong in the image](concepts.md#secrets-never-belong-in-the-image)). Remove the `File`, the import and `deploy.key` before going on.

## 4. Add a variant

`dev` adds root login, which you never ship. For a variant that is the shipped image plus `strace`, add a third entry to `variants`:

```python
        Variant("debug", parent="default", add=Fragment("debug", items=(Package("strace"),))),
```

```console
$ tundravm inspect node.py --diff-variants default debug
variants default -> debug: 1 added, 0 removed, 0 changed
  + Package(strace, runtime)
[exit 0]
```

The generated `tests/test_node.py` names the variants it expects to compile. Add `"debug"` to the set in `test_compiles_every_variant`:

```python
    assert set(tree.variants) == {"default", "dev", "debug"}
```

## 5. Lock and compile

```console
$ tundravm lock node.py
locked build/tundravm.lock
[exit 0]

$ tundravm compile node.py --out mkosi
compiled mkosi
  variants:      default, dev, debug
  recipe_digest: a6dc0dc1f656aa95bef9677463fd16e0a47e324acd21ae260aa0bdf441608ef2
  tree_digest:   62258b694af6a4a8097e2ea85fca3eb21bc0f5229109e70dccc64c46795e6ce1
[exit 0]
```

The lock from step 1 has no `debug` variant: until this `lock`, `lint` reports each of its sections as a `lock-added` error (`variants.debug.packages is not in the lock`, ...). The lockfile (version 4) records a digest per recipe section (the distribution, the compiler, each variant's packages, files, kernel, debloat and the rest) and, for a recipe with source builds, the commit each one resolved to. `compile`, `diff` and `bake` read `build/tundravm.lock` when it exists, so lock before you compile: a recipe with source builds compiles its pins. `recipe_digest` is the digest the lockfile records and `tree_digest` the digest of the emitted tree. `mkosi/` holds one directory per variant (`mkosi.conf`, `mkosi.extra/`, `mkosi.skeleton/`, `scripts/`). Commit it: it is the reviewable form of the image.

## 6. Change the recipe

Set `APP_PORT = 9090`, then ask what changed:

```console
$ tundravm diff node.py --against mkosi --variant default
diff --git a/default/mkosi.extra/etc/app/app.conf b/default/mkosi.extra/etc/app/app.conf
--- a/default/mkosi.extra/etc/app/app.conf
+++ b/default/mkosi.extra/etc/app/app.conf
@@ -1,2 +1,2 @@
 version=0.1.0
-listen=0.0.0.0:8080
+listen=0.0.0.0:9090
[exit 1]

$ tundravm compile node.py --out mkosi --check
M  debug/mkosi.extra/etc/app/app.conf
M  default/mkosi.extra/etc/app/app.conf
M  dev/mkosi.extra/etc/app/app.conf
3 files changed
[exit 1]
```

`diff` and `compile --check` exit 1 when the tree is stale. That is the CI gate for a committed tree. The lockfile drifted too, section by section:

```console
$ tundravm lock node.py --check
~ variants.debug.files: ~/etc/app/app.conf
~ variants.default.files: ~/etc/app/app.conf
~ variants.dev.files: ~/etc/app/app.conf
[exit 1]

$ tundravm lock node.py --check --variant default
~ variants.default.files: ~/etc/app/app.conf
[exit 1]

$ tundravm lock node.py
locked build/tundravm.lock
[exit 0]

$ tundravm lock node.py --check
lock is up to date
[exit 0]

$ tundravm compile node.py --out mkosi
compiled mkosi
  variants:      default, dev, debug
  recipe_digest: 2f0041479c07b652bd4fad98f7e86c1547d4078945ac68948f19d704f31e4cd3
  tree_digest:   873688c6d6945f9349f34c551331afff8a627b58838fdc5e834358f4475b2efc
[exit 0]
```

Each line names a section, `variants.<variant>.<key>`, and what changed in it. `--variant` limits the check to those variants' sections (and the recipe-wide ones), so one lockfile of every variant serves any selection. Re-locking keeps existing source pins; `tundravm lock node.py --update NAME` resolves one source again.

## 7. Bake

```console
$ tundravm bake node.py
[tundravm] lint ...
[tundravm] lint ... ok (0.0s)
[tundravm] verify lockfile ...
[tundravm] verify lockfile ... ok (0.0s)
[tundravm] compile ...
[tundravm] compile ... ok (0.0s)
[debug] prepare inprocess ...
[debug] prepare inprocess ... ok (0.0s)
[debug] build via inprocess ...
[debug] build via inprocess ... ok (0.0s)
[debug] artifact qemu build/debug/disk.qcow2 (112 B)
[debug] report build/debug/report.json
[default] prepare inprocess ...
[default] prepare inprocess ... ok (0.0s)
[default] build via inprocess ...
[default] build via inprocess ... ok (0.0s)
[default] artifact qemu build/default/disk.qcow2 (114 B)
[default] report build/default/report.json
[dev] prepare inprocess ...
[dev] prepare inprocess ... ok (0.0s)
[dev] build via inprocess ...
[dev] build via inprocess ... ok (0.0s)
[dev] artifact qemu build/dev/disk.qcow2 (110 B)
[dev] report build/dev/report.json
[tundravm] baked 3 variants in 0.0s

variant  target  artifact                  size   sha256        time
debug    qemu    build/debug/disk.qcow2    112 B  b50a5a5aeaac  0.0s
default  qemu    build/default/disk.qcow2  114 B  1fa043adea90  0.0s
dev      qemu    build/dev/disk.qcow2      110 B  9cfb8b5c2f31  0.0s
next: tundravm deploy build/bake-result.json --variant debug --target qemu
[exit 0]
```

Progress lines go to stderr and the summary to stdout. The bake was frozen against `build/tundravm.lock`: had the recipe drifted from it, the `verify lockfile` step would have failed with `E_LOCKFILE`, and without the file, which the table configures, `bake` would have refused before `lint`, with `E_LOCKFILE` too. The backend came from `node.py`; `--backend lima|nix|local|inprocess` overrides it. The manifest `build/bake-result.json` records every artifact with its digest, the recipe digest and the tree digest.

## 8. Prove it is reproducible

```console
$ tundravm bake node.py --verify-reproducible -q
variant  target  artifact                  size   sha256        time
debug    qemu    build/debug/disk.qcow2    112 B  b50a5a5aeaac  0.0s
default  qemu    build/default/disk.qcow2  114 B  1fa043adea90  0.0s
dev      qemu    build/dev/disk.qcow2      110 B  9cfb8b5c2f31  0.0s
reproducible: yes (3 artifacts match a second build)
next: tundravm deploy build/bake-result.json --variant debug --target qemu
[exit 0]
```

`--verify-reproducible` bakes the same variants a second time into `build/.reproduce`, from the same lockfile and source checkouts, and compares every artifact's sha256 with the first build's (`-q` hides the progress of both bakes). All three match, so the second build is removed; `bake-result.json` records the outcome as `declarative.reproducible`, and `tundravm status` shows `reproducible` on each artifact line. A mismatch keeps `build/.reproduce` and fails with `E_REPRODUCIBILITY`, a table of the artifacts that differ and a hint naming `tundravm diff` and the usual causes (see [CLI: Bake](cli.md#bake)). The in-process backend is deterministic, so here the check only exercises the pipeline. With a real backend the second build runs mkosi again from scratch, which catches timestamps, build ids and packages installed without an archive snapshot (`Recipe(snapshot=...)`; see [Reproducibility](reproducibility.md)).

## 9. Measure

```console
$ tundravm measure build --variant default
error [E_MEASUREMENT]: Refusing to measure a simulated artifact.
Hint: Bake with a real backend, or pass allow_placeholder=True for test values.
  variant: default
  path: build/default/disk.qcow2
[exit 2]

$ tundravm measure build --variant default --allow-placeholder
PLACEHOLDER: not real measurements. These values are derived from artifact digests; never put them in an attestation policy.
measurements default (rtmr)
source: placeholder (build/default/disk.qcow2)
  RTMR0  7d4a783e463026f1bf368940a2756fecfa424b441f168a9ff7379e64694193e7
  RTMR1  dd1479258b13a05aae3225aafd02ed18734c750d8942fb0b90287461f5bcbbcd
  RTMR2  44d2ceafa105d213275b16a09db56cc14a2e5ca83a1233bbed1c806d31d53279
[exit 0]
```

`measure` takes the manifest (or the directory holding it) and first checks the artifact against the sha256 the bake recorded: a disk file changed since the bake is `E_ARTIFACT_CHANGED`. With a real bake and `measured-boot` or `dstack-mr` on `PATH`, the `source:` line names that tool instead of `placeholder`.

A verifier, an image that checks its peers' quotes, needs these values as its expected measurements. `--export-policy` writes them to a policy file:

```console
$ tundravm measure build --variant default --allow-placeholder --export-policy peer.json
PLACEHOLDER: not real measurements. These values are derived from artifact digests; never put them in an attestation policy.
wrote policy peer.json
measurements default (rtmr)
source: placeholder (build/default/disk.qcow2)
  RTMR0  7d4a783e463026f1bf368940a2756fecfa424b441f168a9ff7379e64694193e7
  RTMR1  dd1479258b13a05aae3225aafd02ed18734c750d8942fb0b90287461f5bcbbcd
  RTMR2  44d2ceafa105d213275b16a09db56cc14a2e5ca83a1233bbed1c806d31d53279
[exit 0]

$ cat peer.json
{
  "artifact": {
    "path": "build/default/disk.qcow2",
    "sha256": "1fa043adea909e9ad008e0db46ed1c9935adf90aee9c49d20940d6316c5c2e79"
  },
  "note": "PLACEHOLDER: these registers are derived from artifact digests, not measured; no TEE reproduces them. Replace this file with `tundravm measure --export-policy` output from a real bake before trusting a verifier built from it.",
  "registers": {
    "RTMR0": "7d4a783e463026f1bf368940a2756fecfa424b441f168a9ff7379e64694193e7",
    "RTMR1": "dd1479258b13a05aae3225aafd02ed18734c750d8942fb0b90287461f5bcbbcd",
    "RTMR2": "44d2ceafa105d213275b16a09db56cc14a2e5ca83a1233bbed1c806d31d53279"
  },
  "schema_version": 1,
  "scheme": "rtmr",
  "tool": "placeholder",
  "tool_version": null
}
```

Placeholder values are written only with `--allow-placeholder`, and then with the `note`. `Tdxs.from_policy()` turns the file into the verifier fragment, renaming the registers to the keys the tundra-tools validator reads:

```pycon
>>> from tundravm.declarative.utils import Tdxs
>>> verifier = Tdxs.from_policy("peer.json", validator="tdx", allow_placeholder=True)
>>> verifier.validator
'tdx'
>>> dict(verifier.expected_measurements)
{'rtmr0': '7d4a783e463026f1bf368940a2756fecfa424b441f168a9ff7379e64694193e7', 'rtmr1': 'dd1479258b13a05aae3225aafd02ed18734c750d8942fb0b90287461f5bcbbcd', 'rtmr2': '44d2ceafa105d213275b16a09db56cc14a2e5ca83a1233bbed1c806d31d53279'}
>>> Tdxs.from_policy("peer.json", validator="tdx")
Traceback (most recent call last):
  ...
tundravm.errors.MeasurementError: Refusing to build a verifier from a placeholder policy.
Hint: Export the policy from a real measurement, or pass allow_placeholder=True for a test image.
  policy: peer.json
```

A policy exported from a real measurement needs no `allow_placeholder`. MRTD is not in the policy: RTMR tools do not report it, and the validator checks only the registers it is given, so pass `mrtd="<hex>"` to have it checked too. The verifier is a fragment like any other, so it goes in a variant's `add` (`Variant("verifier", parent="default", add=verifier)`); `examples/06_attestation.py` builds one from `examples/peer.policy.json`. `"peer.json"` is relative to the working directory: in a recipe, anchor it to the recipe file with `Path(__file__).resolve().parent / "peer.json"` (see [Concepts: Lowering and compiling](concepts.md#lowering-and-compiling)).

Once the image runs, `tundravm attest` checks it against the policy: it asks the image's `tdxs` issuer for a quote bound to a fresh nonce and compares the quote's registers with the policy's. It refuses a placeholder policy before contacting anything:

```console
$ tundravm attest --endpoint unix:./tdxs.sock --policy peer.json
error [E_MEASUREMENT]: Refusing to attest against a placeholder policy.
Hint: Export the policy from a real measurement: `tundravm measure MANIFEST --export-policy FILE` with measured-boot or dstack-mr.
  policy: peer.json
[exit 2]
```

With a policy from a real measurement and the image running on a TDX host with the `tdxs` service (`Tdxs()` in the variant; the `prover` template has it), forward the service's socket over SSH and attest. This was not run here, since it needs a TDX VM:

```bash
ssh -p 2222 -N -L ./tdxs.sock:/var/tdxs.sock root@localhost &
tundravm attest --endpoint unix:./tdxs.sock --policy peer.json
```

It prints the nonce check and one `match`, `mismatch` or `unchecked` line per register (MRTD, RTMR0..RTMR3), then `verdict: trusted` (exit 0) or `verdict: untrusted` (exit 1); [CLI: Attest](cli.md#attest) shows the output. It checks measurements only: a verifier built with `Tdxs.from_policy()` also verifies the quote's signature and collateral.

## 10. What is in the image

```console
$ tundravm sbom build --variant default --format text
sbom default
  variant          default
  base             debian/trixie
  arch             x86_64
  snapshot         -
  mirror           -
  recipe digest    2f0041479c07b652bd4fad98f7e86c1547d4078945ac68948f19d704f31e4cd3
  tree digest      873688c6d6945f9349f34c551331afff8a627b58838fdc5e834358f4475b2efc
  artifact         build/default/disk.qcow2
  artifact sha256  1fa043adea909e9ad008e0db46ed1c9935adf90aee9c49d20940d6316c5c2e79
  manifest         build/default/default.manifest (missing)
  tundravm         0.1.0

packages (7)
  NAME               VERSION  ARCH   ORIGIN
  ca-certificates    -        amd64  declared
  kmod               -        amd64  declared
  linux-image-amd64  -        amd64  declared
  systemd            -        amd64  declared
  systemd-boot-efi   -        amd64  declared
  systemd-sysv       -        amd64  declared
  udev               -        amd64  declared

sources (0)
note: the artifact is simulated (in-process backend): nothing was installed
note: no mkosi package manifest at build/default/default.manifest: listing the packages the recipe declares, without versions
[exit 0]
```

`sbom` merges three records of one baked variant: mkosi's package manifest beside the artifact, the lockfile's source pins, and the recipe metadata and digests from `bake-result.json`. The in-process bake installed nothing, so there is no manifest: the notes say so, and the table lists the packages the recipe declares, without versions. After a real bake the table lists every installed package with the version and architecture from mkosi's manifest, `sources` lists each source build, built kernel and `EfiStub` package with its pin, and `snapshot` names the archive snapshot when the recipe pins one (the starter does not). See [CLI: SBOM](cli.md#sbom).

The default format is SPDX 2.3 JSON, and `--output` writes it to a file to publish next to `peer.json`:

```console
$ tundravm sbom build --variant default --output build/default.spdx.json
note: the artifact is simulated (in-process backend): nothing was installed
note: no mkosi package manifest at build/default/default.manifest: listing the packages the recipe declares, without versions
wrote spdx-json build/default.spdx.json
[exit 0]
```

`--format cyclonedx-json` writes CycloneDX 1.5 instead. Every list is sorted and the ids derive from the content, so with `SOURCE_DATE_EPOCH` set the same bake gives the same document.

## 11. Package the evidence

The lockfile, step 8's reproducibility check, `peer.json` and the SBOM each answer one question about the bake. `evidence` gathers them into one record that someone without your machine can check:

```console
$ SOURCE_DATE_EPOCH=1700000000 tundravm evidence node.py --variant default --policy peer.json --bundle evidence.tar.gz --html report.html
wrote build/evidence
wrote evidence.tar.gz
wrote report.html
evidence 2023-11-14T22:13:20Z  verdict pass
  recipe     node.py  digest 2f0041479c07
  integrity  verified
  lock       current
  reproduce  reproducible
  lint       clean
  default/qemu  build/default/disk.qcow2  sha256 1fa043adea90  verified
members:
  3b2f3058f0f74fa6361994d400c8a76492f1aec1f93a73c98d1f78c97798b8c1  bake-result.json
  37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570  lint.json
  259d38ee530784c61ae5514f21c8dd11670025d41ece1992946c7b77224179e8  tundravm.lock
  489d22bcf640902f1aa464238184967658d0236d461e68b81b59b77664d11d2d  variants/default/policy.json
  d930bca4b8373fdbc3b055fb16a045efb1e170b8b9bfe704ea1b5780b1ec80d7  variants/default/sbom-qemu.spdx.json
note: default/qemu sbom: the artifact is simulated (in-process backend): nothing was installed
note: default/qemu sbom: no mkosi package manifest at build/default/default.manifest: listing the packages the recipe declares, without versions
[exit 0]
```

`evidence` builds nothing: it reads `build/bake-result.json`, hashes each artifact again against the sha256 the bake recorded (`integrity`), compares the lockfile with the recipe (`lock`), reads back the outcome step 8 recorded (`reproduce`) and lints the recipe with the lock's pins (`lint`). The verdict is `pass` only when all four hold; a bake never checked for reproducibility passes as `not checked`. Append one byte to `build/default/disk.qcow2` and the same command prints `integrity  mismatch` and `verdict fail` and exits 1. `build/evidence/` holds the files under `members`, indexed with their sha256 by `evidence.json`; `--policy peer.json` adds step 9's policy, which the index marks as a placeholder (without the flag, `evidence` looks for `build/default/policy.json`). The notes say what could not be included, here the package manifest a simulated bake never has.

`evidence.tar.gz` is what you hand a reviewer: publish its sha256 with it, and they unpack it and check every member against `evidence/evidence.json`. Its members are sorted, owned by `0:0` and stamped with `SOURCE_DATE_EPOCH`, so packing the same bake again gives the same bytes. `report.html` is the same record as one self-contained page, opening with a pass/fail banner. See [CLI: Evidence](cli.md#evidence).

## 12. Deploy

```console
$ tundravm deploy build --variant default --target qemu
error [E_DEPLOYMENT]: Refusing to deploy a simulated artifact.
Hint: Bake with a real backend first, or pass allow_simulated=True for a test run.
  variant: default
  path: build/default/disk.qcow2
[exit 2]

$ tundravm deploy build --variant default --target qemu --allow-simulated-artifact
error [E_DEPLOYMENT]: QEMU binary not found: qemu-system-x86_64
Hint: Install QEMU and ensure it is in PATH.
  binary: qemu-system-x86_64
[exit 2]
```

On a host with QEMU and a real bake, `deploy` boots the baked image (a UKI, on OVMF firmware) in the background and prints the deployment id, its `ssh://localhost:PORT` endpoint, and the `serial_log`, `monitor` socket and `pidfile` it left in `build/default/`; `--attach` keeps QEMU in the foreground with its console on the terminal instead. Target settings are `--param KEY=VALUE` (`memory`, `cpus`, `ssh_port`, `tdx`, `daemonize`, `forward=HOST:GUEST,...` for qemu). [CLI: Deploying to each target](cli.md#deploying-to-each-target) has the whole sequence for QEMU, Azure and GCP, including how to check the VM and tear it down.

## 13. Write a fragment

Say every image that runs the app should also probe it once a minute. Write that as a `Composite`, like `App`, in `fragments.py` next to the recipe:

```python
from dataclasses import dataclass

from tundravm import Diagnostic, File, Fragment, Package, Resolved, Service, Unit
from tundravm.declarative.utils import Composite

TIMER = """\
[Unit]
Description=Probe the app once a minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min

[Install]
WantedBy=minimal.target
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthCheck(Composite):
    """A timer that probes the app's port once a minute. Fragment name: ``healthcheck``."""

    port: int

    def compose(self) -> Fragment:
        return Fragment(
            "healthcheck",
            requires=("app",),  # the fragment App composes
            checks=(self.check_port,),
            items=(
                Package("curl"),
                Service(
                    "app-healthcheck",
                    f"/usr/bin/curl -fsS -o /dev/null http://127.0.0.1:{self.port}/",
                    description="Probe the app",
                    type="oneshot",
                ),
                Unit("app-healthcheck.timer", TIMER, enabled=True),
            ),
        )

    def check_port(self, resolved: Resolved) -> tuple[Diagnostic, ...]:
        """Lint rule: probe the port the app's config listens on."""
        listen = f"listen=0.0.0.0:{self.port}\n"
        for item in resolved.items:
            if isinstance(item, File) and item.path == "/etc/app/app.conf" and listen in item.content:
                return ()
        message = f"the app does not listen on port {self.port}"
        return (Diagnostic("healthcheck-port", message, variant=resolved.variant),)
```

- `HealthCheck(port=9090)` is itself a `Fragment`: it goes in `common`, a variant's `add`, or another fragment's `items`. `compose()` runs once, at construction.
- `requires` names fragments that must be in the same variant; without `App`, lint reports `fragment-requires-missing`.
- `checks` are functions from the resolved variant to diagnostics; they run during `lint` (and `resolve`, which fails on errors). `check_port` reads the config file `App` declared, so the probe and the service cannot disagree on the port.
- A generated `Service` is enabled into `minimal.target` unless `wanted_by` names another target, so the probe also runs once at boot; the timer repeats it every minute.

Use it in `node.py`: add `from fragments import HealthCheck` after the tundravm imports and `HealthCheck(port=APP_PORT),` after `App(...)` in `common`. A wrong port is a lint error:

```console
$ tundravm lint node.py --variant default
error healthcheck-port [default]: the app does not listen on port 8080
1 error, 0 warnings, 0 infos
[exit 1]
```

That was `HealthCheck(port=8080)`. With `port=APP_PORT` the `healthcheck-port` error is gone, and what `lint` reports is drift: it checks the recipe against the lockfile the table configures, which step 6 wrote before the probe existed:

```console
$ tundravm lint node.py --variant default
error lock-changed [common] variants.default.files: variants.default.files changed since the lock: +/usr/lib/systemd/system/app-healthcheck…
error lock-changed [common] variants.default.packages: variants.default.packages changed since the lock: +curl
error lock-changed [common] variants.default.services: variants.default.services changed since the lock: +app-healthcheck +app-healthcheck.timer
3 errors, 0 warnings, 0 infos
[exit 1]

$ tundravm inspect node.py --variant default | grep -E "Fragments|Units|app-healthcheck "
Fragments (3): base app healthcheck
Units: enable app-healthcheck.timer
  app-healthcheck  /usr/bin/curl -fsS -o /dev/null http://127.0.0.1:9090/  restart=no  type=oneshot

$ tundravm diff node.py --against mkosi --variant default --stat
M  default/mkosi.conf
A  default/mkosi.extra/usr/lib/systemd/system/app-healthcheck.service
A  default/mkosi.extra/usr/lib/systemd/system/app-healthcheck.timer
M  default/scripts/06-postinst.sh
4 files changed
[exit 1]

$ tundravm lock node.py
locked build/tundravm.lock
[exit 0]

$ tundravm lint node.py
no findings
[exit 0]

$ tundravm compile node.py --out mkosi
compiled mkosi
  variants:      default, dev, debug
  recipe_digest: d08f2d6df99bcab4956c5e0e402d57ac15116fc642bde8c0e3f61dac95be4586
  tree_digest:   a9f21aa2f44800185a4d187ff2144a4c3f7a7e0a9c75b297c4995b0217b03c8e
[exit 0]
```

`mkosi.conf` gained `curl`, and `06-postinst.sh` enables the probe and its timer. The recipe directory is importable while the recipe loads, so `fragments.py` needs no packaging. The [fragment guide](module-authoring.md) covers builds, units and ordering in depth.

## 14. Test it

`tests/test_node.py`, which `init` wrote, has three tests:

- `test_lints_clean`: `assert_clean(RECIPE, strict=True)`, so a warning fails it too;
- `test_compiles_every_variant`: every variant compiles, and the variants are the ones it names (step 4);
- `test_matches_committed_tree`: `mkosi/` is what the recipe compiles to with `build/tundravm.lock`. After an intended change, `tundravm compile node.py --out mkosi` (or `TUNDRAVM_UPDATE_GOLDEN=1 uv run pytest tests`) rewrites it.

Add tests for the fragment in `tests/test_healthcheck.py`. `tundravm.testing` works on the same values the lifecycle returns:

```python
"""Tests for the HealthCheck fragment in fragments.py."""

from fragments import HealthCheck
from node import App

from tundravm import Fragment, Package, Recipe, lint
from tundravm.testing import assert_clean, assert_diagnostic


def image(*items: Fragment) -> Recipe:
    return Recipe(name="t", common=Fragment("base", items=(Package("linux-image-amd64"), *items)))


def test_probes_the_app_port() -> None:
    assert_clean(lint(image(App(version="1", port=8080), HealthCheck(port=8080))))


def test_needs_the_app() -> None:
    assert_diagnostic(lint(image(HealthCheck(port=8080))), "fragment-requires-missing")


def test_probes_the_wrong_port() -> None:
    found = lint(image(App(version="1", port=8080), HealthCheck(port=9090)))
    assert_diagnostic(found, "healthcheck-port", variant="default")
```

- `image()` builds a one-variant recipe (`default`, for qemu) around the fragments under test, with the kernel package so `kernel-missing` stays quiet.
- `assert_clean(lint(recipe))` fails on errors and, for a list of diagnostics, on warnings too (`allow=("code",)` to accept some).
- `assert_diagnostic` returns the first diagnostic matching every field you give, or fails listing what was found.

The tests import `fragments` and `node` from the project directory, which the generated `pyproject.toml` puts on pytest's path:

```toml
[tool.pytest.ini_options]
pythonpath = ["."]
```

```console
$ uv run pytest -q tests
......                                                                   [100%]
6 passed in 0.07s
[exit 0]
```

In a project of your own, the loop is: edit the recipe, `tundravm compile node.py --out mkosi`, `uv run pytest tests`, `tundravm lock node.py`.

## 15. CI

`tundravm ci` runs `lint --strict`, `compile --check` and `lock --check` and stops at the first failure:

```console
$ tundravm ci node.py --out mkosi
ok lint: no findings
ok compile: mkosi is up to date
ok lock: build/tundravm.lock is up to date
[exit 0]
```

A failing step prints its report and `FAIL`, the remaining steps print `skip`, and the command exits 1. Commit `mkosi/` and `build/tundravm.lock`; `tundravm init --ci github` writes a workflow that runs this gate on every push. See [CLI: CI](cli.md#ci).
