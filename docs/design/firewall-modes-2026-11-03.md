# Firewall modes

Date: 2026-11-03. Status: design, then landed in the commits that follow it. Item 37 of
`status-2026-11-01.md`, section 2 of `flashbots-images-gaps-2026-11-02.md`: an operator
opens a maintenance window (SSH, a debug port) on a running instance and closes it, and
whoever verifies the instance sees that it happened.

## The declaration

```python
Firewall(modes={
    "production": Mode(ingress=[TCP(9000)]),
    "maintenance": Mode(ingress=[TCP(22)], egress=Egress(hosts=["deb.debian.org:443"]), lasts="30m"),
})
```

`production` is required and the image boots into it. A port a mode names opens in that
mode only, whatever listens on it, so `SshAccess`'s port 22 stays closed until a switch.
What no mode names is open in every mode, as today; a mode's `egress=` adds to the image's.
`lasts=` (`for` is a Python keyword) closes the window: every mode but `production` has one,
30 minutes unless given, counted on `CLOCK_BOOTTIME`, which neither the host's clock nor a
paused VM moves backwards.

Booting into `production`, not `stopped`: flashbots' `stopped` is a cooldown before
maintenance, timed on the host-set wall clock and skipped by a reset. Here a reset boots
into production with an empty history, which is the evidence's default, and a window never
survives a reboot. A cooldown is the verifier's to want: every switch carries its boot time.

## Refused

- a mode named `production` that opens 22: production is what the policy trusts;
- a mode named `tdx` or `simulated`, the security mode's words, or not `[a-z][a-z0-9]*`;
- `lasts=` on `production`, and a mode's `egress=` when the image does not filter egress;
- a switch on a production build without an owner a volume's encrypted copy confirmed this
  boot: the endpoint answers 403, and the CLI refuses an image with
  no `OperatorKey()` volume before sending.

## The switch

`tundravm run switch INSTANCE maintenance` (`deploy switch` for a cloud instance; `run mode`
is the security mode) verifies the instance again with the last approved policy, as
`secrets unlock` does, and sends `POST /mode {"mode", "nonce"}` on that quote-bound
session. In an image that pins its operator the write carries the owner's SSH signature over
the evidence's user data, checked by the endpoint's `authorize` (`tools/pkg/owner`), like every
other write.

The endpoint hands the name to `mode-switch` (`tools/cmd/mode-switch`): a root daemon with
no network address family, on `/run/tundravm/mode-switch.sock` (`0600`). For each switch it:

1. extends RTMR3 with `SHA-384("tundravm:mode:<name>:<boottime-seconds>")`;
2. appends the event to `/run/tundravm/firewall-events`;
3. loads one `nft -f` transaction: `mode_input`/`mode_output` jump to the mode's chains and
   the `current_mode` set holds the mode's mark.

The extend comes first: a switch the quote does not show never happens. A failed load
exits the daemon, and its restart loads the mode the log ends in. The window closes itself
the same way, with an event of its own.

The extend is the switcher's, not tdxs's: tdxs is a non-root service any `tdx` member may
ask, and its sandbox would have to open `/sys`. On TDX the switcher writes
`/sys/class/misc/tdx_guest/measurements/rtmr3:sha384` (Linux 6.16; trixie's 6.12 lacks it,
so a TDX instance refuses every switch until its `Kernel` is newer). Under the simulator
tdxs reports RTMR3 as the replay of the log, which proves nothing, as the simulator never
does.

## Flows

Every flow carries a `ct mark`: 0 for the ports open in every mode, the mode's for the ports
it opens. The established rule accepts mark 0 and the `current_mode` mark only, so a switch
drops the old mode's flows on their next packet, in the same transaction, without
`conntrack -F` (which the image does not ship).

## The evidence

`GET /evidence` carries `firewall: {mode, events}`, read before and after the quote until
both agree. `attest` replays the events from zero and compares RTMR3:

```
modes  production; 2 switches since boot: maintenance at 412s, production at 530s (RTMR3 replays them)
```

A policy with `"modes": ["production"]` refuses an instance that was in any other mode since
boot (`mode-refused`); a log that does not replay is `mode-history-mismatch`. Both make the
verdict a mismatch. Without `firewall` in the evidence RTMR3 stays outside the policy, as
before. `run status` keeps the last mode seen; the probe's `mode:firewall` row
prints the mode, its switches and whether RTMR3 replays them.

## Not here

A switch scheduled ahead, modes as scripts, a mode in `examples/attested-node`.
