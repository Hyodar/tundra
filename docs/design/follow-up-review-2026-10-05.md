# Follow-up review (external, 2026-10-05, after the contract rounds)

Read-only follow-up to final-review-2026-10-05.md. Findings drive the next polish round.

The contract-closing work materially improves the SDK: verified sources, artifact checks, variant-local pins and explicit lifecycle values now form a credible foundation. The public model is settled, and the new inspection features are useful. I still would not call frozen builds dependable: ordering and cache gaps can silently change—or preserve the wrong—image contents.

Static, read-only review; no files written, tests or bakes run. The corrected [QEMU TDX flags](https://www.qemu.org/docs/master/system/i386/tdx.html), [Azure VHD-to-Gallery arguments](https://learn.microsoft.com/en-us/cli/azure/sig/image-version?view=azure-cli-latest), and [gcloud TDX selection](https://docs.cloud.google.com/sdk/gcloud/reference/compute/instances/create) match current official syntax; that does not establish successful booting.

Top eight remaining issues, ranked:

1. **Lock v4 loses runtime-init ordering.**  
   [_lowered.py:521](/home/franco/tundra/src/tundravm/declarative/_lowered.py:521): `sorted(entries, key=lambda item: (item.priority, item.script))`. The emitter instead sorts stably by priority. Changing `Init.after` between equal-priority steps changes `/usr/bin/runtime-init`, while the lock payload remains identical; frozen verification accepts it. **Fix:** serialize the actual resolved execution sequence, preserving order within each priority, using the same ordering/deduplication helper as emission. **M.**

2. **The cache fingerprint still does not identify the toolchain.**  
   [_source.py:820](/home/franco/tundra/src/tundravm/_source.py:820): `"toolchain": {"kind": self.build.kind, "packages": list(self.packages)}`. Package names remain unchanged when the distribution snapshot changes their versions. With persistent `BuildDirectory`, updating the snapshot and re-locking can restore a binary produced by the previous compiler or libraries. **Fix:** incorporate the effective distribution, repositories, build packages/settings and resolved toolchain identity into the fingerprint; disable cross-build reuse where that identity cannot be established. **M.**

3. **`Install.tree` silently drops dotfiles.**  
   [build_cache.py:120](/home/franco/tundra/src/tundravm/build_cache.py:120): `cp -r "{a.src}"/* ...`; restoration repeats the glob at line 130. A build output containing ordinary files and `.config` succeeds without installing `.config`; an empty or dotfiles-only tree fails. Directory declarations were repaired, but build-output directories remain lossy. **Fix:** use quoted `cp -a "$source/." "$destination/"` for both cache storage and restoration, preserving links, modes and empty directories. **S.**

4. **Project configuration can silently select another lock.**  
   [cli.py:985](/home/franco/tundra/src/tundravm/cli.py:985): `... not Path(str(value)).is_file(): continue`. A missing configured `release.lock` falls back to `build/tundravm.lock`, or an unfrozen bake, while `config` still reports `release.lock`. Existing configured locks also skip lint drift because line 1133 requires `origin == "flag"`. **Fix:** preserve the configured path even when absent; consuming commands must diagnose that path. Apply identical drift behavior to flag- and project-selected locks, and make `config` report that shared resolution. **M.**

5. **Azure defaults cannot boot the ordinary unsigned output.**  
   [azure.py:42](/home/franco/tundra/src/tundravm/deploy/azure.py:42): `params.pop("secure_boot", "true")`; Python’s `Azure` also defaults to `True`. The generated UKI has no trusted-signing contract, yet deployment uploads it and creates resources before firmware rejects it. Azure requires trusted signatures with Secure Boot enabled. [Microsoft’s requirements](https://learn.microsoft.com/en-us/azure/confidential-computing/confidential-vm-overview). **Fix:** record signing state, reject incompatible `secure_boot=True` before uploading, and give the explicit `--param secure_boot=false` remedy for intentionally unsigned images. **M.**

6. **Dependency-cache “verification” trusts only its marker.**  
   [_source.py:1106](/home/franco/tundra/src/tundravm/_source.py:1106): `recorded.get("spec") == build.deps_spec()`. Remove `deps/go` while retaining its JSON marker: fetch reports “kept,” and offline preflight passes despite missing dependencies. `fetch --force` refreshes checkouts but still keeps that marker. **Fix:** validate cache contents against a per-build dependency manifest, invalidate incomplete records, and make force refresh dependencies too. Offline failure should name the missing cache entry and repair command before mkosi starts. **M.**

7. **`status` still lints against a different lock.**  
   [status.py:197](/home/franco/tundra/src/tundravm/status.py:197): `check_report(loaded.recipe, loaded.image, variants=names)` omits the lock already loaded by `project_status`. Thus `status --lockfile custom.lock` can report healthy pinned sources alongside `source-unpinned` lint errors under strict policy—or hide missing pins behind an unrelated default lock. **Fix:** pass the selected `Lock` through `_lint`; represent “no selected lock” explicitly so it cannot trigger implicit default lookup. **S.**

8. **`inspect --why` invents provenance for nonexistent directory descendants.**  
   [explain.py:940](/home/franco/tundra/src/tundravm/explain.py:940): `path.startswith(f"{key[2]}/")` is enough to match a `Directory`. Asking why `/etc/app/typo.conf` exists succeeds whenever `/etc/app` is imported, then lists that directory’s actual files. **Fix:** require an emitted-path match for descendant queries, retain explicit removed-declaration explanations, and restrict returned files to the requested path. Include directory entries in the emitted-path index. **M.**

Five documentation nits:

- **README → Reproducibility:** replace “`bake --no-fetch` works on an air-gapped host” with the dependency-prefetch, `--offline`, and local package-mirror prerequisites.
- **Reproducibility → Pinned sources:** replace “`compile(recipe)` uses the refs” with “unpinned current-dialect builds emit failure hooks.”
- **CLI → Explaining one object:** replace “nothing is written” with “only temporary compilation files are written.”
- **Reproducibility → Lockfile migration:** show `lock RECIPE --lockfile PATH → fetch → compile`; explain that unchanged pins survive migration and newly tracked EFI sources may require network access.
- **CLI → Project configuration:** qualify “effective values”: current `config` prints default path strings and a backend placeholder, not the loaded recipe’s concrete backend.

Two simplifications:

- **Unify `load` and `load_recipe`.** Their renamed keyword now agrees, but `load(attribute="recipe")` still rejects `RECIPE`/`build()` files that CLI discovery accepts. Keep one discovery default, `attribute=None`, with one implementation.
- **Delete GCP’s `_copy_command` compatibility probe and `gsutil` fallback.** Require a documented contemporary gcloud version and call `gcloud storage cp` directly; the TDX adapter already depends on contemporary compute functionality.