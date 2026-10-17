"""Task, Service(socket=Listen), Mount: generated unit bytes, enablement, needs, lint."""

from __future__ import annotations

from tundravm.declarative import (
    BootStep,
    Calendar,
    Debian,
    Every,
    Listen,
    Mount,
    Recipe,
    Service,
    Task,
    User,
    Variant,
)
from tundravm.declarative.systemd import Dynamic, Hardening
from tundravm.testing import CompiledTree, assert_clean, compile_tree

OFF = Hardening.none()


def _tree(*items: object) -> CompiledTree:
    recipe = Recipe(name="shapes", platform=Debian("bookworm"), items=items)  # type: ignore[arg-type]
    return compile_tree(recipe)


def _recipe(*items: object) -> Recipe:
    return Recipe(name="shapes", platform=Debian("bookworm"), items=items)  # type: ignore[arg-type]


def _postinst(tree: CompiledTree) -> str:
    return tree.read("scripts/06-postinst.sh")


def test_boot_task_is_an_enabled_oneshot() -> None:
    tree = _tree(Task("hello", "/usr/bin/cat /etc/motd", hardening=OFF))
    assert tree.unit("hello") == (
        "[Unit]\nDescription=hello\n\n"
        "[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/usr/bin/cat /etc/motd\n"
        "User=hello\nTimeoutStartSec=5min\n\n"
        "[Install]\nWantedBy=minimal.target\n"
    )
    script = _postinst(tree)
    assert "useradd --system --shell /usr/sbin/nologin hello" in script
    assert "systemctl enable hello.service" in script


def test_task_defaults_to_strict_hardening() -> None:
    text = _tree(Task("hello", "/usr/bin/true")).unit("hello")
    lines = [f"{key}={value}" for key, value in Hardening.strict().entries()]
    assert "\n".join(lines) in text
    assert text.index("TimeoutStartSec=5min") < text.index(lines[0])


def test_scheduled_task_renders_a_timer_and_leaves_the_service_to_it() -> None:
    task = Task(
        "backup",
        ("/usr/bin/backup", "--all"),
        schedule=Calendar("*-*-* 03:00:00", jitter="10min"),
        user=Dynamic(),
        state="backup",
        hardening=OFF,
    )
    tree = _tree(task)
    assert tree.unit("backup") == (
        "[Unit]\nDescription=backup\n\n"
        "[Service]\nType=oneshot\nExecStart=/usr/bin/backup --all\nDynamicUser=yes\n"
        "WorkingDirectory=/var/lib/backup\nStateDirectory=backup\nTimeoutStartSec=5min\n"
    )
    assert tree.unit("backup.timer") == (
        "[Unit]\nDescription=backup\n\n"
        "[Timer]\nOnCalendar=*-*-* 03:00:00\nPersistent=yes\nRandomizedDelaySec=10min\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    script = _postinst(tree)
    assert "systemctl enable backup.timer" in script
    assert "systemctl enable backup.service" not in script


def test_every_renders_monotonic_timers() -> None:
    task = Task("probe", "/usr/bin/true", schedule=Every("1min", first="30s"), hardening=OFF)
    assert _tree(task).unit("probe.timer") == (
        "[Unit]\nDescription=probe\n\n"
        "[Timer]\nOnBootSec=30s\nOnUnitActiveSec=1min\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )


def test_socket_activated_service() -> None:
    service = Service(
        "app",
        "/usr/bin/app",
        user="app",
        socket=Listen("/run/app.sock", group="app"),
        type=None,
        restart="no",
        hardening=Hardening.none(),
    )
    tree = _tree(User("app"), service)
    assert tree.unit("app.socket") == (
        "[Unit]\nDescription=app\n\n"
        "[Socket]\nListenStream=/run/app.sock\nSocketMode=0660\nSocketGroup=app\nAccept=no\n\n"
        "[Install]\nWantedBy=sockets.target\n"
    )
    assert tree.unit("app") == (
        "[Unit]\nDescription=app\nAfter=app.socket\nRequires=app.socket\n\n"
        "[Service]\nType=simple\nExecStart=/usr/bin/app\nUser=app\n"
    )
    script = _postinst(tree)
    assert "systemctl enable app.socket" in script
    assert "systemctl enable app.service" not in script


def test_accepting_socket_starts_a_template() -> None:
    tree = _tree(Service("echo@", "/usr/bin/cat", socket=Listen(7777, accept=True)))
    assert "ListenStream=7777\nAccept=yes\n" in tree.unit("echo.socket")
    assert tree.unit("echo@.service").startswith("[Unit]\nDescription=echo@\nAfter=echo.socket\n")


def test_mount_unit_is_escaped_and_enabled() -> None:
    mount = Mount("/var/lib/my-app", "tmpfs", type="tmpfs", options=("size=64M", "mode=0750"))
    tree = _tree(mount)
    assert tree.unit("var-lib-my\\x2dapp.mount") == (
        "[Unit]\nDescription=/var/lib/my-app\n\n"
        "[Mount]\nWhat=tmpfs\nWhere=/var/lib/my-app\nType=tmpfs\nOptions=size=64M,mode=0750\n\n"
        "[Install]\nWantedBy=local-fs.target\n"
    )
    assert "systemctl enable 'var-lib-my\\x2dapp.mount'" in _postinst(tree)


def test_needs_wait_for_steps_and_boot_tasks() -> None:
    seed = BootStep("seed", "true")
    hello = Task("hello", "/usr/bin/true", needs=[seed.ready], hardening=OFF)
    app = Service("app", "/usr/bin/app", needs=[hello.ready])
    bind = Mount("/var/lib/app", "/persistent/app", options=("bind",), needs=[seed.ready])
    tree = _tree(seed, hello, app, bind)
    wait = "runtime-init-seed.service"
    assert f"After={wait}\nRequires={wait}\n" in tree.unit("hello")
    assert "After=hello.service\nRequires=hello.service\n" in tree.unit("app")
    mount = tree.unit("var-lib-app.mount")
    assert f"After={wait}\nRequires={wait}\n" in mount
    assert "Options=bind,nofail\n" in mount and "WantedBy=minimal.target\n" in mount


def test_generated_units_lint_clean() -> None:
    hello = Task("hello", "/usr/bin/true")
    assert_clean(
        _recipe(
            hello,
            Task("tick", "/usr/bin/true", schedule=Every("1h"), needs=[hello.ready]),
            User("app"),
            Service("app", "/usr/bin/app", socket=Listen("/run/app.sock", user="app")),
            Mount("/var/cache/app", "tmpfs", type="tmpfs"),
        )
    )


def test_variants_edit_and_remove_shapes() -> None:
    base = (Task("tick", "/usr/bin/true", schedule=Every("1h")), Mount("/var/x", "tmpfs"))
    recipe = Recipe(
        name="shapes",
        platform=Debian("bookworm"),
        items=base,
        variants=(
            Variant("default", outputs=("qemu",)),
            Variant(
                "slow",
                outputs=("qemu",),
                changes=(
                    Task.ref("tick").edit(schedule=Calendar("daily")),
                    Mount.ref("/var/x").remove(),
                ),
            ),
        ),
    )
    tree = compile_tree(recipe)
    assert "OnCalendar=daily" in tree.unit("tick.timer", "slow")
    assert "OnUnitActiveSec=1h" in tree.unit("tick.timer", "default")
    assert not tree.exists("mkosi.extra/usr/lib/systemd/system/var-x.mount", "slow")


def test_example_03_health_check_runs_only_from_its_timer() -> None:
    """The units the 03 re-record moved: the service lost [Install], the timer is typed."""
    from tests.helpers import REPO_ROOT
    from tundravm.declarative import load

    tree = compile_tree(load(REPO_ROOT / "examples" / "03_fragments.py", extra_paths=[REPO_ROOT]))
    service = tree.unit("healthcheck")
    assert "Type=oneshot\n" in service and "[Install]" not in service
    assert "User=healthcheck\n" in service and "ProtectSystem=strict\n" in service
    assert tree.unit("healthcheck.timer") == (
        "[Unit]\nDescription=Probe the service\n\n"
        "[Timer]\nOnBootSec=1min\nOnUnitActiveSec=1min\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    assert "systemctl enable healthcheck.service" not in _postinst(tree)


def test_image_queries_see_the_generated_units() -> None:
    from tundravm.testing import Image

    hello = Task("hello", "/usr/bin/true")
    image = Image(
        _recipe(
            hello,
            Task("tick", "/usr/bin/true", schedule=Every("1h")),
            Service("app", "/usr/bin/app", socket=Listen(8080), needs=[hello.ready]),
            Mount("/var/cache/app", "tmpfs"),
        ),
        "default",
    )
    assert image.unit("tick.timer").get("Timer", "OnUnitActiveSec") == "1h"
    assert image.unit("tick.timer").enabled and not image.unit("tick").enabled
    assert image.unit("hello").enabled and image.unit("hello").get("Service", "Type") == "oneshot"
    assert image.unit("app.socket").enabled and not image.unit("app").enabled
    assert image.unit("var-cache-app.mount").get("Mount", "What") == "tmpfs"
