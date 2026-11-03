# Container: a tenant root built with the image

Date: 2026-11-03. Status: the first slice is built (below); a non-empty `egress=` and OCI
images are not. Item 38 of `status-2026-11-01.md`, section 1 of
`flashbots-images-gaps-2026-11-02.md`.

## The spelling

```python
Container("web", ["/usr/bin/busybox", "httpd", "-f", "-p", "8081", "-h", "/srv/www"],
          packages=["busybox"], files=[File.text("/srv/www/index.html", PAGE)],
          state=data.directory("web"), user="web", expose=[TCP(8081)], egress=Egress())
```

A `Component` in `tundravm.components`, not `Service(container=...)`: it composes several
units (the program, one forward per port) and a second root, and a `Service` keyword would
give every other keyword a second meaning (whose root, whose network). Its keywords are
`Service`'s where they mean the same thing (`argv`, `state=`, `user=`, `expose=`), so a
service moves into a container by changing its class and naming its packages. `state=` is
one volume directory, as on a `Service`: the container sees it at the same path, so its
working directory, `$STATE_DIRECTORY` and the volume's ordering are unchanged.

## What it lowers to

- `mkosi.images/<name>/`: a subimage with `Format=portable` (mkosi's unsigned portable
  layout: one erofs root partition in a GPT image), `Packages=`, `files=` in its
  `mkosi.extra/`, and a postinst that makes the bind mount points. `Snapshot=`,
  `ToolsTree=` and `WithNetwork=no` are universal settings, so it is the image's snapshot,
  tools and network policy; it takes no scripts, so mkosi's apt is its only fetch.
- The payload: `Dependencies=<name>` and
  `ExtraTrees=%O/<name>.raw:/usr/lib/tundravm/containers/<name>.raw`, in the shared
  `payload/mkosi.conf` or the profile's overlay. The root image is a file of the measured
  root (the UKI's initramfs or the verity partition), so the quote covers it.
- `<name>.service`: the `Service` under the `static` sandbox, run as `user` (resolved on the
  host), with a drop-in setting `RootImage=`, `MountAPIVFS=yes`, `PrivateUsers=yes`,
  `PrivateNetwork=yes` and `BindPaths=` of its state directory, after `<name>-loop.service`
  loads `loop` as root (nothing else would), skipped by
  `ConditionPathExists=!/dev/loop-control` where it is built in, as in a built `Kernel`. The read-only root ships
  every mount point the namespace mounts over (`/proc`, `/sys`, `/dev`, `/run`, `/tmp`,
  `/var/tmp`).
- Per exposed port: `<name>-forward-<port>.socket` on the host, which opens the port in the
  firewall, and `systemd-socket-proxyd` with `JoinsNamespaceOf=<name>.service` and
  `PrivateNetwork=yes` relaying it into the container's network namespace.
- The curated modules gain `loop` and `erofs`; the manifest records
  `containers.<name>`: `containers/<name>.raw` with its sha256 and size.

Not `systemd-nspawn@.service`: it boots an init (`--boot`) and its veth needs a DHCP client
in the root. A unit with `RootImage=` is one the probe, the hardening ceiling and the
firewall already understand.

## Egress

The container's network namespace holds `lo` only, so `egress=Egress()`, the default, is
enforced by the kernel with no rule. A non-empty `Egress` needs a veth: the host side
`ve-<name>`, an address pair from 169.254.0.0/16 and a forward chain accepting
`iifname "ve-<name>"` for exactly the destinations `egress_rules()` already builds, then
dropping. That lands on top of service-egress's chains and is refused here (problem, then
`fix:` drop `egress=` or run the program as a `Service`). Matching by the uid range was
rejected: an owner match sees only sockets in the host namespace, where the container would
reach every host loopback service.

## Refused

- An OCI reference or tag in `packages=` (`ubuntu:24.04`, `docker.io/...`, `@sha256:`), at
  construction: the root is Debian packages from the image's snapshot, in its SBOM.
- Scripts: there is no field for one, so the subimage build fetches nothing beyond apt.
- `container-shell` (error, production profiles): an `argv` that runs a shell. In
  production a container runs its program; a shell into it is a development profile's.
  Nothing ships `machinectl` or nspawn, so no tool enters it but root's `nsenter`.

## The probe

`container:<name>` in a development profile: the unit is active, the root image's
`sha256:<hex>` is printed (CI compares it with the manifest's `containers.<name>.sha256`),
and a datagram to 192.0.2.1:9 sent from inside its network namespace finds no route.

## OCI images, for a later agent

systemd 257's `importctl` pulls tar and raw images, not OCI, and only at boot. Two ways forward: (a) the lock pins an OCI image by digest, the
host fetches it in the sync phase like any `Http` source, and the subimage takes its
flattened layers as `BaseTrees=`, so the manifest still names every byte; the SBOM then
holds a userland outside the snapshot, which the lint should call out. (b) Refuse OCI
altogether. Recommended: (b), until a user needs a userland Debian does not ship.
