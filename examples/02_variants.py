"""Profiles: one image, one payload, each profile a variation on it.

Teaches ``Profile``: ``extends`` the profile it starts from (``None``: the image itself,
which is the ``production`` profile), ``add`` for more content, and ``changes`` that edit
or remove what the image declares, by reference (``motd.ref`` for a value you hold).
``production=False`` marks a profile's artifacts as never shipped, and
``dev=DevAccess(...)`` is the sugar for one of those. ``targets=`` names the disks the
image builds: one UKI wrapped for QEMU and for each cloud, whose glue is gated at boot
(``ConditionVirtualization=``), so one set of measurements covers all three.

    tundravm show examples/02_variants.py --profile production --profile staging
    tundravm build examples/02_variants.py --tree-only build/variants/mkosi
    tundravm build examples/02_variants.py --profile production
"""

from tundravm import TCP, DevAccess, File, Image, Profile, Resources, Service

motd = File.text("/etc/motd", "production\n")

image = Image(
    "variants",
    packages=["ca-certificates", "busybox"],
    files=[motd, File.text("/srv/www/index.html", "<h1>variants</h1>\n")],
    services=[
        Service(
            "app",
            ["/usr/bin/busybox", "httpd", "-f", "-p", "8080", "-h", "/srv/www"],
            expose=[TCP(8080)],
            resources=Resources(memory_max="512M"),
        ),
    ],
    # One UKI, one set of measurements, three disks.
    targets=("qemu", "azure", "gcp"),
    profiles={
        # Same services, a different banner.
        "staging": Profile(changes=[motd.ref.edit(content="staging\n")]),
        # Adds a benchmark tool, and is never shipped by a release.
        "bench": Profile(add=["fio"], production=False),
    },
    # Root over SSH, by public key only, in a non-production "dev" profile.
    dev=DevAccess(ssh_keys=["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyForTheDocs me@host"]),
)
