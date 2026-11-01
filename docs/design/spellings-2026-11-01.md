# Public spellings: a packaged unit, an apt repository, a build-time command

Date: 2026-11-01. Status: decided and built on branch `spellings`. Base: master at `8015325`.
`examples/03_components.py` was the last example importing `tundravm.declarative`
(`Repository`, `Unit`, `Run`) because these three had no public spelling. Each now has
one, chosen to keep the image's opinions: a unit runs sandboxed, a package comes from a
pinned and signed archive, and a build step says what goes in and what comes out.

## 1. A packaged unit: `Service.packaged(unit)`

**Want.** Run the unit a Debian package ships (`prometheus-node-exporter.service`)
instead of writing it again. **Model.** `Unit(name, enabled=True, dropin=DropIn(...))`
enables a packaged unit and patches it with a drop-in. That drop-in's `hardening=` is
opt-in, so `Unit` runs the package's own sandbox by default, whatever that is.

```python
yield Package("prometheus-node-exporter")                               # before
yield Unit("prometheus-node-exporter.service", enabled=True)
yield Service.packaged("prometheus-node-exporter.service")              # after
yield Service.packaged("ssh.service", package="openssh-server", expose=[TCP(22)])
```

**Chosen over `Package("chrony", services=[...])`** because a packaged unit is a service
the image runs, not a property of an archive member. It goes where services go
(`services=` or `compose()`), has `.ready` for `requires=`, opens a port only through
`expose=`, is changed by `svc.ref`, and its sandbox is the `hardening=` keyword every
service takes. `package=` defaults to the unit's stem and is installed for you.

**Refused.** The package's unit keeps its own `ExecStart=`, `User=`, directories and
`Type=`. So `argv`, `user=`, `groups=`, `quotes=`, `state=`, `credentials=`,
`environment=`, `working_directory=`, `logs=`, `runtime=`, `timer=`, `socket=`,
`resources=`, `shutdown=` and `readiness=Notify()` are refused, each naming
`unit=DropIn(...)` where systemd can still say it. `readiness=TcpProbe`/`HttpProbe` stay,
since a probe is its own unit. `hardening=Hardening.none()` is refused: the unit runs
under the sandbox or not at all, and `"relaxed"` is the floor. A `unit=` drop-in that
sets a sandbox is refused, because `hardening=` already says it.

**Lowering.** `Package(package)` and `Unit(name, enabled=True, ports=expose,
dropin=DropIn(hardening=<preset>, after=requires, requires=requires, ...unit=))`. The
preset sets every field, with list resets, so it replaces the package's sandbox outright.
A probe is the same `-ready` task a plain service gets. `convert --from systemd` writes
`Service.packaged(name, unit=DropIn(...))` for a drop-in whose unit is not in the set,
which used to be unsupported.

## 2. An apt repository: `Debian(repositories=[AptRepository(...)])`

**Want.** Packages from a second archive (Debian security, a vendor's repository) with
the same guarantees as the base. **Model.** `Repository(name, url, suite, components,
keyring, priority, in_image)`. It writes a deb822 source for the build's apt and the
image's, and nothing in it is pinned or required to be signed.

```python
yield Repository("debian-security", "https://deb.debian.org/debian-security",   # before
                 suite="trixie-security", priority=10)
SECURITY = AptRepository(                                                         # after
    "https://snapshot.debian.org/archive/debian-security", "trixie-security",
    key="/usr/share/keyrings/debian-archive-keyring.gpg",
)
image = Image("x", base=Debian(repositories=[SECURITY]))
vendor = AptRepository("https://apt.example.com", "stable", key=HERE / "vendor.asc",
                       sha256="<the suite's InRelease sha256>")
```

**Pinned.** A `snapshot.debian.org/archive/<archive>` URL is dated, by `snapshot=` or
by the base's own snapshot, so the repository moves when the image's snapshot moves.
Any other URL takes `sha256=` of `dists/<suite>/InRelease`. That file is signed and
lists the digest of every index, and each index lists every `.deb`, so one digest pins
the whole archive. **Signed.** `key=` is required: a host keyring file (`Path`, shipped
at `/etc/apt/keyrings/<name>.gpg` or `.asc`), or the path of a keyring a package
installs under `/usr/share/keyrings/`.

**Refused.** No key. A URL with apt options (`[trusted=yes]`), `http://`, `snapshot=`
on a URL snapshot.debian.org does not serve, and `sha256=` on a dated snapshot URL,
which is already pinned. Two repositories with one name. `priority=` is not offered:
apt's version order decides, as it does between Debian's own suites. The repository
lives on `base=` rather than `packages=` or a component because it is part of the
foundation every profile shares; the internal model already refuses a profile that
changes the build's repositories.

**Lowering.** `Repository(name, url[/<date>], suite, components, keyring=<path>,
in_image=False)`, because an image never runs apt. A `Path` key adds `File.copy` at its
path, which the sandbox tree copies too. A `sha256=` adds a `sync` hook. It fetches the
InRelease before mkosi runs apt and fails the build, naming both digests, when the
repository has moved; an offline build passes it the digest `lock` recorded instead. `convert --from mkosi` writes an `AptRepository` for each signed,
dated deb822 source of a `SandboxTrees=` tree; an undated one is unsupported, with the
command that prints its digest.

## 3. A build-time command: `Build(..., command=[...])`

**Want.** Produce files at build time from inputs you can name: render a site, generate
a config, compile something that is not Go or Rust. **Model.** `Build(name, source,
script=, install=)`, which runs in the build environment, never the image, and is cached
by a fingerprint of its source pin, spec and toolchain. It reaches an `Image` only through
a component, and `script=` is shell text. `Run(...)` is the in-image hook: a shell line
run by `mkosi-chroot` over whatever the image holds by then.

```python
yield Run("update-ca-certificates --fresh")                                 # before
site = Build("site", Local("site"), command=["hugo", "--minify"],          # after
             packages=["hugo"], install=[Install("public", "/srv/www", mode=None, directory=True)])
image = Image("x", files=[site])
```

**Extended, not a sibling.** `sources.Build` already has the right shape: inputs are the
`source` and `packages`, outputs are `install=`, and the lock pins it. `command=` is a
third way to say how, beside `script=` and `using=`: an argv run in the source tree,
quoted for you, with no shell. `script=` stays for a recipe of several shell steps, which
still runs only in the build environment. `files=` takes a `Build`, since what it ships is
files, so a build no longer needs a component to reach the image.

**Refused.** A command run inside the image has no public spelling. Its inputs are the
whole image and its outputs are undeclared, so neither the lock nor `check` can say what
it did. `update-ca-certificates` runs from the `ca-certificates` trigger anyway.
`Mkosi(scripts=)` stays the recorded escape; a `Build` without `install=` stays refused.

**Lowering.** Unchanged: `command=` becomes the build script `shlex.join(command)`, so the
fingerprint and the cache see it as they see `script=`. A `Build` in `files=` lowers to
itself. `convert --from mkosi` still writes `BuildScripts=` as `Mkosi(scripts=)`: a tree's
build script names no source and no outputs, so nothing maps it onto a `Build`.

## Open items

- ~~`Build(command=[...])` failed mypy: its fields were tuples.~~ Every public value whose
  docs show a list takes a `Sequence` and keeps a tuple.
- ~~`build --offline` failed on a digest-pinned repository's `curl`.~~ `lock` records each
  `InRelease` digest and date (lockfile v7 `repositories`); the offline build hands the
  recorded digest to the `sync` step.
- ~~A packaged unit that needs a capability had no working spelling.~~
  `Hardening.preset("static", capabilities=["CAP_SYS_TIME"])` also lifts `ProtectClock=` and
  `@clock`; a capability still missing shows as the probe's `service:` failure.
- ~~A snapshot repository assumed the tools tree ships `debian-archive-keyring`.~~ The tree
  names it in `ToolsTreePackages=`.
