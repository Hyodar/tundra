# Tutorial

This walk-through starts from an empty directory and follows the project `tundravm init` writes: lock it, inspect the starter recipe and ask where one of its objects comes from, lint it (and see what happens without a kernel), add a variant, lock again and compile, leave `watch` running while you edit, change the recipe and see the diff and the lock drift, bake, prove the bake reproducible, measure and export a verifier policy, list what is in the image, package the evidence and verify it, deploy, then write a fragment of your own, test it next to the generated tests, run the CI gate and read the workflow that runs it, sign the evidence of a release and review the next change against it, ask where the project stands and clean the build directory, and finally cut a release, resume it after a failed run and verify it on another machine. Everything runs on the in-process backend, which needs no build tools and writes simulated artifacts. Every transcript below is real output; exit codes are shown as `[exit N]`. The one command that needs a running TDX VM, attesting it, is shown but not run.

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), and the `tundravm` command (`uv sync` in a checkout of this repository installs it in `.venv/bin`; tundravm is not on PyPI yet). The `pyproject.toml` that `init` writes depends on the tundravm you ran it with, pinned as a git requirement, `tundravm @ git+https://github.com/Hyodar/tundra@<commit>` (`master` when the running commit cannot be determined), so `uv sync` and `uv run` in the project install it on a fresh machine and in CI. To use an unreleased local checkout instead, `uv add --editable PATH/TO/tundravm` overrides that requirement; step 15 does so before running the project's tests.

## 1. Start a project

```console
$ mkdir node && cd node
$ tundravm init . --backend inprocess --ci github
created node.py
created tests/test_node.py
created .github/workflows/tundravm.yml
created pyproject.toml
created README.md
created .gitignore
lint node.py: no findings
next:
  1. tundravm lock node.py                  record recipe sections and pin source repositories/downloads in build/tundravm.lock
  2. tundravm compile node.py --tree mkosi  write the mkosi tree; commit it
  3. uv run pytest tests                    run test_node.py against mkosi/
  4. tundravm ci node.py --tree mkosi       the lint, tree and lockfile checks CI runs
  5. tundravm bake node.py --build build    build the image
  `uv sync` installs the pinned dependency, tundravm at 0db3f45de51b
then commit mkosi/ and build/tundravm.lock; the workflow checks both

checking the inprocess backend (tundravm doctor --backend inprocess):
tundravm 0.1.0
python 3.12.3
backend inprocess: available
  no external tools required
measurement tools:
  ok tundravm-measure 1.0+attest-12f1e29.dstack-mr-gcp-b16e08b (built in)
[exit 0]
```

The recipe is named after the directory (`--name` overrides it). `init` writes the `service` starter (`--template` picks another, `tundravm init --list-templates` lists them), a tests module for it, `pyproject.toml` and `README.md` when they are absent, and, with `--ci github`, the GitHub Actions workflow [step 16](#16-ci) walks through. Pass `--ci github` here: `init` refuses to run again over an existing `node.py` without `--force`, which rewrites the recipe. It lints the recipe, prints the next steps, and ends with `tundravm doctor --backend inprocess`, which needs no tools; measurement is built in (see [Measure](#10-measure)). `--no-doctor` skips the probe. For tab completion of verbs and flags, see [CLI: Shell completion](cli.md#shell-completion).

`node.py` binds a `Recipe` to `recipe` and a backend to `backend`. Below its docstring and imports:

```python
APP_VERSION = "0.1.0"
APP_PORT = 8080


@component
class App(Composite):
    """The app: its config file, the service and a lint rule.

    Ship ``/usr/bin/app`` with a ``GoBinary``, a ``Build`` or a ``Package``. Fragment name: ``app``.
    """

    version: str
    port: int

    @property
    def http(self) -> HttpEndpoint:
        """Where the app answers HTTP inside the image, for a component that probes it."""
        return HttpEndpoint("127.0.0.1", self.port, provider=self)

    def compose(self) -> Iterator[Declaration]:
        config = {"version": self.version, "listen": f"0.0.0.0:{self.port}"}
        yield ConfigFile.env("/etc/app/app.conf", config, sort_keys=False)
        # Runs as system user "app" (declared for you), sandboxed by Hardening.strict(),
        # restarted on failure; state="app" is /var/lib/app, made by systemd, owned by
        # app and its working directory. It waits for every runtime-init step (needs=).
        # ports= is where it listens: TeeBase's firewall opens it.
        yield Service(
            "app",
            "/usr/bin/app --config /etc/app/app.conf",
            description=f"app {self.version}",
            state="app",
            ports=(self.port,),
        )

    def check(self, resolved: Resolved) -> tuple[Diagnostic, ...]:
        """Lint rule: the app runs as a regular user, so it cannot bind a port below 1024."""
        if self.port >= 1024:
            return ()
        message = f"app runs as user app and cannot bind port {self.port}"
        return (Diagnostic("app-privileged-port", "error", message),)


backend = InProcessBackend()

recipe = Recipe(
    name="node",
    # Debian with its kernel and systemd boot set: recipe.platform.packages lists them.
    platform=Debian("trixie"),
    items=(
        # NTS time, DHCP, a firewall opening only declared ports, RAM-only journal, sysctl
        # hardening, DNS, no apt timers: opt out per flag.
        TeeBase(),
        Package("ca-certificates"),
        App(version=APP_VERSION, port=APP_PORT),
    ),
    variants=(
        Variant("default", outputs=("qemu",)),
        Variant("dev", inherits="default", add=DevTools(), release=False),  # never ship it
    ),
)
```

- `platform` is what the image builds on: `Debian("trixie")` is the distribution plus its boot set (the kernel, systemd as init, udev and the UKI's EFI stub), declared in every variant with provenance `platform`. `tundravm inspect` lists them like any other package.
- `items` holds what every variant shares: `TeeBase()`, `ca-certificates` and the `App` fragment.
- `TeeBase()` is the baseline a confidential VM wants, each piece behind a flag: `chrony` over NTS so the host cannot set the clock, DHCP on every Ethernet interface (`Network()`), an nftables `Firewall()` that drops inbound traffic except the ports the variant's services declare, a RAM-only journal, sysctl hardening, a static `/etc/resolv.conf`, and masked apt timers and tty1 getty. `TeeBase(firewall=False)` drops one piece; see [Concepts: The TEE baseline](concepts.md#the-tee-baseline).
- `App` is a component: `@component` makes the class a frozen dataclass whose fields (`version`, `port`) are its configuration, and each instance is a `Fragment` named after the class. `compose()` yields its contents: a config file rendered by `ConfigFile.env` and a generated systemd `Service`, which runs as its own system user `app` (declared for you), restarts on failure, runs in the `Hardening.strict()` sandbox and keeps its state in `state="app"` (`StateDirectory=`, `/var/lib/app`), waiting for every boot step by default; `ports=(self.port,)` is where it listens, which the firewall opens. Its `check` method runs during `lint`: `App(port=80)` reports `app-privileged-port`. Its `http` property exports where it listens, for [step 14](#14-write-a-fragment).
- The service runs `/usr/bin/app`, which the recipe does not ship yet. Add a `Build(...)` or a `Package(...)` for it before you bake a real image.
- `dev` inherits `default` and adds `DevTools()`: a serial console, root login and debugging tools. Never ship it: `release=False` keeps it out of every release ([step 19](#19-cut-a-release)).

The `.gitignore` block ignores `build/` but keeps `build/tundravm.lock`, which you commit.

`pyproject.toml` ends with the project's `[tool.tundravm]` table:

```toml
[tool.tundravm]
recipe = "node.py"
tree = "mkosi"
build = "build"
lockfile = "build/tundravm.lock"
backend = "inprocess"
```

Every command looks for it in the working directory and its parents, so in this project `node.py` may be left out: `tundravm lint` lints `node.py`, `tundravm compile` writes `mkosi/`, `tundravm status` reports on the whole project, and `tundravm measure`, `deploy` and `sbom` read the bake in `build/`. The steps below spell `node.py` and `--tree mkosi` out so that each command reads on its own; a flag always wins over the table. `tundravm config` prints what applies:

```console
$ tundravm config
pyproject: /home/me/node/pyproject.toml
  recipe    node.py              pyproject
  tree      mkosi                pyproject
  build     build                pyproject
  lockfile  build/tundravm.lock  pyproject; does not exist; run `tundravm lock`
  backend   inprocess            pyproject
  require   reproducible,lock    default
[exit 0]
```

`require` is the project's requirement set: the evidence checks `tundravm status` judges release readiness against, and that `tundravm evidence` and `tundravm release` require when `--require` is omitted (steps 12, 18 and 19). The table sets none, so it is the default, a second build that matched and a lockfile; `require = ["reproducible", "lock", "measured"]` in the table would change it for all three, and `--require ''` requires nothing for one run.

The lockfile the table configures does not exist yet. Until `tundravm lock` writes it, `inspect`, `lint`, `compile`, `diff` and `fetch` print `` note: configured lockfile build/tundravm.lock does not exist; run `tundravm lock` to create it `` and run without pins, and `bake` refuses with `E_LOCKFILE` instead of baking unfrozen. Lock first:

```console
$ tundravm lock node.py
locked build/tundravm.lock
[exit 0]
```

`tundravm config` now prints `lockfile  build/tundravm.lock  pyproject`. Every later change to the recipe drifts from this lock until you lock again, which steps 5, 7, 14 and 17 do.

## 2. Inspect

`inspect` is a dry run: what each variant will contain, without writing anything.

```console
$ tundravm inspect node.py --variant dev
Image: debian/trixie (x86_64)  variant=dev  reproducible=yes
Policy: mutable_ref_policy=warn network_mode=online require_frozen_lock=no storage_safety=warn
Inherits: default
Fragments (4): tee-base network app devtools
Packages (22): apt bash-completion ca-certificates chrony curl dnsutils iputils-ping kmod linux-image-amd64 net-tools netcat-openbsd nftables openssh-server socat strace systemd systemd-boot-efi systemd-sysv tcpdump tcpflow udev vim
Files (8):
  /etc/app/app.conf                                                                        0644  34B  sha256:681b227aa566
  /etc/chrony/chrony.conf                                                                  0644  605B  sha256:c75508d70f9b
  /etc/nftables.conf                                                                       0755  829B  sha256:9a686b2478b3
  /etc/resolv.conf                                                                         0644  57B  sha256:d822a90e8975
  /etc/sysctl.d/50-tundravm-tee.conf                                                       0644  2152B  sha256:ea9e425204c9
  /etc/systemd/journald.conf.d/50-tundravm-tee.conf                                        0644  72B  sha256:8a2a24c8d3a1
  /etc/systemd/network/50-tundravm.network                                                 0644  234B  sha256:180df2f77bea
  /usr/lib/systemd/system/systemd-networkd-wait-online.service.d/50-tundravm-network.conf  0644  96B  sha256:57be339766ae
Users (1):
  app  system  shell=/usr/sbin/nologin
Units: enable chrony.service, systemd-networkd.service, systemd-networkd.socket, systemd-networkd-wait-online.service, nftables.service, serial-getty@ttyS0.service; mask apt-daily.timer, apt-daily-upgrade.timer, dpkg-db-backup.timer, getty@tty1.service
Services (1):
  app  /usr/bin/app --config /etc/app/app.conf  restart=on-failure  type=exec  user=app  cwd=/var/lib/app
Hooks:
  postinst (1):
    SALT=$(printf 'tundravm-devtools:%s' "${IMAGE_ID:-devtools}" | sha256sum | cu...
  finalize (1):
    sed -i '/^IMAGE_VERSION=/d' "$BUILDROOT/usr/lib/os-release"
Debloat: enabled, 30 paths removed, systemd minimize=yes
Targets: qemu
[exit 0]
```

`Inherits:` is the variant `dev` builds on and `Fragments:` the fragments it includes: `tee-base` and the `network` fragment it nests, and `app` from `App`, all in `Recipe.items`, and `devtools` from its own `add`. Most of the files are `TeeBase`'s: the chrony, journald, sysctl, resolver and networkd configuration, and `/etc/nftables.conf`, the firewall's ruleset, which drops inbound traffic except loopback, replies, DHCP and TCP port 8080, the port `App`'s service declares (`comment "app.service"`). `Units:` lists the units the variant enables and masks: `TeeBase` enables chrony, networkd and nftables and masks the apt timers and the tty1 getty, and `DevTools` enables the serial getty. `Hooks:` shows each script's first line, cut at 80 characters: the `postinst` hook derives a fixed salt from the variant name before hashing the root password, so a second build of `dev` gives the same image (step 9). Without `--variant` every variant is shown. `--format json` adds the recipe digest the lockfile records; `--format markdown` renders tables for a CI job summary.

To ask where one object comes from, name it with `--why`: an image path, or `unit:`, `package:`, `hook:` or `init:` and a name.

```console
$ tundravm inspect node.py --variant dev --why unit:app
why unit:app (variant dev)
  Service(app.service)
    declared at node.py:58 in common via app (App)
files (in dev/):
  mkosi.extra/usr/lib/systemd/system/app.service
generated:
  scripts/06-postinst.sh: mkosi-chroot systemctl enable app.service
  scripts/06-postinst.sh: minimal.target.wants/app.service link
[exit 0]
```

The service is declared in `common` (the recipe's own `items`), inside the `app` fragment that the `App` composite returns. Its unit file lands in `dev/mkosi.extra/`, and the compiler added the enablement into `minimal.target` (a variant with runtime-init steps would also get the ordering after `runtime-init.service`: `Service` waits for every step by default; see [Concepts: Runtime init](concepts.md#runtime-init)). `node.py:58` is the line in `App.compose()` that constructs the `Service`: every declaration records where it was written, and `lint` points at the same lines. A declaration a variant adds, edits or removes lists that step too, with the line of the edit (`content edited at FILE:LINE in variant prod`). `--why` compiles the variant into a scratch directory and writes nothing (see [CLI: Explaining one object](cli.md#explaining-one-object)).

## 3. Lint

```console
$ tundravm lint node.py
no findings
[exit 0]
```

Every variant here bakes a bootable UKI, and a UKI needs a kernel. Pass `Debian("trixie", kernel_package=None)` and lint again:

```console
$ tundravm lint node.py
error kernel-missing [default]: variant builds a bootable UKI but installs no kernel, so mkosi stops with 'A kernel must be installed in the image to build a UKI'
    hint: Keep Debian(kernel_package="linux-image-amd64") for the distribution kernel, declare Kernel(...) to build one, or Setting("Content", "Bootable", ("no",)) with Debian(boot=None) for a non-bootable image.
error kernel-missing [dev]: variant builds a bootable UKI but installs no kernel, so mkosi stops with 'A kernel must be installed in the image to build a UKI'
    hint: Keep Debian(kernel_package="linux-image-amd64") for the distribution kernel, declare Kernel(...) to build one, or Setting("Content", "Bootable", ("no",)) with Debian(boot=None) for a non-bootable image.
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

A credential baked into the image is refused the same way, since every copy of the disk would carry it. Generate a key with `ssh-keygen -t ed25519 -N "" -f deploy.key`, ship it with `File.copy("/etc/app/deploy.key", Path("deploy.key"))` in `items`, after `Package("ca-certificates")` (and `from pathlib import Path`, `File` from `tundravm`), and lint:

```console
$ tundravm lint node.py
error secret-in-file [default] /etc/app/deploy.key: the file holds a private key (line 1); it is baked into the image
    at: node.py:87
    hint: Deliver it at boot instead of baking it into the image, where every copy and every registry holding it exposes it. The built-in Secrets() delivery provisions only an SSH key: put the value in a Secret with a SecretFile or SecretEnv target under Secrets(delivery=ExternalDelivery(NAME)), naming what writes it at boot (a cloud secret store, a custom unit). If it is not a secret, pass allow_secret=True to the declaration.
error secret-in-file [dev] /etc/app/deploy.key: the file holds a private key (line 1); it is baked into the image
    at: node.py:87
    hint: Deliver it at boot instead of baking it into the image, where every copy and every registry holding it exposes it. The built-in Secrets() delivery provisions only an SSH key: put the value in a Secret with a SecretFile or SecretEnv target under Secrets(delivery=ExternalDelivery(NAME)), naming what writes it at boot (a cloud secret store, a custom unit). If it is not a secret, pass allow_secret=True to the declaration.
2 errors, 0 warnings, 0 infos
[exit 1]
```

The message names the kind of credential and its line in the file, never the value; `at:` is the recipe line that declares the `File`. A real key reaches the VM at boot instead, as a `SecretFile` of a `Secrets` whose `ExternalDelivery` writes it (the built-in delivery writes only the SSH key); `allow_secret=True` on the `File` is for a value that only looks like one (see [Concepts: Secrets never belong in the image](concepts.md#secrets-never-belong-in-the-image)). Remove the `File`, the import and `deploy.key` before going on.

## 4. Add a variant

`dev` adds root login, which you never ship. For a variant that is the shipped image plus `strace`, add `Fragment` to the `tundravm` imports and a third entry to `variants`:

```python
        Variant("debug", inherits="default", add=Fragment("debug", items=(Package("strace"),))),
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

$ tundravm compile node.py --tree mkosi
compiled mkosi
  variants:      default, dev, debug
  recipe_digest: 4e7d7ab0b907b51d6834bc97d5976d47f086c9e8650b7ac71f0edec879db426a
  tree_digest:   863d2589314f04de07c3198c7759c544b0733b68fe2d3eee005e9d650174064a
[exit 0]
```

The lock from step 1 has no `debug` variant: until this `lock`, `lint` reports each of its sections as a `lock-added` error (`variants.debug.packages is not in the lock`, ...). The lockfile (version 5) records a digest per recipe section (the distribution, the compiler, each variant's packages, files, kernel, debloat and the rest), the build `toolchain` (the tundravm and measurement tool versions, and with a real backend mkosi and systemd), and, for a recipe with source builds, the commit each one resolved to. `compile`, `diff` and `bake` read `build/tundravm.lock` when it exists, so lock before you compile: a recipe with source builds compiles its pins. `recipe_digest` is the digest the lockfile records and `tree_digest` the digest of the emitted tree. `mkosi/` holds one directory per variant (`mkosi.conf`, `mkosi.extra/`, `mkosi.skeleton/`, `scripts/`). Commit it: it is the reviewable form of the image.

## 6. Iterate with watch

`watch` keeps the edit loop to one line per save. Start it in a second terminal, set `APP_PORT = 80` in `node.py`, then `APP_PORT = 9090`, then press Ctrl-C:

```console
$ tundravm watch node.py
watching node.py every 1s (Ctrl-C stops)
05:09:25 lint 0 errors 0 warnings; tree up to date
05:09:29 lint 3 errors (app-privileged-port: app runs as user app and cannot bind port 80)
05:09:33 lint 3 errors 0 warnings; lock drifted (3 sections); tree stale (6 files)
^Cstopped
[exit 0]
```

- The first line is the check at start: `mkosi/` (the table's `tree`) matches what step 5 compiled.
- Port 80 trips `App`'s `app-privileged-port` check in all three variants. An error-level fragment check keeps the recipe from lowering, so the line names the first error instead of the counts and checks no tree; `watch` keeps polling.
- Port 9090 passes the check. The three errors left are lockfile drift, one `variants.<variant>.files` section per variant (`lock drifted (3 sections)`), and six files of `mkosi/` are stale: per variant, `app.conf` and the firewall's `/etc/nftables.conf`, which now opens 9090 instead of 8080. `watch` never writes the tree or the lock; step 7 shows the diff, locks again and recompiles.
- Ctrl-C prints `stopped` and exits 0.

`watch` polls `node.py` and every `.py` beside it, such as the `fragments.py` of step 14, the files the recipe reads, the lockfile, `pyproject.toml` and the tree, every second (`--interval` changes that), and re-imports the modules the recipe takes from its directory before each check. Its counts are `lint`'s, lockfile drift included, so a `tundravm lock` in another terminal clears them. `--write` recompiles a stale tree on each change instead of reporting it. See [CLI: Watch](cli.md#watch).

To see the whole image rather than one line, run `tundravm preview` instead: it serves the [explore](cli.md#explore) page on localhost, re-renders it on the same changes `watch` polls for and the open page reloads itself, keeping the last good page under an error banner while the recipe does not load. See [CLI: Preview](cli.md#preview).

```console
$ tundravm preview node.py
previewing node.py at http://127.0.0.1:8765/ (Ctrl-C stops)
05:09:56 v1 lint 0 errors 0 warnings
```

Each re-render prints a line with the page's version. The page's variant table lists each variant's fragments (`tee-base, network, app` for `default`), and every declaration on it names where it came from: the chrony configuration, the journald and sysctl drop-ins, the resolver, the masked units and the firewall's `/etc/nftables.conf` all read `declared at node.py:84 in common via tee-base (TeeBase)`, the line of `TeeBase()` in `items`, and the networkd configuration reads `via tee-base (TeeBase) > network (Network)`, the fragment `TeeBase` nests.

## 7. Change the recipe

Step 6 left `APP_PORT = 9090` in the recipe. Ask what changed:

```console
$ tundravm diff node.py --tree mkosi --variant default
diff --git a/default/mkosi.extra/etc/app/app.conf b/default/mkosi.extra/etc/app/app.conf
--- a/default/mkosi.extra/etc/app/app.conf
+++ b/default/mkosi.extra/etc/app/app.conf
@@ -1,2 +1,2 @@
 version=0.1.0
-listen=0.0.0.0:8080
+listen=0.0.0.0:9090
diff --git a/default/mkosi.extra/etc/nftables.conf b/default/mkosi.extra/etc/nftables.conf
--- a/default/mkosi.extra/etc/nftables.conf
+++ b/default/mkosi.extra/etc/nftables.conf
@@ -12,7 +12,7 @@
 		icmpv6 type { destination-unreachable, packet-too-big, time-exceeded, parameter-problem, nd-router-advert, nd-neighbor-solicit, nd-neighbor-advert } accept
 		udp sport 67 udp dport 68 accept comment "DHCPv4 replies"
 		ip6 saddr fe80::/10 udp sport 547 udp dport 546 accept comment "DHCPv6 replies"
-		tcp dport 8080 accept comment "app.service"
+		tcp dport 9090 accept comment "app.service"
 	}
 	chain forward {
 		type filter hook forward priority filter; policy drop;
[exit 1]

$ tundravm compile node.py --tree mkosi --check
M  debug/mkosi.extra/etc/app/app.conf
M  debug/mkosi.extra/etc/nftables.conf
M  default/mkosi.extra/etc/app/app.conf
M  default/mkosi.extra/etc/nftables.conf
M  dev/mkosi.extra/etc/app/app.conf
M  dev/mkosi.extra/etc/nftables.conf
6 files changed
[exit 1]
```

`diff` and `compile --check` exit 1 when the tree is stale. That is the CI gate for a committed tree. The lockfile drifted too, section by section:

```console
$ tundravm lock node.py --check
~ variants.debug.files: ~/etc/app/app.conf ~/etc/nftables.conf
~ variants.default.files: ~/etc/app/app.conf ~/etc/nftables.conf
~ variants.dev.files: ~/etc/app/app.conf ~/etc/nftables.conf
[exit 1]

$ tundravm lock node.py --check --variant default
~ variants.default.files: ~/etc/app/app.conf ~/etc/nftables.conf
[exit 1]

$ tundravm lock node.py
locked build/tundravm.lock
[exit 0]

$ tundravm lock node.py --check
lock is up to date
[exit 0]

$ tundravm compile node.py --tree mkosi
compiled mkosi
  variants:      default, dev, debug
  recipe_digest: e79b8c583a5910d662abeba6e0003f0f6d9a08e1e55d917594a21b0f44308a7a
  tree_digest:   61fc56c610b68658b9d12321587eee17c4e636585221983681c6bdf73cc34c9a
[exit 0]
```

Each line names a section, `variants.<variant>.<key>`, and what changed in it. `--variant` limits the check to those variants' sections (and the recipe-wide ones), so one lockfile of every variant serves any selection. Re-locking keeps existing source pins; `tundravm lock node.py --update NAME` resolves one source again.

## 8. Bake

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
next: tundravm deploy qemu --variant debug
[exit 0]
```

Progress lines go to stderr and the summary to stdout. The bake was frozen against `build/tundravm.lock`: had the recipe drifted from it, the `verify lockfile` step would have failed with `E_LOCKFILE`, and without the file, which the table configures, `bake` would have refused before `lint`, with `E_LOCKFILE` too. The backend came from `node.py`; `--backend lima|nix|local|inprocess` overrides it. The manifest `build/bake-result.json` records every artifact with its digest and, per variant, the recipe digest and the digest of that variant's directory of the tree. A later bake of some variants (`--variant`, or the release of step 19) replaces only their entries: the others keep theirs, and `status` calls them stale once the recipe moves. The `next:` line leaves out `--build build`, since the build directory came from the table.

## 9. Prove it is reproducible

```console
$ tundravm bake node.py --verify-reproducible -q
variant  target  artifact                  size   sha256        time
debug    qemu    build/debug/disk.qcow2    112 B  b50a5a5aeaac  0.0s
default  qemu    build/default/disk.qcow2  114 B  1fa043adea90  0.0s
dev      qemu    build/dev/disk.qcow2      110 B  9cfb8b5c2f31  0.0s
reproducible: yes (3 artifacts match a second build)
next: tundravm deploy qemu --variant debug
[exit 0]
```

`--verify-reproducible` bakes the same variants a second time into `build/.reproduce`, from the same lockfile and source checkouts, and compares every artifact's sha256 with the first build's (`-q` hides the progress of both bakes). All three match, so the second build is removed; `bake-result.json` records the outcome as `declarative.reproducibility` (state `matched`), for the bake and for each variant it built, and `tundravm status` shows `reproducibility matched` on each artifact line. A mismatch keeps `build/.reproduce` and fails with `E_REPRODUCIBILITY`, a table of the artifacts that differ and a hint naming `tundravm diff` and the usual causes (see [CLI: Bake](cli.md#bake)). The in-process backend is deterministic, so here the check only exercises the pipeline. With a real backend the second build runs mkosi again from scratch, which catches timestamps, build ids and packages installed without an archive snapshot (`Debian(snapshot=...)`; see [Reproducibility](reproducibility.md)).

## 10. Measure

```console
$ tundravm measure --variant default
error [E_MEASUREMENT]: Refusing to measure a simulated artifact.
Hint: Bake with a real backend, or pass allow_placeholder=True for test values.
  variant: default
  path: build/default/disk.qcow2
[exit 2]

$ tundravm measure --variant default --allow-placeholder
PLACEHOLDER: not real measurements. These values are derived from artifact digests; never put them in an attestation policy.
measurements default (qemu)
source: placeholder (build/default/disk.qcow2)
  RTMR1  21be70e83c1c6600f1f9fc1c08a09c54aaceb74e0a4d2ad0151e113f4ed0a47d
  RTMR2  5eadb91e4bb32aee2c8d3c296877128ef2af386be007ffa17b6b08b37e23bb2d
  MRTD   unverifiable: QEMU: depends on the host's TDVF/OVMF build, its ACPI tables and the VM's memory layout; take it from a quote of a host you trust
  RTMR0  unverifiable: QEMU: depends on the host's TDVF/OVMF build, its ACPI tables and the VM's memory layout; take it from a quote of a host you trust
[exit 0]
```

`measure` reads the bake in the build directory (`--build`, here the table's `build`) and first checks the artifact against the sha256 the bake recorded: a disk file changed since the bake is `E_ARTIFACT_CHANGED`. With a real bake the registers are replayed from the UKI offline and the `source:` line names `tundravm-measure` instead of `placeholder`. The default scheme, `qemu`, predicts RTMR1 and RTMR2; MRTD and RTMR0 depend on the host's firmware, so they are listed as unverifiable with the reason. `--scheme` picks another verifier's view of the same UKI (see [Policy: Schemes](policy.md#schemes)): `gcp` adds every MRTD/RTMR0 Google's firmwares produce, `portable` keeps only RTMR1/RTMR2, leaving the firmware registers to the platform's own verifier, and `azure` predicts no TDX register. A cloud scheme needs that cloud's output:

```console
$ tundravm measure --variant default --allow-placeholder --scheme portable
PLACEHOLDER: not real measurements. These values are derived from artifact digests; never put them in an attestation policy.
measurements default (portable)
source: placeholder (build/default/disk.qcow2)
  RTMR1  62b35ad438c5fb8c5a389a03c77a893928b576e70a836ac55573aa91828e39f3
  RTMR2  59f9281c924ffaf9272e96da67ad32f738c34500abf9f9c15fc51a8db9068a5e
  MRTD   unverifiable: portable: firmware register, left to the platform's own verifier
  RTMR0  unverifiable: portable: firmware register, left to the platform's own verifier
[exit 0]

$ tundravm measure --variant default --allow-placeholder --scheme gcp
error [E_STATE]: No baked artifact for default/gcp.
Hint: Baked: debug/qemu, default/qemu, dev/qemu. Bake that variant and target first.
[exit 2]
```

To ship the same image to the clouds, give the variant `outputs=("qemu", "azure", "gcp")`: it is still one UKI (the Azure and GCP glue only runs on its cloud, `ConditionVirtualization=`), so each scheme's policy comes from that one UKI and covers all three disks, and `measure` says `one UKI, 3 outputs: azure, gcp, qemu`.

A verifier, an image that checks its peers' quotes, needs these values as its expected measurements. `--export-policy` writes them to a policy file:

```console
$ tundravm measure --variant default --allow-placeholder --export-policy peer.json
PLACEHOLDER: not real measurements. These values are derived from artifact digests; never put them in an attestation policy.
wrote policy peer.json
measurements default (qemu)
source: placeholder (build/default/disk.qcow2)
  RTMR1  21be70e83c1c6600f1f9fc1c08a09c54aaceb74e0a4d2ad0151e113f4ed0a47d
  RTMR2  5eadb91e4bb32aee2c8d3c296877128ef2af386be007ffa17b6b08b37e23bb2d
  MRTD   unverifiable: QEMU: depends on the host's TDVF/OVMF build, its ACPI tables and the VM's memory layout; take it from a quote of a host you trust
  RTMR0  unverifiable: QEMU: depends on the host's TDVF/OVMF build, its ACPI tables and the VM's memory layout; take it from a quote of a host you trust
[exit 0]

$ cat peer.json
{
  "artifact": {
    "path": "build/default/disk.qcow2",
    "sha256": "1fa043adea909e9ad008e0db46ed1c9935adf90aee9c49d20940d6316c5c2e79"
  },
  "boot_payload": {
    "path": "build/default/disk.efi",
    "sha256": "227533ae07e66ae91bac62c26b5a28ca329cdc6233143e06eb421b291dea1a27"
  },
  "note": "PLACEHOLDER: these registers are derived from artifact digests, not measured; no TEE reproduces them. Replace this file with `tundravm measure --export-policy` output from a real bake before trusting a verifier built from it.",
  "registers": {
    "RTMR1": [
      "21be70e83c1c6600f1f9fc1c08a09c54aaceb74e0a4d2ad0151e113f4ed0a47d"
    ],
    "RTMR2": [
      "5eadb91e4bb32aee2c8d3c296877128ef2af386be007ffa17b6b08b37e23bb2d"
    ]
  },
  "schema": "tundravm.policy/2",
  "schema_version": 2,
  "scheme": "qemu",
  "tool": "placeholder",
  "tool_version": null,
  "unverifiable": {
    "MRTD": "QEMU: depends on the host's TDVF/OVMF build, its ACPI tables and the VM's memory layout; take it from a quote of a host you trust",
    "RTMR0": "QEMU: depends on the host's TDVF/OVMF build, its ACPI tables and the VM's memory layout; take it from a quote of a host you trust"
  }
}
```

Each register holds a list of accepted values: a quote matches when it holds any of them (a `gcp` policy accepts one MRTD per Google firmware). Placeholder values are written only with `--allow-placeholder`, and then with the `note`. `Tdxs.from_policy()` turns the file into the verifier fragment, renaming the registers to the keys the tundra-tools validator reads:

```pycon
>>> from tundravm.declarative.utils import Tdxs
>>> verifier = Tdxs.from_policy("peer.json", validator="tdx", allow_placeholder=True)
>>> verifier.validator
'tdx'
>>> dict(verifier.expected_measurements)
{'rtmr1': '21be70e83c1c6600f1f9fc1c08a09c54aaceb74e0a4d2ad0151e113f4ed0a47d', 'rtmr2': '5eadb91e4bb32aee2c8d3c296877128ef2af386be007ffa17b6b08b37e23bb2d'}
>>> Tdxs.from_policy("peer.json", validator="tdx")
Traceback (most recent call last):
  ...
tundravm.errors.MeasurementError: Refusing to build a verifier from a placeholder policy.
Hint: Export the policy from a real measurement, or pass allow_placeholder=True for a test image.
  policy: peer.json
```

A policy exported from a real measurement needs no `allow_placeholder`. A `qemu` policy lists MRTD as unverifiable, and the validator checks only the registers it is given, so pass `mrtd="<hex>"` to have it checked too. The verifier is a fragment like any other, so it goes in a variant's `add` (`Variant("verifier", inherits="default", add=verifier)`); `examples/06_attestation.py` builds one from `examples/peer.policy.json`. `"peer.json"` is relative to the working directory: in a recipe, name it next to the recipe file with `Resources.from_module(__name__).file("peer.json")` (see [Concepts: Paths and assets](concepts.md#paths-and-assets)).

Once the image runs, `tundravm attest` checks it against the policy: it asks the image's `tdxs` issuer for a quote bound to a fresh nonce and compares the quote's registers with the policy's. It refuses a placeholder policy before contacting anything:

```console
$ tundravm attest --issuer unix:./tdxs.sock --policy peer.json
error [E_MEASUREMENT]: Refusing to attest against a placeholder policy.
Hint: Export the policy from a real measurement: `tundravm measure --scheme SCHEME --export-policy FILE` on a real bake.
  policy: peer.json
[exit 2]
```

With a policy from a real measurement and the image running on a TDX host with the `tdxs` service (`Tdxs()` in the variant; the `prover` template has it), relay the service's socket over SSH and attest. The key the image's secret delivery installs carries `no-port-forwarding`, so `ssh -L` with it is refused; a command channel is not, so each connection runs `socat` in the image ([CLI: Real TDX](cli.md#real-tdx) has the whole sequence, including a second account for `ssh -L`). This was not run here, since it needs a TDX VM:

```bash
socat UNIX-LISTEN:./tdxs.sock,fork EXEC:'ssh -p 2222 root@localhost socat - UNIX-CONNECT\:/var/tdxs.sock' &
tundravm attest --issuer unix:./tdxs.sock --validator unix:./validator.sock --policy peer.json
```

`--validator` points at a `tdxs` validator on your side (not in the image), which checks the quote's signature, certificate chain and collateral. It prints the signature status, the nonce check and one `match`, `mismatch` or `unchecked` line per register (MRTD, RTMR0..RTMR3), then `verdict: trusted` (exit 0), `verdict: partial` (exit 1: nothing disagreed, but a `qemu` policy cannot predict MRTD and RTMR0; approve them from a quote of a host you trust with `measure --export-policy peer.json --approve-mrtd HEX --approve-rtmr0 HEX`) or `verdict: untrusted` (exit 1); [CLI: Attest](cli.md#attest) shows the output. Without `--validator` the signature is unchecked and the verdict is only `measurements-match` (exit 0) or `measurements-mismatch` (exit 1), never `trusted`.

## 11. What is in the image

```console
$ tundravm sbom --variant default --format text
sbom default
  variant          default
  base             debian/trixie
  arch             x86_64
  snapshot         -
  mirror           -
  recipe digest    e79b8c583a5910d662abeba6e0003f0f6d9a08e1e55d917594a21b0f44308a7a
  tree digest      541ba0951ea6b4e9cf071c27067eeb903ad4593fe62e6df4f5c7beadc4aee586
  artifact         default/disk.qcow2
  artifact sha256  1fa043adea909e9ad008e0db46ed1c9935adf90aee9c49d20940d6316c5c2e79
  manifest         default/default.manifest (missing)
  bound            yes
  installed        no (declared packages)
  tundravm         0.1.0

packages (9)
  NAME               VERSION  ARCH   ORIGIN
  ca-certificates    -        amd64  declared
  chrony             -        amd64  declared
  kmod               -        amd64  declared
  linux-image-amd64  -        amd64  declared
  nftables           -        amd64  declared
  systemd            -        amd64  declared
  systemd-boot-efi   -        amd64  declared
  systemd-sysv       -        amd64  declared
  udev               -        amd64  declared

sources (0)
note: the artifact is simulated (in-process backend): nothing was installed
note: no mkosi package manifest at default/default.manifest: listing the packages the recipe declares, without versions
[exit 0]
```

`sbom` merges three records of one baked variant: mkosi's package manifest beside the artifact, the lockfile's source pins, and the recipe metadata and digests from `bake-result.json` (`tree digest` is the digest of the variant's own directory of the tree, which the bake records per variant, so it is not `compile`'s `tree_digest` of every variant). Paths are relative to the build directory, so moving `build/` gives the same document. `bound  yes` says the document describes this bake: the artifact still hashes to the sha256 the bake recorded, and the lockfile (and a manifest, when there is one) is the one the bake read; otherwise `sbom` fails with `E_ARTIFACT_CHANGED`. The in-process bake installed nothing, so there is no manifest: the notes say so, `installed  no` says the table lists the packages the recipe declares, without versions. After a real bake the table lists every installed package with the version and architecture from mkosi's manifest, `sources` lists each source build, built kernel and `EfiStub` package with its pin, and `snapshot` names the archive snapshot when the recipe pins one (the starter does not). Once the recipe ships `/usr/bin/app` as a `GoBinary` or a `Cargo` build, `--binaries DIR` (a directory holding the built binaries) adds the Go modules or `cargo auditable` crates embedded in each one as components of that build, provided the binary's sha256 is the one the bake recorded from the UKI; any other binary is listed as unbound and the document is marked incomplete. See [CLI: SBOM](cli.md#sbom).

The default format is SPDX 2.3 JSON, and `--output` writes it to a file to publish next to `peer.json`:

```console
$ tundravm sbom --variant default --output build/default.spdx.json
note: the artifact is simulated (in-process backend): nothing was installed
note: no mkosi package manifest at default/default.manifest: listing the packages the recipe declares, without versions
wrote spdx-json build/default.spdx.json
[exit 0]
```

`--format cyclonedx-json` writes CycloneDX 1.5 instead. Every list is sorted and the ids derive from the content, so with `SOURCE_DATE_EPOCH` set the same bake gives the same document.

## 12. Package the evidence

The lockfile, step 9's reproducibility check, the SBOM and, after a real bake, the measurements policy each answer one question about the bake. `evidence` gathers them into one record that someone without your machine can check:

```console
$ SOURCE_DATE_EPOCH=1700000000 tundravm evidence node.py --variant default --bundle evidence.tar.gz --html report.html
wrote build/evidence
wrote evidence.tar.gz
wrote report.html
evidence 2023-11-14T22:13:20Z  verdict pass
  recipe     node.py  digest e79b8c583a59  matches
  tree       matches
  integrity  verified
  lock       current
  reproduce  matched
  toolchain  measure.gcp-firmware-table dstack-mr-gcp@b16e08b, measure.tundravm-measure 1.0+attest-12f1e29.dstack-mr-gcp-b16e08b, tundravm 0.1.0+0db3f45de51b
  policy     missing
  measured   none
  sbom       declared
  attested   none
  artifacts  simulated
  lint       clean
  required   reproducible, lock
  default/qemu  build/default/disk.qcow2  sha256 1fa043adea90  verified
members:
  73f9008f1a2e5d13a419ba4ebace02c71bd8e1dfa8d6c7823687c6c7618a0f5f  bake-result.json
  37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570  lint.json
  c2af36fcb02e1da306e37839be192bb20b7e2f070480e32bd798950778796777  tundravm.lock
  6357dfe39c50c974001024d2d0d7557d926e2f0b4cb3c301438b6568f85c2378  variants/default/sbom-qemu.spdx.json
  84d25f9f03004cbdbbb17ea2f01a692848aa53d2120eed437400c4e7466fde10  variants/default/tree.json
note: default/qemu sbom: the artifact is simulated (in-process backend): nothing was installed
note: default/qemu sbom: no mkosi package manifest at default/default.manifest: listing the packages the recipe declares, without versions
note: no measurements policy for variant default (build/default/policy.json does not exist); write one with `tundravm measure --build build --variant default --export-policy build/default/policy.json`
note: the artifacts are simulated (in-process backend): nothing was built
[exit 0]
```

`evidence` builds nothing. It reads `build/bake-result.json`, checks that the recipe and its compiled tree are the ones the bake recorded (`recipe`, `tree`), hashes each artifact again against the sha256 the bake recorded (`integrity`), compares the lockfile with the recipe (`lock`), reads back the outcome step 9 recorded (`reproduce`), names the toolchain the bake recorded (`toolchain`; a real bake adds mkosi and systemd) and lints the recipe with the lock's pins (`lint`). A contradiction makes the verdict `fail`, with one line per reason under `reasons:`, and exits 1: append one byte to `build/default/disk.qcow2` and the summary reads `integrity  mismatch` and the reason `integrity-mismatch  default/qemu: build/default/disk.qcow2 changed since the bake`. What the bake must hold is the project's requirement set, `required  reproducible, lock`: the `[tool.tundravm]` table's `require`, else those two, the same set `status` judges release readiness against and `release` requires. A required check that is missing makes the verdict `incomplete` (exit 1); here both are present. What is absent and not required is a note, here the policy and real artifacts; `--require measured,real` makes those required instead (the flag replaces the set, and `--require ''` requires nothing), as the CI workflow of step 16 does with `reproducible,lock` (plus `real` for a real backend).

Step 10's placeholder policy is not evidence: `--policy peer.json` adds it as `variants/default/policy.json`, the summary reads `policy  placeholder`, and the verdict fails with `policy-placeholder  default: peer.json holds placeholder registers`. Without the flag, `evidence` takes `build/default/policy.json` when it exists, so after a real bake export the policy there (`measure --variant default --export-policy build/default/policy.json`).

`build/evidence/` holds the files under `members`, indexed with their sha256 by `evidence.json`; `tree.json` lists every file of the variant's compiled tree, which step 17 compares a later recipe against. `evidence.tar.gz` is what you hand a reviewer. Its members are sorted, owned by `0:0` and stamped with `SOURCE_DATE_EPOCH`, so packing the same bake again gives the same bytes. `report.html` is the same record as one self-contained page, opening with a pass/fail banner. See [CLI: Evidence](cli.md#evidence).

The reviewer checks the bundle with `verify`, which needs neither the recipe nor your build directory: every member against the index, the index against its members, the recorded verdict recomputed from the recorded facts and, with `--build DIR`, the artifacts hashed again:

```console
$ tundravm verify evidence.tar.gz --build build
verify evidence.tar.gz  verdict pass  (19 pass, 0 fail, 0 missing)
  evidence   2023-11-14T22:13:20Z  recipe node  recorded verdict pass
  member     5/5 pass
  index      11/11 pass
  artifact   1/1 pass
  verdict    2/2 pass
note: unsigned: the evidence carries no evidence.json.sig
[exit 0]
```

A member, index entry or artifact that does not match fails its check and `verify` exits 1. The note says the bundle is unsigned, so nothing says who made it; step 17 signs it. See [CLI: Verify](cli.md#verify).

## 13. Deploy

```console
$ tundravm deploy qemu --variant default
error [E_DEPLOYMENT]: Refusing to deploy a simulated artifact.
Hint: Bake with a real backend first, or pass allow_simulated=True for a test run.
  variant: default
  path: build/default/disk.qcow2
[exit 2]

$ tundravm deploy qemu --variant default --allow-simulated-artifact
error [E_DEPLOYMENT]: QEMU binary not found: qemu-system-x86_64
Hint: Install QEMU and ensure it is in PATH.
  binary: qemu-system-x86_64
[exit 2]
```

On a host with QEMU and a real bake, `deploy` boots the baked image (a UKI, on OVMF firmware) in the background and prints the deployment id, its `ssh://localhost:PORT` endpoint, and the `serial_log`, `monitor` socket and `pidfile` it left in `build/default/`; `--attach` keeps QEMU in the foreground with its console on the terminal instead. Target settings are `--param KEY=VALUE` (`memory`, `cpus`, `ssh_port`, `tdx`, `daemonize`, `forward=HOST:GUEST,...` and `firmware=PATH`, a TDVF or OVMF image to boot instead of the first one installed, for qemu). [CLI: Deploying to each target](cli.md#deploying-to-each-target) has the whole sequence for QEMU, Azure and GCP, including how to check the VM and tear it down.

## 14. Write a fragment

Say every image that runs the app should also probe it once a minute. Write that as a component, like `App`, in `fragments.py` next to the recipe:

```python
from collections.abc import Iterator

from tundravm import Declaration, Every, Package, Task, component
from tundravm.declarative.utils import Composite, HttpEndpoint


@component(name="healthcheck")
class HealthCheck(Composite):
    """A scheduled task that probes *target* once a minute. Fragment name: ``healthcheck``."""

    target: HttpEndpoint

    def compose(self) -> Iterator[Declaration]:
        yield Package("curl")
        yield Task(
            "app-healthcheck",
            f"/usr/bin/curl -fsS -o /dev/null {self.target.url}",
            description="Probe the app",
            schedule=Every("1min", first="1min"),
        )
```

- `HealthCheck(target=app.http)` is itself a `Fragment`: it goes in `Recipe.items`, a variant's `add`, or another fragment's `items`. `compose()` runs once, at construction. `name=` names the fragment; without it, it would be `health-check`, after the class.
- `target` is the `HttpEndpoint` `App` exports, so the probe reads the port from the app instead of parsing `/etc/app/app.conf`: the two cannot disagree. Subclassing `Composite` lets a type checker see `HealthCheck(...)` as a `Fragment`.
- Holding an export makes the component require its provider: without `App` in the variant, lint reports `fragment-requires-missing`. `@component(requires=(...))` names other fragments a component needs, and a `check(self, resolved)` method, like `App.check`, is a lint rule.
- `Task(..., schedule=Every("1min", first="1min"))` renders `app-healthcheck.service` (a oneshot, sandboxed, as its own system user, not enabled) and `app-healthcheck.timer` (`OnBootSec=1min`, `OnUnitActiveSec=1min`, enabled into `timers.target`), so the probe runs only on its schedule.

Use it in `node.py`: add `from fragments import HealthCheck` after the tundravm imports, bind `app = App(version=APP_VERSION, port=APP_PORT)` above the recipe, and put `app,` and `HealthCheck(target=app.http),` in `items` in place of `App(...)`. Without `app` in `items`, lint names the missing provider:

```console
$ tundravm lint node.py --variant default
error fragment-requires-missing [default] healthcheck: fragment 'healthcheck' requires fragment 'app', which this variant does not include
    at: node.py:90
1 error, 0 warnings, 0 infos
[exit 1]
```

With `app` back in `items` that error is gone, and what `lint` reports is drift: it checks the recipe against the lockfile the table configures, which step 7 wrote before the probe existed:

```console
$ tundravm lint node.py --variant default
error lock-changed [common] variants.default.files: variants.default.files changed since the lock: +/usr/lib/systemd/system/app-healthcheck…
error lock-changed [common] variants.default.packages: variants.default.packages changed since the lock: +curl
error lock-changed [common] variants.default.services: variants.default.services changed since the lock: +app-healthcheck +app-healthcheck.timer
error lock-changed [common] variants.default.users: variants.default.users changed since the lock: +app-healthcheck
4 errors, 0 warnings, 0 infos
[exit 1]

$ tundravm inspect node.py --variant default | grep -E "Fragments|Units|app-healthcheck "
Fragments (4): tee-base network app healthcheck
  app-healthcheck  system  shell=/usr/sbin/nologin
Units: enable chrony.service, systemd-networkd.service, systemd-networkd.socket, systemd-networkd-wait-online.service, app-healthcheck.timer, nftables.service; mask apt-daily.timer, apt-daily-upgrade.timer, dpkg-db-backup.timer, getty@tty1.service
  app-healthcheck  /usr/bin/curl -fsS -o /dev/null http://127.0.0.1:9090/  restart=no  type=oneshot  user=app-healthcheck  disabled

$ tundravm diff node.py --tree mkosi --variant default --stat
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

$ tundravm compile node.py --tree mkosi
compiled mkosi
  variants:      default, dev, debug
  recipe_digest: ad93886075725ba0357cdc4612c4962204abbe62be2c60843ddaf28208036c1d
  tree_digest:   165af2e82e50a6009c013be72f5b97c2ffc08fea9e6b57da27964c770610056b
[exit 0]
```

`mkosi.conf` gained `curl`, and `06-postinst.sh` creates the probe's system user and enables its timer. The recipe directory is importable while the recipe loads, so `fragments.py` needs no packaging. The [fragment guide](module-authoring.md) covers builds, units and ordering in depth.

## 15. Test it

`tests/test_node.py`, which `init` wrote, has three tests:

- `test_lints_clean`: `assert_clean(RECIPE)`, so a warning fails it too;
- `test_compiles_every_variant`: every variant compiles, and the variants are the ones it names (step 4);
- `test_matches_committed_tree`: `mkosi/` is what the recipe compiles to with `build/tundravm.lock`. After an intended change, `tundravm compile node.py --tree mkosi` (or `TUNDRAVM_UPDATE_GOLDEN=1 uv run pytest tests`) rewrites it.

Add tests for the fragment in `tests/test_healthcheck.py`. `tundravm.testing` works on the same values the lifecycle returns:

```python
"""Tests for the HealthCheck fragment in fragments.py."""

from fragments import HealthCheck
from node import App

from tundravm import Fragment, Package, Recipe, Task, lint
from tundravm.declarative.utils import Network
from tundravm.testing import assert_clean, assert_diagnostic

APP = App(version="1", port=8080)


def image(*items: Fragment) -> Recipe:
    return Recipe(name="t", items=(Package("linux-image-amd64"), Network(), *items))


def test_probes_the_app() -> None:
    assert_clean(lint(image(APP, HealthCheck(target=APP.http))))


def test_needs_the_app() -> None:
    assert_diagnostic(lint(image(HealthCheck(target=APP.http))), "fragment-requires-missing")


def test_probes_the_port_the_app_exports() -> None:
    (probe,) = [item for item in HealthCheck(target=APP.http).items if isinstance(item, Task)]
    assert probe.command == "/usr/bin/curl -fsS -o /dev/null http://127.0.0.1:8080/"
```

- `image()` builds a one-variant recipe (`default`, for qemu) around the fragments under test, with the kernel package so `kernel-missing` stays quiet and `Network()` so `network-missing` does: the app's service declares a port, and lint warns when no interface is configured to reach it (the recipe gets `Network()` from `TeeBase()`).
- `assert_clean(lint(recipe))` fails on errors and warnings, whatever it is given (a recipe, `lint()` findings or a `fragment_plan(...)` image); `warnings="allow"` fails on errors only and `allow=("code",)` accepts some.
- `assert_diagnostic` returns the first diagnostic matching every field you give, or fails listing what was found.

The tests import `fragments` and `node` from the project directory, which the generated `pyproject.toml` puts on pytest's path:

```toml
[tool.pytest.ini_options]
pythonpath = ["."]
```

`uv run` runs them in the project's own environment, which `uv sync` fills from `pyproject.toml`: the pinned tundravm from GitHub and `pytest`. This tutorial ran against a local checkout of tundravm, so it pointed the project at that first:

```console
$ uv add --editable /home/me/tundra
Using CPython 3.12.3 interpreter at: /usr/bin/python3
Creating virtual environment at: .venv
Resolved 8 packages in 2ms
   Building tundravm @ file:///home/me/tundra
      Built tundravm @ file:///home/me/tundra
Prepared 1 package in 265ms
Installed 1 package in 1ms
 + tundravm==0.1.0 (from file:///home/me/tundra)
[exit 0]

$ uv run pytest -q tests
Installed 5 packages in 11ms
......                                                                   [100%]
6 passed in 0.13s
[exit 0]
```

The override records your checkout's path in `[tool.uv.sources]` and in `uv.lock`; keep it out of commits, since the path exists only on your machine.

These tests never boot anything. With a real bake, QEMU and OVMF, `tundravm.testing.boot()` boots the baked UKI and reads its serial console: `boot(uki, expect_units=("app",)).assert_booted()` fails unless the boot reaches `minimal.target` and `app.service` starts, and the `boot` pytest fixture skips the test on a host without QEMU or OVMF, like this one. See [Testing: Booting an image in tests](testing.md#booting-an-image-in-tests).

In a project of your own, the loop is: edit the recipe with `tundravm watch node.py` running, `tundravm compile node.py --tree mkosi` once its line says `tree stale`, `uv run pytest tests`, `tundravm lock node.py`.

## 16. CI

`tundravm ci` runs `lint --strict`, `compile --check` and `lock --check` and stops at the first failure:

```console
$ tundravm ci node.py --tree mkosi
ok lint: no findings
ok compile: mkosi is up to date
ok lock: build/tundravm.lock is up to date
[exit 0]
```

A failing step prints its report and `FAIL`, the remaining steps print `skip`, and the command exits 1. See [CLI: CI](cli.md#ci).

`.github/workflows/tundravm.yml`, which `init --ci github` wrote in step 1, runs on pushes to `main` and on pull requests. Its two jobs, with the setup steps (checkout, uv, mkosi, dependencies) cut (`# ...`):

```yaml
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      # ...
      - name: Summarize the image
        run: |
          uv run tundravm inspect node.py --format markdown >> "$GITHUB_STEP_SUMMARY"

      - name: Check recipe, compiled tree and lockfile
        run: uv run tundravm ci node.py --tree mkosi

  bake:
    needs: check
    if: vars.TUNDRAVM_BAKE_BACKEND != ''
    runs-on: ubuntu-latest
    steps:
      # ...
      - name: Fetch sources
        run: uv run tundravm fetch node.py --build build

      - name: Bake twice and compare
        run: >-
          uv run tundravm bake node.py --backend ${{ vars.TUNDRAVM_BAKE_BACKEND }}
          --build build --verify-reproducible

      - name: Collect evidence
        if: ${{ !cancelled() && hashFiles('build/bake-result.json') != '' }}
        run: >-
          uv run tundravm evidence node.py --build build
          --require ${{ vars.TUNDRAVM_BAKE_BACKEND == 'inprocess'
          && 'reproducible,lock' || 'reproducible,lock,real' }}
          --bundle evidence.tar.gz --html evidence.html

      - name: Upload evidence and artifacts
        if: ${{ !cancelled() && hashFiles('build/bake-result.json') != '' }}
        uses: actions/upload-artifact@v4
        with:
          name: tundravm-bake
          include-hidden-files: true
          path: |
            evidence.tar.gz
            evidence.html
            build/bake-result.json
            build/.reproduce/**/bake-result.json
            build/*/output/*
            build/*/*.qcow2
            build/*/*.vhd
            build/*/*.tar.gz
```

- `check` always runs. It posts `inspect --format markdown`, the dry run of step 2, to the job summary, then runs the gate above; its findings show inline on the pull request.
- `bake` is opt-in. Until the repository variable `TUNDRAVM_BAKE_BACKEND` (Settings > Secrets and variables > Actions > Variables) names a backend, `local` or `inprocess` (`lima` and `nix` need limactl or Nix, which `ubuntu-latest` lacks), the `if:` skips it. Once set, it runs after `check` passes: it installs mkosi, fetches sources, bakes twice with that backend as step 9 did, packs the evidence bundle and HTML report of step 12, requiring the reproducibility check and the lockfile (and real artifacts, unless the backend is `inprocess`), and uploads them with `bake-result.json` and the built images as the `tundravm-bake` artifact. A bake that fails after writing `bake-result.json` still has its evidence collected and uploaded.

Commit `mkosi/`, `build/tundravm.lock` and the workflow; `check` fails on a pull request whose recipe no longer matches either.

## 17. Review the next change

A reviewer of the next release asks two questions: is this evidence the one the last release shipped with, and what changed since? Step 14 changed the recipe after the bake of step 8, so bake again, then sign the evidence with an SSH key. Here a throwaway key outside the project stands in for the release key, with an allowed-signers file naming its owner:

```console
$ tundravm bake node.py --verify-reproducible -q
variant  target  artifact                  size   sha256        time
debug    qemu    build/debug/disk.qcow2    112 B  b50a5a5aeaac  0.0s
default  qemu    build/default/disk.qcow2  114 B  1fa043adea90  0.0s
dev      qemu    build/dev/disk.qcow2      110 B  9cfb8b5c2f31  0.0s
reproducible: yes (3 artifacts match a second build)
next: tundravm deploy qemu --variant debug
[exit 0]

$ ssh-keygen -q -t ed25519 -N '' -C review -f ../review-key
[exit 0]

$ echo "me@example.com namespaces=\"tundravm-evidence\" $(cat ../review-key.pub)" > ../allowed_signers
[exit 0]

$ SOURCE_DATE_EPOCH=1700000000 tundravm evidence node.py --variant default --sign ../review-key --bundle v1.tar.gz
wrote build/evidence
signed evidence.json with ../review-key: evidence.json.sig
wrote v1.tar.gz
evidence 2023-11-14T22:13:20Z  verdict pass
  recipe     node.py  digest ad9388607572  matches
  tree       matches
  integrity  verified
  lock       current
  reproduce  matched
  toolchain  measure.gcp-firmware-table dstack-mr-gcp@b16e08b, measure.tundravm-measure 1.0+attest-12f1e29.dstack-mr-gcp-b16e08b, tundravm 0.1.0+0db3f45de51b
  policy     missing
  measured   none
  sbom       declared
  attested   none
  artifacts  simulated
  lint       clean
  required   reproducible, lock
  default/qemu  build/default/disk.qcow2  sha256 1fa043adea90  verified
members:
  6bef3df1cced8afaba648e1322ce93c217bd3de41f1613802b458dc7dc4297b1  bake-result.json
  37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570  lint.json
  d89a7fc763f11bc0575f0d9dbbfa8b37fe03fe901c54a3e52bdb5c7f5be73a7d  tundravm.lock
  442414cfe9644ce61d48c5e8de44e37d3c667da07f99c306dd7617683368147c  variants/default/sbom-qemu.spdx.json
  87aff139bdf78beda8899a07e03cc98d48de586f87031452134824174d551c34  variants/default/tree.json
note: default/qemu sbom: the artifact is simulated (in-process backend): nothing was installed
note: default/qemu sbom: no mkosi package manifest at default/default.manifest: listing the packages the recipe declares, without versions
note: no measurements policy for variant default (build/default/policy.json does not exist); write one with `tundravm measure --build build --variant default --export-policy build/default/policy.json`
note: the artifacts are simulated (in-process backend): nothing was built
[exit 0]

$ tundravm verify v1.tar.gz --allowed-signers ../allowed_signers --build build
verify v1.tar.gz  verdict pass  (20 pass, 0 fail, 0 missing)
  evidence   2023-11-14T22:13:20Z  recipe node  recorded verdict pass
  member     5/5 pass
  index      11/11 pass
  artifact   1/1 pass
  signature  1/1 pass, signed by me@example.com
  verdict    2/2 pass
[exit 0]
```

`--sign` writes `evidence.json.sig` (`ssh-keygen -Y sign -n tundravm-evidence`) beside the index and into the bundle; the signature covers every member through the sha256 the index lists. `verify --allowed-signers` checks it and names the signer; a bundle whose index or members were edited after signing fails. Publish `v1.tar.gz` with the release.

Now the next change: set `APP_VERSION = "0.2.0"` in `node.py`, lock and compile, and ask what changed since `v1.tar.gz`, as markdown for the pull request:

```console
$ tundravm lock node.py
locked build/tundravm.lock
[exit 0]

$ tundravm compile node.py --tree mkosi
compiled mkosi
  variants:      default, dev, debug
  recipe_digest: 12c49c11c89d0da4eea89276637e898d7160cf91317e4ce0639eb81aea1fd9eb
  tree_digest:   ce1d27ac082a26a54c512d6730143893766e152f29c080d7f55fde516af0ed5f
[exit 0]

$ tundravm changes node.py --since v1.tar.gz --variant default --format markdown
## Changes since evidence `2023-11-14T22:13:20Z`

Recipe `node`, variants `default`: 6 changes.

| Section | Changes |
|---|---|
| Recipe and tree | 2 |
| Lockfile sections | 2 |
| Source pins | 0 |
| Declared packages | 0 |
| Installed packages | 0 |
| Emitted files | 2 |
| Measurements | 0 |

### Recipe and tree (2)

| Change | Subject | Before | After |
|---|---|---|---|
| changed | recipe digest | `ad9388607572` | `12c49c11c89d` |
| changed | default tree | `3e680be53604` | `a6588398ebc7` |

### Lockfile sections (2)

| Change | Section | Detail |
|---|---|---|
| changed | `variants.default.files` | `~/etc/app/app.conf` |
| changed | `variants.default.services` | `~app` |

### Emitted files (2)

| Change | Variant | File | What | Declared by |
|---|---|---|---|---|
| changed | `default` | `mkosi.extra/etc/app/app.conf` | content | File(extra, /etc/app/app.conf) (declared in app) |
| changed | `default` | `mkosi.extra/usr/lib/systemd/system/app.service` | content | Service(app.service) (declared in app) |

### Not compared (2)

| Variant | Section | Why |
|---|---|---|
| `default` | Installed packages | the evidence holds no installed inventory (no SBOM from a mkosi manifest) |
| `default` | Measurements | the evidence holds no valid measurements policy |
[exit 0]
```

`changes` reads the bundle and the recipe as it is now, and nothing from the network: the recipe and per-variant tree digests, the lockfile sections that drifted, source pins, the declared packages, the installed packages, every emitted file that changed with the declaration behind it, and the measured registers. The version bump touched two files of `default`, the config and the service's description, both from `App`. `--variant default` matches the bundle, which holds only `default`; without it `debug` and `dev` would be listed as added variants. Declared packages are compared with declared ones and installed with installed: the in-process bake left no installed inventory and there is no current bake to compare registers with, so both are listed as not compared rather than as removals. `--build build` would compare the installed packages and registers of a new bake of this recipe. The default format is text; `--format json` carries `"schema": "tundravm.changes/2"`. See [CLI: Changes](cli.md#changes).

## 18. Status and clean

`status` answers where the project stands, writing nothing and touching no network. Past the bake it also answers whether the project can ship:

```console
$ tundravm status
recipe    ok       node.py  digest 12c49c11c89d  variants default, dev, debug  dialect current  base debian/trixie
lint      ok       0 errors, 0 warnings, 0 infos
lock      ok       build/tundravm.lock  v5  up to date
source    n/a      no source builds or kernels
tree      ok       mkosi: up to date with the recipe
artifact  stale    default/qemu  build/default/disk.qcow2  114 B  sha256 1fa043adea90  integrity unchecked (--verify hashes it)  reproducibility matched  simulated  lock build/tundravm.lock  recipe and tree changed since the bake
artifact  stale    dev/qemu  build/dev/disk.qcow2  110 B  sha256 9cfb8b5c2f31  integrity unchecked (--verify hashes it)  reproducibility matched  simulated  lock build/tundravm.lock  recipe and tree changed since the bake
artifact  stale    debug/qemu  build/debug/disk.qcow2  112 B  sha256 b50a5a5aeaac  integrity unchecked (--verify hashes it)  reproducibility matched  simulated  lock build/tundravm.lock  recipe and tree changed since the bake
manifest  n/a      build/default/default.manifest  simulated: nothing was installed
manifest  n/a      build/dev/dev.manifest  simulated: nothing was installed
manifest  n/a      build/debug/debug.manifest  simulated: nothing was installed
backend   ok       inprocess: no host tools needed
release   blocked  default: blocked (recipe-mismatch, tree-mismatch, lock-unbound, sbom-unbound)  requires reproducible, lock
release   blocked  debug: blocked (recipe-mismatch, tree-mismatch, lock-unbound, sbom-unbound)  requires reproducible, lock
next: tundravm bake
[exit 0]
```

The recipe, lock and committed tree agree, but the version bump came after the bake, so every artifact is `stale` and `next` is the bake. The `release` lines say the same from the release's side, with the reason codes `evidence` would give: the bake no longer matches the recipe and its tree (`recipe-mismatch`, `tree-mismatch`), and the lockfile and the SBOMs' inputs changed since (`lock-unbound`, `sbom-unbound`). There is one per variant that can ship; `dev` has none. Readiness is judged against the project's requirement set, `reproducible, lock` unless the `[tool.tundravm]` table sets `require = [...]`; `tundravm config` prints it on its `require` line, and `evidence` and `release` require the same set when `--require` is omitted. Judging readiness hashes each artifact; `status` keeps those sha256s in `build/.tundravm-hashes.json`, keyed by path, size, mtime and inode, so the next `status` hashes only files that changed. `--verify` bypasses the cache and also shows, on each `artifact` line, whether the file still hashes to the sha256 the bake recorded. `next` leaves out what the `[tool.tundravm]` table supplies. See [CLI: Project status](cli.md#project-status).

`clean` removes the build directory's output by part. Without `--what` it lists what `--what all` would remove:

```console
$ tundravm clean
would remove build/mkosi
would remove build/debug
would remove build/default
would remove build/dev
would remove build/bake-result.json
would remove build/evidence
would remove build/.tundravm-hashes.json
pass --what all to remove them, or --what with some of sources, tree, artifacts, evidence, state, reproduce (the lockfile stays unless --remove-lockfile names it)
[exit 0]

$ tundravm clean --what all
removed build/mkosi
removed build/debug
removed build/default
removed build/dev
removed build/bake-result.json
removed build/evidence
removed build/.tundravm-hashes.json
[exit 0]

$ tundravm status
recipe    ok       node.py  digest 12c49c11c89d  variants default, dev, debug  dialect current  base debian/trixie
lint      ok       0 errors, 0 warnings, 0 infos
lock      ok       build/tundravm.lock  v5  up to date
source    n/a      no source builds or kernels
tree      ok       mkosi: up to date with the recipe
artifact  missing  default: not baked (no build/bake-result.json)
artifact  missing  dev: not baked (no build/bake-result.json)
artifact  missing  debug: not baked (no build/bake-result.json)
backend   ok       inprocess: no host tools needed
release   blocked  default: blocked (not-baked)  requires reproducible, lock
release   blocked  debug: blocked (not-baked)  requires reproducible, lock
next: tundravm bake
[exit 0]
```

`build/mkosi` is the bake's own copy of the tree; the committed `mkosi/` is never touched. `build/.tundravm-hashes.json`, the hash cache, goes with the `state` part. The lockfile stays unless `--remove-lockfile` names it, and so does `build/default.spdx.json` from step 11, which is no part `clean` knows. Nothing is baked now, so each `release` line is `blocked (not-baked)`.

## 19. Cut a release

A release is one directory holding the shipped artifacts with their policies, SBOMs, evidence and notes, cut from the locked recipe and immutable once it passes. `release` runs every step itself (lock check, fetch, a bake built twice and compared, measure, SBOM, evidence, verify, changes, notes), so it starts from the cleaned build directory. Require a check the in-process backend cannot pass, `real`, to see a release that does not finish:

```console
$ tundravm release --version 1.0 --require real
skip variant dev: Variant(release=False): not for release
[1/9] lock-check  done      0.0s  lock current (build/tundravm.lock), sha256 5f5ba608ef4d
[2/9] fetch       skipped   0.0s  in-process backend: nothing is built, nothing to fetch
[3/9] bake        done      0.1s  2 artifacts, reproducible: matched, simulated
[4/9] measure     skipped   0.0s  simulated artifacts: nothing to measure
[5/9] sbom        done      0.0s  2 SPDX documents, 21 packages
[6/9] evidence    done      0.2s  verdict incomplete
[7/9] verify      done      0.0s  verdict incomplete: 28/29 checks pass
[8/9] changes     skipped   0.0s  no earlier release of this recipe to compare with
[9/9] notes       done      0.0s  RELEASE.md, verdict fail
resume with: tundravm release --version 1.0 --require real
  evidence  simulated-artifact  debug/qemu, default/qemu: the artifacts are simulated (in-process backend): nothing was built (required: real)
  verify    simulated-artifact  debug/qemu, default/qemu: the artifacts are simulated (in-process backend): nothing was built (required: real)
release releases/node-1.0: fail (simulated)
[exit 1]
```

`dev` is skipped: it is `Variant(release=False)`, so only `default` and `debug` ship (`--variant dev --allow-ineligible` would force it, recorded in `release.json`). Every stage ran, but the evidence is `incomplete`: the artifacts are simulated and `real` was required. A release that fails keeps `release.state.json` in its directory, so it can resume, and the run ends with the command that does. `status` shows both:

```console
$ tundravm status
recipe    ok       node.py  digest 12c49c11c89d  variants default, dev, debug  dialect current  base debian/trixie
lint      ok       0 errors, 0 warnings, 0 infos
lock      ok       build/tundravm.lock  v5  up to date
source    n/a      no source builds or kernels
tree      ok       mkosi: up to date with the recipe
artifact  ok       default/qemu  build/default/disk.qcow2  114 B  sha256 1fa043adea90  integrity unchecked (--verify hashes it)  reproducibility matched  simulated  lock build/tundravm.lock
artifact  missing  dev: not in build/bake-result.json
artifact  ok       debug/qemu  build/debug/disk.qcow2  112 B  sha256 b50a5a5aeaac  integrity unchecked (--verify hashes it)  reproducibility matched  simulated  lock build/tundravm.lock
manifest  n/a      build/default/default.manifest  simulated: nothing was installed
manifest  n/a      build/debug/debug.manifest  simulated: nothing was installed
backend   ok       inprocess: no host tools needed
release   ok       default: ready  requires reproducible, lock
release   ok       debug: ready  requires reproducible, lock
release   pending  node-1.0: in progress at releases/node-1.0, verdict fail (simulated-artifact)  resume with: tundravm release --version 1.0
next: tundravm bake
[exit 0]
```

The release's own bake is current and was built twice, so `default` and `debug` are `ready` against `reproducible, lock`. The `pending` line is the unfinished release with its resume command, which carries no `--require`: a resume keeps what its release started with. `next` is still the bake because `dev` has not been baked since step 18 cleaned the build directory. The release bakes only what it ships; a bake of some variants keeps the entries other variants already have in `build/bake-result.json` (each with its own tree digest, so `status` calls one `stale` once its recipe moves), and here there were none.

Resuming as is would fail the same way, since the resume inherits `--require real`. Dropping a check weakens what the release accepts, so it is refused unless you say so:

```console
$ tundravm release --version 1.0 --require reproducible,lock
error [E_STATE]: Release node-1.0 was started with another specification: require ["real"] -> ["reproducible", "lock"].
Hint: Omit the option to keep what the release started with, or pass --change-spec (release(change_spec=True)) to change it; the change is recorded in release.json's spec_history.
  release: node-1.0
  changed: require
[exit 2]

$ tundravm release --version 1.0 --require reproducible,lock --change-spec
skip variant dev: Variant(release=False): not for release
[1/9] lock-check  kept            lock current (build/tundravm.lock), sha256 5f5ba608ef4d
[2/9] fetch       skipped         in-process backend: nothing is built, nothing to fetch
[3/9] bake        kept            2 artifacts, reproducible: matched, simulated
[4/9] measure     skipped         simulated artifacts: nothing to measure
[5/9] sbom        kept            2 SPDX documents, 21 packages
[6/9] evidence    done      0.2s  verdict pass
[7/9] verify      done      0.0s  verdict pass: 29/29 checks pass
[8/9] changes     skipped   0.0s  no earlier release of this recipe to compare with
[9/9] notes       done      0.0s  RELEASE.md, verdict pass
release releases/node-1.0: pass (simulated)
[exit 0]

$ jq .spec_history releases/node-1.0/release.json
[
  {
    "at": "2026-10-18T03:14:56Z",
    "changes": {
      "require": {
        "new": [
          "reproducible",
          "lock"
        ],
        "old": [
          "real"
        ]
      }
    }
  }
]
[exit 0]
```

With `--change-spec` the resumed run takes the new requirement. Stages whose inputs did not change are `kept`, and the first one whose inputs did, `evidence`, runs again with every stage after it. `release.json` records the change in `spec_history` with the old and new values, so a reviewer sees that `real` was dropped and when.

A finished release is self-contained. Copy it elsewhere (a sibling directory stands in for another machine here) and `verify` checks it without the recipe or the build directory: every file against `release-manifest.json`, `release.json` against the manifest, and the bundled evidence again:

```console
$ mkdir ../handoff && cp -r releases/node-1.0 ../handoff/
[exit 0]

$ tundravm verify ../handoff/node-1.0
verify ../handoff/node-1.0  verdict pass  (38 pass, 0 fail, 0 missing)
  manifest   node-1.0  version 1.0  created 2026-10-18T03:14:56Z
  file       8/8 pass
  release    1/1 pass
  evidence   pass: 29/29 checks pass
  note       unsigned: the release carries no release-manifest.json.sig
[exit 0]
```

The note says the release is unsigned: `release --sign KEY` signs `release-manifest.json` and the evidence, and `verify --allowed-signers` then names the signer, as in step 17. Back in the project:

```console
$ tundravm status
recipe    ok       node.py  digest 12c49c11c89d  variants default, dev, debug  dialect current  base debian/trixie
lint      ok       0 errors, 0 warnings, 0 infos
lock      ok       build/tundravm.lock  v5  up to date
source    n/a      no source builds or kernels
tree      ok       mkosi: up to date with the recipe
artifact  ok       default/qemu  build/default/disk.qcow2  114 B  sha256 1fa043adea90  integrity unchecked (--verify hashes it)  reproducibility matched  simulated  lock build/tundravm.lock
artifact  missing  dev: not in build/bake-result.json
artifact  ok       debug/qemu  build/debug/disk.qcow2  112 B  sha256 b50a5a5aeaac  integrity unchecked (--verify hashes it)  reproducibility matched  simulated  lock build/tundravm.lock
manifest  n/a      build/default/default.manifest  simulated: nothing was installed
manifest  n/a      build/debug/debug.manifest  simulated: nothing was installed
backend   ok       inprocess: no host tools needed
release   ok       default: ready  requires reproducible, lock  released as releases/node-1.0
release   ok       debug: ready  requires reproducible, lock  released as releases/node-1.0
next: tundravm bake
[exit 0]

$ tundravm status --variant default --variant debug
recipe    ok       node.py  digest f233478eac3e  variants default, debug  dialect current  base debian/trixie
lint      ok       0 errors, 0 warnings, 0 infos
lock      ok       build/tundravm.lock  v5  up to date
source    n/a      no source builds or kernels
tree      ok       mkosi: up to date with the recipe
artifact  ok       default/qemu  build/default/disk.qcow2  114 B  sha256 1fa043adea90  integrity unchecked (--verify hashes it)  reproducibility matched  simulated  lock build/tundravm.lock
artifact  ok       debug/qemu  build/debug/disk.qcow2  112 B  sha256 b50a5a5aeaac  integrity unchecked (--verify hashes it)  reproducibility matched  simulated  lock build/tundravm.lock
manifest  n/a      build/default/default.manifest  simulated: nothing was installed
manifest  n/a      build/debug/debug.manifest  simulated: nothing was installed
backend   ok       inprocess: no host tools needed
release   ok       default: ready  requires reproducible, lock  released as releases/node-1.0
release   ok       debug: ready  requires reproducible, lock  released as releases/node-1.0
next: everything is up to date
[exit 0]
```

Each shipped variant is `ready` and `released as releases/node-1.0`, the newest finished release, cut from this recipe and lockfile. Over every variant `next` still bakes `dev` (`tundravm bake --variant dev` would add it to `build/bake-result.json` beside the release's two); over the variants the release ships, nothing is left to do. Releasing version `1.0` again fails with `E_STATE`: a finished release is immutable, so bump `--version`. Releases live in `releases/`, outside the build directory, and `clean` never removes one:

```console
$ tundravm clean --what all
removed build/mkosi
removed build/debug
removed build/default
removed build/bake-result.json
removed build/.tundravm-hashes.json
[exit 0]
```

`releases/node-1.0` is untouched. With a real backend, `status` goes on past the bake to `measure` and `attest` before it suggests the release; [CLI: Real TDX](cli.md#real-tdx) walks through that path, and [CLI: Release](cli.md#release) has every option. From here the loop starts again at step 8.
