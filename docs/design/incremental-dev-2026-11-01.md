# Incremental dev builds: where a dev rebuild's time goes

Date: 2026-11-01. Status: measured. Section 1 is the cost before, section 2 what landed,
section 3 the result. Scope: `tundravm dev`, the inner loop (edit the image file, build the
dev profile, boot, probe). Before this change any change rebuilt the whole image from the
Debian snapshot; the change is usually one service, one file or one Go program.

## 1. Before

CI's `fixture-boot` job (run 41315278225, job 124873876635, 2026-11-01 14:16, the latest
green one; the host this note is written on cannot build) locks the fixture and builds its
production profile, then its dev profile, each from a clean build root. The build report's
`time` row is the only split the log holds: the job does not run `build -v`, so mkosi's own
steps (package install from the snapshot, the three source builds, the UKI, repart, the
manifests) are one `mkosi` figure; section 3 splits it from a verbose build.

| Phase | production, cold | dev, cold | What it is |
|---|---|---|---|
| checkouts | 0.6 s | 0.0 s | one Git clone and two Local copies; the dev build finds them |
| prefetch | 16.5 s | 0.1 s | `go mod download` twice, `cargo fetch` once, the go1.26.0 and Rust 1.91.0 toolchains; the dev build finds them |
| mkosi | 2m27s | 1m41s | everything mkosi does: the tools tree (the workspace's first build), apt from the snapshot, the three source builds, the extra trees and units, the UKI, repart |
| measurements | 1.2 s | 2.0 s | the event-log replay of `payload.efi` and `qemu.raw` |
| assembly | 1.7 s | 0.9 s | `total` minus the phases: `mkosi summary`, the SBOM, the manifest, the publish rename |
| **total** | **2m47s** | **1m44s** | |

The dev build's whole 1m41s is mkosi, and it starts over: `Incremental=` is off, so apt
installs the payload's packages from the snapshot again, the three programs compile again (the
hooks' own artifact cache lives in `$BUILDROOT/build`, the build overlay, which mkosi
discards with the build), ukify packs the 125 MiB initramfs UKI again. The second `dev` of
a one-line change costs the same as the first.

What mkosi 27.1 caches when `Incremental=yes` and `CacheDirectory=` are set, read from its
source (`mkosi/__init__.py`, `build_image`, `save_cache`, `have_cache`; `mkosi/config.py`,
`cache_manifest`): after the skeleton trees, `apt install` of `Packages=`, the prepare
scripts and `BuildPackages=` (in the build overlay) it moves the tree to
`<CacheDirectory>/<CacheKey>.cache` (and the overlay to `.build.cache`) and writes
`<CacheKey>.manifest`; the next build reuses them when the manifest still equals the
distribution, release, mirror, snapshot, architecture, package manager, `Packages=`,
`BuildPackages=`, `PackageDirectories=`, `Repositories=`, `Overlay=` and the prepare
scripts' bytes. Everything after the cache point runs again: build scripts, volatile
packages, extra trees, postinst and finalize scripts, the UKI, repart. The manifest does
not cover the skeleton trees ("any changes to skeleton trees after a cached image has been
built are only applied when the cached image is rebuilt", `mkosi.1`), the sandbox trees
(the apt sources tundravm writes), the tools tree or mkosi's own version, so tundravm adds
those to the key itself (section 2).

## 2. What changed

| Commit | What | Measured |
|---|---|---|
| a development build keeps mkosi's base tree in an incremental cache | `--incremental=yes` under `build/<image>/.work/.mkosi/incremental/`, `CacheKey=` extended with what mkosi's manifest leaves out; a production build carries no key and never names the directory | mkosi 1m41s to 13.9s on a one-line change |
| a development build says what it reused from mkosi's cache | `cached:` or `caching:` next to `built`, `incremental` in the manifest and `show BUILD` | `caching: packages (Debian 20261018T082309Z, 165 packages) for the next build`, then `cached: ...` |
| a development build keeps the Go and cargo caches in mkosi's build directory | `BuildDirectory=` under the cache, `GOCACHE` and cargo's `build.build-dir` in it | the three source builds take 2.5 s of the rebuild |

## 3. After

Scratch run 41326359420 (branch `ci/incremental-dev`, job 124907144762, 2026-11-01 16:27),
a temporary step in `fixture-boot` before its usual builds: `tundravm dev --no-follow` on a
clean build root, a one-line change to the fixture (the timer's description), `dev
--no-follow` again, then a third change built with `build --dev -v`. The time lines:

```
building: no dev build yet
caching: packages (Debian 20261018T082309Z, 165 packages) for the next build
time  checkouts 0.4s  prefetch 18.6s  mkosi 2m29s  measurements 1.6s  total 2m51s

building: image changed
cached: packages (Debian 20261018T082309Z, 165 packages)
time  checkouts 0.0s  prefetch 0.1s  mkosi 13.9s  measurements 1.5s  total 16.8s
```

The first build is the workspace's first, so it also builds the tools tree and fetches
the toolchains: its 2m29s of mkosi matches section 1's cold production build (2m27s), so
storing the cache costs nothing measurable. The second is the inner loop: 1m44s before,
16.8 s after, mkosi 1m41s to 13.9 s. The verbose third build (mkosi 13.5 s) splits it:

| Step | Time |
|---|---|
| copy the cached tree and build overlay | 0.9 s |
| the three source builds, restored from `build/` | 2.5 s |
| extra trees and postinstall scripts | 2.1 s |
| users, presets, kernel module filters | 1.6 s |
| finalize scripts | 1.2 s |
| cpio, zstd and the UKI | 2.8 s |
| the main image: extra trees, repart, the ESP | 1.5 s |

The same job's usual steps then built the production profile (mkosi 1m23s, no `cached:`
line: it carries no key, so it never reads the dev cache) and the dev profile again (mkosi
14.2 s, `cached:`). The run failed later, on the `run probe` step's `endpoint:8745`
check, the nonce match that `fix: the probe's endpoint check matches the nonce in the
evidence's JSON` corrected on master; the boot tests passed.
