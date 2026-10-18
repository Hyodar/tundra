# Policy

Two kinds of policy:

- [Measurement policies](#measurement-policies): the TDX register values a quote of an image may hold, written by `tundravm measure --export-policy` and read by `attest`, `Tdxs.from_policy`, `evidence` and `verify`.
- [Recipe policy](#recipe-policy): `Recipe(policy=Policy(...))`, which makes the lifecycle stricter.

## Measurement policies

A measurement policy is JSON, schema `tundravm.policy/2`:

```json
{
  "schema": "tundravm.policy/2",
  "schema_version": 2,
  "scheme": "gcp",
  "tool": "tundravm-measure",
  "tool_version": "1.0+attest-12f1e29.dstack-mr-gcp-b16e08b",
  "artifact": {"path": "build/default/disk.tar.gz", "sha256": "…"},
  "boot_payload": {"path": "build/default/image.efi", "sha256": "…"},
  "registers": {
    "MRTD": [{"value": "feb74866…", "firmware": "gcp-ovmf-f53fdf89544e"}, "…"],
    "RTMR0": [{"value": "e1d02354…", "firmware": "gcp-ovmf-f53fdf89544e", "label": "c3-standard-4 acpi-2026-04"}, "…"],
    "RTMR1": ["…"],
    "RTMR2": ["…"]
  },
  "unverifiable": {},
  "firmware_table": "dstack-mr-gcp@b16e08b",
  "sources": ["https://storage.googleapis.com/gce_tcb_integrity/ovmf_x64_csm/<sha384>.fd", "…"]
}
```

| Field | Meaning |
|---|---|
| `schema`, `schema_version` | `tundravm.policy/2`, `2`. A schema 1 file (one value per register) is refused with `E_VALIDATION` and the hint to export it again with `tundravm measure --export-policy` |
| `scheme` | `qemu`, `gcp`, `azure` or `portable` (below) |
| `tool`, `tool_version` | `tundravm-measure` and the replay's version, which names the pinned tools it ports; `placeholder` for test values |
| `artifact` | `path` and `sha256` of the measured artifact |
| `boot_payload` | `path` and `sha256` of the UKI the registers measure, when known (the artifact itself for a UKI, the UKI a disk boots) |
| `registers` | Register name (`MRTD`, `RTMR0`..`RTMR3`) to a non-empty list of accepted values: a SHA-384 hex string, or `{"value", "firmware", "label"}` naming what the value belongs to |
| `unverifiable` | Register name to the reason the scheme cannot predict it offline |
| `platform_approval` | Optional: `MRTD` and/or `RTMR0` to a non-empty list of values the operator approves for the platform (`measure --export-policy FILE --approve-mrtd HEX --approve-rtmr0 HEX`). Kept apart from the measured `registers`; matched like them, printed `(operator-approved)` |
| `firmware_table`, `sources` | `gcp` only: the firmware table version and the Google firmware files its values come from |
| `vtpm_pcrs` | `azure` only: `PCR4`, `PCR9`, `PCR11` (SHA-256) the guest's boot extends into the vTPM. A vTPM quote verifier checks them; `tdxs` and `attest` do not |
| `note` | Placeholder policies only: says the values are not measurements |

Each of `MRTD`, `RTMR0`, `RTMR1` and `RTMR2` is either accepted (in `registers`) or unverifiable (in `unverifiable`, with the reason), never both and never neither. `RTMR3` is optional.

### Matching

A register matches when the quote's value is any accepted value. Values labelled with a `firmware` pair up: a quote whose MRTD matches one firmware and whose RTMR0 matches another firmware's value is an RTMR0 mismatch. `attest` reports, per register, the quote's value, the accepted values, the verdict and the label of the value that matched (`firmware label`, for example `gcp-ovmf-f53fdf89544e c3-standard-4 acpi-2026-04`). A register the policy accepts no value for is `unchecked`.

`Tdxs.from_policy` gives the `tdxs` validator one string per single-valued register and a list for a register with several accepted values.

`evidence --require measured` makes a variant whose policy has unverifiable registers `incomplete` (code `policy-unverifiable`), with each register and its reason; without `--require measured` it is a note. A register the policy's `platform_approval` approves no longer counts as unverifiable.

### Trust coverage

`attest` says `trusted` only when the quote is authentic (a `tdxs` validator accepted it), it binds the nonce, no register mismatches, and every one of these matched an accepted value:

| Registers | What they measure | Accepted values come from |
|---|---|---|
| MRTD, RTMR0 | The platform: the TD firmware (TDVF) and its configuration (ACPI tables, CFV) | The scheme (`gcp`), or the operator's `platform_approval` (`qemu`, `portable`) |
| RTMR1, RTMR2 | The image: its boot path and UKI | The scheme's replay |

When nothing disagreed but one of the four had no accepted value, the verdict is `partial` (reasons `platform-unverified`, `image-unverified`): `attest` exits 1 unless `--accept-partial`, and `evidence --require attested` reports `attestation-partial`. A validator's `valid` says the quote is genuine; it does not say the firmware was one you approve, so a `qemu` or `portable` policy needs `platform_approval` (take MRTD and RTMR0 from a quote of a host you trust) before it can be trusted. `Tdxs.from_policy` checks the approved values too. An `azure` policy is refused: the TDX quote measures Azure's paravisor, the image is measured into the vTPM, and vTPM evidence is not supported yet.

### Schemes

`tundravm measure --scheme SCHEME` (default `qemu`) computes the registers offline from the artifact. RTMR1 and RTMR2 depend only on the image; MRTD and RTMR0 depend on the host firmware.

| Scheme | Accepted | Unverifiable |
|---|---|---|
| `qemu` | RTMR1, RTMR2. A disk artifact is booted from its disk: RTMR1 includes the artifact's GPT event. A bare UKI uses the direct-boot order | MRTD, RTMR0: depend on the host's TDVF/OVMF build, its ACPI tables and the VM's memory layout |
| `gcp` | MRTD (3 Google firmwares), RTMR0 (27: 3 firmwares x c3-standard-4/8/22/44 ACPI table sets), RTMR1, RTMR2. Needs the gcp disk artifact (GCP boots from a disk) | none |
| `portable` | RTMR1, RTMR2 only: one policy for every firmware version | MRTD, RTMR0: left to the platform's own verifier |
| `azure` | no TDX register: Azure's paravisor owns the TD, so the quote measures Azure's firmware. `vtpm_pcrs` records PCR4/9/11 instead | MRTD, RTMR0, RTMR1, RTMR2 |

The replay:

- RTMR1: `Calling EFI Application from Boot Option`, separator, GPT event (disk boot), UKI Authenticode, kernel (`.linux`) Authenticode, `Exit Boot Services Invocation`, `Exit Boot Services Returned with Success`. Direct UKI boot puts the UKI Authenticode first and has no GPT event.
- RTMR2: the UKI sections systemd-stub measures (stub 256 and later: `.linux`, `.osrel`, `.cmdline`, `.initrd`, `.splash`, `.dtb`, `.uname`, `.sbat`, `.pcrkey`, name then data), the command line (UTF-16LE, NUL-terminated), the initrd.
- RTMR0 (gcp): TD HOB (per machine type), the firmware's CFV, SecureBoot/PK/KEK/db/dbx, separator, ACPI loader/RSDP/tables (per machine type and ACPI set), BootOrder, Boot0001, Boot0002, Boot0000.

### Sources

- The replay is a pure-Python port of Easy-TEE `attest` at `12f1e29` and flashbots `dstack-mr-gcp` at `b16e08b`, the tools flashbots-images pins in its `flake.nix`. No external tool runs.
- The GCP firmware table is embedded in tundravm (`tundravm.measure.gcp_firmware`): per firmware its SHA-384 (the file name of `gs://gce_tcb_integrity/ovmf_x64_csm/<sha384>.fd`), its MRTD (published under `gs://gce_tcb_integrity/ovmf_x64_csm/tdx`) and its CFV hash, computed from the published `.fd`; TD HOB and ACPI hashes per machine type come from dstack-mr-gcp. Embedded, not fetched: `measure` stays offline and deterministic, and the table is reviewable in a diff. A new Google firmware or machine type needs a new tundravm release.
- Test vectors: measured-boot v1.2.0's Authenticode, PCR4, PCR9 and PCR11 vectors, and a real GCP quote from flashbots-images' flashbox-l1 readme: MRTD `feb7486608…` and RTMR0 `e1d0235496…` are reproduced as firmware `gcp-ovmf-f53fdf89544e`, `c3-standard-4 acpi-2026-04`.
- The lockfile's `toolchain.measure` records `tundravm-measure` and `gcp-firmware-table` with their versions.

### Placeholders

An artifact that is no UKI or disk (the in-process backend's simulated ones) is measured only with `--allow-placeholder` (`allow_placeholder=True`): RTMR1/RTMR2 are derived from the artifact digest, `tool` is `placeholder` and `note` says so. `attest`, `Tdxs.from_policy` and `evidence` refuse a placeholder policy unless told to allow it.

## Recipe policy


```python
from tundravm import Fragment, Package, Policy, Recipe

recipe = Recipe(
    name="node",
    items=(Package("curl"),),
    policy=Policy(require_frozen_lock=True, mutable_ref_policy="error"),
)
```

`Recipe(policy=None)`, the default, is `Policy()`.

### Options

| Option | Default | Effect |
|---|---|---|
| `require_frozen_lock` | `False` | `True`: a bake without a lockfile fails with `E_POLICY` instead of baking unpinned |
| `mutable_ref_policy` | `"warn"` | How an unpinned source build (a `Git` branch or tag, an `Http` without `sha256`, and no pin in the lockfile) is treated: `"warn"` reports `source-unpinned` as a warning; `"error"` makes it a lint error and makes `compile` and `bake` fail with `E_POLICY`; `"allow"` reports it as info |
| `network_mode` | `"online"` | `"offline"`: `lock` behaves as `lock --offline` and fails with `E_LOCKFILE` for any source it would have to resolve; `fetch` reuses complete checkouts, fails with `E_POLICY` for a missing one and prefetches no dependencies; `bake` behaves as `bake --offline`: mkosi runs with `--with-network=no`, and a missing Go/Cargo/.NET dependency cache fails with `E_STATE` before mkosi runs (see [reproducibility](reproducibility.md#hermetic-builds)) |
| `storage_safety` | `"error"` | `"warn"`: the `disk-device-guessed` lint (a `Disk(device=None)` whose `format` is not `"never"`, which can pick and format the boot disk) is a warning instead of an error |

A pin in `build/tundravm.lock` satisfies `mutable_ref_policy` for that source, so the usual flow under `"error"` is `tundravm lock` first, then compile and bake.

```console
$ tundravm compile m.py --tree mk
error [E_POLICY]: Unpinned source builds are not allowed by policy: tdxs@master.
Hint: Run `tundravm lock RECIPE` to pin them, or relax mutable_ref_policy.
  operation: compile
  sources: tdxs@master
```

### CI

For CI, refuse anything that is not pinned:

```python
Policy(require_frozen_lock=True, mutable_ref_policy="error", network_mode="online")
```

With it, `tundravm ci` fails at `lint` for every unpinned source, and a bake without a committed lockfile fails before building. `inspect` prints the effective policy on its `Policy:` line.
