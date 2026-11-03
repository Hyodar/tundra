# Trust, limits and recovery

What a tundravm image defends against, where it stops, and what to do when something
breaks. Each claim is labelled **implemented** (in the code on master, with tests) or
**proven** (also run for real in CI). What is not built says so, with the gate of
[the v2 plan](design/plan-v2-2026-10-30.md) that builds it. The
[manual](manual.md#what-is-proven-today) has the same split per feature.

## Threat model

The adversary is whoever runs the machine: the cloud provider or host operator, with the
hypervisor, the host kernel, the disks and the network. The assets are the guest's code
identity, the secrets delivered to it, the data on its volumes and the answers it gives.
Intel TDX keeps guest memory and CPU state from the host. Everything else below decides
whether what runs in the TD is what you approved, and whether secrets reach only that.

| Mechanism | Defends against | Does not defend against | State |
|---|---|---|---|
| TDX | the host reading or changing guest memory and registers | side channels, denial of service, a broken TDX module or firmware | images boot with a `tdx-guest` object under `run --mode tdx`, and as TDX confidential VMs under `deploy`; no real TDX launch is recorded yet (gate 2) |
| Measured UKI | the host swapping the kernel, the initrd or the command line unnoticed: they are one PE file the firmware measures into RTMR1 and RTMR2 | a verifier that does not check, or approves the wrong values | implemented; predictions are offline replays, never compared with a real quote (gate 2) |
| dm-verity root (`RootFS.verity()`) | changes to the root partition at rest: the root hash is on the measured command line | runtime writes, which land in a volatile overlay and vanish at reboot; volume data | proven: CI boots from dm-verity |
| Firewall | inbound connections to anything not exposed | outbound traffic unless `Egress` is set; requests on exposed plaintext ports | implemented; the rendered rules are asserted |
| Service sandbox | a compromised service reaching the rest of the guest | kernel exploits; the service's own data and credentials | implemented; each preset's directives are asserted |
| LUKS2 volumes | the host reading data at rest | rollback to an older disk, deletion, a key released to the wrong TD | implemented; unlocked over the attested endpoint |
| Secrets as credentials | values leaking through the image, the tree, the manifest, receipts or argv | a compromised service that binds the secret | implemented |
| Metrics keep-list | series `export=` does not name leaving the TD, a side channel | the journal, which leaves whole | implemented; the dev probe reads the sink |
| NTS time | the host setting the guest clock, which certificates and freshness depend on | the host delaying or blocking time traffic | implemented |
| Fail-closed production profile | a production build starting its services where no TDX quote path exists | a host that offers TDX to a TD you did not approve: that is the verifier's call | proven: CI boots the production fixture without TDX and its services never start |

How the last row works (plan 6.1):

- Every tree ships `tdx-evidence.service`, a root oneshot. It looks for the `tdx_guest`
  CPU flag, then for `/dev/tdx_guest` or configfs-tsm's report directory, and records
  `tdx` in `/run/tundravm/mode`. Every unit made from a `Service` (a timer's too, and
  `tdxs.service`) has `Requires=` and `After=` on it.
- In a production profile, no quote path fails the gate with
  `tdx-evidence: refused: no TDX quote path` on the console, and systemd starts none of the
  gated services.
- In a development profile (`dev=`, `Profile(production=False)`), no quote path records
  `simulated`, the services run, and `tdxs` takes the `simulator` issuer the profile ships
  beside the real one, so a dev build attests `simulated` under QEMU. A production profile
  ships the real issuer only. `attest` reads the mode over the session: `simulated` makes
  the evidence simulated, and never the other way round.
- Not gated: boot tasks and probes, `Unit` declarations, mounts, `runtime-init` (keys,
  disks, secret delivery: the control plane) and the cloud glue. The console stays readable
  and the management port still answers on a refused boot.
- The gate checks that a quote path exists, not that a quote verifies: it stops a
  production build serving where TDX is missing. What keeps production secrets
  from a host you do not trust is the release rule under
  [Delivering secrets](manual.md#delivering-secrets).

The boot probe a development profile carries (`run --probe`) checks from inside the guest
that what the image declares holds: services up, ports bound, volumes on dm-crypt, egress
refused, sandboxes under their ceilings, modules, symbols and host keys as declared,
attested TLS certificates bound. It catches a broken image, not an attacker: a compromised
guest can print `ok` for anything. Evidence is the
quote `attest` verifies. A production profile carries no probe.

### What the quote covers

`build` writes `measurements.json`: one record per target and boot path (`uki` through
`-kernel`, `disk` through the ESP), each with its predicted registers, its unknowns with
the reason, and a `tundravm.policy/2` policy.

| Target | Predicted | Left to the operator |
|---|---|---|
| `qemu` | RTMR1, RTMR2 (the disk record includes the GPT event) | MRTD, RTMR0: they depend on the host's TDVF, its ACPI tables and the memory layout |
| `gcp` | MRTD and RTMR0 for Google's published firmware and machine types, RTMR1, RTMR2 | nothing |
| `azure` | no TDX register: the quote measures Azure's paravisor | everything; Azure's evidence is its vTPM |

RTMR3 is outside every policy: no event schema is defined for it, and each build lists that
under `unchecked`. MRTD and RTMR0 values carry the firmware they belong to, and an MRTD and
an RTMR0 from different firmware never match together.

Every package in the UKI comes from one pinned Debian snapshot: a moved snapshot is a
new image, whose measurements need approving again.

`attest` says `trusted` only when a `tdxs` validator accepted the quote's signature, the
quote binds the fresh nonce, and MRTD, RTMR0, RTMR1 and RTMR2 each matched an accepted
value. For a production build, a TD with the `DEBUG` attribute is `untrusted` and simulated
evidence never passes. Without a validator the best verdict is `measurements-match`: the registers match,
but nobody checked that a TDX module signed them. With a register missing it is `partial`,
which never releases a secret. For `qemu`, approve MRTD and RTMR0 in the policy's
`platform_approval` from a quote of a host you trust, or the verdict stays `partial`.

Predictions (the build), observations (`attest --output`) and the approved policy
(`policies/`) are three files, never one.

### What `build` prints

The report at the end of `build` (and at the top of `show BUILD`) mixes three kinds of
value. Only the measurements are something a quote is checked against.

| Printed | Kind | Comes from |
|---|---|---|
| artifact sizes and sha256s, source pins and toolchains, the UKI's Authenticode digest | facts about the bytes, recomputed by `verify` | `manifest.json` |
| `checkout` and `deps` (fetched, copied or cached), the time per phase | observations of this build on this host, not identity | `observations.json` |
| MRTD, RTMR0, RTMR1, RTMR2 per target and boot path | predictions, replayed offline from the UKI and the disk | `measurements.json` |

The Authenticode digest (SHA-384, over the UKI as a PE image) is the event OVMF extends
RTMR1 with when it loads the UKI, so a fleet can match a running image to a build by the
event log alone. It names the UKI. It is not a register value and attests nothing by
itself. A row such as `MRTD/RTMR0 3/27 accepted values` is a firmware table that the
policy pairs by firmware. A record with no prediction prints its state (`unknown: ...`)
in place of a value.

### What first contact proves

An image with a volume, a secret or an `AttestedTLS` ingress runs the attested endpoint on
port 8745, with a TLS key made in memory at start. Its evidence is a quote whose user data
is `sha512(purpose ‖ nonce ‖ TLS key ‖ SSH host keys ‖ instance public key ‖
AttestedTLS certificate keys)`: length-prefixed parts (big-endian uint32), keys as
DER SubjectPublicKeyInfo, ports ascending (`Binding` in `tools/pkg/endpoint`). `attest`
sends a fresh nonce and refuses evidence for another nonce (a replay) or another TLS key
than its session's (a relay), then judges the quote; on success it writes the bound host
keys as `known_hosts`. `secrets deliver` and `unlock`
verify again and write only on that session, with its nonce, once per value per boot.
With an `OperatorKey` volume each write is also signed over its nonce by the SSH key the
first signed write pinned in the volume's LUKS2 header, so after a reboot the endpoint
refuses any other key. The header is plaintext, so the client also refuses a volume back
without the owner its receipt names; a lost key means a new volume.

| Issuer | First contact proves | It does not prove |
|---|---|---|
| `tdx`, `gcp` | the session ends in a TD whose registers matched the policy; with `--validator`, a genuine quote (`trusted`) | without a validator, Intel's signature |
| `azure` | nothing: `attest` refuses Azure policies | |
| `simulator` | the binding and the quote's structure | what runs, or where: its key is published; `simulated`, never production |

An `AttestedTLS` port serves a certificate made at boot whose key that evidence binds.
`attest --tls PORT` and `tools/cmd/attested-client` refuse a certificate without that key,
on the evidence the policy judged, so the connection ends in that TD. It proves nothing of
the client or the upstream. No TDX host has run it yet (gate 2).

## Platform limits

| Platform | What works | What does not |
|---|---|---|
| Local QEMU, `--mode simulated` | boots `qemu.raw` through OVMF, KVM or TCG; a development build's services run, and CI fetches a page through one | no TDX; evidence is `simulated`; a production build boots fail-closed |
| Local TDX host, `--mode tdx` | boots TDVF with a `tdx-guest` object where `doctor --for tdx` is ready | no real quote recorded; MRTD and RTMR0 need operator approval |
| GCP | `deploy`, volumes as device `tundravm-NAME`, `deploy upgrade` and `rollback`, full register predictions | tested against a fake `gcloud` only; no GCP deployment or quote recorded; tundravm creates no firewall rules |
| Azure | `deploy` of an image without volumes | volumes (data disks are named by LUN); `attest`, which is TDX-only and refuses Azure; `upgrade` |
| Apple Silicon | builds in an emulated x86-64 Lima guest, same bytes | speed: several times slower than x86-64 |

More limits that are not platform-specific:

- **Simulated instances and secrets.** `secrets deliver` and `secrets unlock` refuse a
  production build in a simulated instance before they contact it. A development build in
  a simulated instance releases on a `simulated` verdict only. tundravm cannot tell a test
  value from a production one: never deliver production values to it.
- **Volumes.** No backups, no resize, no rollback protection: a host can attach an older
  copy of a disk and the guest cannot tell. Local instances get a fresh sparse 16 GiB disk
  per volume, per instance; nothing reattaches them.
- **`TpmSeal`.** `key-gen` seals the key in the vTPM's NV storage without a PCR policy
  today; the image records `pcrs=` for gate 3 to enforce. `recovery="none"` is the only
  method: if the vTPM state is lost, the data is gone.
- **`BrokerKey`.** The pull client is not built: the image waits as for an operator, who
  can stand in for the broker over the attested endpoint.
- **The kernel.** `Kernel.default()` is Debian's `linux-image-amd64` with a curated module
  set. A weekly CI workflow builds and boots one `Kernel.build` with every target's drivers
  built in; the tested kernel matrix of plan 6.7 is not published yet.
- **No boot-time tmpfiles.** The debloat masks them: nothing unmeasured rewrites paths.
- **The tools.** The evidence service, the endpoint, the key and disk tools build from
  `tools/` at the SDK's commit, pinned by the lock like any source.

## Recovery

The model is replacement, never repair: a new VM from an approved build, with the
persistent volumes moved to it; no in-place upgrades, no A/B slots.
State that matters lives on a `Volume`; everything else is rebuilt from the build
directory.

### Upgrade and rollback

`deploy upgrade INSTANCE --build NEW --policy FILE NAME=@FILE...` runs, on GCP, in order:

1. Launch the candidate from `NEW`, without volumes.
2. Attest it and deliver its credentials: its control plane is ready.
3. Stop the old instance. tundravm cannot drain connections, so stopping is the drain and
   the fence.
4. Detach every volume from the old instance and attach it to the candidate.
5. Unlock the candidate's `OperatorKey` volumes.
6. Wait for application readiness.

Downtime runs from step 3 to step 6: a stateful service is not ready before it owns its
volume. The command refuses Azure and a build whose
volumes differ from the instance's in name or unlock kind. If the candidate fails
attestation, the old instance keeps running with its volumes. `deploy rollback INSTANCE`
runs the same workflow back to the instance it replaced. Retiring the old policy and
migrating the data are yours.

Approve the new build's policy before the upgrade: copy its `measurements.json` record into
`policies/`, add the platform approvals, review `tundravm show --diff OLD NEW`, commit.

### A lost instance

The volume outlives the VM. Deploy the same build again and attach the existing disk:

```console
$ tundravm deploy gcp --build build/attested-node/production/<id> --volume data=existing:<disk>
$ tundravm attest attested-node-2 --policy policies/attested-node-gcp.json
$ tundravm secrets unlock attested-node-2 data
$ tundravm secrets deliver attested-node-2 --policy policies/attested-node-gcp.json logs-client=@logs-client.pem
```

Stop the old instance first: two instances must never hold one volume.

### When an unlock fails

Never reformat: the image formats a volume only while it carries no initialisation
token. A volume that does not open stays
closed and its services wait: the safe state; keep it.

1. Read why: `tundravm deploy logs INSTANCE` (or `run logs`) shows the serial console,
   including `runtime-init-keys` and `runtime-init-disks`.
2. Check the instance runs the build you expect: `tundravm attest INSTANCE --policy FILE`.
   A different build is a deployment mistake, not a disk problem.
3. Send the passphrase again (`tundravm secrets unlock INSTANCE VOLUME`): typos are the
   usual cause.
4. If the disk is attached elsewhere, or an upgrade stopped half-way, stop the other
   instance, then reattach with `deploy --volume NAME=existing:ID`.
5. A `TpmSeal` volume whose vTPM state is gone does not open again. Keep the disk, restore
   the data from your own backup onto a new volume, and retire the old one deliberately.

Never attach a volume to an unattested instance, and never `create:` (a new, empty disk)
over a name whose old disk you still need.
