# Walkthrough 4: the stage story, read as the first user

Date: 2026-11-01. Base: master at `fe57c77`. Branch: `walkthrough-4`. Host: no sudo, no
QEMU. Run: every `--help`, `check`, `show` and `show --diff` on each stage, `lock` and
`lock --check` on stage 1, `build --tree-only` of stage 1 and `convert` of that tree,
`convert --from systemd`, `check --explain`, a misspelt command, an unknown flag, an empty
argv, `new demo --ci`.

## README

- "a signed manifest": a manifest is signed with `--sign` only.
- The listing's docstring and the paragraph under it both list the baseline.
- The production-build sentence joins two ideas.

## The guide

- Intro: "the gates of the v2 plan", never linked to `design/plan-v2-2026-10-30.md`.
- Stage 1: "the probe" before it is defined; the prose repeats the docstring.
- Stage 2: four ideas at once (the component, a volume, a generated secret, verity);
  attest, `secrets unlock attested-node-1` and export before their definition. What the
  volume brings goes unsaid: `show --diff` of stages 1 and 2 prints
  `+ in 22/tcp ssh.service` and `+ in 8745/tcp attested-endpoint`.
- Stage 4: "the key each boot generates" is the instance key, defined under Services.
- Stage 5: "The firewall opens 8546 and nothing else". `show` of stage 5: `in 22/tcp
  ssh.service`, `in 8546/tcp attested-tls-8546.service`, `in 8745/tcp attested-endpoint`.
- Stage 6: the `JournalExport` sentence joins three ideas; "in it" points two sentences
  back; "never shipped" is undefined.
- What is implied: "8745 when a volume or secret needs the SSH key delivery": 22 opens too.
- Services: "`run status`, `attest` and `show` print its public key". `show` prints
  `Instance key: secp256k1, fresh each boot; taken by signer.service`.
- Timers, Network and observability: "adds node metrics to stage 6's journal": stage 6
  had no `metrics=`.
- Profiles: `DevAccess(password=)`, what stage 1 uses, is not explained.
- The lock: "`source-unpinned` ... naming the profiles that build it". `check` prints
  `warning source-unpinned [image] sources.tdxs: ...`, then `hint (all 3 above)`.
- The mkosi tree: the starter's tree names `30-tdxs` and "the dev overlay (public keys
  only)". Stage 1's has `30-firewall.conf`, and its dev overlay holds the probe, a drop-in
  and the password's postinst.
- Deploy, attest, secrets, upgrade: `prover-1`, `build/prover/...`,
  `policies/prover-gcp.json`, `api-token=@token.txt`: the old example's names.
- Converting: "a `# unsupported:` names what no slot holds". `OOMScoreAdjust=` lands in
  `unit=DropIn(extra=...)` under `# note:` lines; unsupported is a unit that is no service.
- When something fails: "An unknown command or flag gets a did you mean". `check --foo`:
  `tundravm check: error: unrecognized arguments: --foo`.

## Trust and release

- Trust labels **design** and uses it nowhere; "plan 6.1" links nothing; "A lost
  instance" uses `prover` and `api-token`.
- Release: "with no clone" and the PyPI sentence each join two ideas.
- `release.yml`'s test job checks `examples/surge-tdx-prover/image.py`, which is gone: a
  `v*` tag fails there (`.github/workflows/` is another branch's).

## Fixed on this branch

Every stumble above but the workflow, and `PrometheusRemoteWrite(url=)` optional as
`JournalExport(url=)` is, so stage 6 carries `metrics=` without guessing a receiver.
