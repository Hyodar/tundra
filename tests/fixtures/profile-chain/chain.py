"""Three profiles, plan section 3.3: an ``extends`` chain, an edit and a removal.

``production`` (the image) runs ``web`` and ``worker`` and ships a motd; ``staging`` edits
the motd and ``web``'s environment and adds ``strace``; ``bench`` extends ``staging``,
adds ``fio`` and removes ``worker``. ``tests/compiler/test_profile_overlays.py`` compiles
it and resolves the tree under ``mkosi summary --json`` for each profile: ``worker``
leaves the shared payload for the overlays of the two profiles that keep it.
"""

from tundravm import TCP, File, Image, Profile, Service

web = Service(
    "web",
    ["/usr/bin/busybox", "httpd", "-f", "-p", "8080", "-h", "/srv/www"],
    expose=[TCP(8080)],
)
worker = Service("worker", ["/usr/bin/busybox", "sleep", "infinity"])
motd = File.text("/etc/motd", "production\n")

image = Image(
    "chain",
    packages=["busybox"],
    files=[motd],
    services=[web, worker],
    profiles={
        "staging": Profile(
            add=["strace"],
            changes=[
                motd.ref.edit(content="staging\n"),
                web.ref.edit(env={"STAGE": "staging"}),
            ],
        ),
        "bench": Profile(
            extends="staging", add=["fio"], changes=[worker.ref.remove()], production=False
        ),
    },
)
