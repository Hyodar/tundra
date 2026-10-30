**I would approve the direction, but not this implementation plan.** The shorter API is worthwhile. Several proposed simplifications, however, hide unresolved trust and lifecycle contracts, and the mkosi replacement is not yet technically specified well enough to justify the deletions.

I reviewed everything except the placeholders in sections 4 and 5. I only inspected files and sources; I did not build images, run tests, or validate hardware behavior. The supplied mkosi checkout identifies itself as **`v27.1-110-g10f43ac`**, not v27.1; I also inspected relevant files at its `v27.1` tag.

No output-file exception or path was supplied, so I preserved your explicit prohibition on file writes and return the complete review here.

**1. Keep `Image(...)`, but make its slots describe resources and relationships.**

The keyword-slot approach is substantially better than requiring every beginner to learn components, resolution, variants, and backends. The problem is the proposed contents of those slots.

| Proposed slot | Problem and user consequence | Specific alternative |
|---|---|---|
| `release` | Debian codename and a releasable build both become “release.” Bookworm also multiplies kernel, stub, and credential compatibility promises. | Initially support one tested Debian baseline. Use advanced `base=Debian(...)` for exceptions. |
| `packages` | Sensible, but runtime and build dependencies must remain distinct. A compiler should not enter the guest merely because a source build needs it. | Keep `packages`; let `GoBinary`, `CargoBinary`, and other build declarations own build dependencies. |
| `files` | `str` means either file contents or a source path. Ownership is missing, and persistent files need different handling from image files. | `File.text(path, contents)`, `File.copy(src, dst)`, `Tree(src, dst)`, with explicit ownership when necessary. |
| `services` | Good primary slot. Mixing `Service`, timers, sockets, and arbitrary parts makes its contract unclear. | Services here; scheduled jobs and socket activation use typed declarations or typed service options. |
| `users` | A reasonable advanced slot, not introductory material. Shared groups matter more than additional users for many stacks. | Generate dedicated users; support explicit `User` and `Group` declarations for shared resources. |
| `state` | One disk is an artificial limit, and a mount path is not a disk identity or key policy. | `volumes=[Volume(...)]`, with typed service references to directories on them. |
| `secrets` | Names alone do not identify a delivery mechanism, authorization policy, or rotation behavior. | Named secret requirements plus an explicit delivery source; service bindings reference those objects. |
| `kernel` | `hardened=True` makes an undefined security promise. | A tested, versioned default kernel policy; advanced source/config/patch configuration remains available. |
| `network` | `Network` and `Firewall` are separate concerns with different cardinality. | One network configuration object with explicit ingress/egress rules; services expose typed endpoints. |
| `attestation` | Conflates producing evidence, validating peers, and authorizing secret release. `False` contradicts the product’s main guarantee. | Default guest evidence service; separate verifier policies and secret-delivery configuration. |
| `outputs` | Defaulting to all clouds adds packaging time and prerequisites to every edit. It also confuses packaging with security identity. | Default local raw disk; `build --target azure,gcp` requests additional artifacts without changing the payload. |
| `dev` | Useful, but debug access and simulated confidential execution are different things. | `DevAccess(...)` controls debug access; runtime mode separately controls TDX versus simulation. |
| `profiles` | Useful advanced API, but `set={...}` is an untyped configuration language. | Typed edits against stable references, with explicit inheritance/composition rules. |
| `parts` | Useful composition boundary. “Anywhere a declaration can appear” defeats typed slots. | Restrict reusable bundles to `components=[...]`; resolve their declarations before validation. |
| `mkosi` | Necessary escape hatch, but arbitrary settings/scripts can invalidate advertised guarantees. | Preserve it, record its resolved effects, and reject contradictions with mandatory image invariants. |

Missing first-class concerns are **ingress, observable health, volume bindings, and root filesystem layout**. These do not all need to appear in the starter. They need defined contracts before the convenience API freezes.

A user should be able to write:

```python
data = Volume("data", mount="/persistent", unlock=broker.key("database"))
Service(
    "api", ["/usr/bin/api"],
    state=data.directory("api"),
    credentials={"jwt": jwt},
    readiness=HttpProbe("http://127.0.0.1:8080/ready"),
)
```

The important simplification is automatic dependency wiring, not merely fewer constructor fields.

**2. Fourteen fields is the wrong success criterion for `Service`.**

The proposed fourteen preserve `after`, `timer`, and `socket`, but drop readiness, working directory, resource limits, and explicit dependency requirements. That is a poor trade for Raiko/Nethermind/Taiko.

`After=nethermind.service` does not start Nethermind, require its success, or wait until its RPC endpoint is usable. Removing these distinctions sends the first realistic stack into raw unit overrides.

Keep a small common constructor, with grouped advanced values:

```python
Service(
    "taiko", [taiko_binary, "--execution-endpoint", nethermind.rpc],
    requires=[nethermind.ready],
    readiness=HttpProbe("/health"),
    resources=Resources(memory_max="8G", tasks_max=4096),
    shutdown=Shutdown(timeout="120s"),
)
```

`requires=[other.ready]` must lower to an actual readiness mechanism, not just `After=`. For `Type=notify`, document what `READY=1` means; for HTTP readiness, implement a bounded probe.

Keep typed `working_directory`, credentials, writable-directory bindings, and group access. Put unusual systemd directives in `unit=DropIn(...)`.

Also fix the proposal’s internal inconsistency: its `Prometheus` example supplies `enabled=True`, which is absent from the fourteen-field list, and omits the required executable. Moreover, declaring port 9090 does not configure the packaged node exporter to listen there.

Hardening must be tested against runtime classes. A .NET service, a Rust prover, and a process exporter do not tolerate identical syscall, executable-memory, and `/proc` restrictions. Use tested presets with explicit deviations, rather than making users reverse-engineer why the “simple” example crashes.

**3. Use argv as the normal executable API.**

`exec="busybox httpd ..."` plus `args=[...]` introduces two argument representations and ambiguity about quoting and shell execution.

Systemd command lines are not ordinary shell command lines. A user will eventually try redirection, pipes, `$VAR`, or a literal `%`, and receive behavior different from their terminal.

Prefer:

```python
Service("app", ["/usr/bin/busybox", "httpd", "-f", "-p", "8080", "-h", "/srv/www"])
Service("app", [binary, "--jwt-file", jwt.path])
```

A built binary can be the first argv element; the compiler automatically includes its build/install declaration. Remove the separate `args` field.

The renderer must preserve literal arguments through systemd quoting, dollar expansion, and percent specifiers. `jwt.path` should be a typed credential-path reference, not a string containing shell syntax. Systemd supports credential-directory references, but that does not make arbitrary string interpolation safe. [Systemd credential documentation](https://github.com/systemd/systemd-stable/blob/v255-stable/docs/CREDENTIALS.md)

Allow explicit shell execution as the visible exception:

```python
Service("maintenance", ["/bin/sh", "-eu", "-c", script])
```

**4. “Every image is a TDX image” is useful positioning, but an incorrect execution invariant.**

TDX requires a suitable host and launch configuration. Image bytes alone cannot guarantee it.

The proposal simultaneously says “no plain VM mode” and offers `run [--tdx]`. That contradiction will reach every laptop user immediately. Production services waiting for attested secrets also cannot run normally without either real attestation or an explicit development substitute.

Define three independent properties:

- The artifact is **TDX-capable** and built with the supported boot/runtime baseline.
- A production deployment **requires TDX** and fails closed if unavailable.
- A development execution may use **explicit simulation**, with separate credentials and an unmistakable untrusted verdict.

For example:

```text
tundravm run --dev --mode simulated
tundravm run --mode tdx --platform local-tdx.toml
```

Never silently fall back from TDX to ordinary QEMU. Production key brokers must reject simulated evidence and debug-enabled images.

`attestation=False` should either be unavailable for production images or produce an explicitly nonproduction artifact. An absent evidence service can be legitimate in specialized deployments, but it cannot coexist with an unconditional claim that every image can prove what it runs.

**5. `Disk("/persistent", encrypted=True)` conceals the most consequential decisions.**

The plan promises first-boot formatting and vTPM sealing across Azure, GCP, and local swtpm. Those are not interchangeable security or persistence mechanisms.

An ordinary host-operated swtpm does not protect secrets from that host. Its state also needs persistence across launches; mkosi’s `start_swtpm()` creates a temporary state directory. Replacing the current launcher with that default would not provide the promised later-boot key recovery.

GCP’s documentation explicitly distinguishes its software-attested vTPM from TDX hardware attestation. Azure has a different design connecting vTPM evidence to its protected paravisor. Treating all three as “TPM-backed” hides the trust boundary. [GCP measurement documentation](https://docs.cloud.google.com/confidential-computing/confidential-vm/docs/measurement-register-contents), [Azure attestation design](https://learn.microsoft.com/en-us/azure/confidential-computing/guest-attestation-confidential-virtual-machines-design)

For a user, the failure appears during the first upgrade: either the disk no longer unlocks, or the key remains available under a weaker policy than expected.

Use an explicit key source:

```python
Volume("data", mount="/persistent", unlock=broker.key("prover-data"))
```

An advanced `TpmSeal(...)` option should state the supported platform, PCR policy, recovery method, and update authorization mechanism.

Specify these behaviors before implementing the shorthand:

- Stable logical disk identity mapped to a QEMU serial or cloud attachment identity.
- Creation, attachment, and initialization as separate operations.
- Formatting only a positively identified, authorized empty volume.
- Unlock failure never interpreted as permission to reformat.
- Key recovery after instance replacement, disk restore, or firmware change.
- Ownership and permissions for multiple services sharing directories.
- Backup, resize, and recovery behavior.
- Whether rollback protection exists; ordinary encrypted storage does not provide it.

Disk capacity belongs primarily to deployment configuration. The image declares the expected volume and mount contract:

```text
tundravm deploy gcp --volume data=create:100GiB
tundravm deploy gcp --volume data=existing:prover-data
```

Default to one convenient volume, not an API that permits only one.

**6. Secret delivery requires a protocol, not just `Secret("jwt")`.**

The current implementation already distinguishes delivery mechanisms and targets. Deleting that detail before defining the replacement loses functionality rather than simplifying it.

The plan must distinguish:

1. An image’s secret requirements.
2. An operator or broker’s authorization to release them.
3. The guest’s authenticated delivery endpoint.
4. Per-service credential presentation.
5. Rotation and revocation.

A quote containing a fresh nonce does not by itself authenticate the TLS connection receiving the secret. An attacker can relay a quote from a genuine guest while terminating a different connection.

Specify a protocol that binds evidence to a key generated and held inside the guest, then delivers secrets over the channel authenticated by that key. Bind the protocol purpose and freshness challenge as well. Checking measurements against a file supplied by the same untrusted endpoint is not authorization.

The verifier must also check the appropriate evidence chain, collateral/TCB policy, debug attributes, and complete required measurement coverage. Preserve the existing distinction between `trusted`, `partial`, `measurements-match`, and `simulated` in [attestation.py](/home/franco/tundra/src/tundravm/attestation.py:1).

For applications:

```python
jwt = Secret("jwt", source=broker.secret("prover-jwt"), rotation="restart")
Service("api", [binary, "--jwt-file", jwt.path], credentials=[jwt])
```

Define startup timeouts, required versus optional values, file ownership, atomic delivery, restart/reload behavior, and whether stale credentials remain usable during broker outages.

The bootstrap chain must be:

```text
network/time → attested endpoint → key/secret delivery → volume unlock → application
```

The attested endpoint cannot depend on the encrypted volume whose key it must receive. The existing prover template wires `Tdxs(..., needs=[secrets.ready])`; carrying that dependency into the new delivery flow would create a bootstrap deadlock.

**7. `attest --secrets` combines a diagnostic operation with a security-sensitive mutation.**

An operator reasonably expects `attest HOST` to inspect a host. With `--secrets`, it also authorizes and transfers credentials. That makes automation, retries, audit records, and permissions harder to understand.

Prefer:

```text
tundravm attest prover-prod --policy policies/prover-gcp.json
tundravm secrets deliver prover-prod --policy policies/prover-gcp.json jwt=@jwt.txt
```

`secrets deliver` must perform a fresh channel-bound verification itself. It must not treat an earlier `attest` exit status or saved report as authorization for a later connection.

For unattended deployments, support broker pull rather than requiring someone to run the CLI after every reboot. The manual command is useful for bootstrap and debugging; it is not the whole production delivery model.

`NAME=@FILE` is reasonable input syntax. Secret values must stay out of argv, logs, generated trees, and build manifests.

**8. Add attested ingress and operational visibility before declaring the prover “complete.”**

`ports=[8080]` opens a firewall. It does not establish an authenticated confidential channel to the service.

The proposal’s default HTTP example is fine as a public demo, but not as the model for confidential request handling. A TDX guest serving plaintext HTTP over an untrusted network does not protect those requests in transit.

Provide a concrete ingress declaration:

```python
ingress=[AttestedTLS(443, upstream=raiko.http)]
```

Its contract must identify the client verifier, certificate/key binding, backend bind address, client authorization, renewal, and whether a load balancer passes TLS through or terminates it. Normal HTTPS and attested TLS should have different names.

Likewise, “no console, no SSH” plus a volatile journal leaves an operator blind when the service fails before becoming ready. Add an authenticated export path:

```python
observability=Observability(
    logs=JournalExport("https://logs.example.net", credential=logs_token),
    metrics=PrometheusRemoteWrite("https://metrics.example.net", credential=metrics_token),
)
```

This can be a reusable component. It still needs bounded buffers, retry behavior, and a policy for whether export failures block application startup. Avoid publicly opening a metrics listener merely because a service declares metrics.

**9. Updates and state compatibility cannot wait until after the API rewrite.**

A usable TDX service must survive changing the image.

The plan needs one supported rollout mechanism. Start with replacement VMs and persistent-volume reattachment; do not simultaneously promise in-place package upgrades, A/B boot slots, and cloud rolling upgrades.

A concrete sequence is:

```text
build new artifact
→ approve its policy at the broker
→ launch candidate
→ attest and deliver credentials
→ establish application readiness
→ drain old instance
→ transfer exclusive volume ownership where needed
→ retire old policy when rollback window closes
```

For a single-writer database, attaching the same writable disk to old and new instances is not a rolling upgrade strategy. Specify fencing and expected downtime.

Keep schema migrations explicit. “Rollback the image” is not equivalent to “rollback the database.”

A useful interface is:

```text
tundravm deploy upgrade prover-prod --build BUILD_ID
tundravm deploy rollback prover-prod --build PREVIOUS_ID
```

These operations should reject incompatible volume/schema transitions rather than presenting rollback as universally safe.

**10. `Part` needs exports, stable identities, and dependency contracts.**

`parts()` plus `check()` is enough for a bundle of unrelated files. It is not enough for a library implementing Nethermind + Taiko + Raiko.

Such a library needs to export:

- RPC endpoints and their readiness.
- JWT credential references.
- Shared directories with access modes.
- Services that callers can configure or depend on.
- Resource identities that survive internal refactoring.

For example:

```python
execution = Nethermind(jwt=jwt, state=data.directory("execution"))
client = TaikoClient(execution=execution.engine_api, jwt=jwt)
prover = Raiko(l2_rpc=client.rpc, state=data.directory("proofs"))
```

The caller should not need `"nethermind.service"`, port 8551, or the library’s internal file paths.

Preserve the useful export/reference concepts in [_component.py](/home/franco/tundra/src/tundravm/declarative/_component.py). Add clear rules for duplicate declarations, dependency cycles, conflicting edits, validation order, and source provenance in diagnostics.

Do not allow a component that emits packages and files in `services=[...]`. That sacrifices useful type checking for very little brevity.

I would retain **`Component` / `@component`**. “Part” saves characters but communicates less.

**11. Renaming `Variant` is fine; deleting its semantics is not.**

The proposal says refs and `changes=` survive, yet the public table exposes `Profile(add, remove, set)` and drops `inherits`. Resolve that discrepancy.

Real stacks use a common production base plus staging, debug, benchmarking, rescue, and hardware-specific derivatives. Without inheritance or equivalent explicit composition, users copy configuration or put branching logic in Python.

A reasonable advanced interface is:

```python
profiles={
    "staging": Profile(
        extends="production",
        changes=[execution.ref.edit(resources=Resources(memory_max="4G"))],
    ),
    "debug": Profile(
        extends="staging",
        add=[DevAccess(...)],
        production=False,
    ),
}
```

Alternatively, compose named immutable overlays explicitly. Do not replace typed edits with string-keyed `set` dictionaries.

Mkosi profiles do not automatically reproduce Tundra’s identity-based removal and inheritance. Tundra still needs to resolve those semantics, then emit effective settings, reset accumulated lists where appropriate, and select the correct file overlays.

A profile removing a service must also remove its unit, enablement symlink, firewall rule, and now-unused dependency where appropriate. Native mkosi inclusion alone does not perform that semantic cleanup.

**12. The CLI should optimize the operator’s loop, not hit nine verbs.**

The table already exceeds nine once `convert` is included.

Several folded commands are genuinely internal stages: automatically performing lock validation, compilation, measurement, and SBOM creation during `build` is correct.

Others represent independent user needs:

- **Check:** validate a project without building a multi-gigabyte image.
- **Diff:** explain what changed before approving new measurements.
- **Inspect measurements:** examine an existing artifact without rebuilding.
- **Fetch:** prepare an offline build or diagnose source acquisition separately.
- **Status/logs/stop:** operate the instance created by `run`.
- **Cleanup:** reclaim caches without deleting persistent disks or release artifacts.
- **Export tree:** leave Tundra without first completing a successful build.

These can be subcommands or focused flags. They cannot disappear into `show --web`.

Make `verify` inspect existing artifacts by default. Rebuilding should be explicit:

```text
tundravm verify BUILD --require-signature
tundravm verify BUILD --rebuild
```

Rebuilding an externally supplied project executes code. The default integrity-check operation should not unexpectedly import its Python or run hooks.

Also decide lock behavior precisely. The prose mentions `build --update`, but the CLI signature omits it. Prefer explicit `lock --update`, followed by a frozen build; a missing lock can have a documented first-build convenience, but stale locks should never update implicitly.

**13. `build/<image>/` needs immutable build identities and separate instance state.**

A single directory cannot safely represent production, debug, multiple profiles, concurrent builds, and the prior release needed for rollback.

Use:

```text
build/prover/
  production/<build-id>/
    image.efi
    image.raw
    image.vhd
    image.tar.gz
    measurements.json
    manifest.json
    manifest.sig
    sbom.spdx.json
    inputs/
    tree/
  development/<build-id>/
  latest-production -> production/<build-id>

.tundravm/instances/<instance-id>/
  instance.json
  console.log
  runtime-state/
```

Only requested artifact formats need exist. Publish a build directory atomically after success. “Last build” must not silently select the latest debug build for a production deployment.

The manifest should bind the artifact bytes, embedded UKI, root hash where applicable, lockfile, generated tree, source inputs, package manifest, SBOM, and toolchain identity. Include file modes and symlink targets in tree identity.

Keep build observations such as timestamps, runner identity, and reproduction attempts separate from the deterministic build-input identity.

A “release is a directory you keep” is insufficient. An operator needs a signed immutable identity that a deployment record and policy can reference.

**14. The mkosi tree’s dependency direction is wrong as drawn.**

Mkosi builds subimages before the main image. Therefore a main-image UKI cannot be the dependency of wrapper subimages.

Make the payload a subimage and the main configuration a coordinator:

```text
tree/
  mkosi.conf
  shared-payload/
    mkosi.conf
    mkosi.extra/
    mkosi.conf.d/
    mkosi.profiles/dev/
  mkosi.images/
    payload/
      mkosi.conf
    qemu/
      mkosi.conf
      mkosi.repart/
    azure/
      mkosi.conf
      mkosi.repart/
      mkosi.postoutput
    gcp/
      mkosi.conf
      mkosi.repart/
      mkosi.postoutput
```

The main configuration selects dependencies:

```ini
[Config]
MinimumVersion=27.1
Dependencies=qemu,azure,gcp

[Distribution]
Distribution=debian
Release=trixie
Architecture=x86-64
Snapshot=<locked-snapshot>

[Output]
Format=none
OutputDirectory=out

[Content]
SourceDateEpoch=<locked-epoch>

[Build]
ToolsTree=yes
ToolsTreeDistribution=debian
ToolsTreeRelease=<tested-tools-release>
ToolsTreeSnapshot=<locked-tools-snapshot>
```

The payload includes its shared configuration and produces `payload.efi`. Each disk subimage declares `Dependencies=payload`.

Crucially, subimages do **not** inherit arbitrary main-image content settings. The parser starts a fresh configuration and passes universal and explicitly inheritable settings. Main-image packages, extra trees, and hooks must not be assumed to materialize in the payload. Inspect `summary`/`cat-config` for every node. [Mkosi v27.1 configuration parser](https://github.com/systemd/mkosi/blob/v27.1/mkosi/config.py)

**15. Native profiles and `[Match]` work, but not as automatic cloud dispatch.**

`[Match] Profiles=azure` is valid. It tests a selected configuration profile. It does not mean “this subimage is named azure,” “produce an Azure artifact,” or “the guest is running on Azure.”

If one UKI serves all clouds, include all required cloud glue in that payload and keep it inert off-platform. Selecting an output wrapper must not accidentally build a different payload or omit its cloud provisioning service.

Likewise, profile names can propagate to subimages, but profile files are resolved through the configuration directories being parsed. A top-level `dev/mkosi.extra` is not automatically copied into every subimage.

Put payload profile content in an explicitly included shared payload directory. Keep packaging configuration separate.

Mkosi’s parsing order is defaults/config, drop-ins, selected profiles, then subimages. Do not assume profile selection added late will retroactively apply earlier unmatched drop-ins. Select profiles through the command line or sufficiently early configuration. [Mkosi v27.1 manual](https://github.com/systemd/mkosi/blob/v27.1/mkosi/resources/man/mkosi.1.md)

The `.d` script directories are supported in v27.1. That portion of the proposal is sound.

**16. Native repart can build the wrappers; it cannot produce all the proposed container formats.**

For the small, all-in-UKI layout, a disk wrapper can copy the completed UKI into its staging root:

```ini
[Config]
Dependencies=payload

[Output]
Format=disk
Output=qemu
Seed=<fixed-uuid>
RepartDirectories=mkosi.repart

[Content]
Bootable=no
Bootloader=none
ExtraTrees=%O/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI
```

Its repart definition:

```ini
[Partition]
Type=esp
Format=vfat
CopyFiles=/efi:/
SizeMinBytes=64M
```

Here `Bootable=no` disables mkosi’s automatic boot-payload generation; the explicitly copied EFI application makes the resulting disk bootable. The production definition still needs tested sizing, FAT determinism, and cloud alignment.

`%O` belongs to mkosi’s setting expansion. Repart `CopyFiles=` sources are relative to the image root because mkosi invokes repart with `--root=`. Do not put a mkosi `%O` dependency path directly into repart configuration and expect mkosi to expand it.

Format support is decisive:

| Desired artifact | Native v27.1 capability | Required remaining work |
|---|---|---|
| QEMU raw GPT disk | Yes, `Format=disk` with repart | Prefer this default; no qcow2 conversion required. |
| QEMU qcow2 | No qcow2 output format | Explicit `qemu-img` conversion, if actually needed. |
| Azure fixed VHD | No VHD output format | Aligned raw disk → `qemu-img convert -O vpc -o subformat=fixed,force_size`, then deterministic footer handling. |
| GCP `tar.gz` | Not a dedicated cloud image format | Package the raw disk as `disk.raw` using the required archive format and deterministic compression. |

Azure requires fixed VHD sizing/alignment. [Azure Linux image preparation](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/create-upload-generic)

`Format=tar` archives an image filesystem; it does not automatically wrap the result of `Format=disk`. A dependent tar image can copy a raw file using an explicit target path, but mkosi’s tar implementation selects PAX through `--xattrs`. GCP’s documented manual-import format requires `disk.raw` in an oldgnu gzip archive. Therefore that configuration is not a demonstrated replacement for the existing packager. [Mkosi archive implementation](https://github.com/systemd/mkosi/blob/v27.1/mkosi/archive.py), [GCP import requirements](https://docs.cloud.google.com/compute/docs/import/import-existing-image)

**Answer to the specific question:** not entirely through native mkosi 27.1 settings. Users need not hand-write scripts, but Tundra must retain a small tested conversion/packaging implementation or generate hooks. Moving that work from postoutput to another hook does not eliminate it.

Preserve the existing VHD footer normalization and GCP deterministic sparse-archive handling in [emit_mkosi.py](/home/franco/tundra/src/tundravm/compiler/emit_mkosi.py:305). They are stronger than the corresponding reference scripts.

**17. `Snapshot=`, `ToolsTree=`, and module pruning are good replacements—with boundaries.**

Use native `Snapshot=`. It replaces custom repository rewriting, not the lockfile: retain exact source commits, package sources, repository keys, EFI stub inputs, and build-tool pins.

Pin the tools-tree distribution, release, and snapshot explicitly. Otherwise it can vary with the host. Pinning `mkosi==27.1` does not pin `ukify`, repart, filesystem creators, compilers, archive tools, QEMU, or firmware.

For modules, v27.1 provides:

- `KernelModules=` for image modules.
- `KernelInitrdModules=` for initrd module selection.
- `KernelModulesInitrd=` as a boolean controlling the separate modules initrd.
- `FirmwareFiles=` for additional firmware.

Do not confuse the boolean with the selection list. The newer patterns are globs with ordered positive/negative matching, not a drop-in substitute for an old regex interface. Dependencies and firmware closure can be delegated to mkosi.

Retain Tundra’s curated policy and boot tests. Never use host-loaded modules as a release default: a laptop’s module set is not a cloud guest’s requirements.

Also account for built-in drivers. Checking that a module filename exists is not proof that TDX, networking, or storage support is absent or present.

**18. Phase 0’s “fresh machine, install nothing else” claim is false.**

Installing the Python package solves the mkosi executable distribution problem. It does not supply the execution environment.

Use a subprocess with the current interpreter:

```python
subprocess.run([sys.executable, "-m", "mkosi", ...], check=True)
```

That is sufficient integration for now. Do not import the build runner into the SDK process: it changes namespaces, mounts, and other process-global state. A “vendored runner” is also unnecessary unless a concrete packaging defect requires one.

The required host capabilities need accurate treatment:

| Capability | Actual requirement |
|---|---|
| Linux namespaces | Rootless builds need usable user and mount namespaces, permitted by kernel, container, and LSM policy. Root execution is a different supported path. |
| `systemd-nsresourced` | Useful for richer UID mappings and particular features, but not mandatory for every build. v27.1 has a fallback user namespace. |
| `newuidmap` / `newgidmap` | Not universal mkosi v27 requirements. A manually selected multi-user namespace workflow can still need subordinate mappings. |
| Bubblewrap | Not the normal mkosi sandbox dependency; mkosi switched to `mkosi-sandbox` in v25. |
| Ownership | The fallback single-user namespace constrains build-time ownership changes. Test packages/hooks that create dedicated-user files; use runtime directory creation where appropriate. |
| Package-manager bootstrap | Building the tools tree itself needs suitable host tools and trust material, unless a pinned prebuilt tools tree is supplied. |
| Repart/ukify/filesystem tools | Needed for the selected outputs; may live inside the tools tree. Versions must satisfy the selected features. |
| QEMU/KVM/firmware | Needed to run images, not necessarily to build them. TDX additionally needs a compatible CPU/host kernel/KVM/QEMU/TDVF stack. |

These distinctions follow the v27.1 execution path and release notes. [Mkosi release notes](https://github.com/systemd/mkosi/blob/v27.1/mkosi/resources/man/mkosi.news.7.md)

Do not remove mkosi checks from `doctor`; change them from “is an executable on PATH?” to “is the pinned runner and its environment usable?”

**19. Lima is a provisioned builder, not automatic macOS compatibility.**

The existing Lima backend provisions Nix and executes a pinned Nix toolchain. Removing `backends/nix.py` does not automatically remove that dependency from Lima. The rewrite must replace its provisioning, tool resolution, and toolchain record together. See [lima.py](/home/franco/tundra/src/tundravm/backends/lima.py).

On Apple Silicon, the production target remains x86-64. Specify whether building uses cross-compilation, translated x86 userspace, or a full x86 VM. These choices affect package scripts, build speed, and reproducibility.

Lima cannot make Apple hardware perform Intel TDX. Local development booting and remote TDX validation are separate capabilities.

`doctor` should report independent capability results:

```text
build:x86-64        ready / blocked, with exact cause
run:simulated       ready / blocked
run:tdx             unavailable: no Intel TDX host
deploy:gcp          ready / missing credentials or configuration
verify:azure        supported / unsupported
```

Check the pinned guest/base image, builder architecture, namespace availability inside the guest, workspace filesystem behavior, sparse files, free disk/RAM, toolchain identity, and source/snapshot reachability. Keep caches and workspaces on a suitable Linux filesystem inside Lima.

Do not automatically switch a failing Linux build to a different backend and quietly change its reproducibility assumptions.

**20. “One measurement set” must be removed from the proposal.**

One UKI gives a shared boot payload. It does not give identical registers across boot paths or platforms.

The current code already knows this:

- Disk GPT events affect RTMR replay.
- QEMU and portable policies leave platform measurements unresolved.
- GCP uses firmware/machine-specific reference data.
- Azure records vTPM PCR expectations.
- Azure policies are rejected by the current TDX-only attestation verification path.

See [schemes.py](/home/franco/tundra/src/tundravm/measure/schemes.py:1) and [tdx.py](/home/franco/tundra/src/tundravm/measure/tdx.py:1).

The replacement contract should be:

| Platform/scheme | Build-time output | What remains a deployment/verifier responsibility |
|---|---|---|
| Local QEMU TDX | Payload-derived expectations for an explicitly identified boot path | Approved TDVF/QEMU/platform configuration and actual quote verification |
| GCP TDX | Payload expectations plus versioned compatible firmware/configuration candidates | Current platform endorsement/TCB policy and observed evidence |
| Azure TDX | Guest vTPM PCR expectations and the selected attestation scheme | Verified binding of vTPM evidence to the protected platform and guest measurements |
| Portable | Explicitly scoped payload identity or event-log policy | Platform trust; it must not silently become “full TDX verification” |

`measurements.json` should contain per-target records, measurement tool/version, stub/kernel identity, digest algorithm, boot mode, artifact-to-payload binding, expected values, and explicit unknowns.

Separate **predictions** from **observations**, and both from an operator-approved **policy**. A successful build cannot approve the platform it has never observed.

RTMR3 needs an explicit statement. It is user-defined runtime measurement state, not universally predictable from the image. If runtime configuration is extended into it, specify the event schema, order, and verification behavior. Otherwise record it as deliberately outside the policy’s coverage rather than inventing a value.

**21. Changing QEMU output to qcow2 introduces an immediate measurement regression.**

Today QEMU can boot the UKI directly. The plan changes that to an ESP disk in qcow2.

The current `_gpt_event()` reads the artifact’s first sectors as raw GPT. It does not decode qcow2. Feeding it the proposed `demo.qcow2` will not recover the guest’s GPT.

Even raw disk boot changes the expected event sequence compared with direct UKI boot.

Prefer raw disk output and record its boot path. If qcow2 is supported, derive measurements from the exact underlying logical disk and verify that it is the disk actually packaged and launched.

Do not claim a successful migration because the extracted UKI hash matches. Preserve actual quote/event-log tests for each launch path.

Also preserve firmware-pair consistency checks: current tests reject MRTD and RTMR0 values from different firmware candidates. Independent allowlists are not an adequate replacement. [Policy tests](/home/franco/tundra/tests/measure/test_policy_v2.py:48)

**22. Verity is an architectural choice, not a late large-image optimization.**

`Format=uki` embeds the OS in the UKI’s initrd. That is attractive for small services, but expensive for large runtimes, proving assets, and blockchain software. It changes boot memory, decompression cost, and update size.

Define two layouts now:

```python
rootfs=RootFS.initramfs()
rootfs=RootFS.verity(filesystem="erofs")
```

For verity:

- Use native `Format=disk` with root data and verity hash partitions.
- Let mkosi collect the root hash from repart and embed it in the UKI command line.
- Ensure the initrd contains the drivers and userspace needed to mount that root.
- Keep writes in explicit persistent volumes or bounded volatile overlays.
- Bind the UKI, root filesystem, and verity metadata in the build manifest.

Mkosi already supports this sequence. The reference’s `modules/verity-root/` demonstrates the basic shape.

The trust statement becomes “the measured UKI authenticates this root filesystem through dm-verity.” A UKI beside an arbitrary mutable root disk does not measure the service stored on that disk.

For this layout, cloud packaging must carry the matching root and verity partitions too. **An ESP-only wrapper around the split UKI is insufficient.** Build the canonical root/verity payload once, then preserve it when producing platform disks.

Avoid making filesystem layout switch silently at a size threshold. If `auto` exists, resolve and record the choice so an application dependency update cannot unexpectedly change its storage and measurement model.

**23. Kernel defaults need a tested compatibility contract.**

`Kernel(hardened=True)` says nothing about:

- Kernel/stub versions and provenance.
- TDX report and quote interfaces.
- ACPI behavior on the selected platform.
- Virtio, Hyper-V, NVMe, and gVNIC support.
- TPM support where explicitly used.
- Device mapper, dm-crypt, and dm-verity.
- Required root filesystem support in the initrd.
- Module-signing/loading policy.
- Compatibility with .NET JITs, prover workloads, and observability tools.

Choose a tested default kernel and publish that matrix. Keep source, configuration, patches, compiler, and resulting binary identity in the lock/build record.

Do not copy flashbots’ hardening configuration wholesale. The inspected tree includes workload-specific choices and patches; its command line also contains `nospectre_v2`, which requires a deliberate decision rather than adoption under a generic “hardened” label.

A boot-success test alone is insufficient: test quote acquisition, encrypted-state recovery, verity failure behavior, and the actual application runtime.

**24. Delete implementations after preserving their guarantees.**

| Proposed deletion | Judgment | Required replacement |
|---|---|---|
| Per-variant copied directories | Delete | Native shared configuration plus profile semantic tests |
| `nethermind-v1` byte-identical golden layout | Delete | Converted-stack boot, measurements, provisioning, state recovery, and application behavior tests |
| Custom module closure | Replace | Native mkosi closure, retained curated policy and driver tests |
| General QEMU command construction | Replace where proven | TDX firmware launch, persistent disks, lifecycle control, logs, and equivalent test assertions |
| All cloud postoutput handling | Reckless as written | Native repart plus retained deterministic format converters |
| Evidence/release/witness implementations | Consolidate | Signed manifest, independent rebuild records, integrity validation, explicit missing/unchecked states |
| In-process backend | Remove from public API | Lightweight fake process/backend boundaries in unit tests |
| Nix backend | Reasonable destination | First replace the pinned toolchain and Lima provisioning it currently supplies |
| Runtime-tools renderers | Reasonable consolidation | Versioned Go/Python protocol, credential readiness, failure semantics, and upgrade compatibility |
| Long documentation | Rewrite | Preserve concise threat model, platform limits, recovery, and operational guidance |

Tree-digest tests do not replace process-failure tests or boot tests. They cannot detect a broken quote endpoint, unusable data disk, or misreported exit status.

Likewise, `build.json` does not preserve signatures merely by mentioning them. Define canonical signed bytes, the artifact index, signer policy, archive/path validation if bundles remain, and independent verification.

`build --reproduce` should use independent workspaces and avoid reusing the first build’s compiled outputs. Two invocations that reuse the same cached binary prove much less than two independent builds. Package-download caches can be shared if their content is verified and the reuse is recorded.

The deletion plan is **not aggressive enough** about public compatibility aliases, duplicate configuration languages, and unsupported baseline combinations. With no compatibility requirement, do not spend a phase maintaining old names. Keep semantic migration tests, not public aliases.

**25. Reorder the phases around unknowns, not line-count reductions.**

The proposed order introduces convenient disk and secret declarations in phase 1 but implements their semantics in phase 5. It also deletes the audit and execution scaffolding before those new security paths are established.

Use these gates:

1. **Prove the substrate:** exact released mkosi package, tools-tree bootstrap on supported Linux/Lima configurations, native payload/dependency tree, deterministic raw/VHD/GCP packaging.
2. **Prove production execution:** real TDX launch and quote verification on each claimed platform; explicitly document unsupported combinations.
3. **Specify and implement runtime contracts:** channel-bound secret delivery, safe volume initialization/unlock, readiness, debug/simulation separation, and one upgrade path.
4. **Implement the concise SDK over those contracts:** rewrite examples and make them executable acceptance tests.
5. **Replace CLI/build records:** immutable build identities, signed manifest, explicit verification and operator lifecycle.
6. **Delete old implementations and rewrite documentation alongside the replacements.**

A 1–2 day phase 0 might install a Python dependency. It cannot credibly include “fresh machine” support, reproducible tool bootstrap, Lima changes, and the host capability matrix without prior proof.

Replace the under-25,000-lines target with acceptance criteria: starter length, actionable failures, build time, independent reproducibility, recovery after replacement, and successful attested request handling. Lines removed can be reported afterward.

**26. Naming should distinguish build identity, runtime resources, and trust decisions.**

| Name | Recommendation | Reason |
|---|---|---|
| `Image` | Keep for the declaration; use `Build`/`Artifact` for results | Avoid making the same value mean source specification and built disk |
| `Profile` | Keep with documented semantics | Familiar, but explicitly distinguish it from a mkosi UKI profile |
| `Part` | Prefer `Component` | Indicates reusable behavior with inputs and exports |
| `Dev` | `DevAccess` | Names what it changes; simulation is separate |
| Image `state` | `volumes` | Names storage resources and allows multiple disks |
| Service `state` | Typed directory binding | Retains concise application language without guessing disk relationships |
| `Disk` | `Volume` for the SDK resource | Physical device selection belongs to deployment |
| `Secret` | Keep for a requirement/reference | Values are delivered separately |
| Service `secrets` | `credentials` | Matches the systemd presentation mechanism |
| `attestation` | `EvidenceService`, `VerificationPolicy`, and delivery configuration | Separates issuer, verifier, and authorization roles |
| `outputs` | `targets` or CLI `--target` | Represents requested packaging; document that it does not identify trusted execution |
| `release` codename | `base` or `debian_release` | Avoid collision with production releases |

Do not add all these names to the beginner import list. Export a small default surface and document advanced namespaces separately.

**Ranked top ten changes to the plan**

1. **Define the attested-channel and secret-release protocol before implementing the convenience API.** Fresh quotes must bind the actual recipient key; simulation and incomplete verification never release production secrets.
2. **Replace automatic vTPM sealing with explicit volume/key policies.** Define recovery, initialization safety, instance replacement, and upgrade authorization.
3. **Correct the native build graph.** Put the payload in a subimage; wrappers depend on it; explicitly include payload settings and profile content.
4. **Retain deterministic cloud packaging.** Repart replaces partition construction, not VHD conversion or GCP archive requirements.
5. **Replace “one measurement set” with per-platform, per-boot-path records.** Preserve unknowns, firmware pairing, Azure’s distinct verification path, and artifact bindings.
6. **Move verity layout and kernel compatibility into the initial architecture.** A large-image disk is more than an ESP around a UKI.
7. **Make the service API argv-based and relationship-aware.** Preserve readiness, credential paths, state bindings, resource controls, and shutdown behavior.
8. **Define explicit development simulation and production TDX execution.** No silent fallback; debug identity and credentials remain separate.
9. **Use immutable build identities and a real operator lifecycle.** Separate inspection, secret delivery, status/logs, upgrade, rollback, and integrity verification.
10. **Gate deletions on equivalent behavior.** Prove host/toolchain support and production paths first; retain trust/reproducibility tests and remove compatibility aliases immediately.

**Rewritten example 1: the public HTTP demo**

This is proposed API syntax, not code supported by the current repository. It deliberately describes a public plaintext service; confidential ingress is explicit in the second example.

```python
# demo.py
from tundravm import File, Image, Service, TCP

image = Image(
    "demo",
    packages=["busybox"],
    files=[File.text("/srv/www/index.html", "<h1>demo</h1>\n")],
    services=[
        Service(
            "app",
            ["/usr/bin/busybox", "httpd", "-f", "-p", "8080", "-h", "/srv/www"],
            expose=[TCP(8080)],
        ),
    ],
)
```

Default output is a bootable raw QEMU disk. Cloud packaging is requested with `build --target gcp,azure`. The default baseline supplies hardening, dedicated users, the evidence service, and the tested boot stack.

`TCP(8080)` controls guest ingress. Local forwarding and cloud exposure remain visible deployment decisions.

**Rewritten example 2: an attested prover with persistent state**

The proposal’s `GoBinary(...raiko..., package="./cmd/raiko")` is not a credible executable example: Raiko is a Rust/Cargo project. It also omits the `Dev` import. Use a tested Raiko component that owns its real build, configuration, credential-path, and readiness conventions. [Raiko repository](https://github.com/taikoxyz/raiko)

```python
# prover.py
from tundravm import AttestedTLS, DevAccess, Image, KeyBroker, Secret, Volume
from tundravm.components.taiko import Raiko

broker = KeyBroker("https://keys.example.net", audience="prover")
data = Volume("data", mount="/persistent", unlock=broker.key("prover-data"))
jwt = Secret("jwt", source=broker.secret("prover-jwt"), rotation="restart")
raiko = Raiko(
    source="./raiko",
    state=data.directory("raiko"),
    jwt=jwt,
    listen="127.0.0.1:8080",
)
image = Image(
    "prover",
    components=[raiko],
    volumes=[data],
    secrets=[jwt],
    ingress=[AttestedTLS(443, upstream=raiko.http)],
    dev=DevAccess(ssh_keys=["~/.ssh/id_ed25519.pub"]),
)
```

The local Raiko source/configuration and Cargo inputs are locked. The component must be implemented and tested against a specific supported Raiko configuration; this example does not invent Raiko command-line flags.

Broker provisioning and approval of the image/platform policy are external deployment prerequisites. The image cannot authorize itself to receive production keys. `DevAccess` only takes effect in an explicitly selected development profile.

**Rewritten CLI table**

Use a compact set of command groups without sacrificing distinct operations:

| Command | Contract |
|---|---|
| `tundravm new NAME [--template service\|prover] [--ci]` | Create a minimal project with executable examples and optional CI. |
| `tundravm check [--profile P] [--tree DIR]` | Validate declarations, references, lock drift, and optionally committed-tree drift without building images. |
| `tundravm lock [--update [INPUT...]] [--check]` | Resolve or check source, baseline, package/toolchain, and firmware-reference pins. |
| `tundravm build [--profile P] [--target qemu,azure,gcp] [--reproduce]` | Build requested artifacts, measurements, SBOM, and immutable manifest; reject stale locks. |
| `tundravm build --tree-only --output DIR` | Export the standalone mkosi project without requiring a completed image build. |
| `tundravm show [BUILD] [--units\|--tree\|--measurements\|--json]` | Inspect resolved configuration or an existing build, including provenance and unchecked guarantees. |
| `tundravm show --diff OLD NEW` | Compare packages, files, units, boot payloads, measurements, and policy coverage. |
| `tundravm run [BUILD] --mode simulated\|tdx [--dev] [--detach]` | Start a named local instance with explicit security mode and recorded artifact identity. |
| `tundravm run status\|logs\|stop INSTANCE` | Inspect and control the instance without modifying its build artifacts. |
| `tundravm run ssh INSTANCE` | Connect only where the image permits debug access, using the defined host-key verification mechanism. |
| `tundravm deploy azure\|gcp --build BUILD [--volume NAME=SPEC]` | Publish/start an explicit build and write a deployment receipt containing cloud identities and security settings. |
| `tundravm deploy status\|logs INSTANCE` | Inspect deployment and application health; report attestation status separately. |
| `tundravm deploy upgrade\|rollback INSTANCE --build BUILD` | Execute the supported replacement workflow with readiness, policy, and state-compatibility checks. |
| `tundravm attest INSTANCE --policy FILE [--output REPORT]` | Perform fresh evidence verification and report coverage; do not deliver secrets. |
| `tundravm secrets deliver INSTANCE --policy FILE NAME=@FILE...` | Authenticate the recipient through fresh channel-bound attestation, authorize delivery, and transfer credentials. |
| `tundravm verify BUILD [--require-signature] [--rebuild]` | Check the signed artifact set; execute an independent rebuild only when requested. |
| `tundravm convert PATH --from mkosi\|systemd` | Convert supported existing inputs and report unsupported semantics explicitly. |
| `tundravm doctor [--for build\|run\|tdx\|azure\|gcp] [--json]` | Diagnose the selected capability with concrete prerequisites and failure reasons. |
| `tundravm cache fetch\|clean [--dry-run]` | Prepare verified build inputs or remove disposable caches; preserve instances, persistent volumes, and retained builds. |