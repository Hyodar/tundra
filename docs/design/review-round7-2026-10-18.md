Static, read-only review of `master` at `0db3f45de51bf96d8039f01ecdfca8e6eb8c29ad`. I read implementation, templates, examples, tests, previous reviews and the flashbots comparison; I ran no builds or tests and modified no files. Concurrent documentation prose changes are excluded.

**1. Verdict — five sentences**

The SDK is substantially closer to the maintainer’s goal: typed units, TeeBase, kernel composition and shared UKIs give users meaningful application-level choices. It still falls short of an accessible path to a working, secure, reachable image because the generated application is missing and the prover’s storage, SSH and attestation prerequisites do not compose correctly. Strict defaults are valuable, but permissions, packaged-unit dependencies and debloat still require systemd knowledge to reconcile. The security claims need tightening because incomplete measurement policies can produce `trusted`, and some provenance claims describe intended inputs rather than verified inputs. I would prioritize a small, executable reference deployment and complete trust-policy enforcement before adding more configuration surface.

**2. Top 12 issues, ranked**

Ranking combines newcomer success **(A)**, security/measurement correctness **(B)** and coherent opinionated defaults **(C)**.

1. **Azure policies can produce `trusted` without checking the image. — B**

   **Where:** [measure/schemes.py:75](/home/franco/tundra/src/tundravm/measure/schemes.py:75), [attestation.py:413](/home/franco/tundra/src/tundravm/attestation.py:413), [attestation.py:273](/home/franco/tundra/src/tundravm/attestation.py:273).

   Azure derivation leaves `registers` empty and puts image measurements in `vtpm_pcrs`. Attestation reads only `registers`; missing measurements become `unchecked`, and the verdict accepts everything that is not a mismatch. Consequently, a nonce-bound quote accepted by the validator can yield `trusted` without comparing any image measurement. The scheme and vTPM evidence are not enforced by this path.

   **Fix:** Immediately reject Azure policies in this attestation path. Implement authenticated vTPM evidence, its binding to the platform/TD, and PCR comparisons before enabling Azure trust. Require scheme-specific measurement coverage, not merely absence of mismatches. **Effort: M for rejection; L for complete support.**

2. **Portable/QEMU policies leave firmware trust unresolved but still allow an unconditional trust verdict. — B**

   **Where:** [measure/schemes.py:35](/home/franco/tundra/src/tundravm/measure/schemes.py:35), [attestation.py:431](/home/franco/tundra/src/tundravm/attestation.py:431), [attestation.py:494](/home/franco/tundra/src/tundravm/attestation.py:494).

   Portable explicitly leaves MRTD/RTMR0 verification to “the platform’s own verifier”; QEMU similarly omits them. However, the validator interface returns a generic `valid` flag, with no required assertion that an approved measuring firmware was verified. Unless the operator independently configures that verification, RTMR1/2 matching plus quote acceptance is promoted to a stronger claim than this policy establishes.

   **Fix:** Make platform provenance an explicit required part of the policy and validator result. Return a partial-verification result until approved firmware/platform evidence and application measurements both pass. **Effort: M–L.**

3. **The prover defaults permit formatting the wrong disk. — A/B/C**

   **Where:** [templates.py:249](/home/franco/tundra/src/tundravm/templates.py:249), [declarative/model.py:1573](/home/franco/tundra/src/tundravm/declarative/model.py:1573), [_modules/disk_encryption.py:36](/home/franco/tundra/src/tundravm/_modules/disk_encryption.py:36), [policy.py:24](/home/franco/tundra/src/tundravm/policy.py:24).

   Both the public `Disk` defaults and generated prover combine automatic device selection with `format="on_fail"`. Selection includes the largest whole `/dev/sd*` disk—including the boot disk—and excludes virtio disks. The existing warning accurately describes this, but warning-by-default is incompatible with secure-by-default storage.

   **Fix:** Require an explicit stable device identity whenever formatting is possible; default to `format="never"`. Make initialization a separate explicit operation, and reject boot-device selection. Generate a matching deployment data-disk declaration instead of leaving discovery to a boot script. **Effort: M.**

4. **The default starter does not contain its application, and its boot test conceals that gap. — A**

   **Where:** [templates.py:163](/home/franco/tundra/src/tundravm/templates.py:163), [templates.py:181](/home/franco/tundra/src/tundravm/templates.py:181), [tests/integration/test_templates_boot.py:35](/home/franco/tundra/tests/integration/test_templates_boot.py:35), [test_templates_boot.py:104](/home/franco/tundra/tests/integration/test_templates_boot.py:104).

   The service/cloud/prover templates execute `/usr/bin/app` without installing it. The integration test adds a replacement that sleeps forever, so it proves neither that the generated starter works unchanged nor that its declared HTTP port serves anything.

   **Fix:** Ship a tiny real HTTP application through the normal build/package API. Bake the unchanged generated recipe, request its endpoint from the host, and verify its response. Alternatively, require a concrete application input during initialization and reject incomplete scaffolds before baking. **Effort: S–M.**

5. **The default Tdxs sandbox does not establish permission to obtain quotes. — A/C**

   **Where:** [_modules/tdxs.py:130](/home/franco/tundra/src/tundravm/_modules/tdxs.py:130), [_modules/tdxs.py:161](/home/franco/tundra/src/tundravm/_modules/tdxs.py:161), [declarative/utils.py:401](/home/franco/tundra/src/tundravm/declarative/utils.py:401).

   The implementation itself documents that configfs report inputs require root or `CAP_DAC_OVERRIDE`, but runs Tdxs as an unprivileged user without that capability. `ReadWritePaths` removes a mount restriction; it does not grant filesystem permission. The device fallback receives `DeviceAllow`, but the component installs no ownership rule granting the `tdx` group access; the old prover example explicitly supplies such [udev rules](/home/franco/tundra/examples/surge-tdx-prover/files/etc/udev/rules.d/65-tdx-guest.rules:1).

   **Fix:** Choose and implement a reviewed quote-access model: a narrowly privileged helper or appropriate service privilege, device ownership rules, and explicit provider readiness. Test an actual quote request on TDX hardware. **Effort: M.**

6. **Debloat removes the network ordering barrier, and firewall failure does not prevent networking. — A/B/C**

   **Where:** [models.py:67](/home/franco/tundra/src/tundravm/models.py:67), [models.py:640](/home/franco/tundra/src/tundravm/models.py:640), [emit_mkosi.py:1537](/home/franco/tundra/src/tundravm/compiler/emit_mkosi.py:1537), [declarative/_network.py:159](/home/franco/tundra/src/tundravm/declarative/_network.py:159).

   The keep list omits `network-pre.target` and `network.target`; preservation only considers explicitly enabled units, not their dependency closure. This removes the barrier between [Debian nftables’ `Before=network-pre.target`](https://sources.debian.org/src/nftables/1.1.3-1/debian/nftables.service/) and [networkd’s `After=network-pre.target`](https://raw.githubusercontent.com/systemd/systemd/v257/units/systemd-networkd.service.in). Firewall expansion also supplies no failure dependency that prevents networking when nftables fails. GCP’s DNS setup likewise relies on the removed barrier at [platforms/gcp.py:114](/home/franco/tundra/src/tundravm/platforms/gcp.py:114).

   **Fix:** Preserve required ordering/dependency targets, and explicitly require successful firewall installation before bringing up external networking. Compute debloat from the enabled boot graph rather than a flat whitelist. **Effort: M.**

7. **The prover advertises SSH access without composing an SSH service and firewall opening. — A/C**

   **Where:** [templates.py:272](/home/franco/tundra/src/tundravm/templates.py:272), [declarative/_network.py:89](/home/franco/tundra/src/tundravm/declarative/_network.py:89), [declarative/utils.py:856](/home/franco/tundra/src/tundravm/declarative/utils.py:856), [emit_mkosi.py:1441](/home/franco/tundra/src/tundravm/compiler/emit_mkosi.py:1441).

   The template installs `openssh-server` and promises root login with the delivered key. TeeBase’s firewall does not open SSH by default, and port collection cannot infer listeners from installed packages. The recipe also never explicitly enables SSH under its custom minimal boot target. Meanwhile, QEMU unconditionally creates an SSH forward at [deploy/qemu.py:165](/home/franco/tundra/src/tundravm/deploy/qemu.py:165).

   **Fix:** Provide an `SshAccess` component that owns package installation, unit activation, host-key initialization, authorized-key readiness, firewall allowance and deployment forwarding. Use it in the prover template. **Effort: M.**

8. **The host-key cleanup glob is quoted into a literal filename. — B/C**

   **Where:** [models.py:34](/home/franco/tundra/src/tundravm/models.py:34), [emit_mkosi.py:1485](/home/franco/tundra/src/tundravm/compiler/emit_mkosi.py:1485).

   The default removal list includes `/etc/ssh/ssh_host_*_key*`, but emission produces `rm -rf "$BUILDROOT/etc/ssh/ssh_host_*_key*"`. Shell glob expansion cannot occur inside those quotes. Any matching host keys present at this stage survive this cleanup, silently; whether another build stage removes them does not make this scrub effective.

   **Fix:** Implement constrained glob removal with the build-root prefix safely quoted, and verify the finished image contains no baked host private keys. Couple this with per-instance host-key generation. **Effort: S.**

9. **An unrelated Nix shell bypasses the pinned toolchain while provenance records the lock’s versions. — B**

   **Where:** [backends/nix.py:79](/home/franco/tundra/src/tundravm/backends/nix.py:79), [backends/nix.py:119](/home/franco/tundra/src/tundravm/backends/nix.py:119), [toolchain.py:386](/home/franco/tundra/src/tundravm/toolchain.py:386).

   Any nonempty `IN_NIX_SHELL` or `NIX_STORE` causes direct execution of the ambient `mkosi`, bypassing the generated pinned environment and its version guard. `running_toolchain()` nevertheless copies the locked Nixpkgs/mkosi/systemd identities as the toolchain that ran. A developer’s ordinary shell can therefore invalidate both reproducibility and the recorded execution identity.

   **Fix:** Always enter the exact pinned environment, or verify the executable’s derivation identity before reusing an existing one. Record and compare actual executable/store identities. **Effort: S–M.**

10. **`sbom --binaries` adds unverified materials to an artifact-bound document. — B**

    **Where:** [declarative/bom.py:328](/home/franco/tundra/src/tundravm/declarative/bom.py:328), [declarative/bom.py:341](/home/franco/tundra/src/tundravm/declarative/bom.py:341), [declarative/bom.py:358](/home/franco/tundra/src/tundravm/declarative/bom.py:358).

    Artifact, lock and package-manifest bindings are checked, but additional binaries are accepted from an arbitrary directory, including a basename fallback. Their hashes are never compared with the baked image. Dependencies from a stale or unrelated binary are appended while the document retains its existing bound status.

    **Fix:** Record installed-binary digests in verified bake materials, or extract binaries from the verified artifact. Treat external unmatched binary inventories as unbound and identify their provenance separately. **Effort: M.**

11. **Typed hardening overrides do not have the advertised meaning in drop-ins. — A/C**

    **Where:** [declarative/model.py:1278](/home/franco/tundra/src/tundravm/declarative/model.py:1278), [declarative/systemd.py:135](/home/franco/tundra/src/tundravm/declarative/systemd.py:135), [_units/render.py:298](/home/franco/tundra/src/tundravm/_units/render.py:298).

    Hardening rendering assumes a fresh unit: false settings are generally omitted. In a drop-in, `jit=True` consequently cannot cancel inherited `MemoryDenyWriteExecute=yes`; allowing devices does not cancel inherited `PrivateDevices=yes`; `Hardening.none()` cannot remove an existing sandbox. Users must understand systemd’s merge/reset semantics to discover why their typed override did nothing.

    **Fix:** Give drop-ins explicit patch semantics, including boolean negations and list resets, or render complete replacement presets appropriate to inherited units. Test the effective merged configuration, not just emitted fragments. **Effort: M.**

12. **Kernel v2 still leaves the build environment and module retention to the user. — A/C**

    **Where:** [emit_mkosi.py:807](/home/franco/tundra/src/tundravm/compiler/emit_mkosi.py:807), [emit_mkosi.py:1391](/home/franco/tundra/src/tundravm/compiler/emit_mkosi.py:1391), [backends/base.py:163](/home/franco/tundra/src/tundravm/backends/base.py:163), [check.py:561](/home/franco/tundra/src/tundravm/check.py:561).

    The composed kernel invokes patch/configuration/compiler tooling directly in the generated build script, but does not establish a kernel-specific build environment. The Nix tools environment explicitly replaces PATH and does not supply the required kernel build toolset. Separately, `Kernel(modules=True)` conflicts with default debloat and requires the user to discover `Debloat(keep_modules=True)`; the checker catches this, but the defaults still fight each other.

    **Fix:** Make `Kernel` own a pinned build environment and derive module retention automatically. Add one real DebianConfig-based kernel build to integration coverage; emitted-script assertions cannot establish that the build environment works. **Effort: M.**

**3. Defaults most likely to break a real boot or usable service**

These are static predictions, ranked by likelihood **when the stated configuration is used**, not claims that I observed a boot failure.

| Rank | Configuration | Expected failure |
|---|---|---|
| 1 | Unchanged service/cloud/prover starter | `app.service` cannot execute the absent `/usr/bin/app`; no application endpoint. [templates.py:181](/home/franco/tundra/src/tundravm/templates.py:181) |
| 2 | Prover deployed through ordinary QEMU defaults | The key requires TPM persistence, but deployment supplies no TPM; disk initialization expects `/dev/sd*`, while deployment supplies either a UKI or a virtio boot disk and no data disk. Initialization cannot complete, blocking dependent services. [model.py:1547](/home/franco/tundra/src/tundravm/declarative/model.py:1547), [qemu.py:159](/home/franco/tundra/src/tundravm/deploy/qemu.py:159) |
| 3 | Prover after storage prerequisites are satisfied | SSH remains unreachable through the default firewall, with no explicit minimal-target SSH activation. [templates.py:275](/home/franco/tundra/src/tundravm/templates.py:275), [_network.py:102](/home/franco/tundra/src/tundravm/declarative/_network.py:102) |
| 4 | Tdxs on a guest exposing configfs TSM | Quote issuance encounters filesystem permission failures under the default unprivileged sandbox. Device fallback also needs ownership permissions that this component does not install. [_modules/tdxs.py:130](/home/franco/tundra/src/tundravm/_modules/tdxs.py:130) |
| 5 | A packaged service whose sandbox is relaxed using `DropIn` | JIT or device access still fails because inherited restrictions remain active. [systemd.py:139](/home/franco/tundra/src/tundravm/declarative/systemd.py:139) |
| 6 | TeeBase networking under default systemd minimization | Network/firewall and GCP DNS ordering becomes unreliable after `network-pre.target` is masked; firewall failure can leave an otherwise booted guest exposed. [models.py:67](/home/franco/tundra/src/tundravm/models.py:67), [gcp.py:114](/home/franco/tundra/src/tundravm/platforms/gcp.py:114) |

For modules specifically, I found a **custom-kernel/default-debloat conflict**, but the existing checker rejects it before boot; I would not report that as a silent module-pruning failure. I also found no supported basis for claiming that the curated Debian module list universally prevents boot.

The new harness is useful but currently weaker than “working image”: it recognizes startup-failure phrases, retains historical “Started” observations, and `BootResult.ok` ignores QEMU’s return code. A service that starts and then exits can escape that model. See [_boot.py:55](/home/franco/tundra/src/tundravm/testing/_boot.py:55) and [_boot.py:127](/home/franco/tundra/src/tundravm/testing/_boot.py:127).

**4. Three next big steps**

1. **Compile component requirements into a complete boot and access plan.** Components should declare needed devices, writable storage, network readiness, listeners and privileges. The compiler should derive accounts, udev rules, sandbox exceptions, firewall rules, unit dependencies, module retention and deployment resources. Debloat should operate on that resolved plan.

2. **Make one unchanged reference recipe the product acceptance test.** Start with a real HTTP service, then add persistent encrypted storage and attestation. Verify external reachability, continued service health, reboot persistence, negative firewall probes and actual quote issuance. Run ordinary QEMU coverage frequently and a smaller TDX/cloud matrix before release.

3. **Make trust an explicitly complete, scheme-specific contract.** Separate quote authenticity, platform/firmware approval, image measurement, freshness and artifact provenance. Release should fail when any required part is missing, with a precise explanation of the missing evidence. Bind executed toolchains and embedded-binary inventories to the same verified release materials.