# Review of the login CLI: generator, runtime, sudoers, `tundravm ssh`

Date: 2026-11-08. Scope: `tools/logincli/cli.go` and its tests, the reference program
(`main.go`, `dev.go`), `src/tundravm/declarative/_login.py` and its tests, the flashbox and
fixture trees, `src/tundravm/_ssh.py` with the `ssh` command in `cli.py`, and
`bootstrap.pin_host_keys`, read as someone holding a login's key who can type anything into
`SSH_ORIGINAL_COMMAND`, and as a reviewer who wants the sudoers file to admit exactly what
the program can exec. No boot test was available; sudoers parsing was checked with sudo
1.9.15p5's `visudo -c -f` and `cvtsudoers -f json`, which show how each line is read.
Line numbers are those of the branch head.

## Findings and fixes

- `1bfcb605` (security): a literal `runs` word holding `#` turned the rest of its sudoers
  line into a comment, so `["/usr/bin/x", "#c"]` became `/usr/bin/x`, which admits any
  arguments as root, and `visudo -c` passes it. The path of a pattern line went unescaped
  (`=` split it, `:` broke the file), and a literal line whose arguments start with `^` and
  end with `$` is read as a regex. `_sudo_word` (`_login.py:841`) now escapes `, : = #`
  everywhere and a leading `^` is escaped.
- `08f687e5` (security): sudo matches a pattern line against the arguments joined by
  spaces, so a pattern able to match a space (`.`, `[^/]`) admitted argvs of more words
  than the program passes. `_sudo_pattern` (`_login.py:941`) refuses those and anything
  outside printable ASCII.
- `4ebf822d` (security): `ParamType.check()` was pasted into `main.go` after `return`, so a
  type could close the `Check` closure and add a `func init()`. `_checked`
  (`_login.py:1057`) writes it only when `_CHECK_CALL` (`_login.py:44`) matches: one call of
  `Int`, `OneOf` or `Match` on `value` with decimal or Go string literal arguments.
- `a2b9e5a1` (security): the declaring file's name went raw into the first `//` comment, so
  a name with a newline put Go into the program; it is now JSON-escaped (`_login.py:993`).
- `faf5fe05` (security): sudoers lines were spelled word by word, so a choice or flag named
  in two words took independent values (`/x a --to=b` was admitted, never passed). The
  lines are now every assignment of the command's flags and choices (`_login.py:741`), and
  a patterned param named twice is refused (`_login.py:705`).
- `3253fc97` (fix): the 64-line cap was `len(self.sudoers())`, spelling 2^n lines before
  refusing; it is now the product of the domains (`_login.py:725`).
- `6837c00a` (security): `Match` ran its pattern unanchored, so `"[0-9a-f]{64}"` admitted
  `--output=/etc/x<hex>`, and a command not run through sudo had no other check. It now
  compiles `^(?:pattern)$` (`cli.go:126`); the three trees carry the new runtime.
- `b2e71360` (fix): `--` did not end `tundravm ssh`'s own options, so
  `ssh I --login=bob -- -x` re-parsed `-x` as INSTANCE (`cli.py:2105`).
- `93f8dc19` (fix): `known_hosts` was written in place, so a torn write left a truncated
  file that the boot probe never re-pinned; it now replaces the file by rename
  (`bootstrap.py:690`).

Franco's decisions on the open questions became five more commits:

- `7a48b196` (security): a `pattern()` or `Match` check that may admit a value starting
  with `-` is refused at declaration (`login-param-option-like`): each alternative must
  begin with a literal character or a class that leaves `-` out, with no quantifier letting
  it be absent, and a group, `.`, a negated or POSIX class counts as admitting it.
- `e2f3b6f3` (fix): `help` is refused as a choice, as `--help` and `-h` already were.
- `320b7831` (security): evidence is refused unless each host and container key is
  `ssh-ed25519`, `ecdsa-sha2-*` or `ssh-rsa` and its base64, one key per entry, so no
  newline or `@cert-authority` reaches `known_hosts`.
- `82fe1a3e` (fix): a sudoers line over 1024 characters is refused at generation
  (`login-sudo-line-length`), since `visudo -c` passes it and sudo refuses it at run time.
- `tundravm ssh I -- --help` stays as it is: argparse's pattern for INSTANCE takes the `--`
  after it and strips it before the remote words' action runs (`argparse.py:1976`, `-*A-*`),
  so the action cannot tell `I -- --help` from `I --help`; fixing it means making INSTANCE
  part of the remote words, which changes the command's grammar and its reference.

## The six checks

1. Generated-code injection: no, after `4ebf822d` and `a2b9e5a1`. Every other string
   (command and param names, help, usage, literals, paths, choices, modes, defaults) goes
   through `_go` (`_login.py:124`), JSON escaping, which Go reads as the same string; a
   U+FEFF in a help string fails the build rather than changing it. The `go.mod` module line
   is the login name, held to `[a-z_][a-z0-9_-]{0,31}` (`utils.py:1067`).
2. Sudoers exactness: yes, after the four sudoers fixes. `*?[]\"'` stay refused
   (`_login.py:38`); a pattern line is one anchored regex with each literal from
   `[A-Za-z0-9/_.+-]` and `.`/`+` bracketed (`_login.py:40`); choices come from
   `_login.py:30` and need no escaping; a command without arguments gets `""`. The `--`
   after `sudo -n` (`cli.go:138`) ends sudo's options (sudo(8): "the -- is used to delimit
   the end of the sudo options"), so sudoers matches the argv after it. `Int`'s `[0-9]+`
   admits what the program refuses (leading zeros, out of range), never the reverse. The
   regenerated `sudoers.d/searcher` and both `sudoers.d/tenant` pass `visudo -c -f`, as does
   a file of the new table rows.
3. Request parsing: safe. `strings.Fields` (`cli.go:205`) splits the raw request and no word
   is ever joined back into an argv, so a value cannot hold a space. An option takes the next
   word whatever it is (`cli.go:284`), and `Check` refuses `-5` or `--follow` as an `Int`;
   `--name=` is refused as empty; `--` is refused (`cli.go:265`); a repeated option or extra
   positional is refused. `Int` is an ASCII regex before `ParseInt` (`cli.go:96`), so no
   non-ASCII digit passes; `OneOf` compares bytes. A help word anywhere prints the help and
   exits 0 before any `Check` or exec (`cli.go:228`). Refusals exit 2, a mode refusal 1.
   `dev=True` commands live only in `dev.go`, which `//go:build dev` keeps out of a build
   without `-tags dev`.
4. Environment and exec: safe. The exec environment is fixed (`cli.go:160`): `PATH`, `HOME`,
   `USER`, `LOGNAME`, `LANG`, and `TERM` only if it matches `cli.go:98` (no `/`). The
   argv's first word is an absolute path (refused otherwise in `_check_words`) and
   `syscall.Exec` gets it as is; a missing program prints `login: path: no such file or
   directory` and exits 2. `modeIn` (`cli.go:176`) reads production for a missing file and
   refuses a gated command on any malformed event; the file is root's, 0644
   (`tools/pkg/modes/switch.go:130`), under a root-owned directory.
5. `tundravm ssh` argv: safe. `--` precedes `user@host` (`_ssh.py:155`), so OpenSSH takes
   no option after it; `--login` must equal an account the tree names or `root` on a
   development build (`_ssh.py:70`); `-i` and its path are separate words (`_ssh.py:149`);
   `-p` is the receipt's port as an int (`_ssh.py:151`); `os.execvp` runs no shell.
6. `pin_host_keys`: safe after `93f8dc19` and `320b7831`. It writes only when the verdict
   passes against the build's `measurements.json` (`bootstrap.py:702`), makes one attempt,
   and the probe pins only when no `known_hosts` exists (`cli.py:1544`). A failed judgement
   writes nothing and the probe says which verdict it got; each key it writes is one
   `type base64`.

## Left as they are

- The mode gate is the program's alone, accepted by design: sudoers cannot say
  "maintenance only", so a gated root command's line admits it in every mode, and
  ForceCommand is the only route from the login to sudo.
- argparse drops a `--` right after INSTANCE, as above; the login's own spelling is `help`.
