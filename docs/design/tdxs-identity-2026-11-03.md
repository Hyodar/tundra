# Whose quote: tdxs tags each quote with its caller

Date: 2026-11-03. Fixes finding 5 of [the tools review](tools-review-2026-11-03.md): any
service in the `tdx` group could ask tdxs for a quote over an endpoint binding for its own
TLS key; the host then routes 8745 to it and receives passphrases and credentials.

## The protocol

tdxs learns who asks from the socket, never from the request. At accept it reads the
peer's `SO_PEERCRED` and names the caller:

- uid 0 is `endpoint`;
- any other uid is its user name in `/etc/passwd`: the unit's stem for a `Service`
  without `user=` (`signer`).

On `issue` it replaces the user data with `lp(tag) ‖ userData`, `lp(x)` being `x`'s
big-endian uint32 length then `x`, before any issuer sees it. So `report_data` is
`sha256(lp(tag) ‖ userData ‖ nonce)` and the document's `user_data` is the tagged
bytes. The simulator issuer gets the same tagged request, so a development fixture
proves the tag as a TD does.

Refused, each with a `fix:` line:

- a non-root caller whose user is named `endpoint`;
- a uid with no `/etc/passwd` entry (a `DynamicUser=` unit): it has no stable name;
- a request whose optional `caller` field names another tag than the peer's. The
  endpoint sends `caller: endpoint`, so an endpoint that is not root fails at tdxs, not
  at every verifier.

Why the uid, not a second root-only socket: the endpoint already runs as root, a
root-only socket admits exactly the same processes, and one listener keeps tdxs's socket
activation as it is. Root in the guest reads the endpoint's memory anyway: it is the
boundary either way.

## Verifiers

A verifier requires the tag of what it verifies, and refuses an untagged document (its
`user_data` is not `lp(tag) ‖ the binding`):

- 8745 evidence: `endpoint`. `tools/pkg/verify` (and so `attested-client`),
  `bootstrap.verify` through `attestation.attest(caller="endpoint")`. An `AttestedTLS`
  certificate key is bound by the endpoint's quote, so its tag is `endpoint` too.
- A `quotes=True` service's own quote: that service's user name.
- `attest --issuer` (a relayed tdxs, bound to no session) accepts any tag and records it.

`attest --output` writes `tundravm.attestation/3`, which records `caller`; a `/2` record
has no tag and is refused offline. Evidence from an image built before this change is
refused: its document is untagged. That is the breaking change.

## Lowering

- The endpoint stays root and keeps `/var/tdxs.sock`; no unit or tree changes.
- `Service(quotes=True)` is refused with `user="root"` (it would quote as the endpoint)
  and for a user named `endpoint`.

## The signer

The signer's quote carries `user_data = lp("signer") ‖ compressed key`, and
`report_data = sha256(that ‖ nonce)`. Stage 5 does not change and neither does the
signer's request; only its comment says what the document holds. A client of its
`GET /quote` requires the tag `signer`, the unit's stem, and so tells the signer's quote
from the endpoint's.

## What stays

- Two services under one user share a tag: give each its own user to tell them apart.
- A root service could still quote as the endpoint: root is the guest's whole trust.
- broker-key: the broker's check (`verify.JudgeQuote`) takes the caller it requires, the
  broker client's user name, and compares the document's `user_data` with
  `lp(caller) ‖ the broker binding`.
