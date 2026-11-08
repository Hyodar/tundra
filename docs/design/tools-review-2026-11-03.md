# Security review of tools/, 2026-11-03

`tools/` against [trust.md](../trust.md), the host as adversary (network, disks and
plaintext LUKS2 headers, clock, reboots). Ours first, then Codex, read-only; merged. By: A
ours, C Codex. **B** breaks a trust claim, **W** weakens it, **H** hygiene.

| # | Sev | By | Where | Attack | Fix | Here |
|---|---|---|---|---|---|---|
| 1 | B | AC | `endpoint` `authorize`, `rotate`; `bootstrap.signed` | Owners signed the client's nonce alone: any endpoint that saw one replays it on the real one under that nonce, and `/owner` rotates to its key. | Sign the hex user data, which binds the nonce and the boot's TLS key. | yes |
| 2 | B | AC | `endpoint` `sshKeyHandler`, `credentials`; `_runtime.delivery` | Every `Image` endpoint wrote the first `POST /ssh-key` into root's `authorized_keys`, and the first `Login(keys=Secret)` credential into `authorized_keys.d`; the host is always first. | On TDX only a confirmed owner's keys (#3); simulated keeps the first. | yes |
| 3 | B | AC | `disks.reconcile`, `authorize` | A rewritten token 2 made the host the boot's owner before the encrypted copy was checked: root. | `disk-setup` writes `<owner>.confirmed` once the copy matches; the SSH gate needs it. | yes |
| 4 | B | C | `keys.generateRandom` | Raw `/dev/hwrng`, the host's virtio-rng, became the `TpmSeal` key. | `crypto/rand` only. | yes |
| 5 | B | A | `tdxs` socket, `endpoint.Binding` | A `tdx`-group service quotes an endpoint binding for its own TLS key; the host routes 8745 to it and gets passphrases. | Endpoint quotes on a root-only socket; the group socket domain-separates by caller. | no: tdxs, units, verifiers; trust.md says it |
| 6 | B | A | `endpoint.unlock`, `BrokerKey` | `/unlock` is unauthenticated: the host unlocks a fresh broker volume first, keyed by the host. | Below, for broker-key. | no |
| 7 | W | A | `authorize`, no owner | The first writer of each credential wins: the host injects values. | An owner for any image with secrets. | doc |
| 8 | W | AC | OperatorKey first boot | The host pins, unlocks with its own passphrase, gets a confirmed owner; AttestedTLS clients see no owner. | Bake the operator key. | doc |
| 9 | W | AC | `verify.judge`, `attestation/verifier` | No collateral, TCB or revocation check: a revoked platform is `trusted`. | Check them for production. | no |
| 10 | W | AC | `verify.AttestedTLS` | The certificate is checked on a throwaway connection against any bound port. | Return a `tls.Config` pinning the requested port's key. | no |
| 11 | W | C | in-guest validators | Before NTS syncs, the host's clock judges certificates. | Order after `time-sync.target`. | yes: time-sync |
| 12 | W | C | `verifier/*` | DEBUG TDs are `Valid`; `VerifyIdentityToken`, `VerifyIMDS` check presence only. | Refuse DEBUG; verify or drop the options. | yes: refuse DEBUG, drop tokens |
| 13 | W | C | `rotate`, `WatchOwners` | A kill after the 200, before the header write, keeps the old owner. | Answer once header and copy are written. | yes: rotate waits for headers |
| 14 | W | C | signer `signResult` | Signatures omit method, params, freshness. | Sign request, result and a challenge. | no |
| 15 | W | C | `mountExistingEncrypted`, `checkCopy` | A failure after `luksOpen` left the mapping open. | Close on every failure. | yes |
| 16 | H | A | `tdxs` `handleConnection` | A malformed request spun forever (sticky decoder error). | Answer once, hang up. | yes |
| 17 | H | A | `authorize` | A second OperatorKey volume was never pinned, so never created. | Pin every empty owner file. | yes |
| 18 | H | A | trust.md recovery | It said resend a mistyped passphrase; a boot takes one per volume. | Restart first. | doc |
| 19 | H | AC | `credentials` | A failed second target leaves a half delivery. | Stage all, then rename. | no |
| 20 | H | A | `formatAndMountEncrypted` | A kill between init token and `mkfs` leaves a volume never formatted. | Token after `mkfs`; close on failure. | no |
| 21 | H | A | `LuksOps` | Stdin passphrases stop at the first newline. | `--key-file=-`. | no |
| 22 | H | AC | `evidence`, `/quote`, IMDS, socket, `image-metrics` | Unauthenticated quotes and unbounded reads (DoS); `%q` is not Prometheus escaping. | Limits; escape labels. | no |

## For other agents

- **broker-key** (#6, #9, #10; `verify.go` is theirs): give `endpoint.Volume` a
  `Kind string \`yaml:"kind"\`` that `SecretDelivery.render_config` writes, and in
  `unlock`, before `writePipe`, answer 403 when `v.Kind == "broker-key" && s.mode() !=
  "simulated" && !isLoopback(r.RemoteAddr)`. A broker client writing to an owned image
  signs `hex(user_data)`, as `bootstrap.signed` now does.
- **ssh-login**: credentials under `login_keys` (default `/etc/ssh/authorized_keys.d`)
  or `ssh.dir` pass the `/ssh-key` gate. Docstrings of `SshAccess(from_delivery=)`, `Login`
  and `_runtime.py` should say: on TDX, keys from a confirmed owner only.

## Decisions

- #2 and #3 change behaviour: on TDX an image without an `OperatorKey()` volume gets no
  delivered root key; `deploy upgrade` hands none.
- `FakeEndpoint` verifies signatures and mirrors the SSH gate (`confirmed=`).
