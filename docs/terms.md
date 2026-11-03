# Terms

Each term the [guide](guide.md) uses, defined once, in the order it needs them. Read this
page first if mkosi or TDX are new to you.

## The two tools underneath

**mkosi** is systemd's image builder. It builds a bootable OS image from a directory of
`mkosi.conf` files. tundravm writes that directory for you. You can still build it with
mkosi alone.

**Intel TDX** runs a VM as a *trust domain* (TD), whose memory the host cannot read or
change. The CPU signs a *quote*, a report of what booted in the TD. A remote party checks
the quote before it trusts the VM.

## Stage 1

**Image.** An `Image(...)` value is the whole OS: base, packages, services, ports, disks.
An *image file* is a Python file that binds one to the name `image`.

**Implied.** What every image gets without asking: Debian trixie, systemd, a firewall, a
measured kernel, and the *evidence service* `tdxs`, which issues quotes. Each implied piece
has one typed escape hatch, listed in the [manual](manual.md#what-is-implied).

**Profile.** A variant of the image. The image itself is the `production` profile.
`dev=DevAccess(...)` adds a `dev` profile with a console, debugging tools and a root
password.

**Development and production.** A development build comes from a non-production profile,
such as `dev`. Only a production build is meant to hold real secrets.

**Mode.** `simulated` runs a build under plain QEMU, without TDX. `tdx` runs it in a real
TD. A production build that finds no TDX starts none of its services: it *fails closed*.
A development build runs its services either way.

**Boot probe.** A checklist a development build runs inside the guest at boot: each
service up, each port bound, egress refused. It proves the image works, not that it is
trustworthy.

## Stages 2 to 6

**Component.** A Python class that groups declarations, such as a build and the service
running it. It exports typed values, such as an endpoint. You write your own.

**Service.** A program the image runs as a systemd unit, under its own user, in a sandbox.

**Volume.** An encrypted disk that keeps data across boots. Something must *unlock* it at
each boot, such as an operator who sends its passphrase.

**Secret.** A value the image needs and never contains. An operator *delivers* it after
checking the quote. A *generated* secret is made in the guest at each boot instead.

**Control plane.** What an image with a volume, a secret or an attested ingress runs: the
*attested endpoint* on port 8745, which serves quotes and takes deliveries, and SSH on 22.

**Firewall mode.** A named set of open ports, such as `maintenance` opening SSH, that an
operator switches to with `run switch`. The image boots into `production`, a window closes
itself, and each switch is extended into RTMR3, so `attest` shows every mode since boot. It
is not the security *mode* (`simulated`, `tdx`).

**Instance key.** A key pair each boot generates. The quote covers its public key, so a
client can tell that a signature came from this TD.

**Ingress.** A port that takes outside requests and relays them to a service on the
loopback.

## After the stages

**Lock.** `build/tundravm.lock` pins every input: the Debian snapshot, each source's commit
or digest, the toolchain. A production build refuses a missing or stale lock.

**Tree.** The mkosi directory an image compiles to. A project commits it in `mkosi/`, so a
review shows what changed in the OS.

**Build directory.** Where one build lands: `build/<image>/production/<id>/`. It holds the
disk images and a *manifest*, a JSON record of every file's digest. The id is the
manifest's digest. The directory is never written again.

**Measurements.** The register values a TD of this build should report, predicted at
build time. A *policy* is the set of values you approve.

**Instance.** One running copy of a build: under QEMU on your machine, or a cloud VM.

**Attest.** Ask an instance for a quote, check it against a policy and print a *verdict*.
Only `trusted` releases secrets to a production build.
