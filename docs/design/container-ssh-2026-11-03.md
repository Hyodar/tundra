# Container SSH: an account, keys and a host key the quote binds

Date: 2026-11-03. Gap 2 of `examples/flashbox` (flashbox-l1's searcher logs into its
container's sshd on 10022), and the account clash a `Container("searcher")` beside a
`Login("searcher")` hid. Builds on `container-2026-11-03.md`.

## The spelling

```python
Container("searcher", packages=["build-essential"], state=data.directory("searcher"),
          ssh=ContainerSsh(port=10022, keys=Secret("searcher-keys")))
```

`ContainerSsh`, not `Login`: a `Login` is an account of the host's sshd, with `command=`,
`shell=` and `sudo=` the host runs. None of those exists in a container: its sshd runs as
the container's user, unprivileged in its user namespace, so it logs in that one account
and has no root to sudo to. `SshAccess(login=Login(...))` reads as "the host's sshd lets
this account in"; `Container(ssh=ContainerSsh(...))` as "the container runs an sshd of its
own". *keys* is a `Secret` (delivered after attestation) or a list of public key lines, as
on a `Login`.

`ssh=` adds `openssh-server` to the root. Without `argv`, sshd is the container's
program; with one, sshd runs beside it as `<name>-ssh.service`, from the same root image,
in its network namespace (`NetworkNamespacePath=`). Its port is DNATed like any `expose=`
port, so `tundravm run` forwards it and a firewall mode may name it.

## The account

The container's user is its own account: `Container` declares the group and the system user
with a pinned id, `60578 + crc32(user) % 606` (systemd's unused range between the
nspawn-mapped and the dynamic users), and the subimage's postinst writes the same
`passwd`/`group` lines into the root. `PrivateUsers=yes` maps root and the unit's own user
and group to themselves, so the uid the host's `useradd` gave is the one the root names.
Baked at build: the root is erofs and tmpfiles is masked.

sshd as that user can only log that user in, so the SSH login *is* the run user: home its
`state=` directory (else `/`), shell `/bin/sh`, password field `x` with no shadow entry
(not locked: OpenSSH without PAM refuses a `!` hash even to a key).

## sshd

`/etc/ssh/tundravm/sshd_config` in the root: the port, the host key, `AuthorizedKeysFile`,
`AllowUsers <user>`, `PermitRootLogin no`, `PasswordAuthentication no`,
`KbdInteractiveAuthentication no`, `DisableForwarding yes`, `PermitTunnel no`,
`PermitUserEnvironment no`, `PermitUserRC no`, `UsePAM no`, `PidFile none` (dropbear's
`-s -w -g -j -k`). The host keys the `openssh-server` postinst made are deleted from the
root: they would be the same in every instance and break reproducibility. The unit's
sandbox is `static` with `@resources` allowed back: sshd's preauth sandbox calls
`setrlimit` and dies on `EPERM`.

## Keys and host key

`<name>-ssh-keys.service`, a root oneshot needing no runtime-init step (the endpoint waits
for it), runs `/usr/lib/tundravm/container-ssh/<name>`: it makes
`/run/tundravm/containers/<name>/{host,keys}`, generates an ed25519 host key into `host/`
once per boot, owned by the container's user, mode 0600, and prints
`tundravm-container-ssh: <name> SHA256:...` on the console. The unit binds `host/` and
`keys/` read-only at `/etc/ssh/tundravm/host` and `/etc/ssh/tundravm/keys`: directories, so
the endpoint's atomic rename of a delivered key shows inside. A delivered `keys=` is also
written to `keys/authorized_keys` (0644), and the endpoint gates it as a login's key (TDX:
only after the owner is confirmed). Per boot, like the host's own keys: `attest` pins them
again each boot. Not in `state=`: the container could rewrite what it writes.

## The quote

The endpoint's config lists each container: name, port, host key glob, keys directory.
`GET /evidence` carries `containers: [{name, port, ssh_host_keys}]` and the binding gains
one part after the instance key: the sorted lines `<name> <port> <type> <base64>`. The
purpose becomes `tundravm-endpoint/2`. `tundravm attest` writes
`[host]:<forwarded port> <key>` lines for them into the instance's `known_hosts`, so
`ssh -p <port> -o UserKnownHostsFile=... <user>@host` checks the bound key.

## Account clashes

User accounts gain an `owner` (`Login('searcher')`, `Container('searcher')`):

- two declarations of one account are refused, naming both owners, with a `fix:`;
- a service whose implicit user (its unit's stem) is an owned account is refused: it
  would run as the login, with its keys and sudo rules, or share the container's uid;
- a service whose explicit `user=` is a login account is refused for the same reason;
- two pinned uids that are equal are refused (two container users that hash alike).

## Proof

The fixture's `box` container gains `ssh=ContainerSsh(port=10022, keys=Secret("box-keys",
required=False))`. The probe's `container-ssh:box` row: the unit runs as the uid its root's
`passwd` names, sshd listens on 10022 in its namespace, and the evidence binds the key on
disk. `fixture-boot` delivers `box-keys`, logs in with `ssh -p` against attest's
`known_hosts` and runs `true`, is refused as root and with a password, and compares
`ssh-keyscan` with the bound key.
