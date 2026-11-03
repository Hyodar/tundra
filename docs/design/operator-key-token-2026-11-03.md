# The operator key in the volume's header

Date: 2026-11-03. Status: design, then landed in the commits that follow it. Gap 31 of
`flashbots-images-gaps-2026-11-02.md`: after a reboot the attested endpoint takes the first
SSH key delivered, so whoever reaches it first gets root. `tdx-init` keeps the searcher's
key in the disk's LUKS header; this does the same for an `OperatorKey()` volume.

## The token

Token 2 of every `OperatorKey()` volume's LUKS2 header (token 1 is `disk-setup`'s
initialisation token):

```json
{"type": "tundravm-owner", "keyslots": [], "version": 1,
 "key": "ssh-ed25519 AAAA...", "fingerprint": "SHA256:..."}
```

`key` is an OpenSSH public key line without comment (ed25519, ECDSA P-256 or RSA, what the
endpoint already takes); `fingerprint` is its `ssh-keygen -l` form. One key: `attest` hands
over the key it signs with, so there is no second client identity to record.

## Who reads and writes it

The endpoint faces the network and keeps its sandbox (`PrivateDevices=yes`): it never opens
a block device. `disk-setup owner` does, as root:

- At boot, `tundravm-volume-owner.service` is ready before the endpoint starts, once it
  copied each volume's token to `/run/tundravm/owner/<volume>` (nothing for a volume
  without one: not attached, not formatted yet, or made before this change).
- When the endpoint pins or rotates an owner it writes that file; the service, which keeps
  watching the files, writes it into the header of a volume that is LUKS already.
- `disk-setup` creates an `OperatorKey()` volume only with an owner file, writing the
  token; it gives a tokenless one the boot's owner, and refuses one whose token names
  another key.

## The copy inside the volume

The header is plaintext the host can rewrite, so pin and rotation also write the owner's
public key to `/.tundravm/owner` inside the volume (root-only). After every unlock, before
anything that waits for the volume starts, `disk-setup` compares the token with that copy;
a mismatch closes the volume again and says `volume <name>: owner token does not match
the volume's owner` on the console, in the probe's `volume:` row and, through the evidence,
to the client. A volume made before this change has no copy: its first unlock writes it.

## Deliveries

Every write on the quote-bound session (`POST /ssh-key`, `/credentials`, `/unlock`,
`/owner`) also carries `signer` and `signature`: an SSH signature (`ssh-keygen -Y sign`,
namespace `tundravm-endpoint`) over the nonce in hex. Without a token the first signed write
pins its signer; with one any other key is refused (403, `the volumes are owned by
SHA256:...`). The evidence's `accepts.owner` names the fingerprint, which `run status` and
the probe's `volume:` row (`owner SHA256:...`) show.

The client signs with the key `attest` uses (`--identity`, else `$TUNDRAVM_SSH_KEY`, else
`~/.ssh/id_ed25519`) and records it in the receipt, so `secrets deliver` and `unlock`
sign with it and take no flag. It refuses, before sending, an instance owned by another
key, and one whose receipt names an owner that came back with none (a stripped token).

## Rotation

`tundravm secrets rotate INSTANCE --identity NEW` signs with the receipt's key and carries
NEW's public key with NEW's signature over the same nonce. The endpoint replaces the owner
file and `authorized_keys`, the owner service the token and the copy, and the receipt
records NEW.

## What does not change

`TpmSeal` and `BrokerKey` volumes get no token: their key is released to a measured image
by a vTPM or a broker, never by a person, so there is no one to pin, and an image without an
`OperatorKey()` volume keeps one key per boot as today.

A lost key means a new volume, by design: anyone who could re-pin without the old key could
take the instance.
