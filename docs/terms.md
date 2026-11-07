# Terms

This page defines each term that the [guide](guide.md) and the [how-to](howto.md) use. It
defines them once, in the order the guide needs them. Read it first if mkosi or TDX are new
to you.

## The two tools underneath

**mkosi** is the image builder of systemd. It builds a bootable OS image from a directory of
`mkosi.conf` files. tundravm writes that directory for you, and you can still build it with
mkosi alone.

**Intel TDX** runs a VM as a *trust domain* (TD), and the host can neither read nor change
the memory of a TD. The CPU signs a *quote*, which is a report of what booted in the TD. A
remote party checks the quote before it trusts the VM.

## Stage 1

**Image.** An `Image(...)` value describes a whole OS, with its base, packages, services,
ports and disks. An *image file* is a Python file that binds one such value to the name
`image`.

**Implied.** Every image gets some things without asking for them. These are Debian trixie,
systemd, a firewall, a measured kernel and the *evidence service* `tdxs`, which issues
quotes. Each implied piece has one typed escape hatch, and the
[manual](manual.md#what-is-implied) lists them.

**Profile.** A profile is a variant of the image. The image itself is the `production`
profile. Passing `dev=DevAccess(...)` adds a `dev` profile with a console, debugging tools
and a root password.

**Development and production.** A development build comes from a profile that is not
production, such as `dev`. Only a production build is meant to hold real secrets.

**Security mode.** In `simulated` mode a build runs under plain QEMU, without TDX. In `tdx`
mode it runs in a real TD. A production build that finds no TDX starts none of its services,
so it *fails closed*. A development build runs its services in either mode.

**Boot probe.** The boot probe is a checklist that a development build runs inside the
guest at boot. It checks that each service is up, each port is bound and egress is refused.
It shows that the image works. It does not show that the image is trustworthy.

## Stages 2 to 6

**Component.** A component is a Python class that groups related declarations, such as a
build and the service that runs it. It exports typed values, such as an endpoint. You write
your own components.

**Service.** A service is a program that the image runs as a systemd unit. It runs under its
own user and inside a sandbox.

**Volume.** A volume is an encrypted disk that keeps data across boots. Something must
*unlock* it at each boot, such as an operator who sends its passphrase.

**Owner.** The owner is the one SSH key from which an instance takes writes, when the image
has a volume with `OperatorKey()`. The first signed write pins that key in the volume's
header.

**Secret.** A secret is a value that the image needs and never contains. An operator
*delivers* it after checking the quote. A *generated* secret is made in the guest at each
boot instead.

**Control plane.** An image runs the control plane when it has a volume that an operator
unlocks, a secret or an attested ingress. The control plane is the *attested endpoint* on
port 8745, which serves quotes and takes deliveries, together with SSH on port 22.

**Instance key.** The instance key is a key pair that each boot generates. The quote covers
its public key, so a client can tell that a signature came from this TD.

**Ingress.** An ingress is a port that takes requests from outside and relays them to a
service on the loopback.

**Keep-list.** The keep-list names the metric series that may leave the guest. Every other
series stays inside.

## After the stages

**Lock.** The file `build/tundravm.lock` pins every input: the Debian snapshot, the commit
or digest of each source, and the toolchain. A production build refuses a missing or stale
lock.

**Tree.** The tree is the mkosi directory that an image compiles to. A project commits it in
`mkosi/`, so a review shows what changed in the OS.

**Build directory.** Each build lands in its own directory, `build/<image>/production/<id>/`.
It holds the disk images and a *manifest*, which is a JSON record of every file's digest.
The id is the digest of the manifest. Nothing writes to the directory again.

**Measurements.** Measurements are the register values that a TD of this build should
report. They are the RTMRs (runtime measurement registers) and MRTD, and tundravm predicts
them at build time. A *policy* is the set of values you approve.

**Instance.** An instance is one running copy of a build, either under QEMU on your machine
or as a cloud VM.

**Attest.** To attest is to ask an instance for a quote, check it against a policy and print
a *verdict*. Only the verdict `trusted` releases secrets to a production build.

**Firewall mode.** A firewall mode is a named set of open ports, such as `maintenance`,
which opens SSH. The image boots into `production`. An operator switches modes over the
control plane with `run switch`, and `attest` shows every switch since boot.

**Container.** A container is a program that runs in a Debian root of its own. The build
makes that root with the image, from the same snapshot. Its network is a veth that the
firewall forwards. The container declares the ports it exposes, the destinations its egress
names and the host endpoints it requires.
