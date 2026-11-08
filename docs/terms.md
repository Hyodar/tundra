# Terms

This page defines the words the [guide](guide.md) and the [how-to](howto.md) rely on, in the
order the guide meets them. Start here if mkosi or TDX are new to you.

## The two tools underneath

**mkosi** is systemd's image builder, which turns a directory of `mkosi.conf` files into a
bootable OS image. tundravm writes that directory for you, and mkosi alone can still build
it.

**Intel TDX** runs a VM as a *trust domain* (TD), whose memory the host can neither read nor
change. The CPU signs a *quote*, a report of what booted, which a remote party checks before
it trusts the VM.

## Stage 1

**Image.** An `Image(...)` value describes a whole OS: its base, packages, services, ports
and disks. An *image file* is a Python file that binds one to the name `image`.

**Implied.** What every image gets without asking, such as Debian trixie, systemd, a
firewall, a measured kernel and the *evidence service* `tdxs`, which issues quotes. The
[manual](manual.md#what-is-implied) lists each piece with its escape hatch.

**Profile.** A variant of the image, which is itself the `production` profile. Passing
`dev=DevAccess(...)` adds a `dev` profile with a console, debugging tools and a root
password.

**Development and production.** Any profile other than production, such as `dev`, makes a
development build. Only a production build is meant to hold real secrets.

**Security mode.** A build runs in `simulated` mode under plain QEMU, or in `tdx` mode inside
a real TD. A production build that finds no TDX *fails closed* and starts none of its
services, while a development build runs in either mode.

**Boot probe.** A checklist a development build runs inside the guest at boot, confirming
that each service is up, each port is bound and egress is refused. It shows that the image
works, not that it can be trusted.

## Stages 2 to 6

**Component.** A Python class that groups related declarations, such as a build and the
service that runs it. Your application becomes one, and it exports typed values, such as an
endpoint, that the rest of the image connects to.

**Service.** A program the image runs as a systemd unit, under its own user and inside a
sandbox.

**Volume.** An encrypted disk that keeps data across boots. Because it is encrypted,
something must *unlock* it at each boot, such as an operator who sends its passphrase.

**Owner.** The one SSH key an instance takes writes from, in an image with an
`OperatorKey()` volume. The first signed write pins it in the volume's header.

**Secret.** A value the image needs but never contains, which an operator *delivers* after
checking the quote. A *generated* secret is made in the guest at each boot instead.

**Control plane.** The *attested endpoint* on port 8745, which serves quotes and takes
deliveries, together with SSH on port 22. An image runs it as soon as it has something to
receive, such as a volume to unlock or a secret to deliver.

**Instance key.** A key pair each boot generates. The quote covers its public key, so a
client can tell that a signature came from this TD.

**Ingress.** A port that takes requests from outside and relays them to a service on the
loopback.

**Keep-list.** The metric series that may leave the guest. Every other series stays inside.

## After the stages

**Lock.** The file `build/tundravm.lock`, which pins every input: the Debian snapshot, each
source's commit or digest, and the toolchain. A production build refuses a missing or stale
lock.

**Tree.** The mkosi directory an image compiles to. A project commits it in `mkosi/`, so a
review shows what changed in the OS.

**Build directory.** Where a build lands, `build/<image>/production/<id>/`, and nothing
writes to it again. It holds the disk images and a *manifest*, a JSON record of every file's
digest, whose own digest is the id.

**Measurements.** The register values a TD of this build should report, MRTD and the RTMRs
(runtime measurement registers), which tundravm predicts at build time. A *policy* is the
set of values you approve.

**Instance.** One running copy of a build, under QEMU on your machine or as a cloud VM.

**Attest.** To ask an instance for a quote, check it against a policy and print a *verdict*.
Only the verdict `trusted` releases secrets to a production build.

**Firewall mode.** A named set of open ports, such as `maintenance`, which opens SSH. The
image boots into `production`, and `attest` shows every switch an operator made since boot.

**Container.** A program that runs in a Debian root of its own, built with the image from
the same snapshot. It declares the ports it exposes and where it may send traffic.
