# Decisions taken on 2026-11-08

Franco: "Choose what you think is most reasonable, document for me and continue
development." These are the calls that had waited on him, each with the reasoning, what it
changes for a recipe author, and the branch that carries it out. Every one follows the
same rule the project already lives by: the secure or measured behaviour is the default, the
other way is spelled out and warned about in production, and nothing is kept for
compatibility.

## 1. Egress is filtered by default

An image that says nothing about egress gets what `Network(egress=Egress())` gives today:
only what its components declare, plus DNS, time and the attested endpoint. Opening egress
is an explicit `Network(egress=Open())`, which `egress-unfiltered` warns about in production
and `check --explain` describes. The guide's stages drop the `egress=Egress()` line where it
only restated the default, and the manual's network section describes the default first.

Why: `egress-unfiltered` was the one finding every new image started with, and the fix was a
line that added nothing but the word. A measured image whose network is open by omission
is the wrong failure mode for a TEE. Branch `egress-default`.

## 2. An image with secrets has an owner, baked when the operator has a key

Two findings of the tools review said the same thing from two sides: without an owner, the
first writer of a credential, the cloud host included, becomes the owner (item 39), and an
`OperatorKey()` volume's first boot pins whichever key arrives first (item 40). The
decision: an `Image` may name its operator's public key, `Image(owner=OwnerKey("ssh-ed25519
AAAA..."))`, and that key is measured with the image, so the attested endpoint accepts
deliveries and unlocks from it alone from the first boot, with nothing to pin. An image
with secrets or an operator-unlocked volume and no owner, baked or pinned by a volume, is
`secrets-without-owner`: an error in production, a warning in development.

Why: trust on first use is the one place the current model lets the host in, and the fix is
cheap because the operator who builds the image is the operator who holds the key. Each
operator builds their own image already (they lock, build and measure it), so a key in the
image costs no reproducibility. Branch `owner-baked`.

## 3. A container takes credentials; the flashbox searcher gets the engine jwt only if upstream does

A `Container(credentials=[...])` slot passes a `Secret` or `Generated` value into the
container the way `Service(credentials=)` does, as a file under the container's credentials
directory, never a bind. The parity row says flashbox upstream hands its searcher container
the engine API's jwt; the branch confirms that against upstream's configuration before
wiring `nethermind.jwt` into the example, and the README says what that lets a searcher do.
If upstream does not, the slot lands and the example stays as it is.

Why: the slot is the general mechanism the example lacked, and the example should mirror
upstream rather than my guess. Branch `container-credentials`.

## 4. No writable container layer

A container's root stays immutable and measured. What a searcher needs at run time is
declared in `Container(packages=)` and built into the image; a tool installed after boot is
unmeasured code in the TEE, which is what the whole design exists to prevent. The flashbox
README's "still lacks" bullet becomes a sentence in its design section saying so.

Why: the feature needs a uid-range user namespace and a day of systemd work that no boot
can verify while CI is locked, and it buys a property the project does not want. Branch
`docs-decisions`.

## 5. `broker-key` lands

The parked branch (seven commits from 2026-11-03: the broker client, the reference broker, a
`BrokerKey` volume unlocked at boot, `AttestedTLS` pinning the requested port's key) is
rebased onto master, gated and landed, with items 41 and 42 of the status note addressed:
`verify.judge` checks collateral, TCB level and revocation in production, and
`AttestedTLS` returns a `tls.Config` pinning the requested port's key.

Why: it closes the last gate-3 blocker and nothing on master contradicts it; the parking was
about Franco's time, not the design. Branch `broker-key-land`.

## 6. No tag and no PyPI yet; the README installs from master

mkosi is not on PyPI, so tundravm cannot be published there without vendoring it, and
nothing has booted under CI since 2026-11-05. The README's install line pins `@master` until
the first release, which `docs/release.md` describes as cut from a commit CI has booted;
`v2.0.0` is that commit, not an arbitrary one. PyPI waits for mkosi upstream or a decision
to vendor, which is not taken today.

Why: a tag that no boot has proven is a promise the project cannot keep, and a README line
that fails is worse than one that moves. Branch `docs-decisions`.

## 7. The SSH host key is flashbox's TLS identity

The container's SSH host key stays the server identity of the input channel. It is bound by
every quote and written to `known_hosts` by `attest`, so the sender pins it without a new
secret or a new attestation step; Ed25519 signatures in SSH and in TLS 1.3 sign
differently framed messages (TLS prefixes the context string), so a signature from one
protocol is not a signature in the other. A dedicated key would add a second binding to
carry for no new property. Documented in `docs/trust.md` and the flashbox README. Branch
`docs-decisions`.

## 8. The exported `instance` label is the deployment's name

Every exported series carries `instance=<the instance key>`, which is fresh on every boot,
so a reboot starts a new series at the sink. The label becomes the stable identity the
operator knows, the instance name `deploy` gives (`flashbox-1`), carried into the image as
a credential the boot reads; the instance key stays a separate label for anyone who needs
the per-boot identity. Branch `instance-label-stable`.

## 9. A boot prints its host keys on the console

An image without an attested endpoint gets no pinned `known_hosts` from `pin_host_keys`, and
`tundravm ssh` then refuses rather than trusting on first use. The boot probe prints the
guest's host keys on the console (`tundravm run` reads it), which becomes the second source:
`known_hosts` is written from the console when the endpoint is absent, and from the
endpoint when it exists. Branch `console-hostkey`.

## 10. Reviews are part of a landing

Today's review of the login CLI found nine real bugs after two author agents and a passing
gate. Two more adversarial reviews run now: the flashbox input channel and the signer (Go
compiled into images), and the Go toolchain's checksum-database client. A kernel hardening
audit against KSPP and flashbots' deny-list is report-only, since no kernel change can be
booted until CI returns. Branches `review-channels-signer`, `review-sumdb`,
`kernel-audit`.

## Not decided today

Re-enabling the `ci` workflow is Franco's, once the account's billing clears. PyPI
publishing waits for mkosi upstream.
