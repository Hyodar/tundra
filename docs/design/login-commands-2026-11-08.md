# A login's commands, typed and gated

Date: 2026-11-08. Status: design, then landed in the commits that follow it. Franco: "Could
we make Login have modes also? As one use-case would be having additional command in dev
build for example. Also in Login, I think it would be better to have a clearer way to
declare the commands and arguments and what they end up running."

On master a command is a list of strings, an argument a mini-language inside one of them,
and who runs it the type of the wrapper around the list. Nothing gates a command on the
firewall's mode or on the build.

## The spelling

The flashbox menu, as `examples/flashbox/image.py` writes it:

```python
ssh = SshAccess(
    login=Login(
        "searcher",
        owner=True,
        commands={
            "status": firewall.status,
            "logs": Sudo("/usr/bin/tail", "--lines", Int("lines", 1, 10000), delayed / "output.log"),
            "toggle": firewall.switch,
            "reboot": Sudo("/usr/bin/systemctl", "reboot"),
            "shell": Command("/usr/bin/bash", dev=True),
        },
    )
)
```

- `Command(*argv)` runs as the login; `Sudo(*argv)` runs as root, through `sudo -n` and the
  exact lines of `/etc/sudoers.d/<login>`. Each word is a positional argument, so the argv
  is what the line says, in order. Who runs it is the first word of the value.
- `Int("lines", 1, 10000)` is a word the client types as canonical decimal within both
  bounds, which are required. `Choice("mode", "production", "maintenance")` is one of the
  values. A bare string (or `directory / "file"`) is a literal word.
- `modes=["maintenance"]` offers a command in those firewall modes only. A command
  without `modes=` is offered in every mode, as a port no `Mode` names is open in every
  mode and a `Container`'s egress outside `modes=` is sent in every mode. Each name must be
  a mode the image's `Firewall(modes=)` declares (`login-mode-undeclared`, an error).
- `dev=True` makes the command exist in development builds only: every profile whose
  `production=` is `False`, the `dev` profile `DevAccess` adds included. The production
  build's menu file and sudoers lines do not hold it, so no `dev=True` shell is reachable in
  production. A `dev=True` command in an image with no development profile is refused,
  as is a login whose every command says it.
- `firewall.status` is `Sudo(MODE_SWITCH, "status")`; `firewall.switch` is
  `Sudo(MODE_SWITCH, "to", Choice("mode", *modes))`.

The menu stays a dict `name -> command`: the typed name on the left, its argv on the
right. A list of named commands hides the name inside the call.

## What it lowers to

The menu file (`/etc/tundravm/logins/<name>.json`, `0444`) keeps each word typed and adds
`"modes"` (empty: every mode). A `dev=True` command is in the development profiles' copy
of that file only, which their overlay writes over the production one; its sudoers lines
and, when it is the login's only root command, the sudoers file and `sudo` itself, are in
those overlays too. sshd's `Match User` block is the same in every build.

`login-menu` reads the mode in force from `/run/tundravm/firewall-events` (`0644` in the
`0711` `/run/tundravm`, so the login reads it without root; no log is production), as
`mode-switch status` does, only when the requested command names modes. A command not
offered refuses:

```
login-menu: logs is offered in maintenance only, and production is in force
fix: switch to maintenance first (tundravm run switch INSTANCE maintenance)
```

An empty request lists every command, a gated one with `maintenance only` beside it. The
sudoers lines admit a gated root command in every mode: `login-menu` is the login's only
way to reach `sudo`, and the forced command is what enforces the gate.

## What `show` prints

A `Logins` section after `Profiles`, one row per command:

```
Logins (1):
  searcher  the owner's key, a menu
    logs    root      /usr/bin/tail --lines <lines 1-10000> /persistent/data/delayed/output.log
    shell   searcher  /usr/bin/bash  (profiles dev)
    toggle  root      /usr/bin/mode-switch to <mode production|maintenance>
```

Each row names who runs it, the argv with each argument's name and bounds, the modes it is
offered in when not every mode, and the profiles it exists in when not every profile.
`show --json` carries `logins`: per login its commands, each with `runs_as`, `argv` (the
menu file's typed words), `modes` and `profiles`.

## Refused, with the new spelling in the hint

- a word written in the old mini-language (`"{lines:int:1-1000}"`): `Int("lines", 1, 1000)`;
- `Sudo([...])`, a list as the first word: `Sudo("/usr/bin/x", ...)`;
- a plain list as a command: `Command("/usr/bin/x", ...)`;
- an `Int` whose bounds are reversed, negative or above 999999999, a `Choice` with no
  value, a repeated one or one starting with `-`;
- a mode named `tdx` or `simulated`, or one the firewall does not declare.

`tests/lint/test_user_strings.py`'s denylist refuses both old spellings in the package's
strings and in the examples.

## Rejected

- `Command(..., root=True)`: the who lands at the end of a long line; `Sudo(` is its first word.
- `Login(modes={"maintenance": {...}})`, like `Firewall(modes=)`: one command's story splits
  over two keywords, and a command in two modes is written twice.
- `Login(dev={...})`, like `Image(dev=)`: cannot say a command that is both dev-only and
  mode-gated, and moves the command away from its siblings.
- `Profile(production=False, changes=[...])` editing the login: the escape hatch for any
  change, too far from the command for the common case.
- `production=False` on the command: reads like the firewall's `production` mode.
- A runtime check of the build in `login-menu`: the dev command's sudoers line would ship in
  production.

## Not here

A terminal for a command (`PermitTTY` stays off: `shell` reads stdin
without a prompt), an argument inside a word (`--lines=N`), and free strings.
