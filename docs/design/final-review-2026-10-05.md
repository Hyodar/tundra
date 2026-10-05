# Final review of the rebuilt SDK (external, 2026-10-05)

Read-only review by an external model of the declarative SDK after the redesign landed. Findings drive the contract-closing rounds that follow; see CHANGELOG for what was fixed.

**1. Verdict — five sentences**

The redesign has produced a coherent SDK: `Recipe`, explicit variant overlays, reusable fragments, and lifecycle functions form a model worth keeping. Construction-time validation, contextual errors, section-level drift reports, and inspectable compiler output are particularly good. The examples, parity fixtures, and reported real bakes provide substantially stronger evidence than the original design alone. However, several advertised guarantees—immutable inputs, frozen builds, preserved permissions, and artifact provenance—still have implementation gaps that can mislead users or change image contents silently. My verdict is **strong architecture and substantial implementation, but not yet an excellent end-to-end experience; close these contracts before adding more surface area**.

This was a static, read-only review; I ran no tests or bakes and wrote no files. Findings below distinguish implementation evidence from reported build validation.

**2. Top 10 remaining issues, ranked**

1. **The lock does not cover all image-defining inputs.**  
   **Where:** [_lowered.py:168](/home/franco/tundra/src/tundravm/declarative/_lowered.py:168), payload at line 346.  
   **Problem:** `return recipe_payload(self.state, self.active)` excludes configuration held outside `state`: mirrors, snapshot, mkosi options, kernel configuration and command line. Changing a snapshot or kernel config can therefore leave the lock “up to date”; pinning the kernel repository does not pin its configuration. Several debloat options are also absent from the payload.  
   **Fix:** Introduce lock v4 sections `distribution`, `compiler`, `variants.<name>.kernel`, and complete `variants.<name>.debloat`; include config **bytes**, effective settings, epoch, and compiler version. Old locks should require migration rather than silently retain incomplete coverage. **Effort: L.**

2. **Service-specific secret delivery loses its destination.**  
   **Where:** [state.py:879](/home/franco/tundra/src/tundravm/declarative/state.py:879).  
   **Problem:** `SecretEnv("API_TOKEN", service="app")` becomes `SecretTarget.env(target.name, scope="service")`. The literal service name disappears; `"app"` and `"other"` produce the same delivery target. The storage example consequently promises a distinction the emitted configuration cannot express.  
   **Fix:** Preserve `service="app.service"` through the internal model and manifest: `{"kind":"env","location":"API_TOKEN","service":"app.service"}`. Wire the corresponding service environment and reject unknown service references; until supported, reject non-`None` `service`. **Effort: M.**

3. **Build-cache keys ignore changes to the build itself.**  
   **Where:** [_source.py:562](/home/franco/tundra/src/tundravm/_source.py:562).  
   **Problem:** The default key is `f"{self.name}-{url_hash}-{version}"`; a supplied key merely gains the pin prefix. With persistent `BuildDirectory`, changing `Go(tags=...)`, environment, script, or toolchain at the same commit can restore old binaries. Updating the recipe lock does not invalidate that cache.  
   **Fix:** Use `cache_namespace + sha256(source_pin, build_spec, install_spec, arch, toolchain_identity)`. Treat `cache_key=` as a namespace, never a replacement for the semantic fingerprint. **Effort: M.**

4. **Artifact provenance is reported without enforcing artifact integrity.**  
   **Where:** [lifecycle.py:1078](/home/franco/tundra/src/tundravm/declarative/lifecycle.py:1078), [status.py:337](/home/franco/tundra/src/tundravm/status.py:337).  
   **Problem:** Measurement returns `artifact_digest=artifact.sha256` from the manifest, although the measurement layer reads the current file. Neither that freshly computed digest nor deployment is checked against the recorded one; status checks existence and recipe/tree metadata, then prints the old SHA. Replacing the artifact can yield measurements attributed to the wrong bytes and an `ok` status.  
   **Fix:** Add `verify_artifact(artifact)` and call it before measure/deploy; mismatches raise `E_ARTIFACT_CHANGED`. Status should expose `integrity="verified|unchecked|mismatch"` and offer `--verify`. **Effort: M.**

5. **A completion marker is mistaken for verified source contents.**  
   **Where:** [_source.py:617](/home/franco/tundra/src/tundravm/_source.py:617).  
   **Problem:** `is_fetched()` checks only whether `.tundravm-complete` contains the pin. Edited or deleted checkout files still count as complete, and changing `submodules=False` to `True` at the same commit reuses the same directory without populating submodules. Frozen builds can consume those changed contents.  
   **Fix:** Key checkouts by the full checkout specification and pin; record a content manifest. Verify Git HEAD, tracked/untracked changes, and submodule state—or extracted HTTP contents—before reuse, with `E_SOURCE_MODIFIED` naming the repair command. **Effort: M.**

6. **`Directory(mode=None)` broadens permissions silently.**  
   **Where:** [state.py:631](/home/franco/tundra/src/tundravm/declarative/state.py:631); API table says `mode=None (keep)`.  
   **Problem:** `file_mode = mode or ("0755" if host.stat().st_mode & 0o111 else "0644")` turns a private `0600` configuration into `0644`. The directory importer also omits empty directories and directory symlinks, and dereferences file symlinks. This is a lossy import, not preservation.  
   **Fix:** Make `Directory(..., mode=None, symlinks="preserve")` preserve permission bits, links, and empty directories; offer explicit normalization separately. Ensure image permissions survive committed-tree round trips through emitted metadata. **Effort: M.**

7. **Explicit locks are not consistently used by lint and CI.**  
   **Where:** [cli.py:1282](/home/franco/tundra/src/tundravm/cli.py:1282), [lifecycle.py:478](/home/franco/tundra/src/tundravm/declarative/lifecycle.py:478).  
   **Problem:** `ci --lockfile custom.lock` uses the override only in `_ci_lock`; lint and compile consult the default location. Likewise, `lint(recipe, lock=locked)` performs compiler checks before applying the supplied lock, so a pinned source can still trigger `source-unpinned`, especially under strict policy.  
   **Fix:** Pass one explicit lock through every stage: `check_report(..., lock=locked)`; add `lint --lockfile PATH`; have CI load the lock once and share it. **Effort: M.**

8. **Source identity is variant-local in declarations but global when locking.**  
   **Where:** [_lowered.py:193](/home/franco/tundra/src/tundravm/declarative/_lowered.py:193).  
   **Problem:** `builds.update(...source_builds)` overwrites earlier variants’ same-named builds. A valid overlay replacing `Build("app", Git(..., "stable"))` with another ref cannot retain both pins when locking all variants; the earlier variant may compile an unpinned failure hook.  
   **Fix:** Address pins as `sources.<variant>.<build>` while deduplicating identical source specifications internally; support `lock --update dev/app`. At minimum, diagnose conflicting names before writing a lock. **Effort: M.**

9. **The “immutable value” guarantee stops at toolchain mappings.**  
   **Where:** [_source.py:152](/home/franco/tundra/src/tundravm/_source.py:152), also `CargoBuild` and `DotnetBuild`.  
   **Problem:** `env: Mapping[str, str] = field(default_factory=dict)` stores caller-owned mutable dictionaries, as does `Dotnet.properties`. Mutating an input dictionary after constructing a `Recipe` changes subsequent compilation despite every surrounding dataclass being frozen.  
   **Fix:** Preserve ergonomic `Go(env={"CGO_ENABLED":"0"})`, but defensively copy mappings into immutable storage and normalize sequence fields at construction. Apply the same rule to configurable `Composite` fields or clearly constrain their supported types. **Effort: M.**

10. **Generated onboarding becomes stale immediately after locking sources.**  
    **Where:** [cli.py:1509](/home/franco/tundra/src/tundravm/cli.py:1509).  
    **Problem:** The numbered sequence is `compile → pytest → lock → ci`. For the prover template, locking changes emitted source hooks, so the tree just compiled and tested is stale at CI. The explanation “pin packages and sources” also overstates package-list locking.  
    **Fix:** Print `lock → compile --out mkosi → pytest → ci --out mkosi → bake`. Replace the wording with “record recipe sections and pin source repositories/downloads.” **Effort: S.**

**3. Naming and consistency nits**

| Current → proposed | Reason |
|---|---|
| `compile(lock=…)`, `bake(locked=…)` → consistently `lock=` | One concept, one keyword |
| `lock --path` → `lock --lockfile` | Matches consuming commands |
| `load(attribute=…)`, `load_recipe(attr=…)` → one loader with `attribute=` | Avoid duplicate discovery interfaces |
| Root imports except `diff/measure/deploy` → document `tundravm.declarative` consistently | Removes module/function surprises |
| `Backports(mirror=…)` → `archive_url=` | Its value is verbatim, unlike `Recipe.mirror` |
| “one artifact per variant” → “one artifact per target” | Matches `targets=` |
| `deploy --allow-placeholder` → `--allow-simulated-artifact` | Deployment creates no placeholder measurement |
| Bare compile `digest:` → `recipe_digest:` / `tree_digest:` | CLI and Python currently describe different digests |
| API’s contradictory fetch-export sentence → “Import `fetch` and `FetchedSource` from `tundravm.declarative`.” | Those exports exist |

**4. Documentation gaps**

- **[Concepts → Keys, disks and secrets](/home/franco/tundra/docs/concepts.md:128):** Add the actual HTTP request, payload schema, readiness signal, failure response, and how delivered environment values reach a service. Explain disk selection and formatting failure behavior beside the first example.
- **[CLI → Measure and deploy](/home/franco/tundra/docs/cli.md:411):** Add one complete real deployment per target: prerequisites, required arguments, expected endpoint, verification, and teardown. Distinguish successful image construction from boot/application/attestation validation.
- **[Concepts → Lowering and compiling](/home/franco/tundra/docs/concepts.md:68):** Document `Path` anchoring with `ROOT = Path(__file__).resolve().parent`. Relative file inputs currently follow the process working directory, which surprises users invoking recipes from elsewhere.
- **[API → Lifecycle](/home/franco/tundra/docs/api.md:177):** Replace “without lock … source builds use their refs” with the dialect-specific behavior: current-dialect unpinned hooks fail; historical hooks fetch. Show the single supported sequence for explicit Python locks.
- **[Tutorial → Measure](/home/franco/tundra/docs/tutorial.md:323):** Show conversion from measurement JSON to `Tdxs.expected_measurements`; the attestation example requests lowercase names and MRTD, while RTMR output uses uppercase register names and does not supply MRTD.

**5. Additional risks**

- **Historical dialect:** `nethermind-v1` excludes kernel pins and retains in-build fetching. Golden parity demonstrates compatibility, not the current dialect’s reproducibility guarantees.
- **Partial hermeticity:** Lock dependencies contain package names, not resolved package versions; source fetching does not prefetch Go/Cargo/.NET dependencies or `EfiStub` downloads. `--no-fetch` alone does not establish an offline build.
- **Destructive storage default:** `Disk(device=None, format="on_fail")` combines automatic disk selection with formatting after failure. Require deliberate selection/format policy for production recipes.
- **Tree digest scope:** [lifecycle.py:378](/home/franco/tundra/src/tundravm/declarative/lifecycle.py:378) normalizes modes to executable/non-executable; permission-only changes are not fully represented by `Tree.digest`.
- **Architecture mismatch:** `Recipe.arch` accepts `aarch64`, but `EfiStub` downloads `_amd64.deb` and the default QEMU adapter selects x86-64. Reject unsupported combinations during lint.

**6. Three delight features**

**Explain an emitted object.** Add `tundravm inspect image.py --variant azure --why /usr/lib/systemd/system/app.service`. Show its declaring fragment, overlay changes, generated dependencies, and destination file. Expose the same result as `explain(recipe, variant="azure", subject=...)`.

**One project configuration for every command.** Let `init` write `[tool.tundravm] recipe="image.py", tree="mkosi", output="build", lockfile="tundravm.lock"`. Then `tundravm status`, `ci`, and repair suggestions use identical paths and backend settings. Explicit command flags remain overrides.

**A safe measurement-to-verifier handoff.** Add `Tdxs.from_measurements(measurements, validator="tdx")` and `tundravm measure build --export-policy peer.json`. Normalize register names, retain tool/artifact provenance, and reject placeholders or missing required registers. The resulting file should be directly consumable by the verifier example without manual tuple transcription.