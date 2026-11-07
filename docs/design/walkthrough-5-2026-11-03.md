# Walkthrough 5: the day's sentences, read as one story

Date: 2026-11-03. Base: master at `6549296`. Run: every `--help`, `check` and
`show --diff` of each stage, `lock --check` on the example, `check --explain` of the new
rules, both conversions, `new --ci`, and `check` of scratch images with `Firewall(modes=)`
and a `Container`.

## Terms

- "Mode" and "Firewall mode" are two terms; `run --help` calls the first "Security mode".
- Firewall mode and Container sit under "Stages 2 to 6", which use neither.
- Owner and keep-list are used on three pages and defined on none.

## Guide

- Stage 5: `go run ./tools/cmd/attested-client`, run from `examples/attested-node`, finds
  no directory, and `tools/` is its own module.
- Stage 6: `check` prints `warning observability-export-unset [production, staging, bench,
  dev]`; the prose names only `observability-sink-unset`.
- Stage 2 never says who owns the volume.

## How-to

- No task pins the operator key, logs in a tenant, runs a container, keeps some metrics or
  uses a private source. `secrets rotate` is on no prose page.
- Attest: "hands over your SSH key". On TDX the endpoint answers `a TDX boot takes a
  delivered SSH key only from the owner an OperatorKey() volume's encrypted copy
  confirmed`.
- Window: silent on `Refused: ... a production build that pins no operator` and on the
  switcher's `(Linux 6.16 or newer)`.
- Three tasks repeat other pages: the committed tree, Run CI, Upgrade or roll back.

## Manual

- "a volume or a secret opens 8745 and 22": `show` of an image with `Firewall(modes=)`
  lists `in 8745/tcp attested-endpoint` and `in 22/tcp maintenance: ssh.service`; a
  `TpmSeal` volume opens neither.
- "SSH on 22, for the key `attest` hands over": not on TDX without an owner.
- "Refused: ... RTMR3 extends on a kernel older than 6.16" reads as a build check; it is
  every TDX switch, and both kernels are 6.12.
- The quote binds four keys; `attest --help` adds "each AttestedTLS port's certificate key".
- "only a service's `Egress(metadata=True)`": stage 6 shows
  `out 169.254.169.254 gcp-hostname.service`.
- "without it a production profile writing to a `url=` is refused": `--explain` says the
  agent keeps `health`.
- `NAME.py`: `new walkthrough-5-demo` wrote `walkthrough_5_demo.py`.

## Trust and release

- First contact omits firewall modes, the TDX root-SSH rule and the 6.16 limit.
- Release step 1: `release.yml` also checks the example against its committed tree.

## Changelog

- "accepts the baseline's NTS time, resolvers and cloud metadata" contradicts the metadata
  rule. Lockfile versions 7 and 8, `new --ci`, firewall modes and the owner appear twice.

## Wrong in the code

All four were fixed on master on 2026-11-03 (`f7b49102`, `3997d9a5`, `03bcf9f8`, `5355c715`); `054d5f91` adds the tests they lacked.

- ~~A `Container` running busybox fails `check`: `error service-program-missing [production] web.service: ... runs /usr/bin/busybox, which nothing in the image ships`. The program is in the container's root.~~
- ~~`convert --from mkosi` prints `OutputDirectory=...: ... never incrementally`; dev builds are incremental.~~
- ~~No check warns that `Firewall(modes=)` on a 6.12 kernel refuses every TDX switch.~~
- ~~`show` omits the signer's `quotes=True`.~~
