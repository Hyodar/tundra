# Trust, limits and recovery

This page says what a tundravm image defends against, where that defence stops, and what to
do when something breaks. A claim marked **implemented** is on master with tests, and one
marked **proven** also runs in CI. Anything not yet built names the gate of [the v2
plan](design/plan-v2-2026-10-30.md) that builds it, and the
[manual](manual.md#what-is-proven-today) makes the same split for each feature.

No real TDX quote is recorded yet (gate 2), on any platform. Every claim below that rests
on one, from the register predictions to attested TLS, is therefore implemented and not
proven.

## Threat model

The adversary is whoever runs the machine, such as the cloud provider or the host operator,
and it controls the hypervisor, the host kernel, the disks and the network. What it wants
are the guest's code identity, the secrets delivered to it, the data on its volumes and the
answers it gives.

Intel TDX keeps the guest's memory and CPU state from that host. Every other mechanism below
decides one of two things: whether what runs in the TD is what you approved, or whether
secrets reach only that.

| Mechanism | Defends against | Does not defend against | State |
|---|---|---|---|
| TDX | the host reading or changing guest memory and registers | side channels beyond `mitigations=auto,nosmt`, denial of service, a broken TDX module or firmware | implemented: images boot with a `tdx-guest` object under `run --mode tdx`, and as TDX confidential VMs under `deploy` |
| Measured UKI (unified kernel image) | the host swapping the kernel, the initrd or the command line unnoticed, because they form one PE file that the firmware measures into RTMR1 and RTMR2 (runtime measurement registers) | a verifier that does not check, or that approves the wrong values | implemented; the predictions are offline replays |
| dm-verity root (`RootFS.verity()`) | changes to the root partition at rest, because the root hash is on the measured command line | runtime writes, which land in a volatile overlay and vanish at reboot, and volume data | proven: CI boots from dm-verity |
| Firewall | inbound connections to anything not exposed, and with `Egress`, a unit sending beyond its own rules, which match its user | outbound traffic unless `Egress` is set, the image's and a mode's `egress=`, open to every process, and requests on exposed plaintext ports | implemented; the rendered rules are asserted |
| Service sandbox | a compromised service reaching the rest of the guest | kernel exploits, and the service's own data and credentials | implemented; the directives of each preset are asserted |
| LUKS2 volumes | the host reading data at rest | rollback to an older disk, deletion, and a key released to the wrong TD | implemented; unlocked over the attested endpoint |
| Secrets as credentials | values leaking through the image, the tree, the manifest, receipts or argv | a compromised service that holds the secret | implemented |
| Metrics keep-list | series that no rule and no `export=` lets out leaving the TD, which would be a side channel | the journal, which leaves whole | implemented; the dev probe reads the sink |
| NTS time (network time security), DNS over TLS | the host setting the clock, on which certificates and freshness depend, or forging lookups | the host delaying or blocking time traffic, which holds the remote log and metrics exporters at `time-sync.target` until NTS syncs | implemented |
| Fail-closed production profile | a production build starting its services where no TDX quote path exists | a host that offers TDX to a TD you did not approve, which is the verifier's call | proven: CI boots the production fixture without TDX and its services never start |

The last row is a gate (plan 6.1). Every tree ships `tdx-evidence.service`, a root oneshot
that looks for the `tdx_guest` CPU flag, then for `/dev/tdx_guest` or the configfs-tsm
report directory, and records the mode in `/run/tundravm/mode`. Every unit made from a
`Service`, a timer's unit and `tdxs.service` included, has `Requires=` and `After=` on it,
so what happens next depends on the profile:

- **Production.** A missing quote path fails the gate with
  `tdx-evidence: refused: no TDX quote path` on the console, and systemd starts none of the
  gated services.
- **Development** (`dev=` or `Profile(production=False)`). A missing quote path records
  `simulated` and the services run. The profile ships a `simulator` issuer beside the real
  one, which `tdxs` takes, so a dev build attests as `simulated` under QEMU, while a
  production profile ships the real issuer only.

`attest` reads the mode over the session. The mode `simulated` makes the evidence
simulated, and the reverse never happens.

The gate leaves out boot tasks and probes, `Unit` declarations, mounts, the cloud glue and
`runtime-init`, which handles keys, disks and secret delivery, so on a refused boot the
console and the management port still answer. It also checks only that a quote path exists,
not that a quote verifies. What keeps production secrets from a host you do not trust is the
release rule under [Delivering secrets](manual.md#delivering-secrets).

A development profile also carries a boot probe, which `run --probe` runs to check from
inside the guest that what the image declares holds ([the checks](manual.md#the-boot-probe)).
It catches a broken image but not an attacker, because a compromised guest can print `ok`,
so the evidence is the quote that `attest` verifies. A production profile carries no probe.

The keep-list holds inside the guest too. With recording rules, `metrics-export` runs
Prometheus in server mode, whose HTTP API on `127.0.0.1:9090` would serve every raw series,
so its web config asks for basic auth with a bcrypt hash no password was hashed to. It
refuses every request, and no process in the guest reads what the keep-list holds back.

### What the quote covers

`build` writes `measurements.json`, with one record for each target and boot path: `uki`
boots through `-kernel`, and `disk` boots through the ESP (EFI system partition). Each
record holds its predicted registers, its unknown values with the reason, and a
`tundravm.policy/2` policy.

| Target | Predicted | Left to the operator |
|---|---|---|
| `qemu` | RTMR1 and RTMR2, where the disk record includes the GPT event | MRTD and RTMR0, which depend on the TDVF of the host, its ACPI tables and the memory layout |
| `gcp` | MRTD and RTMR0 for Google's published firmware and machine types, plus RTMR1 and RTMR2 | nothing |
| `azure` | no TDX register, because the quote measures Azure's paravisor | everything, because Azure's evidence is its vTPM |

RTMR3 holds only firewall mode switches, each written as `tundravm:mode:<name>:<seconds>`,
which `attest` replays. A policy with `"modes": ["production"]` refuses an instance that
opened another mode, and without `Firewall(modes=)` RTMR3 is outside the policy. MRTD and
RTMR0 values carry the firmware they belong to, so values from different firmware never
match.

Every UKI package, and the root of every `Container`, comes from one pinned Debian
snapshot, so a snapshot that moves produces a new image.

The [verdicts](manual.md#deploy-and-attest) say when `attest` answers `trusted`. Without a
validator the best verdict is `measurements-match`, which means the registers match but
nobody checked that a TDX module signed them. For `qemu`, approve MRTD and RTMR0 in the
policy's `platform_approval`, using a quote from a host you trust.

Predictions come from the build, observations from `attest --output`, and the approved
policy lives in `policies/`: three separate files, never one.

### What `build` prints

The report that `build` and `show BUILD` print mixes three kinds of value, and only the
measurements are checked against a quote.

| Printed | Kind | Comes from |
|---|---|---|
| artifact sizes and sha256s, source pins and toolchains, the Authenticode digest of the UKI | facts about the bytes, recomputed by `verify` | `manifest.json` |
| `checkout` and `deps` (fetched, copied or cached), and the time of each phase | observations of this build on this host, which are not part of its identity | `observations.json` |
| MRTD, RTMR0, RTMR1 and RTMR2 for each target and boot path | predictions, replayed offline from the UKI and the disk | `measurements.json` |

The Authenticode digest is a SHA-384 over the UKI as a PE image. OVMF extends RTMR1 with it
as an event when it loads the UKI, so a fleet can match a running image to its build from
the event log alone, but the digest is not a register value and attests nothing.

A row such as `MRTD/RTMR0 3/27 accepted values` is a firmware table that the policy pairs by
firmware. A record with no prediction prints its state, `unknown: ...`, in place of a value.

### What first contact proves

The [control plane](manual.md#the-control-plane) runs the attested endpoint on port 8745,
with a TLS key it makes in memory at start. Its evidence is a quote whose user data is
`sha512(purpose ‖ nonce ‖ TLS key ‖ SSH host keys ‖ instance public key ‖
AttestedTLS certificate keys)`, the parts length-prefixed with a big-endian uint32, the keys
DER SubjectPublicKeyInfo and the ports ascending (`Binding` in `tools/pkg/endpoint`).

`attest` sends a fresh nonce and refuses three kinds of evidence:

- evidence for another nonce, which would be a replay;
- evidence for another TLS key than its session's, which would be a relay;
- evidence without the `endpoint` tag of tdxs, which only root's quotes carry.

Only then does it judge the quote, and on success it writes the bound host keys as
`known_hosts`. `secrets deliver` and `unlock` verify again and write only in that session,
with its nonce, once per value per boot, so the first writer wins. The endpoint also takes
only the secrets the instance's profile reads and refuses any other name, so no value lands
where no unit uses it.

With an `OperatorKey` volume, the owner signs each write over the quote's user data, and the
first signed write pins that SSH key in the volume's LUKS2 header. The header is plaintext,
so a host that rewrites it takes the writes of that boot, but never the volume, because the
owner's encrypted copy then disagrees. The client refuses an owner other than the one in its
receipt, and a lost key means a new volume.

On TDX the endpoint takes an SSH key, root's or a `Login`'s, only from an owner that the
encrypted copy confirmed during this boot, so unlock first and then attest again. An image
without an `OperatorKey` volume takes no key there, and with `Login(owner=True)` the
owner's key logs in as that login while root never takes it.

A `Login` reaches only its shell or the program compiled from its commands, which parses the
client's words and execs one declared argv, never through an interpreter or shell, and its
sudoers lines admit exactly the argvs a root command can produce. In production the program
gets no terminal, behind `PermitTTY no` and `restrict` keys, locks only a development
build with `dev=True` commands lifts (`PermitTTY yes`, `restrict,pty`).

| Issuer | First contact proves | It does not prove |
|---|---|---|
| `tdx`, `gcp` | the session ends in a TD whose registers matched the policy and whose `DEBUG` attribute is off, and with `--validator`, a genuine quote (`trusted`) | without a validator, the signature of Intel |
| `azure` | nothing: `attest` refuses Azure policies | |
| `simulator` | the binding and the structure of the quote | what runs, or where, because its key is published; the result is `simulated`, never production |

An `AttestedTLS` port serves a certificate that the guest makes at boot, and the evidence
binds its key. `attest --tls PORT` and `tools/cmd/attested-client` refuse a certificate
without that key, judged on the evidence the policy accepted, so the connection ends in that
TD. The check proves nothing about the client or the upstream.

## Platform limits

| Platform | What works | What does not |
|---|---|---|
| Local QEMU, `--mode simulated` | boots `qemu.raw` through OVMF with KVM or TCG, runs the services of a development build, and CI fetches a page through one | no TDX, the evidence is `simulated`, and a production build boots fail-closed |
| Local TDX host, `--mode tdx` | boots TDVF with a `tdx-guest` object where `doctor --for tdx` is ready | MRTD and RTMR0 need operator approval |
| GCP | `deploy`, volumes as device `tundravm-NAME`, `deploy upgrade` and `rollback`, and full register predictions | tested against a fake `gcloud` only, and tundravm creates no firewall rules |
| Azure | `deploy`, and volumes as data disks at their LUNs | tested against a fake `az` only, the by-lun names are unproven on a real VM, and `upgrade` does not work; `attest` is TDX-only and refuses Azure |
| Apple Silicon | builds in an emulated x86-64 Lima guest and produces the same bytes | speed, because it is several times slower than x86-64 |

Some limits hold on every platform:

- **Simulated instances and secrets.** tundravm cannot tell a test value from a production
  value, so never deliver production values to a simulated instance.
- **Volumes.** Besides the rollback in the table above, which the guest cannot detect,
  there are no backups and no resize. Each local instance gets a fresh sparse 16 GiB disk
  for each volume, and nothing reattaches those disks.
- **`TpmSeal`.** Today `key-gen` seals the key in the vTPM's NV storage without a PCR
  policy, and the image records `pcrs=` for gate 3 to enforce. The only method is
  `recovery="none"`, so if the vTPM state is lost, the data is gone.
- **`BrokerKey`.** The pull client is not built, so the image waits as it does for an
  operator, and an operator can stand in for the broker over the attested endpoint.
- **The kernel.** `Kernel.default()` is Debian's `linux-image-amd64` with a curated module
  set. A weekly CI workflow builds and boots one `Kernel.build` with every target's drivers
  built in, but the tested kernel matrix of plan 6.7 is not published yet.
- **Firewall modes on TDX.** A switch extends RTMR3 through a kernel file that Linux 6.16
  added. Debian's 6.12 lacks it, so a TDX instance refuses every switch until the image
  builds a newer `Kernel`.
- **No boot-time tmpfiles.** The debloat step masks them, so nothing unmeasured rewrites
  paths.
- **The tools.** The evidence service, the endpoint, and the key and disk tools build from
  `tools/` at the SDK's commit, which the lock pins like any other source.
- **A fetched Go toolchain.** On a host without `go` 1.21 or newer, tundravm downloads the
  release a `go.mod` names and runs it once sum.golang.org vouches for it as `go` checks: a
  tree head signed by `go`'s built-in key, the release's record proven in that tree, and the
  zip's hash matching it. A head contradicting the largest one remembered is refused as a
  fork.

## Recovery

Recovery means replacement, never repair: you start a new VM from an approved build and move
the persistent volumes to it. State that matters lives on a `Volume`, and everything else is
rebuilt from the build directory.

### Upgrade and rollback

On GCP, `deploy upgrade INSTANCE --build NEW --policy FILE NAME=@FILE...` runs these steps in
order:

1. Launch the candidate from `NEW`, without volumes.
2. Attest the candidate and deliver its credentials, so that its control plane is ready.
3. Stop the old instance, which drains and fences it.
4. Detach every volume from the old instance and attach it to the candidate.
5. Unlock the `OperatorKey` volumes of the candidate.
6. Wait until the application is ready.

Downtime runs from step 3 to step 6, and if the candidate fails attestation, the old
instance keeps running with its volumes. The command refuses Azure, and a build whose
volumes differ from the instance's in name or unlock kind.

`deploy rollback INSTANCE` runs the same workflow back to the instance the upgrade replaced.
Retiring the old policy and migrating the data are your job.

Approve the new build's policy before the upgrade: copy its record from `measurements.json`
into `policies/`, add the platform approvals, review `tundravm show --diff OLD NEW`, and
commit.

### A lost instance

The volume outlives the VM, so deploy the same build again with the existing disk. Stop the
old instance first, because two instances must never hold one volume.

```console
$ tundravm deploy gcp --build build/attested-node/production/<id> --volume data=existing:<disk>
$ tundravm attest attested-node-2 --policy policies/attested-node-gcp.json
$ tundravm secrets unlock attested-node-2 data
$ tundravm secrets deliver attested-node-2 --policy policies/attested-node-gcp.json logs-client=@logs-client.pem
```

### When an unlock fails

Never reformat the disk. The image formats a volume only while it carries no initialisation
token, and a volume that does not open stays closed with its services waiting, which is the
safe state to keep.

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

Never attach a volume to an unattested instance, and never use `create:`, which makes a new
and empty disk, over a name whose old disk you still need.
