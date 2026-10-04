# Tutorial

This walkthrough takes a new recipe from an empty directory to a baked, measured image, then sets up CI and a test. It uses `InProcessBackend`, which writes placeholder artifacts instead of running mkosi, so every step runs on any machine in a few seconds. Switching to a real backend is one line (step 1).

All output below is real, captured from tundravm 0.1.0. Digests and timestamps will differ on your machine.

## 0. Install

In a project of your own:

```bash
uv add tundravm
```

Inside this repository, `uv sync` installs the package and the `tundravm` command. Prefix commands with `uv run` if the virtualenv is not activated.

```bash
$ tundravm --version
tundravm 0.1.0
```

## 1. Create a recipe

```bash
$ tundravm new node.py --backend inprocess
wrote node.py
next: tundravm explain node.py
```

`node.py` is a plain Python file:

```python
from tundravm import Image
from tundravm.backends.inprocess import InProcessBackend
from tundravm.modules import Devtools

img = Image(base="debian/trixie", backend=InProcessBackend())
img.install("systemd", "curl", "jq")
img.file("/etc/motd", content="node\n")
img.user("app", system=True, shell="/bin/false")
img.service("app", command="/usr/bin/true")
img.debloat(enabled=True)
img.output_targets("qemu")

with img.profile("dev"):
    img.apply(Devtools())
```

The CLI finds the `Image` bound to `img`. A zero-argument `build() -> Image` function works too (see [cli.md](cli.md#recipe-files)).

Everything above the `with` block goes to the `default` profile. `dev` is a second profile: it extends `default` and adds the `Devtools` module (serial console, root password, debugging packages).

To build real images later, swap the backend: `--backend lima` (mkosi inside a Lima VM, macOS and Linux), `--backend nix` (Linux with Nix), or `--backend local` (mkosi on the host). `tundravm doctor` tells you which ones your host can run:

```bash
$ tundravm doctor
tundravm 0.1.0
python 3.12.3
backend lima_mkosi: available
  ok limactl limactl version 2.0.3
backend nix_mkosi: unavailable
  missing nix — Install Nix with flakes enabled: https://nixos.org/download.html
backend local_linux: available
  ok mkosi mkosi 26
  ok sudo Sudo version 1.9.15p5

$ tundravm doctor node.py
tundravm 0.1.0
python 3.12.3
backend inprocess: available
  no external tools required
check: no findings
```

With a recipe, `doctor` probes only that recipe's backend and exits 1 if a required tool is missing.

## 2. Explain the recipe

`explain` is a dry run. It compiles nothing and writes nothing.

```bash
$ tundravm explain node.py
Image: debian/trixie (x86_64)  profile=default  reproducible=yes
Policy: mutable_ref_policy=warn network_mode=online require_frozen_lock=no require_integrity=yes
Packages (3): curl jq systemd
Files (1):
  /etc/motd  0644  5B  sha256:389b46f04457
Users (1):
  app  system  shell=/bin/false
Services (1):
  app  /usr/bin/true  restart=no
Hooks:
  finalize (1):
    sed -i '/^IMAGE_VERSION=/d' "$BUILDROOT/usr/lib/os-release"
Debloat: enabled, 30 paths removed, systemd minimize=yes
Output targets: qemu
```

The `finalize` hook comes from `reproducible=True`, which strips the volatile `IMAGE_VERSION` from `os-release`.

Look at `dev` with `--profile dev`. It shows `Extends: default`, the applied modules, and the merged result: the default's packages, files, user and service plus what `Devtools` adds.

```bash
$ tundravm explain node.py --profile dev
Image: debian/trixie (x86_64)  profile=dev  reproducible=yes
Policy: mutable_ref_policy=warn network_mode=online require_frozen_lock=no require_integrity=yes
Extends: default
Modules (1): Devtools
Packages (15): apt bash-completion curl dnsutils iputils-ping jq net-tools netcat-openbsd openssh-server socat strace systemd tcpdump tcpflow vim
Files (2):
  /etc/motd                                       0644  5B  sha256:389b46f04457
  /usr/lib/systemd/system/serial-console.service  0644  263B  sha256:016dd361c717
...
```

`--json` gives the same data as `Image.explain()`, and `--all-profiles` prints every profile.

## 3. Add a module and a profile

Edit `node.py`. Add two imports and append a key-generation module and an `azure` profile:

```python
from tundravm.modules import Devtools, KeyGeneration
from tundravm.platforms import AzurePlatform

# ... the starter recipe from step 1 ...

keys = KeyGeneration()
keys.key("key_persistent", strategy="tpm")
img.apply(keys)

azure = img.profile("azure")
azure.apply(AzurePlatform())
azure.service("agent", command="/usr/bin/true", user="agent", env={"LOG_LEVEL": "info"})
```

- `img.apply(...)` applies one or more modules in order. `KeyGeneration` registers a boot script at its class priority (10) in `/usr/bin/runtime-init`.
- `img.profile("azure")` returns a `Profile` object. Calls on it declare into that profile only and return the `Profile`, so they chain. `with img.profile("azure"):` does the same for a block of `img.*` calls.
- `AzurePlatform` adds the Azure provisioning service and the `azure` output target.

The `agent` service runs as user `agent`, which this recipe never creates. That is a deliberate mistake.

## 4. Lint with `check`

```bash
$ tundravm check node.py --all-profiles
error service-user-missing [azure] agent: service 'agent' runs as user 'agent', which is never created
    hint: Declare it with img.user('agent', system=True) in profile 'azure', or drop user= to run as root.
1 error, 0 warnings, 0 infos
$ echo $?
1
```

`check` exits 1 on errors, or on warnings too with `--strict`. `bake` runs the same linter first and refuses to build while an error-level finding exists. Fix it by declaring the user before the service:

```python
azure.user("agent", system=True)
azure.service("agent", command="/usr/bin/true", user="agent", env={"LOG_LEVEL": "info"})
```

```bash
$ tundravm check node.py --all-profiles
no findings
```

The rule codes are listed in [api.md](api.md#diagnostics).

## 5. Compile and inspect the tree

```bash
$ tundravm compile node.py --all-profiles
compiled build/mkosi
  profiles: azure, default, dev
  digest:   72d2d0a806244472abce3156a3c4763967b5ac55191168682e091d4f6f6cff20
```

The output is a plain mkosi project, one directory per profile:

```text
build/mkosi/
  azure/
    mkosi.conf
    mkosi.extra/etc/motd
    mkosi.extra/etc/tdx/key-gen.yaml
    mkosi.extra/usr/bin/azure-complete-provisioning
    mkosi.extra/usr/bin/runtime-init
    mkosi.extra/usr/lib/systemd/system/agent.service
    mkosi.extra/usr/lib/systemd/system/app.service
    mkosi.extra/usr/lib/systemd/system/azure-complete-provisioning.service
    mkosi.extra/usr/lib/systemd/system/runtime-init.service
    mkosi.skeleton/
    scripts/04-build.sh  06-postinst.sh  07-finalize.sh  azure-postoutput.sh
  default/  ...
  dev/      ...
```

`azure/` holds the default's `motd`, `app.service` and key generation, plus its own additions. The generated unit for `agent`:

```ini
[Unit]
Description=agent
After=runtime-init.service
Requires=runtime-init.service

[Service]
Type=simple
ExecStart=/usr/bin/true
User=agent
Environment=LOG_LEVEL=info

[Install]
WantedBy=minimal.target
```

`After=`/`Requires=runtime-init.service` were added because the recipe has init scripts. `compile` recreates each profile directory, so a file you drop from the recipe also disappears from the tree.

## 6. Change the recipe and diff

Add `htop` to the package list:

```python
img.install("systemd", "curl", "jq", "htop")
```

`diff` compiles to a temporary directory and compares it with `build/mkosi`. Nothing is written.

```bash
$ tundravm diff node.py --all-profiles --stat
M  azure/mkosi.conf
M  default/mkosi.conf
M  dev/mkosi.conf
3 files changed

$ tundravm diff node.py --profile default --color never
diff --git a/default/mkosi.conf b/default/mkosi.conf
--- a/default/mkosi.conf
+++ b/default/mkosi.conf
@@ -18,6 +18,7 @@
 CleanPackageMetadata=true
 Packages=
     curl
+    htop
     jq
     systemd
     tpm2-tools
```

All three profiles change because `azure` and `dev` extend `default`. `diff` exits 1 when the trees differ. Accept the change with `tundravm compile node.py --all-profiles`.

## 7. Lock

The lockfile pins the recipe digest, with one digest per recipe section.

```bash
$ tundravm lock node.py
locked build/tundravm.lock
$ tundravm lock node.py --check
lock is up to date
```

Now change the recipe again. Add a file below the `motd` line:

```python
img.file("/etc/issue", content="node\n")
```

`lock --check` reports what drifted, without writing:

```bash
$ tundravm lock node.py --check
~ profiles.default.files: +/etc/issue
$ echo $?
1
```

A frozen bake refuses to build:

```bash
$ tundravm bake node.py --frozen
error [E_LOCKFILE]: Frozen bake lockfile is stale for current recipe state:
  ~ profiles.default.files: +/etc/issue
Hint: Run tundravm lock RECIPE to accept these changes, or revert them.
  lock: build/tundravm.lock
  changed: profiles.default.files
```

Accept the change. `--explain` prints the drift, then writes the new lockfile:

```bash
$ tundravm lock node.py --explain
~ profiles.default.files: +/etc/issue
locked build/tundravm.lock
```

## 8. Bake

```bash
$ tundravm bake node.py --frozen
baked default
  qemu   build/default/disk.qcow2
  report build/default/report.json
next: tundravm deploy node.py --target qemu
```

`bake --lock` combines both steps: write the lockfile, then bake frozen. `--all-profiles` bakes every profile.

Besides the artifact and `report.json`, bake writes `build/bake-result.json`. Later commands read it, so `measure` and `deploy` work in a new process:

```json
{
  "backend": "inprocess",
  "created_at": "2026-10-04T15:40:05+00:00",
  "lock_digest": "decc7b86c95edfbc4f6b5b6bddf3afdaa861155a3426da6b081ed9f058d653c9",
  "profiles": {
    "default": {
      "artifacts": {
        "qemu": "default/disk.qcow2"
      },
      "report_path": "default/report.json"
    }
  },
  "schema_version": 1
}
```

## 9. Measure

```bash
$ tundravm measure node.py --backend rtmr --allow-placeholder
measurements default (rtmr)
  RTMR0  7d4a783e463026f1bf368940a2756fecfa424b441f168a9ff7379e64694193e7
  RTMR1  dd1479258b13a05aae3225aafd02ed18734c750d8942fb0b90287461f5bcbbcd
  RTMR2  44d2ceafa105d213275b16a09db56cc14a2e5ca83a1233bbed1c806d31d53279
```

The in-process artifact is a placeholder, so these values are only deterministic, not meaningful. With a real backend they are the expected TDX registers for the image. `--backend azure` and `--backend gcp` give PCR values. `--json` prints `{schema_version, backend, values}`.

Without a bake there is nothing to measure:

```bash
$ tundravm measure node.py --backend rtmr --allow-placeholder --out elsewhere
error [E_STATE]: No bake result found.
Hint: Run bake() / tundravm bake first.
  path: elsewhere/bake-result.json
```

If you baked with `--out DIR`, pass the same `--out DIR` to `measure` and `deploy`.

## 10. Deploy

```bash
$ tundravm deploy node.py --target qemu --memory 4G --cpus 2
```

`deploy` takes the `qemu` artifact of the last bake and boots it with `qemu-system-x86_64`: KVM, `-m 4G -smp 2`, virtio disk, and host port 2222 forwarded to guest SSH. It runs daemonized and prints the deployment id and adapter metadata. `--param tdx=true` adds the TDX guest object, `--param ssh_port=2223` changes the forward, `--param daemonize=false` keeps QEMU in the foreground.

On a host without QEMU it fails cleanly. This is the output on the machine used for this tutorial:

```bash
$ tundravm deploy node.py --target qemu --memory 4G --cpus 2
error [E_DEPLOYMENT]: QEMU binary not found: qemu-system-x86_64
Hint: Install QEMU and ensure it is in PATH.
  binary: qemu-system-x86_64
```

The placeholder disk from `InProcessBackend` does not boot. Rebake with a real backend before deploying. `--target azure` and `--target gcp` upload and create a VM through the cloud CLIs; see [concepts.md](concepts.md#deploy-adapters) for their parameters.

## 11. Commit the tree and gate CI

Commit `node.py` and `build/mkosi/` together. A recipe change then shows up twice in review: the Python diff and the mkosi diff it causes.

In CI, fail when someone changes the recipe but forgets to recompile:

```bash
$ tundravm compile node.py --all-profiles --check
M  azure/mkosi.conf
M  default/mkosi.conf
M  dev/mkosi.conf
3 files changed
$ echo $?
1
```

That output is from step 6, before the change was compiled. On an up-to-date tree it exits 0. A typical CI job:

```bash
tundravm check node.py --all-profiles --strict
tundravm compile node.py --all-profiles --check
tundravm lock node.py --check
```

## 12. Test the recipe

`tundravm.testing` compiles and lints in-process. Its pytest plugin is registered on install, so fixtures such as `image` need no `conftest.py`. Save as `test_node.py`:

```python
from pathlib import Path

from tundravm import load_recipe
from tundravm.testing import assert_clean, assert_diagnostic, compile_tree

RECIPE = Path(__file__).parent / "node.py"


def test_recipe_is_clean() -> None:
    img = load_recipe(RECIPE)
    with img.all_profiles():
        assert_clean(img)


def test_azure_agent_unit() -> None:
    img = load_recipe(RECIPE)
    tree = compile_tree(img, profiles=["azure"])
    unit = tree.unit("agent", profile="azure")
    assert "User=agent" in unit
    assert "Environment=LOG_LEVEL=info" in unit
    assert "htop" in tree.conf(profile="azure")  # inherited from default


def test_undeclared_user_is_an_error(image) -> None:
    image.service("worker", command="/usr/bin/true", user="worker")
    assert_diagnostic(image, "service-user-missing", level="error", subject="worker")
```

```bash
$ pytest -q test_node.py
...                                                                      [100%]
3 passed in 0.01s
```

[testing.md](testing.md) covers golden trees, `bake_in_process()`, `FakeModule` and `run_cli`.

## Next

- [concepts.md](concepts.md): recipe vs tree vs artifact, profiles, phases, runtime-init.
- [cli.md](cli.md): every command and option.
- [module-authoring.md](module-authoring.md): write your own `Module`.
- [api.md](api.md): `Image` and `Profile` reference.
