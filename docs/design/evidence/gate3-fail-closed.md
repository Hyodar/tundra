# Gate 3 evidence: the fail-closed production profile (plan 6.1)

Plan section 8's gate 3 row asks for `run --mode` separation, and plan 6.1 says how: a
production profile refuses to start its services without a TDX quote path, the dev profile
may run simulated, verifiers reject simulated and debug evidence for production, and there
is no fallback. This page records what was built and the CI run that proves the image half:
run `41162292492` of the `ci` workflow on the scratch branch `ci/fail-closed`, at `9358f60`
(the work rebased on master `977b6e8`). The `fixture-boot`, `verity`, `verity-cloud` and
`reproduce` jobs passed. `test` and `boot` failed only where master does: the template bakes
need the private tundra-tools repository.

## The rule

Every tree the compiler writes ships `tdx-evidence.service` and its script,
`/usr/lib/tundravm/tdx-evidence` (`src/tundravm/_modules/execution.py`). It is a root
oneshot with `RemainAfterExit=yes`, no capabilities and no network, enabled into
`minimal.target` and ordered after `systemd-udev-trigger.service`. It looks for the
`tdx_guest` CPU flag, then waits up to 30 seconds for `/dev/tdx_guest` or
`/sys/kernel/config/tsm/report`. It records `tdx` in `/run/tundravm/mode` when it finds
one. Without one:

| Profile | Gate | Services |
|---|---|---|
| production (`Variant(release=True)`, the image itself) | fails: `tdx-evidence: refused: no TDX quote path; a production profile starts no services` | none start: `Dependency failed` |
| development (`release=False`, `Profile(production=False)`, `dev=`) | records `simulated`, succeeds | run |

The payload's unit carries the default profile's mode in `Environment=TUNDRAVM_EXECUTION=`.
A profile whose mode differs adds the drop-in
`tdx-evidence.service.d/50-tundravm-execution.conf`, so the shared payload stays the same
whichever profiles a compile selects (`tests/compiler/test_profile_overlays.py`).

Gated: `Requires=` and `After=` on the gate are added to every unit lowered from a
`Service`, in images and in declarative fragments, and to every scheduled `Task`. That
includes `tdxs.service`, the attested-TLS and TLS relays, the observability exporters and
the components' services. Not gated:

- boot `Task` units: the fixture's `boot-probe`, readiness probes (they require their
  service, so they fail with it), the secret waits and `ssh-host-keys`;
- `Unit` declarations, packaged or verbatim (`ssh.service`), and mounts;
- `runtime-init.service` and its steps (key generation, disk setup, secret delivery): the
  control plane, which comes up before applications;
- the cloud glue the targets add (`gcp-metadata-dns`, `azure-complete-provisioning`).

The ungated units keep the console readable and the management port answering on a
refused boot. `ConditionSecurity=` is not used: it knows `cvm`, not `tdx`, and a skipped
condition does not stop a `Requires=` dependent. The gate takes no test quote. A quote
round-trip needs the host's quote service and a mounted configfs, which the systemd
debloat leaves unmounted; `tdxs` makes the first quote and the verifier judges it.

## The production boot without TDX

`tests/integration/test_fixture_boot.py` boots the fixture's production build twice
(`payload.efi` through `-kernel`, `qemu.raw` through its ESP) and asserts, through
`tests/integration/support.py`'s `assert_fail_closed`: `minimal.target` reached, nothing
fatal, the probe done, the gate's line on the console, `tdx-evidence.service`,
`web.service` and `greeter.service` failed and nothing else, and neither service started.
The `qemu.raw` boot's console, with the networkd lines cut:

```
[  OK  ] Reached target basic.target.
[    1.646801] systemd[1]: Starting boot-probe.service...
[    1.652659] systemd[1]: gcp-metadata-dns.service was skipped because of an unmet condition check (ConditionVirtualization=google).
         Starting tdx-evidence.service...
[    1.668491] systemd[1]: Starting tdx-evidence.service...
[    1.691493] sh[187]: boot-probe: cmdline console=ttyS0 systemd.show_status=yes systemd.status_unit_format=name systemd.log_color=no systemd.journald.forward_to_console=1 panic=-1
[  OK  ] Finished boot-probe.service.
[    1.703573] sh[187]: boot-probe: mount / rootfs rootfs rw,size=1974288k,nr_inodes=493572,inode64
[    1.707419] sh[187]: boot-probe: usr-bin write=accepted
[    1.720129] sh[187]: boot-probe: done
[    1.733009] tdx-evidence[197]: tdx-evidence: refused: no TDX quote path; a production profile starts no services
[FAILED] Failed to start tdx-evidence.service.
See 'systemctl status tdx-evidence.service' for details.
[DEPEND] Dependency failed for greeter.service.
[    1.737373] systemd[1]: tdx-evidence.service: Main process exited, code=exited, status=1/FAILURE
[DEPEND] Dependency failed for web.service.
[    1.751155] systemd[1]: tdx-evidence.service: Failed with result 'exit-code'.
[    1.763584] systemd[1]: greeter.service: Job greeter.service/start failed with result 'dependency'.
[    1.769050] systemd[1]: web.service: Job web.service/start failed with result 'dependency'.
[  OK  ] Started systemd-networkd.service.
[  OK  ] Reached target network-online.target.
[  OK  ] Reached target minimal.target.
[    1.931304] systemd[1]: azure-complete-provisioning.service was skipped because of an unmet condition check (ConditionVirtualization=microsoft).
[    1.944267] systemd[1]: Startup finished in 1.268s (kernel) + 675ms (userspace) = 1.944s.
```

The `-kernel` boot prints the same lines. The `verity` job boots the production verity
build the same way: the probe proves the erofs root on dm-verity under a volatile overlay
while the services stay fail-closed. `reproduce` built the `azure` and `gcp` profiles twice
with identical digests, so the gate is deterministic.

## The dev build, run simulated

The same job builds the dev profile (`build --profile dev`), the first build of the
fixture's dev overlay in CI, and both directories verify. `show --diff latest
latest-development` lists the overlay, the gate's drop-in among its units:

```
units: 3 changed
  + payload/mkosi.profiles/dev/mkosi.extra/usr/lib/systemd/system/ssh-host-keys.service
  + payload/mkosi.profiles/dev/mkosi.extra/usr/lib/systemd/system/ssh.service.d/10-tundravm-needs.conf
  + payload/mkosi.profiles/dev/mkosi.extra/usr/lib/systemd/system/tdx-evidence.service.d/50-tundravm-execution.conf
```

Then it runs the dev build and fetches the page through the forward:

```console
$ tundravm run latest-development --mode simulated --detach
instance fixture-1: simulated (development build 52cada858fdad00c)
  :22    -> 127.0.0.1:41683  (ssh)
  :8080  -> 127.0.0.1:8080  (service)
  :8745  -> 127.0.0.1:8745  (management)
  verdict: simulated (no TDX: never production evidence)
  console: /home/runner/work/tundra/tundra/.tundravm/instances/fixture-1/console.log
stop it with `tundravm run stop fixture-1`
page served on :8080 after 1 attempts
$ tundravm run status fixture-1
id             fixture-1
mode           simulated
state          running
control_plane  listening
application    listening
build          52cada858fdad00c (development)
$ tundravm run mode fixture-1
fixture-1: simulated on qemu, development build: no TDX: evidence is `simulated`, never production
$ grep -F 'tdx-evidence: no TDX quote path; development profile, running simulated' .tundravm/instances/fixture-1/console.log
[    2.024820] tdx-evidence[204]: tdx-evidence: no TDX quote path; development profile, running simulated
```

`run --mode simulated` of a production build still boots, so its console shows the refusal.
`run status` then prints a `services` line, `fail-closed: a production build without TDX;
tdx-evidence.service refuses and no service starts`, and `run mode` says the same
(`tests/cli/test_run.py::test_a_production_build_runs_simulated_fail_closed`).

## The operator side

- `attest` parses the quote's `TDATTRIBUTES` (`Quote.debug`, bit 0) and reads
  `/run/tundravm/mode` over the bootstrap session. The guest's `simulated` makes the
  evidence simulated. Since the guest says it, it can never make evidence less simulated.
- Checked for a production build, simulated evidence never passes (`allow_simulated` is
  ignored), and a debug TD's quote is `untrusted`. Reasons gain `debug`, and `to_dict()`
  and the `tundravm.attestation/2` record carry `debug`, `simulated`, `production` and
  `guest_mode`. `read_attestation` re-derives `debug` from the quote and flags an edited
  one.
- `release_predicate` refuses a production build in a simulated instance whatever the
  verdict (`fail_closed`). It refuses simulated evidence from a TDX instance, and a
  production build's `DEBUG` attribute. `secrets deliver` and `secrets unlock` refuse that
  instance before the management port, SSH or the passphrase prompt is touched, and
  record `delivery-refused` or `unlock-refused`.

The negative tests possible without a TDX host, in `tests/cli/test_fail_closed.py`:

- simulation: `test_production_delivery_refuses_simulated_evidence`,
  `test_a_production_policy_rejects_simulated_evidence`,
  `test_the_guest_gate_mode_makes_tdx_evidence_simulated`,
  `test_a_production_build_in_a_simulated_instance_gets_nothing_whatever_it_answers`
  (each verdict), `test_a_development_build_in_a_simulated_instance_accepts_simulated_only`,
  `test_deliver_and_unlock_refuse_a_production_build_in_a_simulated_instance`,
  `test_attest_reads_the_guest_gate_mode_over_the_session`;
- debug attributes: `test_the_quote_carries_the_debug_attribute`,
  `test_a_production_policy_rejects_debug_evidence`,
  `test_production_delivery_refuses_debug_attributes`,
  `test_a_record_re_derives_debug_and_catches_it_edited`;
- partial coverage: `test_production_delivery_refuses_partial_coverage`.

The gate itself: `tests/compiler/test_execution_gate.py` checks what is gated and what is
not, each profile's mode and drop-in, an `Image`'s `tdxs.service`, and runs the script
against stand-ins for `/proc/cpuinfo`, the guest device and the mode file:
`test_without_tdx_a_production_profile_refuses`,
`test_a_tdx_cpu_flag_without_a_quote_path_is_still_refused`,
`test_without_tdx_a_development_profile_records_simulated`,
`test_with_a_quote_path_either_profile_records_tdx`.

## The probe

Plan 5.2 item 7: the dev profile carries `tundravm-probe.service`, the checklist lowering
derives from its declarations (`src/tundravm/_modules/probe.py`); the production profile
carries none. The fixture's dev variant adds an egress-filtering `Firewall` (nothing
allowed beyond DHCP) and a scheduled `tick` job for it. Run `41294406710` on `ci/probe`
(on master `e6c7d71`): the `fixture-boot` job ran the dev build with `tundravm run
latest-development --mode simulated --probe`, which exited 0 and wrote `probe.json`:

```
check                      verdict  detail
mode                       ok       simulated
rtmr                       skip     no TDX: a simulated guest has no registers to compare with measurements.json
service:greeter.service    ok       exited 0
service:web.service        ok       active
timer:tick.timer           ok       active
port:8080/tcp              ok       listening
egress:192.0.2.1:9         ok       refused by the output chain (Operation not permitted)
hardening:greeter.service  ok       exposure 1.5, ceiling 2.5 (static)
hardening:web.service      ok       exposure 1.5, ceiling 2.5 (static)
kernel:modules             ok       13 loaded, each curated or held by one
host-keys                  ok       /etc/ssh root:root 755, ssh_host_ed25519_key root:root 600
10 ok, 0 fail, 1 skip
```

`show latest` printed `boot probe    none: a production build carries none`, and the
production boots of the fixture and the verity fixture printed no probe line
(`test_the_production_build_carries_no_boot_probe`).

The dev variant then gained `Observability(proxy=)` exporting to `sink`
(`tests/fixtures/sink`, the proxy and both receivers inside the guest). Run `41311712008`
on `ci/observability-proof`: `run --probe` exited 0 with 17 ok, 0 fail, 1 skip, these rows
among them:

```
observability:journal-export.service  ok  active, uploaded to http://127.0.0.2:19580 (its cursor is in /var/lib/journal-export/state)
observability:metrics-export.service  ok  active, promtool check config --agent accepts /etc/tundravm/metrics-export.yml
observability:no-wait                 ok  3 services start without waiting on them
observability:egress                  ok  1 rules loaded
```

Prometheus 2.53.3 loaded the rendered queue (`retry_on_http_429` included), and `sink`
printed `proxy carries 127.0.0.2:19580` and `proxy carries 127.0.0.2:19581`, then journal
batches (`journal batch 1 32768 bytes`, `journal batch 2 381651 bytes`) and remote-write
batches, both through the proxy. An earlier run (`41308131113`) had the uploader active
and sending nothing: the image drops systemd's tmpfiles.d and masks
`systemd-tmpfiles-setup.service`, so the journal stayed `root:root`, unreadable to the
`systemd-journal` group `journal-export` runs in; `journal-export` now runs
`/usr/lib/tundravm/journal-access` as root first. The same masking means a `File(owner=)`,
which `/etc/tmpfiles.d/50-tundravm-owned.conf` applies, is likely never applied either.

Not yet booted, compiled and asserted only (`tests/compiler/test_probe.py`):

- `volume:`: `run` attaches a disk only for an `Image`'s runtime manifest. Since
  `disk-setup` and the other tools build from `tools/` of this repository, the `boot`
  job's `test_service_template_dev_probe_passes` no longer skips: run `41311483505`
  (`ci/tools-monorepo`) is the first to boot an `Image`'s probe; this entry leaves the
  list once a green run records its `volume:` row.
- `kernel:symbols`: no built kernel boots in CI, and the table of option to symbol
  (`SYMBOLS`, ten global functions) is unverified against a real `/proc/kallsyms`.

## The attested endpoint (plan 6.3)

The relay over SSH is gone. An image with a volume or a secret (an `Image`), or any
`Secrets` block (a declarative recipe), runs `tools/cmd/attested-endpoint` as
`tundravm-endpoint.service` on its port (8745 for an `Image`). It is a control-plane unit:
not gated, ordered after the execution gate, the evidence socket, the SSH host keys and the
instance key, and held up by no runtime-init step, so it answers while a volume still waits
for its passphrase. It is its own binary rather than part of `tdxs`: `tdxs` stays an
`AF_UNIX` quote service with one capability, while the endpoint listens on the network and
writes root-owned files (credentials, `authorized_keys`, key-gen's FIFO), as root with
`CAP_CHOWN` alone and `ProtectSystem=strict`.

The protocol (`tools/pkg/endpoint`, `src/tundravm/bootstrap.py`):

- At start the endpoint makes an ECDSA P-256 TLS key in memory and a self-signed
  certificate; it serves TLS 1.3 only, and every TCP connection is a session.
- `GET /evidence?nonce=HEX` (16 to 64 bytes) reads the SSH host public keys
  (`/etc/ssh/ssh_host_*_key.pub`, sorted `type base64`) and the instance public key
  (`/run/tundravm/instance-key.pub`), computes the binding
  `sha512(lp(purpose) ‖ lp(nonce) ‖ lp(TLS SPKI) ‖ lp(host keys) ‖ lp(instance key))`
  (`lp` is a big-endian uint32 length, the purpose `tundravm-endpoint/1`) and asks `tdxs`
  to issue with that binding as user data and the nonce. The tdxs issuers put
  `sha256(user data ‖ nonce)` in `report_data`, which the simulator's 32-byte quote can
  carry too, so the binding is the user data rather than the raw `report_data`. The
  answer carries the document, the simulator's metadata, every bound part, the gate's mode
  and what the endpoint accepts (credential names, volumes, an SSH key). The session
  remembers the nonce.
- `POST /credentials`, `/unlock` and `/ssh-key` carry a nonce. The endpoint accepts it only
  from the session that fetched evidence for it, once (403 otherwise). A credential name is
  written once per boot (409 again), atomically, with its configured mode and owner. An
  unlock writes the passphrase into key-gen's FIFO for that volume, non-blocking: 409 when
  nothing reads it. The first SSH key wins for the boot.
- The client opens one session, reads its peer's SPKI from the certificate, sends 32 fresh
  bytes and refuses evidence for another nonce, for another TLS key, or whose parts do not
  hash to the user data. Then `attestation.attest` judges the quote with that user data:
  a TDX quote against the policy, a simulator quote by its HMAC tag under the simulator's
  published key, its registers reported and never compared, verdict `simulated` at best.
  Before each write it checks the session's key is still the bound one.

`/unlock` is implemented: disk-setup takes its key from key-gen's pipe strategy, whose FIFO
the endpoint writes.

The negative tests, one table each:

| Contract | Python (`tests/cli/test_secrets.py::test_delivery_is_refused`) | Go (`tools/pkg/endpoint/server_test.go::TestWritesAreBoundToTheSession`) |
|---|---|---|
| relay: evidence on one session, delivery on another | the session's key changed after the evidence: refused before anything is sent | 403 on the second session |
| replay: an old nonce | evidence answering another nonce: refused | a nonce already used: 403 |
| wrong key | the quote binds another TLS key than the session's: refused | `TestEvidenceBindsTheSessionKey` |
| simulated in production | a production build on TDX answered with simulator evidence: nothing sent | |
| debug attributes | a production build's TD with `DEBUG`: nothing sent | |

The fixture's dev profile carries `Tdxs(issuer="simulator")` and a `Secrets` block on 8745
with one optional secret, so the probe gains `endpoint:8745` (evidence answers the nonce it
was given) and `fixture-boot` runs, after `run --probe`, `attest fixture-1 --policy
measurements.json` (verdict `simulated`, `known_hosts` written) and `secrets deliver
fixture-1 greeting-token=@...`, then finds the endpoint's `credential delivered` line on
the console.

The boot that runs it is the `fixture-boot` job of run `41316489558` on the scratch branch
`ci/gate3-endpoint` at `1c280f1`; this page was written before that run finished, so its
result is not recorded here.

## Not proven

- ~~The replayed-quote and wrong-recipient-key tests of plan 6.3~~: done above, against
  `FakeEndpoint` and the Go server; no TDX quote has run the protocol yet.
- The instance key (`/run/tundravm/instance-key.pem`, made every boot) is not in the
  quote's `report_data`; `attest` prints its public key beside the quote as "not yet bound
  to the quote" until gate 3 binds it.
- The gate's `tdx` branch has run only against stand-in paths. No TDX guest has booted a
  gated image (gate 2).
- A dev profile on QEMU records `simulated`, but an `Image`'s `tdxs` still issues as `tdx`
  and has no quote to give, so `attest` of a simulated `Image` fails at the issuer unless
  the image configures the `simulator` issuer itself, as the fixture does.
- ~~`Image` builds are not booted in CI~~: since `tools-monorepo` the `templates` and
  `boot` jobs build and boot them from `tools/` (run `41311483505`); the first green `boot`
  is run `41313287454`.
