# Trust, limits and recovery

What a tundravm image defends against, where it stops, and what to do when something
breaks. Each claim is labelled: **implemented** (in the code on master, with tests),
**proven** (also run for real in CI) or **design** (specified in the v2 plan, not built).
The [guide](guide.md#what-is-proven-today) has the same split per feature.

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
| LUKS2 volumes | the host reading data at rest | rollback to an older disk, deletion, a key released to the wrong TD | implemented; unlock over today's channel only |
| Secrets as credentials | values leaking through the image, the tree, the manifest, receipts or argv | a compromised service that binds the secret | implemented |
| NTS time | the host setting the guest clock, which certificates and freshness depend on | the host delaying or blocking time traffic | implemented |
| Fail-closed production profile | a production build starting its services where no TDX quote path exists | a host that offers TDX to a TD you did not approve: that is the verifier's call | proven: CI boots the production fixture without TDX and its services never start |

How the last row works (plan 6.1):

- Every tree ships `tdx-evidence.service`, a root oneshot. It looks for the `tdx_guest`
  CPU flag, then for `/dev/tdx_guest` or configfs-tsm's report directory, and records
  `tdx` in `/run/tundravm/mode`. Every unit made from a `Service` (a timer's too, and
  `tdxs.service`) has `Requires=` and `After=` on it. `ConditionSecurity=` is not used: it
  knows `cvm`, not `tdx`, and a skipped condition does not stop a dependent unit.
- In a production profile, no quote path fails the gate with
  `tdx-evidence: refused: no TDX quote path` on the console, and systemd starts none of the
  gated services. There is no fallback.
- In a development profile (`dev=`, `Profile(production=False)`), no quote path records
  `simulated` and the services run. `attest` reads the mode over the session: `simulated`
  makes the evidence simulated, and never the other way round.
- Not gated: boot tasks and probes, `Unit` declarations, mounts, `runtime-init` (keys,
  disks, secret delivery: the control plane) and the cloud glue. The console stays readable
  and the management port still answers on a refused boot.
- The gate checks that a quote path exists, not that a quote verifies: `tdxs` makes the
  first quote and the verifier judges it. The gate stops a production build serving where
  TDX is missing by mistake. The release predicate is what keeps production secrets from a
  host you do not trust.

The boot probe a development profile carries (`run --probe`) checks from inside the guest
that what the image declares holds: services up, ports bound, volumes on dm-crypt, egress
refused, sandboxes under their ceilings, no module or kernel symbol beyond the declared
ones, host keys root-owned. It catches a broken image, not an attacker: it runs inside the
guest it checks, so a compromised guest can print `ok` for anything, and it reads no
register. Evidence is the quote `attest` verifies. A production profile carries no probe.

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

Every package in the UKI comes from one pinned Debian snapshot, so the snapshot is part
of the measured identity: an unpinned archive would give a rebuild other bytes and other
registers, and a moved snapshot is a new image whose measurements need approving again.

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

### The channel is not attested yet

This is the largest gap, and it is gate 3 work in `tools/`.

An image with a volume or a secret runs `secret-delivery` on port 8745. It accepts one SSH
public key per boot, from whoever POSTs first, and root SSH then accepts only that key; the
firewall then drops the port until the next boot.
`attest`, `secrets deliver` and `secrets unlock` all run over that SSH session. The quote
binds a nonce, but not the session, the SSH host key or the operator's key. So today:

- Whoever reaches port 8745 first after boot gets root on the instance. Keep the port
  reachable only from the operator's address.
- An attacker on the network path can stand between the operator and a genuine instance,
  relay the genuine quote, pass `attest`, and receive what `secrets deliver` sends.
- `AttestedTLS` serves a certificate generated per boot that no quote vouches for. A client
  pins it on first contact.

Each command prints `unverified first contact`. The design that closes them
(plan 6.3): the guest generates a key pair at boot; the quote's `report_data` binds
`sha512(purpose ‖ nonce ‖ public key ‖ SSH host keys)`; the client speaks TLS to a
certificate carrying that key, so a relayed quote cannot authenticate another connection.
Until it lands, treat delivery to a cloud instance as trusting the network path.

Each image makes that key pair (`InstanceKey()`); until the quote binds it, a signature
under it proves this boot, not what signed.

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
- **`BrokerKey`.** The pull client is gate 3. Until then the image waits for the value as it
  waits for an operator, and an operator can stand in for the broker.
- **The kernel.** `Kernel.default()` is Debian's `linux-image-amd64` with a curated module
  set. A weekly CI workflow builds and boots one `Kernel.build` with every target's drivers
  built in; the tested kernel matrix of plan 6.7 is not published yet.
- **The tools.** The evidence service and the key, secret and disk tools are built from
  `tools/` in this repository at the SDK's own commit, and the lock pins that source like
  any other (from a checkout of the repository, by the digest of its `tools/`).

## Recovery

The model is replacement, never repair: a new VM from an approved build, with the
persistent volumes moved to it. There are no in-place package upgrades and no A/B slots.
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

Downtime runs from step 3 to step 6. A stateful service cannot be ready before it owns its
volume, so readiness comes after the move. The command refuses Azure and a build whose
volumes differ from the instance's in name or unlock kind. If the candidate fails
attestation, the old instance keeps running with its volumes. `deploy rollback INSTANCE`
runs the same workflow back to the instance it replaced. Retiring the old policy once the
rollback window closes is the operator's job. Schema migrations of the data stay explicit
and are yours.

Approve the new build's policy before the upgrade: copy its `measurements.json` record into
`policies/`, add the platform approvals, review `tundravm show --diff OLD NEW`, commit.

### A lost instance

The volume outlives the VM. Deploy the same build again and attach the existing disk:

```console
$ tundravm deploy gcp --build build/prover/production/<id> --volume data=existing:<disk>
$ tundravm attest prover-2 --policy policies/prover-gcp.json
$ tundravm secrets unlock prover-2 data
$ tundravm secrets deliver prover-2 --policy policies/prover-gcp.json raiko-config=@raiko.json
```

Make sure the old instance is stopped first: two instances must never hold one volume.

### When an unlock fails

Never reformat. The image formats a volume only while it carries no initialisation token,
and an unlock failure is never permission to format. A volume that does not open stays
closed, and every service bound to it waits. That is the safe state; keep it.

1. Read why: `tundravm deploy logs INSTANCE` (or `run logs`) shows the serial console,
   including `runtime-init-keys` and `runtime-init-disks`.
2. Check the instance runs the build you expect: `tundravm attest INSTANCE --policy FILE`.
   A different build is a deployment mistake, not a disk problem.
3. Send the passphrase again with `tundravm secrets unlock INSTANCE VOLUME`. A typo is the
   usual cause.
4. If the disk is attached elsewhere, or an upgrade stopped half-way, stop the other
   instance, then reattach with `deploy --volume NAME=existing:ID`.
5. A `TpmSeal` volume whose vTPM state is gone does not open again. Keep the disk, restore
   the data from your own backup onto a new volume, and retire the old one deliberately.

Do not attach a volume to an instance you have not attested, and do not create a new
volume over a name whose old disk you still need: `create:` makes a new, empty disk.
