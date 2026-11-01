# What only a real deploy proves (2026-11-01)

The unit suite walks every `gcloud` and `az` call against recorded exchanges
(`tests/fixtures/clouds/`); it cannot show that GCE boots the image. With a production
build of an image declaring a volume `data` (`OperatorKey()`) and a secret, run:

1. `tundravm deploy gcp --build BUILD --volume data=create:10GiB --plan`, then the same
   without `--plan` (`machine_type` a C3 type, in a zone offering TDX).
2. `tundravm attest INSTANCE --policy BUILD/measurements.json --validator ENDPOINT --json`.
3. `tundravm secrets deliver INSTANCE --policy BUILD/measurements.json --validator
   ENDPOINT NAME=@FILE`, then `tundravm secrets unlock INSTANCE data --validator ENDPOINT`.
4. `tundravm deploy status INSTANCE --json` and `tundravm deploy logs INSTANCE`.

Paste back the plan, every output and the log.

| Output | Proves |
|---|---|
| deploy's `address` | the `instances create` flags, the image's guest OS features |
| attest `trusted` | the GCP MRTD/RTMR0 table for that machine type; RTMR1, RTMR2 |
| unlock, then `application listening` | `65-gce-disk-naming.rules`, `google_nvme_id`, the firewall rule |
| the log | `gcp-metadata-dns`, `gcp-hostname` |

`gcp-forwarded-ips` needs a passthrough load balancer in front. Azure
(`azure-complete-provisioning`, the vTPM PCRs) needs its own run of this list.
