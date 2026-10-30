**Second-pass verdict: substantially improved, but not ready to freeze.** The remaining blockers are inconsistent runtime contracts and several incorrect implementation claims.

No files were modified. No output path or exception to the read-only instruction was supplied, so the review follows here. Plan references are to [the revised document](/home/franco/tundra/docs/design/plan-v2-2026-10-30.md); mkosi references below use **v27.1**, not the newer checkout HEAD.

**1. Internal contradictions**

| Priority | Conflicting passages | Required correction |
|---|---|---|
| High | §5.2:489–491: “`sha512(nonce ‖ keys)`” and verification against “`build/<image>/measurements.json`”; §6.3:597–600: “`sha512(purpose ‖ nonce ‖ public key ‖ SSH host keys)`” against an “operator-approved policy.” | Delete the weaker protocol description. Reference one canonical transcript and distinguish build predictions from authorization policy. |
| High | §5.2:522: “one key, once, then the port closes”; §6.3:602–605: plural “credentials” and rotation “`restart`, `reload`, `none`.” | Specify whether one-shot delivery applies only to a legacy bootstrap operation. It cannot describe the general rotating credential endpoint. |
| High | §6.3:594–595: “volume unlock → application”; §6.8:658–659: “readiness → drain the old instance → transfer exclusive volume ownership.” | A state-dependent candidate cannot become application-ready before receiving its volume. Separate control-plane readiness from application readiness; fence and transfer before the latter. |
| High | §3.1:120–123: firewall opens “exactly `TCP(8080)` inbound,” yet supplies an “attested endpoint on :8745”; §6.3:594 makes that endpoint necessary for bootstrap. | Declare management ingress separately, including how local/cloud clients reach it. Otherwise the default bootstrap endpoint is unreachable. |
| Medium | §3.4:267 specifies “`--mode simulated\|tdx`”; §4.2:420 and §5.2:486 use “`run --tdx`.” §3.4:275 promises “Twelve commands,” but §5.2:501 and §5.3:542 introduce “`tundravm unlock INSTANCE`.” | Canonicalize mode syntax and give unlock an explicit place in the command table. |
| Medium | §3.5:285–296 places measurements under `production/<build-id>/`, identity in `manifest.json`, observations in `observations.json`; §5.2:491 uses `build/<image>/measurements.json`; §4.1:399 hashes resolved configuration into “`build.json`.” | Use the immutable build path and one identity/observations vocabulary throughout. |
| Medium | §3.6:322 embeds `mkosi.extra/root/.ssh/authorized_keys`; §4.3:446–447 promises SSH credentials with “no key in the image.” | Choose runtime credential injection or an embedded public key, and describe local versus cloud development access explicitly. |
| Low | §3.3:224 establishes “`Component`”; §5.1:473–474 still promises “a `Part` later.” §3.1:170 defines `Secret` without `source=`, while §6.3:605 uses `Secret(source=...)`. | Remove stale names and reconcile the declared constructor surface. |

Gate ordering also remains ambiguous: §8:684 requires each gate before “the next one,” whereas §10:735–736 permits gates 4 and 5 once gate 3 is merely “specified.” Distinguish parallel implementation from acceptance prerequisites.

**2. Incorrect or overstated technical claims**

1. **§6.1:571: `ConditionSecurity=tdx` is invalid.** systemd 257 recognizes `cvm`, covering multiple confidential-VM technologies, but not `tdx`; unknown values return false. A failed condition also does not propagate failure through `Requires=`. Use a failing prerequisite service that positively checks TDX and obtains evidence, with explicit application dependencies. Sources: [systemd `condition.c`:623–647](https://github.com/systemd/systemd/blob/v257/src/shared/condition.c#L623), [systemd 257 dependency semantics](https://www.freedesktop.org/software/systemd/man/257/systemd.unit.html#Requires=).

2. **§5.1:466: “every Debian `=m` into `n`” is false as a general Kconfig rule.** tundravm sets `CONFIG_MODULES=n` and runs `olddefconfig`; Linux treats tristates as booleans without modules and promotes surviving `m` values to `y`. Dependencies can still disable particular drivers, but the claimed universal failure and resulting missing devices are not established. Require a concrete before/after config before prescribing `mod2yesconfig` as the fix. Sources: [lower.py:483](/home/franco/tundra/src/tundravm/declarative/lower.py:483), [emit_mkosi.py:839](/home/franco/tundra/src/tundravm/compiler/emit_mkosi.py:839), Linux [symbol.c:45](https://github.com/torvalds/linux/blob/v6.12/scripts/kconfig/symbol.c#L45) and [symbol.c:477](https://github.com/torvalds/linux/blob/v6.12/scripts/kconfig/symbol.c#L477).

3. **§4.1:400: `History=no` does not prevent reading existing history.** It controls history storage. `have_history()` can load an existing history file for `summary`; `summary --force` explicitly bypasses it. Thus the proposed summary can disagree with a fresh build. Sources: mkosi [config.py:5302](https://github.com/systemd/mkosi/blob/v27.1/mkosi/config.py#L5302), [config.py:5574](https://github.com/systemd/mkosi/blob/v27.1/mkosi/config.py#L5574), [config.py:5624](https://github.com/systemd/mkosi/blob/v27.1/mkosi/config.py#L5624).

4. **§4.1:396–401 overstates what summary proves.** `summary` returns before `run_configure_scripts()`, which can change configuration and dependencies during build. Either prohibit configure scripts in the supported tree or capture their effective result too. Sources: mkosi [__init__.py:5080](https://github.com/systemd/mkosi/blob/v27.1/mkosi/__init__.py#L5080), [__init__.py:5294](https://github.com/systemd/mkosi/blob/v27.1/mkosi/__init__.py#L5294).

5. **§4.1:407–410: `parse_config` is not a read-only foreign-tree parser.** Parsing can execute executable configuration inputs through `file_run_or_read()` and, for build arguments, write history. Treat foreign-tree conversion as potentially executing project code; subprocess isolation alone does not make it read-only. Sources: mkosi [config.py:1693](https://github.com/systemd/mkosi/blob/v27.1/mkosi/config.py#L1693), [config.py:5193](https://github.com/systemd/mkosi/blob/v27.1/mkosi/config.py#L5193), [config.py:5624](https://github.com/systemd/mkosi/blob/v27.1/mkosi/config.py#L5624).

6. **§4.2’s `mkosi vm` substitution needs another configuration boundary.** The §3.6 coordinator is `Format=none`; v27.1 refuses `vm` for that main image. Specify an explicit launch configuration referencing the selected disk. Also, native swtpm uses a temporary state directory, so §6.2’s persisted local TPM state still requires custom handling. Sources: mkosi [__init__.py:5219](https://github.com/systemd/mkosi/blob/v27.1/mkosi/__init__.py#L5219), [qemu.py:267](https://github.com/systemd/mkosi/blob/v27.1/mkosi/qemu.py#L267).

7. **§4.1:406: mkosi’s gzip flags do not make the retained GCP converter reproducible “for free.”** Its compressor flags govern its own compression path; the post-output script constructs its own archive. tundravm already controls sparse detection, ownership, timestamps and gzip headers there. Sources: mkosi [__init__.py:2370](https://github.com/systemd/mkosi/blob/v27.1/mkosi/__init__.py#L2370), [emit_mkosi.py:367](/home/franco/tundra/src/tundravm/compiler/emit_mkosi.py:367).

8. **§6.6:643 overstates existing measurement-test coverage.** Current replay tests are offline vectors; template boot tests explicitly use direct UKI `-kernel`, not the proposed raw-GPT TDX path. Describe raw-disk measurement boots as gate-2 work. Sources: [test_replay.py:1](/home/franco/tundra/tests/measure/test_replay.py:1), [test_templates_boot.py:1](/home/franco/tundra/tests/integration/test_templates_boot.py:1).

**3. Review-1 points still incompletely addressed**

- **#6, credential presentation:** §6.3 names `/run/credentials` without distinguishing delivery storage from systemd’s per-unit credential copies. Existing rendering uses `LoadCredential=`; systemd copies credentials into read-only per-unit storage. Replacing the source and sending reload does not automatically refresh that copy. Specify the source directory, ownership, restart behavior, and what makes `rotation="reload"` implementable. Sources: [render.py:176](/home/franco/tundra/src/tundravm/_units/render.py:176), [systemd credential semantics](https://www.freedesktop.org/software/systemd/man/257/systemd.exec.html#LoadCredential=).

- **#9, replacement workflow:** fencing is now named, but the readiness-before-transfer ordering remains unusable for the prover’s stateful service. This also corrects an ambiguity in review 1’s suggested sequence.

- **#19, Lima:** §3.7 still does not choose an Apple Silicon → x86-64 build strategy. “Same pinned mkosi and tools tree” does not specify translated userspace, cross-builds, or full emulation. Gate 1 needs one concrete supported host/guest architecture pair.

- **#22/#25, verity proof:** the layout is specified, but gate 1 does not explicitly require a verity build or preservation of its root/hash partitions through both cloud converters. Gate 4 could otherwise accept the prover while only the small initramfs layout has been proved.

**4. Further concrete improvements, ranked**

1. **Make the release predicate executable.** Sketch: production delivery requires `trusted`, fresh channel binding, approved production identity and no debug attributes. Add rejection cases for partial coverage, simulation, replay and wrong recipient key. Never reuse `Attestation.passed`: it intentionally accepts weaker verdicts ([attestation.py:337](/home/franco/tundra/src/tundravm/attestation.py:337)).

2. **Add one complete operator transcript per example.** Starter: create → build → simulated dev boot → forwarded HTTP request. Prover: acquire pinned source/config → build → attach volume → approve policy → unlock/deliver → attested request → replacement recovery. Include expected outputs and necessary external inputs.

3. **Make the tree sketch an exact fixture.** Include INI sections, explicit output names, target selection, dev overlays and runtime launch configuration. Assert summary values and artifact paths. mkosi defaults to subimage names such as `payload.efi` and `qemu.raw`, not §3.5’s `image.*` names ([config.py:1030](https://github.com/systemd/mkosi/blob/v27.1/mkosi/config.py#L1030)).

4. **Give every gate a durable evidence artifact.** Sketch: gate 1 retains independent digest comparisons and resolved configurations; gate 2 retains launch receipts, quotes and event logs; gate 3 retains a negative-protocol test report and replacement-volume recovery transcript.

5. **Replace “prover, complete” with a supported workload contract.** Name the Raiko revision, Cargo features/toolchain, required RPC/configuration inputs, readiness check and smallest successful proof request. Measure its initramfs RAM footprint or select verity explicitly. This makes the concise constructor credible without adding beginner-facing knobs.