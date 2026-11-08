# Security review of the Go toolchain fetcher, 2026-11-08

`src/tundravm/_go_toolchain.py` and its call sites (`_source.go_toolchain`,
`_source._go_prefetcher`, `project.host_cache`): on a host without `go`, tundravm downloads
the release a go.mod names and runs it. Adversaries: the network (a MITM, a compromised
proxy), the cache directory (stale or planted files), the checksum database (wrong or slow
answers) and the go.mod. **B** breaks the claim that nothing runs before sum.golang.org
vouched for it, **W** weakens it or allows denial of service, **H** hygiene.

| # | Sev | Where | Attack | Fix | Here |
|---|---|---|---|---|---|
| 1 | B | `fetch_go` cache check | Any `toolchains/<go>` holding `.tundravm-verified` and `bin/go` ran: a link elsewhere, a directory another user planted in a shared `XDG_CACHE_HOME`, another processor's release, any marker content. | The marker names module and `h1:`; it counts only in a plain directory of this user's others cannot write, under a toolchains directory of this user's; else moved aside and fetched again. | yes |
| 2 | W | `_download` | `urlopen` followed any redirect, plain HTTP included, read bodies whole, and its timeout bounds only silence: an endless or trickled body hangs or fills memory. | HTTPS to the two hosts; redirects only to the same host or proxy to storage.googleapis.com; 1 MiB per lookup or tile, 200 MB per zip; 60 s and 20 min deadlines, one `recv` per read. | yes |
| 3 | W | `zip_hash`, `_unpack` | Each entry was read whole before the hash was known: a deflate bomb filled memory; a broken stream or an unknown method ended in a traceback. | Stored or deflated, unencrypted entries only, 500 MiB in total (`modzip.MaxZipFile`), hashed in 1 MiB chunks; every `zipfile` error is a `SourceError` naming the fix. | yes |
| 4 | W | `_go_prefetcher` | `GOROOT` was inherited (a host's Go 1.19 tree for the new go), and empty or relative `PATH` entries followed the fetched `bin`: the working directory is the copied source tree, where a git run for a direct module could find a planted `ssh`. | `GOROOT` is the release's, as go's own switch sets it; `PATH` keeps only absolute entries outside the toolchains directory. | yes |
| 5 | H | `fetch_go`, `host_module` | The release name became a cache path and a url unchecked (`../../x`; callers validate, the function did not); an arm64 host fetched arm64 binaries for an amd64 build. | Only `go1.N.P` and `go1.NrcM` shapes pass; a host other than linux/amd64 is refused, naming `apt install golang`. | yes |
| 6 | H | `ed25519_verify` | A key or `R` of small order verified: under the all-zero key, `R` the identity and `S = 0` pass for one message in four. The built-in key is fixed, so only a replaced key was exposed. | Refuse points whose `[8]P` is the identity. | yes |
| 7 | H | `_signed_tree` | The note split at the first blank line, skipped unknown lines, decoded base64 leniently and kept the last signature's verdict (invalid then valid passed). | Read as `note.Open`; exactly one signature by the key, its hash checked; the text exactly `tlog.FormatTree`'s. | yes |
| 8 | H | `_sumdb_hash` | The record index took any digits; with two `h1:` lines for the module the first won. | Canonical decimal; exactly one line. | yes |
| 9 | H | `zip_hash` | A name without the UTF-8 flag was hashed as cp437 text, so a release with a non-ASCII name could never verify (fails closed). | Names are the central directory's bytes, as `archive/zip` reads them. | yes |
| 10 | H | `_unpack` | `rmtree(root)` before the rename removed a concurrent fetch's verified release under the go running from it. | A verified `root` is kept; anything else is moved aside first. | yes |
| 11 | H | `go_toolchain` | `toolchain go1.22`, a language version, failed later as a proxy 404. | Refused up front, naming `go1.22.0`. | yes |
| 12 | H | `host_cache` | A relative `XDG_CACHE_HOME` put the cache, and the fetched `bin` on `PATH`, relative to the working directory. | Ignored, as the XDG spec says. | yes |
| 13 | W | `_sumdb_hash` | No tree head is remembered, so there is no consistency proof between runs: whoever holds the database key can show one client a forked tree. Go keeps the latest head in `GOPATH/pkg/sumdb`. | `toolchains/sum.golang.org.latest` keeps the largest verified head; each new one is proven consistent with it from the larger tree's tiles (RFC 6962), else refused as a fork. | yes |
| 14 | H | `_unpack` | A hard kill mid-unpack leaves a `.go1.N.P.*` scratch directory: never trusted, but kept. | Sweep old scratch directories. | no |
| 15 | H | `_toolchains` | Only the toolchains directory and the release are checked; an owner of a parent directory could swap them after the check. | Check up to the first root-owned parent. | no |

## Checked and found correct

- **Ed25519 against RFC 8032 5.1.7**: `p`, `d`, `sqrt(-1)`, the base point (its encoding
  `5866…66`, order `L`), decoding (`y >= p` refused, the square-root branch, the sign bit,
  `x = 0` with sign 1 refused), the extended-coordinate addition of 5.1.4, double-and-add,
  `S < L`, `k = SHA-512(R || A || M) mod L`, and the cofactorless `[S]B = R + [k]A`
  compared as canonical encodings. Not constant time, which verifying does not need.
  Tested on RFC vectors 1 to 3 and 1024, flipped bits, `S + L` and every non-canonical
  point encoding.
- **The proof**: leaves hash with `0x00`, nodes with `0x01`; `_tree_hash` is RFC 6962's
  with the record's leaf at the lookup's index and every other subtree from tiles, so
  another index, a forged tile or a wrong tile path misses the signed root; `index <
  size`; tile paths and partial widths are `tlog`'s. Tested on logs of 1 to 70000 records
  (three tile levels, partial tiles) against a bottom-up tile builder.
- **A record of another module or version**, though proven, is refused.
- **A replayed older tree head** is harmless: records never change, a head older than the
  release proves nothing, and one the remembered head does not begin with is refused.
- **Extraction**: hash before any write; names outside `<module>/`, `..`, `.`, empty
  parts and absolute paths refused; links and setuid bits in the zip ignored (regular
  files, 0755 under `bin/` and `pkg/tool/`, 0644 elsewhere); staged, marker last, renamed
  into place, so a crash leaves nothing trusted.
- **The proxy's `.info`** is never fetched; versions come from the go.mod and the record.
- **`GOTOOLCHAIN=local`** is set whenever the fetched go runs; an unreachable host names
  `sudo apt install golang`.
- **Live**: an integration test fetches Go 1.25.0 through the redirect and runs it.

## Decisions

- Behaviour changes: arm64 and non-Linux hosts need golang; a release fetched before this
  review is fetched once again (its marker held no module); a go.mod
  `toolchain go1.22` is refused.
- A smaller head the remembered one begins with is accepted, as `go` accepts it, since
  sum.golang.org's lookups are cached for three hours; a wiped cache trusts the first head
  it sees, as `go` does once `GOPATH/pkg/sumdb` is gone.
- The storage host the proxy redirects to is pinned; if proxy.golang.org moves its zips,
  the fetch fails closed with a message naming the redirect.
