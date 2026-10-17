"""Task, Every/Calendar, Listen and Mount: construction, validation, names and refs."""

from __future__ import annotations

import pytest

from tundravm.declarative import (
    BootStep,
    Calendar,
    Every,
    Listen,
    Mount,
    Ref,
    Service,
    Task,
    UnitReady,
)
from tundravm.declarative.systemd import Dynamic, mount_unit
from tundravm.errors import ValidationError


@pytest.mark.parametrize(
    ("where", "unit"),
    [
        ("/var/lib/data", "var-lib-data.mount"),
        ("/var/lib/my-app/cache", "var-lib-my\\x2dapp-cache.mount"),
        ("/srv/.hidden", "srv-.hidden.mount"),
        ("/mnt/a b", "mnt-a\\x20b.mount"),
        ("/home", "home.mount"),
    ],
)
def test_mount_unit_names_follow_systemd_escape_path(where: str, unit: str) -> None:
    assert Mount(where, "tmpfs").unit == unit == mount_unit(where)


def test_shapes_have_refs_keyed_by_unit() -> None:
    assert (
        Task("hello", "/bin/true").ref
        == Task.ref("hello.service")
        == Ref("Task", ("hello.service",))
    )
    assert Mount("/var/lib/data", "tmpfs").ref == Mount.ref("/var/lib/data")
    edit = Task.ref("hello").edit(timeout="1min")
    assert edit.target.kind == "Task"
    assert Mount.ref("/var/lib/data").remove().target.identity == ("/var/lib/data",)


def test_schedules_render_timer_directives() -> None:
    assert Every("1h").entries() == (("OnBootSec", "1h"), ("OnUnitActiveSec", "1h"))
    assert Every("15min", first="2min", jitter="30s").entries() == (
        ("OnBootSec", "2min"),
        ("OnUnitActiveSec", "15min"),
        ("RandomizedDelaySec", "30s"),
    )
    assert Calendar("daily").entries() == (("OnCalendar", "daily"), ("Persistent", "yes"))
    assert Calendar("Mon *-*-* 03:00:00", persistent=False, jitter="1h").entries() == (
        ("OnCalendar", "Mon *-*-* 03:00:00"),
        ("RandomizedDelaySec", "1h"),
    )


def test_listen_renders_socket_directives() -> None:
    assert Listen(8080).entries() == (("ListenStream", "8080"), ("Accept", "no"))
    assert Listen("/run/app.sock", user="app", group="tdx", mode=0o600).entries() == (
        ("ListenStream", "/run/app.sock"),
        ("SocketMode", "0600"),
        ("SocketUser", "app"),
        ("SocketGroup", "tdx"),
        ("Accept", "no"),
    )
    assert Listen("127.0.0.1:53", kind="datagram").entries()[0] == (
        "ListenDatagram",
        "127.0.0.1:53",
    )


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: Every("soon"), "not a systemd time span"),
        (lambda: Every("1h", jitter="lots"), "not a systemd time span"),
        (lambda: Calendar("whenever"), "not a systemd calendar expression"),
        (lambda: Calendar("*-*-* 03:00:00\n"), "not a systemd calendar expression"),
        (lambda: Listen(0), "out of range"),
        (lambda: Listen("app.sock"), "invalid listen address"),
        (lambda: Listen(8080, mode=0o600), "apply to a socket file"),
        (lambda: Listen(53, kind="datagram", accept=True), "needs a stream socket"),
        (lambda: Task("job", "true"), "not an absolute path"),
        (lambda: Task("job.timer", "/bin/true"), "must be a .service unit"),
        (lambda: Task("job", "/bin/true", timeout="forever"), "not a systemd time span"),
        (lambda: Task("job", "/bin/true", schedule="daily"), "schedule must be"),  # type: ignore[arg-type]
        (lambda: Task("Job", "/bin/true"), "pass user="),
        (lambda: Task("j", "/bin/true", schedule=Every("1h"), wanted_by="x.target"), "timer"),
        (lambda: Mount("data", "tmpfs"), "absolute"),
        (lambda: Mount("/", "tmpfs"), "normalized path other than /"),
        (lambda: Mount("/var/lib/x/", "tmpfs"), "normalized path"),
        (lambda: Mount("/x", "tmpfs", options=("size=1M,mode=0700",)), "invalid mount option"),
        (lambda: Service("app", "/bin/app", socket=Listen(80, accept=True)), "must be a template"),
        (lambda: Service("app@", "/bin/app", socket=Listen(80)), "needs accept=True"),
    ],
)
def test_invalid_shapes_are_refused(build: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        build()  # type: ignore[operator]


def test_boot_task_ready_names_its_unit() -> None:
    task = Task("migrate", "/usr/bin/app migrate", user=Dynamic())
    assert task.ready == UnitReady("migrate.service")
    assert task.ready.unit == "migrate.service"
    assert Service("app", "/usr/bin/app", needs=[task.ready]).needs == (task.ready,)
    with pytest.raises(ValidationError, match="schedule"):
        _ = Task("tick", "/bin/true", schedule=Every("1h")).ready
    with pytest.raises(ValidationError, match="cannot wait for unit"):
        BootStep("seed", "true", after=[task.ready])
