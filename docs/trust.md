# Trust, limits and recovery

This page says what a tundravm image defends against, where that defence stops and what to
do when something breaks. Each claim carries one of two labels. **Implemented** means the
code is on master and has tests. **Proven** means CI also runs it. When something is not
built, the page says so and names the gate of [the v2 plan](design/plan-v2-2026-10-30.md)
that builds it. The [manual](manual.md#what-is-proven-today) makes the same split for each
feature.

## Threat model

The adversary is whoever runs the machine, such as the cloud provider or the host operator.
That adversary controls the hypervisor, the host kernel, the disks and the network. The
assets are the code identity of the guest, the secrets delivered to it, the data on its
volumes and the answers it gives. Intel TDX keeps the memory and CPU state of the guest from
the host. Every other mechanism below has one of two jobs. It decides whether what runs in
the TD is what you approved, or whether secrets reach only that.

| Mechanism | Defends against | Does not defend against | State |
|---|---|---|---|
| TDX | the host reading or changing guest memory and registers | side channels beyond `mitigations=auto,nosmt`, denial of service, a broken TDX module or firmware | images boot with a `tdx-guest` object under `run --mode tdx`, and as TDX confidential VMs under `deploy`, but no real TDX launch is recorded yet (gate 2) |
| Measured UKI (unified kernel image) | the host swapping the kernel, the initrd or the command line unnoticed, because they form one PE file that the firmware measures into RTMR1 and RTMR2 (runtime measurement registers) | a verifier that does not check, or that approves the wrong values | implemented; the predictions are offline replays that no real quote has confirmed (gate 2) |
| dm-verity root (`RootFS.verity()`) | changes to the root partition at rest, because the root hash is on the measured command line | runtime writes, which land in a volatile overlay and vanish at reboot, and volume data | proven: CI boots from dm-verity |
| Firewall | inbound connections to anything not exposed | outbound traffic unless `Egress` is set, and requests on exposed plaintext ports | implemented; the rendered rules are asserted |
| Service sandbox | a compromised service reaching the rest of the guest | kernel exploits, and the service's own data and credentials | implemented; the directives of each preset are asserted |
| LUKS2 volumes | the host reading data at rest | rollback to an older disk, deletion, and a key released to the wrong TD | implemented; unlocked over the attested endpoint |
| Secrets as credentials | values leaking through the image, the tree, the manifest, receipts or argv | a compromised service that holds the secret | implemented |
| Metrics keep-list | series that `export=` does not name leaving the TD, which would be a side channel | the journal, which leaves whole | implemented; the dev probe reads the sink |
| NTS time (network time security), DNS over TLS | the host setting the clock, on which certificates and freshness depend, or forging lookups | the host delaying or blocking time traffic | implemented |
| Fail-closed production profile | a production build starting its services where no TDX quote path exists | a host that offers TDX to a TD you did not approve, which is the verifier's call | proven: CI boots the production fixture without TDX and its services never start |

The last row works as follows (plan 6.1):

- Every tree ships `tdx-evidence.service`, a root oneshot. It looks for the `tdx_guest` CPU
  flag, and then for `/dev/tdx_guest` or the report directory of configfs-tsm. It records
  `tdx` in `/run/tundravm/mode`. Every unit made from a `Service` has `Requires=` and
  `After=` on it. That includes the unit of a timer and `tdxs.service`.
- In a production profile, a missing quote path fails the gate with
  `tdx-evidence: refused: no TDX quote path` on the console, and systemd starts none of the
  gated services.
- In a development profile (`dev=` or `Profile(production=False)`), a missing quote path
  records `simulated`, and the services run. The profile ships a `simulator` issuer beside
  the real one, and `tdxs` takes it, so a dev build attests as `simulated` under QEMU. A
  production profile ships the real issuer only. `attest` reads the mode over the session.
  The mode `simulated` makes the evidence simulated, and the reverse never happens.
- The gate does not cover boot tasks and probes, `Unit` declarations, mounts, `runtime-init`
  or the cloud glue. `runtime-init` handles keys, disks and secret delivery, which together
  are the control plane. On a refused boot, the console and the management port still
  answer.
- The gate checks that a quote path exists, not that a quote verifies. It stops a production
  build from serving where TDX is missing. The release rule under
  [Delivering secrets](manual.md#delivering-secrets) is what keeps production secrets from a
  host you do not trust.

A development profile carries a boot probe, which `run --probe` runs. It checks from inside
the guest that what the image declares holds ([the checks](manual.md#the-boot-probe)). It
catches a broken image but not an attacker, because a compromised guest can print `ok`.
Evidence is the quote that `attest` verifies. A production profile carries no probe.

### What the quote covers

`build` writes `measurements.json`. It holds one record for each target and boot path. The
`uki` path boots through `-kernel`, and the `disk` path boots through the ESP (EFI system
partition). Each record has its predicted registers, its unknown values with the reason, and
a `tundravm.policy/2` policy.

| Target | Predicted | Left to the operator |
|---|---|---|
| `qemu` | RTMR1 and RTMR2, where the disk record includes the GPT event | MRTD and RTMR0, which depend on the TDVF of the host, its ACPI tables and the memory layout |
| `gcp` | MRTD and RTMR0 for the published firmware and machine types of Google, plus RTMR1 and RTMR2 | nothing |
| `azure` | no TDX register, because the quote measures the paravisor of Azure | everything, because the evidence of Azure is its vTPM |

RTMR3 holds only firewall mode switches, each written as `tundravm:mode:<name>:<seconds>`.
`attest` replays them. A policy with `"modes": ["production"]` refuses an instance that
opened another mode. Without `Firewall(modes=)`, RTMR3 is outside the policy. MRTD and RTMR0
values carry the firmware they belong to, so values from different firmware never match.

Every UKI package comes from one pinned Debian snapshot, and so do the roots of `Container`
values. A snapshot that moves produces a new image.

The [verdicts](manual.md#deploy-and-attest) say when `attest` answers `trusted`. Without a
validator the best verdict is `measurements-match`. It means the registers match, but nobody
checked that a TDX module signed them. For `qemu`, approve MRTD and RTMR0 in the
`platform_approval` of the policy, using a quote from a host you trust.

Predictions come from the build, observations come from `attest --output`, and the approved
policy lives in `policies/`. These are three separate files, never one.

### What `build` prints

The report that `build` and `show BUILD` print mixes three kinds of value. Only the
measurements are checked against a quote.

| Printed | Kind | Comes from |
|---|---|---|
| artifact sizes and sha256s, source pins and toolchains, the Authenticode digest of the UKI | facts about the bytes, recomputed by `verify` | `manifest.json` |
| `checkout` and `deps` (fetched, copied or cached), and the time of each phase | observations of this build on this host, which are not part of its identity | `observations.json` |
| MRTD, RTMR0, RTMR1 and RTMR2 for each target and boot path | predictions, replayed offline from the UKI and the disk | `measurements.json` |

The Authenticode digest is a SHA-384 over the UKI as a PE image. OVMF extends RTMR1 with it
as an event when it loads the UKI, so a fleet can match a running image to a build by the
event log alone. The digest is not a register value and it attests nothing. A row such as
`MRTD/RTMR0 3/27 accepted values` is a firmware table that the policy pairs by firmware. A
record with no prediction prints its state, `unknown: ...`, in place of a value.

### What first contact proves

The [control plane](manual.md#the-control-plane) runs the attested endpoint on port 8745,
with a TLS key that it makes in memory at start. Its evidence is a quote whose user data is
`sha512(purpose ‖ nonce ‖ TLS key ‖ SSH host keys ‖ instance public key ‖
AttestedTLS certificate keys)`. The parts are length-prefixed with a big-endian uint32, the
keys are DER SubjectPublicKeyInfo, and the ports ascend (`Binding` in `tools/pkg/endpoint`).
`attest` sends a fresh nonce. It refuses evidence for another nonce, which would be a
replay, and evidence for another TLS key than the one of its session, which would be a
relay. It also refuses evidence without the `endpoint` tag of tdxs, because only the quotes
of root carry that tag. Then it judges the quote. On success it writes the bound host keys
as `known_hosts`. `secrets deliver` and `unlock` verify again and write only in that
session, with its nonce, once per value per boot, so the first writer wins.

With an `OperatorKey` volume, the owner of the volume signs each write over the user data of
the quote. The first signed write pins that SSH key in the LUKS2 header of the volume. The
header is plaintext, so a host that rewrites it takes the writes of that boot. It never
takes the volume, because the encrypted copy of the owner then disagrees. The client refuses
an owner other than the one in its receipt. If you lose the key, you need a new volume. On
TDX, the endpoint takes an SSH key, whether root's or a `Login`'s, only from an owner that
the encrypted copy confirmed during this boot, so unlock first and then attest again. An
image without an `OperatorKey` volume takes no key there. A `Login` reaches only its menu or
its shell, plus one exact sudoers line for each root entry. With `Login(owner=True)`, the key
of the owner logs in as that login, and root never takes it.

| Issuer | First contact proves | It does not prove |
|---|---|---|
| `tdx`, `gcp` | the session ends in a TD whose registers matched the policy, and with `--validator`, a genuine quote (`trusted`) | without a validator, the signature of Intel |
| `azure` | nothing: `attest` refuses Azure policies | |
| `simulator` | the binding and the structure of the quote | what runs, or where, because its key is published; the result is `simulated`, never production |

An `AttestedTLS` port serves a certificate that the guest makes at boot, and the evidence
binds its key. `attest --tls PORT` and `tools/cmd/attested-client` refuse a certificate
without that key, judged on the evidence the policy accepted, so the connection ends in that
TD. The check proves nothing about the client or the upstream. No TDX host has run it yet
(gate 2).

## Platform limits

| Platform | What works | What does not |
|---|---|---|
| Local QEMU, `--mode simulated` | boots `qemu.raw` through OVMF with KVM or TCG, runs the services of a development build, and CI fetches a page through one | no TDX, the evidence is `simulated`, and a production build boots fail-closed |
| Local TDX host, `--mode tdx` | boots TDVF with a `tdx-guest` object where `doctor --for tdx` is ready | no real quote is recorded, and MRTD and RTMR0 need operator approval |
| GCP | `deploy`, volumes as device `tundravm-NAME`, `deploy upgrade` and `rollback`, and full register predictions | tested against a fake `gcloud` only, no GCP deployment or quote is recorded, and tundravm creates no firewall rules |
| Azure | `deploy`, and volumes as data disks at their LUNs | tested against a fake `az` only, the by-lun names are unproven on a real VM, and `upgrade` does not work; `attest` is TDX-only and refuses Azure |
| Apple Silicon | builds in an emulated x86-64 Lima guest and produces the same bytes | speed, because it is several times slower than x86-64 |

Some limits do not depend on the platform:

- **Simulated instances and secrets.** tundravm cannot tell a test value from a production
  value, so never deliver production values to a simulated instance.
- **Volumes.** There are no backups, no resize and no rollback protection. A host can attach
  an older copy of a disk, and the guest cannot tell. Each local instance gets a fresh
  sparse 16 GiB disk for each volume, and nothing reattaches those disks.
- **`TpmSeal`.** Today `key-gen` seals the key in the NV storage of the vTPM without a PCR
  policy. The image records `pcrs=` for gate 3 to enforce. The only method is
  `recovery="none"`, so if the vTPM state is lost, the data is gone.
- **`BrokerKey`.** The pull client is not built. The image waits as it does for an operator,
  and an operator can stand in for the broker over the attested endpoint.
- **The kernel.** `Kernel.default()` is the `linux-image-amd64` of Debian with a curated
  module set. A weekly CI workflow builds and boots one `Kernel.build` with the drivers of
  every target built in. The tested kernel matrix of plan 6.7 is not published yet.
- **Firewall modes on TDX.** A switch extends RTMR3 through a kernel file that Linux 6.16
  added. Debian's 6.12 lacks it, so a TDX instance refuses every switch until the image
  builds a newer `Kernel`.
- **No boot-time tmpfiles.** The debloat step masks them, so nothing unmeasured rewrites
  paths.
- **The tools.** The evidence service, the endpoint, and the key and disk tools build from
  `tools/` at the commit of the SDK. The lock pins that commit like any other source.

## Recovery

Recovery means replacement, never repair. You start a new VM from an approved build and
move the persistent volumes to it. State that matters lives on a `Volume`, and everything
else is rebuilt from the build directory.

### Upgrade and rollback

On GCP, `deploy upgrade INSTANCE --build NEW --policy FILE NAME=@FILE...` runs these steps in
order:

1. Launch the candidate from `NEW`, without volumes.
2. Attest the candidate and deliver its credentials, so that its control plane is ready.
3. Stop the old instance, which drains and fences it.
4. Detach every volume from the old instance and attach it to the candidate.
5. Unlock the `OperatorKey` volumes of the candidate.
6. Wait until the application is ready.

Downtime runs from step 3 to step 6. The command refuses Azure, and it refuses a build whose
volumes differ from those of the instance in name or unlock kind. If the candidate fails
attestation, the old instance keeps running with its volumes. `deploy rollback INSTANCE`
runs the same workflow back to the instance that the upgrade replaced. Retiring the old
policy and migrating the data are your job.

Approve the policy of the new build before the upgrade. Copy its record from
`measurements.json` into `policies/`, add the platform approvals, review
`tundravm show --diff OLD NEW`, and commit.

### A lost instance

The volume outlives the VM. Deploy the same build again and attach the existing disk to it:

```console
$ tundravm deploy gcp --build build/attested-node/production/<id> --volume data=existing:<disk>
$ tundravm attest attested-node-2 --policy policies/attested-node-gcp.json
$ tundravm secrets unlock attested-node-2 data
$ tundravm secrets deliver attested-node-2 --policy policies/attested-node-gcp.json logs-client=@logs-client.pem
```

Stop the old instance first, because two instances must never hold one volume.

### When an unlock fails

Never reformat the disk. The image formats a volume only while it carries no initialisation
token. A volume that does not open stays closed and its services wait, which is the safe
state, so keep it that way.

1. Find out why. `tundravm deploy logs INSTANCE` or `run logs` shows the serial console,
   including `runtime-init-keys` and `runtime-init-disks`.
2. Check that the instance runs the build you expect with
   `tundravm attest INSTANCE --policy FILE`. A different build is a deployment mistake, not
   a disk problem.
3. Restart the instance, then send the passphrase again with
   `tundravm secrets unlock INSTANCE VOLUME`, once per boot. A typo is the usual cause.
4. If the disk is attached elsewhere, or an upgrade stopped half-way, stop the other
   instance and reattach the disk with `deploy --volume NAME=existing:ID`.
5. A `TpmSeal` volume whose vTPM state is gone does not open again. Keep the disk, restore
   the data from your own backup onto a new volume, and retire the old one deliberately.

Never attach a volume to an unattested instance. Never use `create:`, which makes a new and
empty disk, over a name whose old disk you still need.
