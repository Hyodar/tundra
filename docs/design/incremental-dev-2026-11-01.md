# Incremental dev builds: where a dev rebuild's time goes

Date: 2026-11-01. Status: section 1 is measured; section 2 records what landed, section 3
the measured result. Scope: `tundravm dev`, the inner loop (edit the image file, build the
dev profile, boot, probe). Today any change rebuilds the whole image from the Debian
snapshot; the change is usually one service, one file or one Go program.

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

## 3. After
