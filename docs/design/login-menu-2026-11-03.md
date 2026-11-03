# A login that owns the volume, into a command menu

Date: 2026-11-03. Status: design, then landed in the commits that follow it. Gaps 5 and 6
of the flashbox port: flashbots-images' searcher owns the disk and logs in to `searchersh`,
a menu of five commands, and never gets root. On master the owner of an `OperatorKey()`
volume gets root SSH, and a `Login` runs one forced command.

## The spelling

```python
Login(
    "searcher",
    owner=True,
    commands={
        "status": ["/usr/bin/cat", "/run/tundravm/firewall-events"],
        "logs": Sudo(["/usr/bin/journalctl", "-u", "searcher-container", "-n", "{lines:int:1-10000}"]),
        "switch": Sudo(["/usr/bin/mode-switch", "to", "{mode:choice:production|maintenance}"]),
    },
)
```

`owner=True` on the `Login`, not `SshAccess(owner_login="searcher")`: which key an account
takes is a property of that account, as `keys=` is. A name in another keyword would be a
second place to read, and a typo there names no account. At most one login is the owner's.
It takes no `keys=`: the owner's key is its only key.

`commands=` replaces `command=` and `sudo=`, with no alias. A plain list runs as the login.
`Sudo([...])` runs as root. Every first word is an absolute path: nothing runs by name.
An argument is a whole word: `{name:int}` (0 to 65535), `{name:int:LO-HI}`, or
`{name:choice:a|b}`. `shell=` stays as the escape hatch, without sudo rules.

## Owner without root

The endpoint's config says where `POST /ssh-key` writes: `ssh: {dir: /root/.ssh}` for root,
or `ssh: {login: searcher}`, which writes `restrict <key>` to
`/etc/ssh/authorized_keys.d/searcher` (`0644`: sshd reads it as the login). Rotation
rewrites the same file. The owner's other powers stay with the key: signing switches,
deliveries and unlocks never depended on the account the key logs in as.

The secure default: an image whose `SshAccess` declares a `Login` no longer hands root the
delivered key. The control plane turns on `from_delivery` only for an `SshAccess` without
logins. `SshAccess(from_delivery=True)` gives root the key again, as an explicit choice.
With an owner login it is refused: one key, one account. An `SshAccess` with no root key
source says `PermitRootLogin no`, so root SSH stays closed in every mode.

## The menu

`login-menu` (`tools/cmd/login-menu`, built from the tools source like `mode-switch`) is the
login's shell and its `ForceCommand`. It reads `/etc/tundravm/logins/<name>.json`
(`root:root 0444`), which the build writes with each word already typed:
`{"word": ...}` or `{"arg": "lines", "int": [1, 10000]}`. The program parses nothing but
`$SSH_ORIGINAL_COMMAND`:

- empty: print the menu, exit 0;
- split on spaces: the first word names an entry, the rest fill its arguments in order;
- an unknown entry, a missing or extra argument, an int that is not canonical decimal or
  out of bounds, or a value not in its choice: a problem line and a `fix:` line, exit 2;
- otherwise exec the argv with a fixed environment: as is, or `sudo -n --` for a root entry.

No shell sees the request. sshd's `Match User` block: `ForceCommand`, `PermitTTY no`,
`DisableForwarding yes`, its own keys file only.

## The sudoers lines

`/etc/sudoers.d/<name>` (`0440`, `visudo -c` at build time) holds one line per root entry:

- no argument: its exact argv (`""` after a bare path, which sudoers would read as any);
- choices only: one exact line per combination;
- an int: one anchored sudoers regular expression (sudo 1.9.10, trixie has 1.9.16). The
  int is `[0-9]` repeated up to its bound's digits, so the line admits digits only. The
  menu checks the exact bound. Literal words in such a line are limited to
  `[A-Za-z0-9/_.+-]`, with `.` and `+` written `[.]` and `[+]`, so no escape is needed.

No wildcard, no shell, no `ALL`.

## Every mode

A port no mode names is open in every mode, as before. A menu that must always answer, as
flashbox's searcher menu does in production, leaves 22 out of `modes=`. A container's own
sshd stays in a mode: `"maintenance": Mode(ingress=[TCP(10022)])`. That is safe because root
never logs in without a key, and the login reaches only its menu. sshd also bounds
what one source asks: `MaxStartups 10:30:60`, `PerSourceMaxStartups 4`, `MaxSessions 1`,
and dead sessions end (`ClientAliveInterval 15`). OpenSSH 10's `PerSourcePenalties` is on
by default beside them.

## The switch entry

`mode-switch` takes a switch from any root writer on its socket. It extends RTMR3, logs the
event, then loads the ruleset, whoever asked. `mode-switch to MODE` is that writer, run
locally. A `Sudo([..., "mode-switch", "to", "{mode:choice:...}"])` entry is flashbox's
`toggle`. The switch is in the quote as any other.
The endpoint's owner check guards the network path, where the host can be first. A login
switching locally already holds a key the image gave it. A switch that closes the window
drops the session's own flow, so the client sees no answer.

## Proof

The fixture's `Login("tenant", owner=True, commands={...})`. The dev probe's `menu:tenant`
row runs the menu as the login: the listing names every entry, `sh` and an out-of-bounds int
are refused, `sudo -l -U tenant` lists exactly the generated lines, and sshd says
`permitrootlogin no`. In CI, `attest` hands the key to `tenant`, and over SSH the listing,
`logs 3` and the refusals run. `root@` is refused, and `switch production` closes the
window locally. `attest` then replays both switches.

## Not here

A terminal for an entry (`PermitTTY` stays off), an argument inside a word (`--lines=N`),
and free strings.
