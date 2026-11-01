# The BrokerKey pull client: design

Date: 2026-11-01. Status: design, not built. `Volume(unlock=BrokerKey(url, audience=))` and
`Secret(name, source=BrokerKey(...))` exist in the SDK; the image records the broker in
`/etc/tundravm/runtime.json` (`unlock: {"kind": "broker-key", "url", "audience"}`) and
waits for the value exactly as it waits for an operator: `key-gen` blocks on the volume's
FIFO (`/run/tundravm/unlock/<volume>`), a `credential-<name>.service` wait blocks on the
secret's file, and the attested endpoint on 8745 takes the value from whoever attests. This
note says exactly what the pull client is, so the next agent builds it without a design
round.

## Who calls whom

The guest pulls. A fleet's TDs have no inbound reachability a broker can count on, and
`Network(egress=Egress())` already names the outbound traffic an image makes; a broker that
had to reach each guest would need the control plane exposed to it. So one unit per image,
`tundravm-broker.service` (`tools/cmd/broker-client`, built from `tools/` like the
endpoint), runs at boot and, for each value the manifest names (every `broker-key` volume
and every secret whose `source` is a broker), does:

1. `POST {url}/tundravm/v1/challenge` with `{"audience", "name"}`. The broker answers a
   nonce of 32 bytes. The client speaks HTTPS to the broker's URL and checks its
   certificate against the image's CA bundle (`ca-certificates`, which a `BrokerKey`
   adds to the image); `BrokerKey(ca=)` is not in this design: a broker with a private CA
   ships it as a `File`, and the client takes `SSL_CERT_FILE` from the unit.
2. Makes an ephemeral X25519 key pair in memory and asks `tdxs` (`/var/tdxs.sock`) for a
   quote whose user data is the binding below, with that nonce.
3. `POST {url}/tundravm/v1/release` with the tdxs attestation document (hex, as the
   endpoint's evidence carries it), the simulator metadata when the platform is
   `simulator`, the nonce, and every bound part in the clear: `purpose`, `audience`,
   `name`, the ephemeral public key, the instance public key and the gate's mode. The
   broker verifies (below) and answers the value wrapped to the ephemeral key.
4. Unwraps the value and delivers it as an operator would, over the loopback to the
   image's own attested endpoint: one TLS session to `127.0.0.1:8745`, `GET /evidence`
   with a fresh nonce, the binding checked as `tundravm attest` checks it (the same
   evidence rules, in Go: `tools/pkg/verify`'s `bound`), then `POST /unlock` for a volume
   or `POST /credentials` for a secret on that session. The endpoint's rules stay the
   only writer of the FIFO and the credential files: once per value per boot, atomic,
   with the configured mode and owner. The client itself writes no file and runs as its
   own user with no capability; it needs the network and the two unix sockets only.

An operator can still stand in: `tundravm secrets unlock` and `secrets deliver` accept a
`broker-key` volume or secret as they accept an operator's, and whoever arrives first wins
the boot (the endpoint answers 409 to the second). Today `unlock` refuses a `broker-key`
volume; that refusal goes.

## What the quote binds

The release quote's user data, a 64-byte SHA-512 like the endpoint's, with `lp(x)` the
big-endian uint32 length of `x` followed by `x`:

```text
sha512(lp("tundravm-broker/1") || lp(nonce) || lp(audience) || lp(name)
       || lp(ephemeral public key) || lp(instance public key))
```

- the purpose keeps a broker quote from being mistaken for an endpoint quote or a
  signer's (`examples/attested-node/signer/quote.go`);
- the broker's nonce is freshness: a replayed document answers an old challenge;
- `audience` and `name` are what the broker is asked for, so a document made for one
  value releases no other, and a broker serving several fleets tells them apart;
- the ephemeral X25519 public key (32 bytes) is where the value goes: the broker wraps the
  value with HPKE (RFC 9180, DHKEM(X25519, HKDF-SHA256), HKDF-SHA256, ChaCha20-Poly1305,
  base mode) to that key, with the user data as the HPKE `info`, so only the TD whose
  quote bound the key can read it, whatever relays the HTTPS session;
- the instance public key (the compressed secp256k1 key the endpoint binds too) lets a
  broker log which boot it released to, and a signer's later signatures pair with it.

The tdxs issuers put `sha256(user data || nonce)` in `report_data`; the simulator's
32-byte quote carries the same hash with its tag, so a dev profile with
`Tdxs(issuer="simulator")` exercises the whole flow against a broker that accepts
simulated evidence for development audiences.

## What the broker checks

The broker is a verifier with a policy per audience: a `tundravm.policy/2` file with the
operator's `platform_approval`, as `attest` takes. It rebuilds the user data from the
parts it was sent, refuses a document whose user data differs or whose nonce is not one it
issued for that audience and name within the last minute, judges the quote as
`tools/pkg/verify` judges one (signature chain, `report_data`, registers, `DEBUG`, the
gate's mode), and releases only on `trusted` (or `simulated` for an audience it marks as
development). It never releases to a `measurements-match`: the broker is unattended, so
the quote's chain must be checked, which Go does offline against Intel's root. The value
it releases is the passphrase or secret bytes the operator gave it; it stores them
however it likes. A reference broker is out of this design's scope: the protocol above is
small enough that `tools/cmd/key-broker` with `policies/<audience>.json` and values from
files is a day's work once the client exists, and makes the CI proof.

## Which unit waits

- `tundravm-broker.service`: `Type=notify`, started at boot into the boot target, ordered
  `After=network-online.target tdxs.socket tdx-evidence.service tundravm-instance-key.service tundravm-endpoint.service`,
  `Wants=network-online.target tdxs.socket`. Not gated by `tdx-evidence.service`, like
  the endpoint: it is control plane. `Restart=always` with `RestartSec=10s`: a broker that
  is down, a network not yet up or a quote `tdxs` cannot issue yet are retried for as long
  as the boot lasts, each attempt logged with the broker's answer. It notifies readiness
  once every value is delivered and exits 0; it never fails the boot.
- `runtime-init-keys.service` keeps blocking on the volume's FIFO through `key-gen`, so
  `runtime-init-disks` and every service with state on the volume wait as they do for an
  operator; `credential-<name>.service` keeps blocking on the secret's file.
- The attested endpoint waits for nothing of this: an operator may still deliver first.

## What the image gains

- `tools/pkg/broker` (the client: challenge, quote, release, unwrap, local delivery) and
  `tools/cmd/broker-client --config /etc/tdx/broker.yaml`; the config lists the values
  (`name`, `kind` volume or secret, `url`, `audience`), the sockets and the endpoint port.
- `src/tundravm/_modules/broker.py` renders the config and the unit;
  `_image/_runtime.py` adds them to `declarations()` whenever a volume or a secret names a
  broker, with `ca-certificates`, and `BrokerKey.outbound()` adds the broker's host and
  port to the egress rules (`Component.outbound()`'s shape), so a filtered image reaches
  it; `runtime.json` records the client beside the broker.
- `tundravm show BUILD` lists the broker under the control plane; the dev probe gains
  `broker:NAME` (the unit active or retrying, the value delivered or still awaited).
- `bootstrap.unlock` accepts `broker-key` volumes.

Tests, one per contract: Go, a table against an in-process fake broker and the real
endpoint server (release lands in the FIFO; a nonce the broker did not issue, a document
for another audience, a wrapped value for another key, each refused and retried); Python,
the lowering (unit, config, egress rule, manifest) in `tests/image/test_runtime.py`.
Nothing here needs a TDX host; the CI fixture's dev profile can carry a broker-unlocked
volume once `run` attaches disks for a declarative recipe, or the attested node example
can, behind the simulator issuer and a fake broker in the guest.
