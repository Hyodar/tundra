# Test pruning: what the suite can lose without losing a guarantee

Date: 2026-10-31. Status: decided and done; section 8 records what landed and the measured
result. Decision: `Recipe`-file loading stays in the CLI and `tundravm.recipe`, because the CI
fixture `tests/fixtures/native-tree/recipe.py` is a declarative `Recipe` and is the only image
CI can build while tundra-tools is private. Study base: master at `ff59f14`. Scope: the
unit suite (`uv run pytest -m "not integration"`); the 34 integration and boot tests, the
docs tests, the user-strings denylist and the two layout tests are out of scope and stay.

## 1. Verdict

The suite is not bloated by a third. Honest reading supports deleting **249 test
functions and merging 90 more into 34 table-driven tests**: 1 413 → 1 108 functions (−22%),
2 183 → 1 896 collected tests (−13%), about 29 450 → 26 700 lines (−9%). Measured with the
deletions deselected, the run goes from 69.1 s to 66.6 s and line coverage from 93.19% to
92.68%; all 106 statements it stops reaching are code nothing but those tests calls (section
5), so no live line loses its test. A further ~13 s (−19%) comes from sleeps and timeouts,
not from test count (section 6).

Why less than a third: by count, the bloat is mostly cheap table rows (one `INVALID` table in
`test_model.py` is 68 items and runs in 0.07 s), and every row is a distinct refusal. The
internal-model tests mostly test rules no `Image`-level test asserts. Coverage saturation
looks dramatic (below), but coverage is not assertion: most coverage-redundant tests are the
only test asserting their outcome.

## 2. Measurements before (ff59f14)

| Measure | Value |
|---|---|
| Test files / all `tests/**/*.py` | 130 / 151 |
| Test functions (AST) / collected unit tests | 1 446 (1 413 unit) / 2 183 (+34 integration) |
| Lines in `tests/` | 29 629 (fixtures 178) |
| Wall time, 12 cores, serial | 69.1 s (31 s CPU; the rest is sleeps, timeouts, subprocesses) |
| Line coverage of `src/tundravm` | 93.19% (19 623 of 21 058 statements, `coverage run -m pytest`) |
| Collected tests that cover no function-body line no other test covers | 1 618 of 2 179 |
| Greedy set cover keeping every covered body line | 694 tests (32%), 543 functions (38%) |
| Functions whose covered lines are a subset of another function's | 426 of 1 412 (30%); 268 by one in another file |
| AST-identical bodies after normalising names and constants | 11 groups, 26 tests |

| Directory | Files | Lines | Functions | Collected | Seconds |
|---|---|---|---|---|---|
| declarative | 29 | 5 254 | 273 | 489 | 2.4 |
| compiler | 21 | 5 921 | 275 | 354 | 18.6 |
| lint | 8 | 1 984 | 108 | 273 | 4.2 |
| cli | 18 | 4 030 | 211 | 268 | 21.5 |
| lifecycle | 23 | 5 530 | 244 | 261 | 7.3 |
| unit | 10 | 1 641 | 88 | 208 | 2.1 |
| image | 6 | 1 514 | 103 | 129 | 2.2 |
| docs | 2 | 302 | 14 | 104 | 0.7 |
| testing_toolkit | 4 | 1 172 | 72 | 72 | 8.7 |
| measure, root | 3 | 672 | 24 | 24 | 0.0 |

Largest parametrize expansions: `docs/test_docs.py` 8 → 97, `lint/test_user_strings.py`
15 → 88, `declarative/test_model.py` 10 → 77, `declarative/test_service_fields.py` 14 → 58,
`unit/test_unit_text.py` and `test_unit_parse.py` 5 and 7 → 44 each.

Slowest tests: `compiler/test_determinism.py::test_the_converters_reproduce_their_outputs`
8.9 s, `cli/test_deploy.py::test_status_logs_and_mode` 4.0 s,
`testing_toolkit/test_boot.py::test_an_http_url_that_never_answers_fails_the_boot` 3.0 s,
`cli/test_run.py::test_a_second_run_is_another_instance` 1.3 s; the 25 slowest sum to 29 s.
Slowest files: `test_determinism.py` 9.0 s, `test_boot.py` 7.6 s, `test_run.py` 6.1 s,
`test_deploy.py` 5.4 s, `test_profile_overlays.py` 3.4 s, `test_hermetic_fetch.py` 2.9 s,
`test_native_tree.py` 2.8 s, `test_build.py` 2.2 s, `test_manifest_verify.py` 1.9 s,
`test_secrets.py` 1.8 s. The 489 declarative tests together take 2.4 s.

## 3. Method

Per-test coverage contexts (`pytest-cov --cov-context=test`), restricted to lines inside
function bodies so lazy imports do not count, gave each test function its covered set, its
unique lines and its supersets. That list was a candidate list, not a verdict: four review
passes (internal model, compiler, lifecycle/unit/measure, CLI/lint/image/toolkit) read every
candidate against the test that would replace it and kept a test whenever it was the only
assertion of its outcome. Then the plan was measured: a scratch pytest plugin deselected
the 249 functions by exact id and the suite ran timed and under coverage. Nine rows were
put back because the coverage data showed them to be the only test of a live line
(`compile_tree`'s errors in the public `tundravm.testing`, GitHub annotations, `get_adapter`,
a missing lockfile, the doctor measurement rows, `check --profile` with a known profile). The scripts
stayed in the scratchpad.

Three established facts drove most decisions:

- **The goldens pin whole outputs.** The native-tree fixture (42 files including every
  cloud-glue file, the dev overlay and the gate), the surge tree and `test_lower.py`'s
  recorded tree compare byte for byte. A test that re-asserts a substring of a file a
  golden pins is `golden-pinned`; a test that states a property the golden cannot explain
  (no static `resolv.conf` because the same UKI boots off GCP) stays.
- **Some subjects are gone.** `report.json` is written and read by nothing (the build
  directory never publishes it); the `sbom` verb went, so `build` renders SPDX only and
  passes no `binaries=`; no example, template or doc loads an internal `Recipe` file any
  more, though the loader still accepts one.
- **The internal model is not public but is not dead.** `Image` lowers into it, so a
  declarative test stays unless an `Image`, compiler or CLI test asserts the same outcome.

## 4. Proposed changes

| Category | Deleted | What it means |
|---|---|---|
| layer-dup | 87 | the same rule asserted at another layer, the survivor named |
| golden-pinned | 53 | a substring of a file a golden pins byte for byte |
| body-dup | 47 | the same body or the same outcome on the same input |
| dead-guard | 31 | `report.json`, SBOM formats and `binaries=`, Recipe-file discovery beyond one test, a removed python3 dependency |
| trivial | 20 | asserts a default, an `isinstance`, a `str()` or exit 0 |
| helper-only | 8 | a test-suite helper or an internal path helper |
| same-path-param, message-pin | 3 | rows through one code path; text another test pins |
| merged | 90 → 34 | N tests become one table, every case a row (appendix B) |

Whole files deleted: `compiler/test_debloat.py`, `lifecycle/test_bake_report.py`,
`lifecycle/test_reproducible_bake.py`. Most-cut files: `compiler/test_emit_mkosi.py` (27
rows of 52), `lint/test_check_rules.py` 14, `compiler/test_build_cache.py` and
`test_keys_disks_secrets.py` 13 each, `compiler/test_source_build.py` 12,
`declarative/test_gcp_target.py` and `test_lower.py` 11 each.

Top ten deletions by size:

| Test | Lines | Covered by |
|---|---|---|
| `compiler/test_tdxs_runtime_init.py::test_tdxs_generates_config_yaml_and_units` | 47 | `compiler/test_surge_golden.py::test_surge_image_matches_the_committed_tree` |
| `compiler/test_build_cache.py::test_wrap_multiple_artifacts` | 33 | `compiler/test_source_build_examples.py::test_install_steps_render_files_modes_and_directories` |
| `compiler/test_keys_disks_secrets.py::test_init_generates_a_unit_per_runtime_init_step` | 28 | `compiler/test_surge_golden.py::test_surge_image_matches_the_committed_tree` |
| `compiler/test_emit_mkosi.py::test_compile_cloud_wrapper` | 26 | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `declarative/test_resolve.py::test_chained_variants_remove_and_edit_any_kind` | 25 | `declarative/test_resolve.py::test_changes_edit_and_remove_by_identity` |
| `declarative/test_utils.py::test_tdxs_issuer_and_validator_render_the_config` | 24 | `compiler/test_tdxs_runtime_init.py::test_tdxs_validator_config_supports_expected_measurements` |
| `cli/test_policy_export.py::test_from_measurements_matches_from_policy` | 23 | `lifecycle/test_lifecycle_api.py::test_measurements_policy_round_trips_into_a_tdxs_verifier` |
| `compiler/test_emit_mkosi.py::test_compile_generates_service_units` | 23 | `compiler/test_emit_mkosi.py::test_compile_generates_service_unit_all_sections` |
| `declarative/test_tee_base.py::test_defaults_declare_time_journal_sysctl_dns_and_masks` | 23 | `image/test_starter.py::test_the_tee_baseline_is_there` |
| `testing_toolkit/test_toolkit_helpers.py::test_recipe_file_and_run_cli` | 23 | `testing_toolkit/test_toolkit_helpers.py::test_run_cli_captures_usage_errors_and_sdk_errors` |

Examples of the duplication found: register matching with labels, firmware pairing and
the schema-1 refusal are each tested in `measure/test_policy_v2.py` and again in
`cli/test_attest.py` (and the refusal a third time in `cli/test_policy_export.py`);
`test_emit_mkosi.py` has five `strip_image_version` tests beside
`test_lower.py::test_strip_os_release_overrides_the_epoch_default`; `test_azure_target.py` and
`test_gcp_target.py` re-assert the glue files the native golden pins.

The full deletion list, each row with the test that keeps its guarantee, is appendix A; the
merge tables are appendix B. Unused helpers left behind (the `RECIPE` constants and fixtures
in `test_check_rules.py`, `test_diff.py`, `test_lock_drift.py`, `test_host_fetch.py`,
`test_policy_export.py`) go with the deletions. The `source` parameter of
`cli/test_convert.py::test_from_is_required_and_closed` is unused: drop it.

## 5. After

| Directory | Functions | Collected | Lines (estimate) |
|---|---|---|---|
| declarative | 273 → 187 | 489 → 418 | 5 254 → 4 476 |
| compiler | 275 → 175 | 354 → 264 | 5 921 → 4 955 |
| lifecycle | 244 → 196 | 261 → 227 | 5 530 → 5 054 |
| cli | 211 → 193 | 268 → 237 | 4 030 → 3 917 |
| lint | 108 → 91 | 273 → 253 | 1 984 → 1 888 |
| unit | 88 → 72 | 208 → 187 | 1 641 → 1 524 |
| testing_toolkit | 72 → 62 | 72 → 62 | 1 172 → 1 072 |
| image | 103 → 100 | 129 → 126 | 1 514 → 1 497 |
| measure, root | 24 → 17 | 24 → 17 | 672 → 614 |
| docs, integration | unchanged | unchanged | unchanged |
| **total** | **1 413 → 1 108** | **2 183 → 1 896** | **29 451 → 26 730** |

Measured (deletions deselected; merges keep their rows and coverage): 1 896 tests, 66.6 s,
92.68% line coverage (−0.51 points). The 106 statements no longer reached are all dead or
vestigial: `declarative/bom.py` 63 (CycloneDX, text and markdown renders and the
`binaries=` path the removed `sbom` verb used), `declarative/_embedded.py` 33 (reading Go
build info and `cargo auditable` sections, same path) and `recipe.py` 10 (discovering a
`Recipe` factory by name, annotation or as the only instance). Deleting that code with the
tests puts coverage at 93.15%.

## 6. Not deletions, but worth doing

- **Runtime.** `cli/test_deploy.py::test_status_logs_and_mode` spends 4 s in four 1 s
  connects to the control-plane address; `cli/test_run.py` spends 4.7 s in `instances.py`'s
  poll sleeps; `test_boot.py`'s two timeout tests wait 3 s and 1 s; three HTTP fixtures wait
  0.5 s each in `serve_forever`'s shutdown poll. Injecting the probe, the poll interval and
  shorter timeouts saves about 13 s with no test removed.
- **Source code these tests were the last users of:** the `report.json` writer in
  `declarative/_bake.py` (`explain_debloat` is not one: `show` uses it), the SBOM formats and
  `binaries=` path above, and `Recipe`-file loading in the CLI and `tundravm.recipe`, which
  stays (the decision above).

## 7. Deliberately kept

- Everything protected: `tests/docs/`, `tests/integration/`,
  `lint/test_user_strings.py`, `unit/test_public_surface.py`, `unit/test_package_layout.py`,
  `lint/test_examples_api.py`, `compiler/test_native_tree.py` (FAT32 cluster count, verity
  `CopyBlocks`), `compiler/test_profile_overlays.py` (profile determinism),
  `compiler/test_debloat_scripts.py`, and the boot assertions in `test_boot.py`.
- One fetch hint per script kind: `compiler/test_source_build.py::test_hooks_copy_the_fetched_checkout`
  (build), `::test_tdxs_hook_copies_the_fetched_checkout` (tdxs),
  `lifecycle/test_kernel_fetch.py::test_an_unpinned_current_kernel_script_only_fails`
  (kernel); the surge golden pins the rest.
- `lint/test_check_rules.py` keeps 31 of 44 functions: it is the only home of about fifteen
  rule codes, since `Image` tests only call `assert_clean`.
- `cli/test_attest.py`, `test_attest_coverage.py` and `test_fail_closed.py` look alike, but
  each test pins a distinct verdict reason (nonce, validator, simulator, platform, image,
  vTPM, debug, guest gate) or a record re-derivation.
- `declarative/test_component.py`: `@component` is its own implementation, separate from
  the public `Component`. `tests/image/` loses three tests: it is the public surface and
  each test pins one slot.
- `lifecycle/test_hermetic_fetch.py`, `test_host_fetch.py`, `test_kernel_fetch.py`: only the
  `fetch` verb went; the fetch step runs in every build.
- `compiler/test_build_cache.py`'s dotfiles round trip,
  `test_determinism.py::test_the_converters_reproduce_their_outputs`, and the remaining
  table-driven validation tests, whose rows are cheap and distinct.
- Files with nothing to cut: `test_mkosi_adapter.py`, `test_unit_directives.py`,
  `test_local_linux_backend.py`, `test_source_fetch.py`, `test_variant_pins.py`,
  `test_replay.py`, `test_kernel_compose.py`, `test_inputs.py`, `test_network.py`,
  `test_platform.py`, `test_resources.py`, `test_unit_dropin.py`, `test_cloud_glue.py`.

## 8. Result

Seven commits on top of this note, each passing the gate chain alone: compiler, internal
model, CLI/lint/image/toolkit and lifecycle/unit/measure deletions (249 functions, three
files), the 34 merges, the dead code, the waits. Measured on the last one against `5b247ae`:

| Measure | Before | After |
|---|---|---|
| Test functions (AST, incl. 34 integration) | 1 446 | 1 135 (−22%) |
| Collected unit tests | 2 183 | 1 913 (−12%) |
| Lines in `tests/` (tracked `.py`) | 29 633 | 26 474 (−11%) |
| Wall time, unit suite | 68.8 s | 47.3 s (−31%) |
| Line coverage of `src/tundravm` | 93.19% (1 435 of 21 058 missed) | 92.98% (1 445 of 20 598 missed) |
| `src/` lines | | −833 net (888 removed) |

Where it differs from sections 4 and 5:

- **The merges grew the tables, not shrank them.** The 34 table tests carry 26 more rows
  than the study counted (members that checked a substring now check the whole value on
  every row), and the merge commit adds 245 lines net. The line cut comes from the
  deletions and the dead code.
- **The dead code went further than section 5 named.** `Artifact.installed` and
  `declarative/_initrd.py` (the UKI initrd digests a bake recorded) had one reader, the
  `binaries=` path, so they went with it, and their nine tests
  (`lifecycle/test_initrd_digests.py`, the last `lifecycle/test_sbom_embedded.py` test) too.
  SPDX output is byte-identical before and after.
- **Coverage.** The missed count rises by ten: `recipe.py` +10 (`Recipe`-factory
  discovery, kept by the decision above, its tests deleted), `bom.py` +3 (the lock-less
  branch; `build` always passes a lock), `templates.py` +8 (its probe for whether the
  running tundravm commit is on a remote branch, which an unpushed checkout answers
  differently: environment, not tests), `cli.py` −1, and the two deleted files take their
  10 missed statements with them. The SBOM's kernel-pin lines were never covered.
- **The waits saved 21 s, not 13 s.** `tundravm.instances` names `LAUNCH_GRACE`, `POLL`,
  `STOP_POLL` and `CONNECT_TIMEOUT` and `tests/cli/conftest.py` shortens them; the boot fake
  polls every 20 ms with 0.3 s and 0.5 s timeouts; the HTTP fixtures serve with a 10 ms
  poll. `test_determinism.py::test_the_converters_reproduce_their_outputs` (9.3 s, real
  disk images) is now a fifth of the run.

## Appendix A. Deletions

Paths are under `tests/`; a bare test name in the last column is in the same file.

| Test | Why | Remaining test or reason |
|---|---|---|
| **cli/test_build.py** | | |
| `test_an_unknown_profile_is_refused` | body-dup: same shared profile resolution and message | `cli/test_check_lock.py::test_an_unknown_profile_is_refused` |
| **cli/test_deploy.py** | | |
| `test_parse_volumes` | layer-dup: create form via the CLI; existing form in upgrade and status tests | `test_deploy_gcp_writes_a_receipt` |
| **cli/test_doctor.py** | | |
| `test_capability_render_pads_the_name` | trivial: full rendered rows already pin the padding | `test_a_ready_linux_host_reports_every_capability_in_the_plan_format` |
| **cli/test_fail_closed.py** | | |
| `test_the_guest_gate_mode_makes_tdx_evidence_simulated` | layer-dup: CLI test asserts verdict simulated and guest_mode simulated | `test_attest_reads_the_guest_gate_mode_over_the_session` |
| **cli/test_grammar.py** | | |
| `test_every_removed_verb_is_an_unknown_command` | same-path-param: 23 rows, one parser.error path; typo test covers the message | `test_the_parser_has_exactly_the_twelve_commands` |
| `test_help_lists_the_twelve_commands` | trivial: asserts only exit 0; the no-arguments test checks the listing | `test_no_arguments_prints_help_and_the_quickstart` |
| **cli/test_policy_export.py** | | |
| `test_from_measurements_matches_from_policy` | layer-dup: from_measurements equals from_policy, placeholder refused, asserted there | `lifecycle/test_lifecycle_api.py::test_measurements_policy_round_trips_into_a_tdxs_verifier` |
| `test_from_policy_picks_one_value_per_register` | body-dup: two MRTD values refused, firmware= picks one, asserted there | `lifecycle/test_lifecycle_api.py::test_measurements_policy_round_trips_into_a_tdxs_verifier` |
| `test_schema_1_policies_are_refused_with_a_re_export_hint` | body-dup: same read_policy refusal and measurements.json hint | `cli/test_attest.py::test_a_schema_1_policy_is_refused_with_a_re_export_hint` |
| **compiler/test_build_cache.py** | | |
| `test_cache_dir_returns_cache_dir` | trivial: isinstance and name; compiled install hook pins the directory artifact | `compiler/test_source_build_examples.py::test_install_steps_render_files_modes_and_directories` |
| `test_cache_file_custom_mode` | trivial: mode attribute; compiled install hook pins install -m 0600 | `compiler/test_source_build_examples.py::test_install_steps_render_files_modes_and_directories` |
| `test_cache_file_returns_cache_file` | trivial: isinstance and default 0755; compiled install hook pins default mode | `compiler/test_source_build_examples.py::test_install_steps_render_files_modes_and_directories` |
| `test_chroot_path_str` | helper-only: str() of internal path helper; full hook pins cd /build/tool | `compiler/test_source_build.py::test_hooks_copy_the_fetched_checkout` |
| `test_declare_returns_cache_decl` | trivial: isinstance, key and len of an internal declaration | `compiler/test_source_build_examples.py::test_install_steps_render_files_modes_and_directories` |
| `test_declare_sanitizes_slashes_in_key` | layer-dup: team/tool -> team_tool asserted through the compiled hook | `compiler/test_build_recipes.py::test_mounted_cache_key_fingerprints_what_the_build_produces` |
| `test_dest_path_str` | helper-only: str() of internal path helper; full hook pins $DESTDIR path | `compiler/test_source_build.py::test_hooks_copy_the_fetched_checkout` |
| `test_exact_trees_copy_the_whole_directory` | layer-dup: cp -a dir/. store and restore pinned; dotfiles test executes it | `compiler/test_source_build_examples.py::test_install_steps_render_files_modes_and_directories` |
| `test_path_types` | trivial: isinstance checks on internal helper return types | `compiler/test_source_build.py::test_hooks_copy_the_fetched_checkout` |
| `test_src_path_str` | helper-only: str() of internal path helper; full hook pins $BUILDROOT/build path | `compiler/test_source_build.py::test_hooks_copy_the_fetched_checkout` |
| `test_wrap_multiple_artifacts` | layer-dup: multi-artifact store/restore pinned byte for byte in compiled hook | `compiler/test_source_build_examples.py::test_install_steps_render_files_modes_and_directories` |
| `test_wrap_single_file` | layer-dup: substrings of wrap(); compiled hook pinned byte for byte | `compiler/test_source_build_examples.py::test_install_steps_render_files_modes_and_directories` |
| `test_wrap_structure_if_not_then_fi` | layer-dup: if-not/fi shape pinned byte for byte in compiled hook | `compiler/test_source_build_examples.py::test_install_steps_render_files_modes_and_directories` |
| **compiler/test_build_recipes.py** | | |
| `test_declarative_names_are_the_compiler_recipes` | trivial: re-export identity; payload kind=cargo shows Cargo is the compiler recipe | `test_lock_payload_records_the_recipe` |
| `test_recipe_builds_are_hashable` | trivial: equality and hash of Go builds asserted there | `test_recipes_copy_the_callers_mappings_and_sequences` |
| `test_script_builds_still_lower_to_script_build` | layer-dup: script Build -> ScriptBuild asserted by full spec equality there | `compiler/test_source_build.py::test_build_lowers_to_source_build_packages_and_hook` |
| **compiler/test_debloat.py** | | |
| `test_bake_report_contains_debloat_section` | dead-guard: pins the report.json debloat section nothing reads | dead guard: report.json, local in-process bake, per-variant directories |
| `test_debloat_default_is_enabled_and_deterministic` | layer-dup: explain_debloat through describe; default debloat golden-pinned in native tree | `unit/test_describe.py::test_summary_contains_key_strings` |
| `test_debloat_keep_paths_by_variant_lowers_to_profile_conditional_paths` | layer-dup: same recipe asserted on the emitted finalize; lowering in test_lower | `compiler/test_emit_mkosi.py::test_compile_debloat_profile_conditional_paths` |
| `test_debloat_paths_skip_for_profiles_excludes_from_effective` | layer-dup: DebloatConfig property; emitted finalize asserts the same split | `compiler/test_emit_mkosi.py::test_compile_debloat_profile_conditional_paths` |
| `test_debloat_variant_override_is_supported` | layer-dup: variant disabling debloat asserted on the emitted tree | `compiler/test_emit_mkosi.py::test_a_debloat_one_profile_changes_moves_out_of_the_shared_payload` |
| **compiler/test_determinism.py** | | |
| `test_finalize_ends_with_the_accounts_step_and_wrappers_convert` | golden-pinned: finalize tail, vhd/tar converters and the one Seed pinned | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_the_converters_spell_their_normalizations` | golden-pinned: substrings of golden-pinned scripts; the reproduce test runs them | `test_the_converters_reproduce_their_outputs` |
| `test_the_emitted_scripts_are_pinned` | golden-pinned: accounts step and both converters pinned byte for byte in native tree | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| **compiler/test_directory.py** | | |
| `test_order_is_deterministic` | body-dup: exact sorted path list asserted there | `test_exclude_globs_match_relative_paths_and_prune_dirs` |
| `test_preserves_modes_unless_mode_given` | body-dup: kept modes there; mode= override in test_mode_normalises_files_and_empty_directories | `test_keeps_modes_symlinks_and_empty_directories` |
| `test_scoped_to_variant` | layer-dup: variant-added items stay in their overlay: generic overlay behaviour | `compiler/test_profile_overlays.py::test_each_profile_resolves_to_the_shared_payload_plus_its_overlay` |
| **compiler/test_emit_mkosi.py** | | |
| `test_backports_composes_no_declarations` | trivial: Backports().items == () asserted there | `declarative/test_utils.py::test_backports_include_sid_false_drops_the_sid_source_and_pin` |
| `test_compile_cloud_wrapper` | golden-pinned: azure/gcp wrapper conf, converter, exec bit, repart pinned in native tree | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_compile_debloat_profile_conditional_unconditional_coexist` | body-dup: same recipe and script; the doc removal is golden-pinned too | `test_compile_debloat_profile_conditional_paths` |
| `test_compile_debloat_uses_dpkg_query` | golden-pinned: the same grep patterns pinned byte for byte in native postinst | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_compile_default_target` | golden-pinned: default.target link and minimal.target skeleton pinned in native tree | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_compile_efi_stub_postinst_hook` | layer-dup: render_script in postinst there; details in installs_from_the_image_root | `declarative/test_utils.py::test_efi_stub_hook_lands_in_the_postinst_script` |
| `test_compile_efi_stub_registered_in_postinst_phase` | layer-dup: postinst hook equals render_script, asserted there | `declarative/test_utils.py::test_efi_stub_hook_lands_in_the_postinst_script` |
| `test_compile_environment_key_value` | body-dup: Environment= in the payload conf asserted there | `test_compile_user_epoch_variable_does_not_change_the_source_date` |
| `test_compile_generates_debloat_finalize` | golden-pinned: debloat rm -rf and dpkg-query lines pinned in native tree | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_compile_generates_extra_tree` | golden-pinned: extra files and machine-id pinned; Template in test_lower template test | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_compile_generates_postinst_with_users` | golden-pinned: useradd --system lines pinned in native and surge postinst | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_compile_generates_service_units` | body-dup: substring subset of the full-bytes service unit test | `test_compile_generates_service_unit_all_sections` |
| `test_compile_is_deterministic` | golden-pinned: repeat-compile equality; goldens recompile identically on every run | `compiler/test_emitted_modes.py::test_emitted_tree_is_identical_under_every_umask` |
| `test_compile_kernel_build_script_with_user_hooks` | layer-dup: 10-kernel.sh beside 50-build.sh asserted there | `lifecycle/test_kernel_fetch.py::test_the_payload_builds_the_kernel_and_the_builds_once_for_every_variant` |
| `test_compile_kernel_custom_source_repo` | layer-dup: a custom kernel Git source is resolved and locked there | `lifecycle/test_kernel_fetch.py::test_lock_pins_the_kernel_ref` |
| `test_compile_overlay_variant_is_a_profile` | golden-pinned: profile mkosi.conf (ImageId=%i-dev, Packages) pinned; resolution in profile_overlays | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_compile_service_enablement_uses_mkosi_chroot` | golden-pinned: mkosi-chroot systemctl enable lines pinned in native postinst | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_compile_strip_image_version_auto_called_when_reproducible` | layer-dup: epoch default on asserted there | `declarative/test_lower.py::test_strip_os_release_overrides_the_epoch_default` |
| `test_compile_strip_image_version_can_be_disabled` | layer-dup: strip_os_release=False asserted there | `declarative/test_lower.py::test_strip_os_release_overrides_the_epoch_default` |
| `test_compile_strip_image_version_finalize_hook` | golden-pinned: sed IMAGE_VERSION os-release line pinned in native finalize | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_compile_strip_image_version_not_called_when_not_reproducible` | layer-dup: epoch=None default off asserted there | `declarative/test_lower.py::test_strip_os_release_overrides_the_epoch_default` |
| `test_compile_strip_image_version_registered_in_finalize_phase` | layer-dup: finalize-phase strip hook asserted there | `declarative/test_lower.py::test_strip_os_release_overrides_the_epoch_default` |
| **compiler/test_emitted_modes.py** | | |
| `test_surge_matches_its_golden_tree_under_any_umask` | layer-dup: umask independence there; surge bytes in test_surge_golden | `test_emitted_tree_is_identical_under_every_umask` |
| **compiler/test_execution_gate.py** | | |
| `test_the_gate_is_an_enabled_root_oneshot_in_the_payload` | golden-pinned: tdx-evidence unit, script bytes, exec bit, enable line pinned | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| **compiler/test_keys_disks_secrets.py** | | |
| `test_disk_encryption_adds_build_hook` | layer-dup: pinned disk-encryption hook there; cmd and dest in the surge marker | `compiler/test_source_build_examples.py::test_every_declaration_that_builds_from_source_is_a_source_build` |
| `test_disk_encryption_installs_cryptsetup` | golden-pinned: cryptsetup in the surge payload Packages, pinned byte for byte | `compiler/test_surge_golden.py::test_surge_image_matches_the_committed_tree` |
| `test_init_generates_a_unit_per_runtime_init_step` | golden-pinned: step scripts, step units, runtime-init.service and packages pinned in surge | `compiler/test_surge_golden.py::test_surge_image_matches_the_committed_tree` |
| `test_init_without_init_scripts_does_not_generate_runtime_init` | golden-pinned: the native tree's file set has no runtime-init files | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_key_generation_adds_build_hook` | layer-dup: pinned key-generation hook there; cmd and dest in the surge marker | `compiler/test_source_build_examples.py::test_every_declaration_that_builds_from_source_is_a_source_build` |
| `test_secret_delivery_adds_build_hook` | layer-dup: pinned secret-delivery hook there; cmd and dest in the surge marker | `compiler/test_source_build_examples.py::test_every_declaration_that_builds_from_source_is_a_source_build` |
| `test_secret_delivery_does_not_add_runtime_packages` | dead-guard: guards a removed python3 dependency | dead guard: python3 secret-delivery runtime (now a Go binary; surge golden has no python3) |
| `test_secrets_not_stored_on_a_disk_do_not_wait_for_it` | golden-pinned: surge runtime-init-secrets.service has no disks dependency, pinned | `compiler/test_surge_golden.py::test_surge_image_matches_the_committed_tree` |
| **compiler/test_needs.py** | | |
| `test_needs_no_duplicate_if_already_present` | layer-dup: the already-present runtime-init row of that table | `declarative/test_lower.py::test_inject_after_init` |
| `test_needs_nothing_adds_no_dependency` | layer-dup: needs=() leaves no runtime-init, asserted there | `declarative/test_lower.py::test_service_needs_picks_the_runtime_init_units_it_waits_for` |
| `test_needs_runtime_init_prepends_init_when_steps_present` | layer-dup: app.service == UNIT_AFTER_INIT there; inject_after_init rows too | `declarative/test_lower.py::test_lowered_recipe_compiles_to_the_recorded_tree` |
| `test_runtime_init_runs_every_step_in_topological_order` | body-dup: aggregate Requires order early keys disks hello asserted there | `test_step_units_require_their_direct_dependencies` |
| `test_service_needs_a_builtin_step` | layer-dup: needs=(disk.ready,) After/Requires asserted there | `declarative/test_lower.py::test_service_needs_picks_the_runtime_init_units_it_waits_for` |
| `test_service_waits_for_all_of_runtime_init_by_default` | layer-dup: the default runtime-init.service wait asserted there | `declarative/test_lower.py::test_service_needs_picks_the_runtime_init_units_it_waits_for` |
| **compiler/test_recompile.py** | | |
| `test_bake_after_compile_across_variants` | dead-guard: asserts BakeResult profile keys after a preview emit | dead guard: bake results (BakeResult.profiles) of in-process bake_image |
| `test_compile_of_one_variant_writes_only_its_profile` | body-dup: re-emit drops what the new tree lacks: the same path | `test_recompile_removes_stale_variant_files` |
| **compiler/test_service_defaults.py** | | |
| `test_debloat_keeps_timers_target` | golden-pinned: timers.target is in the pinned debloat unit whitelist | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_remain_after_exit_renders_on_a_service` | layer-dup: Type=oneshot RemainAfterExit=yes ExecStart order pinned in full bytes there | `compiler/test_unit_shapes.py::test_boot_task_is_an_enabled_oneshot` |
| **compiler/test_source_build.py** | | |
| `test_cli_offline_build_without_a_lock_fails_clearly` | layer-dup: offline build without a lock refused there, on an Image project | `cli/test_build.py::test_offline_without_a_lock_is_refused` |
| `test_disk_encryption_hook_is_a_source_build` | layer-dup: pinned hook there; setup line in test_disk_encryption_registers_init_script | `compiler/test_source_build_examples.py::test_every_declaration_that_builds_from_source_is_a_source_build` |
| `test_existing_digest_is_unchanged` | golden-pinned: Recipe-file digest pin of a recipe without source builds | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_key_generation_hook_is_a_source_build` | layer-dup: pinned hook there; setup line in test_key_generation_registers_init_script | `compiler/test_source_build_examples.py::test_every_declaration_that_builds_from_source_is_a_source_build` |
| `test_secret_delivery_hook_is_a_source_build` | layer-dup: pinned hook there; setup line in test_secret_delivery_registers_init_script | `compiler/test_source_build_examples.py::test_every_declaration_that_builds_from_source_is_a_source_build` |
| `test_unpinned_and_pinned_rendering` | layer-dup: unpinned, pinned and moved-pin render asserted on the compiled hook | `test_hooks_copy_the_fetched_checkout` |
| `test_unpinned_compile_emits_marker` | body-dup: the exact unpinned hook asserted there | `test_hooks_copy_the_fetched_checkout` |
| `test_variant_inherits_common_source_builds` | layer-dup: builds stay in the payload for every variant; wrapper rule in test_lower | `lifecycle/test_kernel_fetch.py::test_the_payload_builds_the_kernel_and_the_builds_once_for_every_variant` |
| **compiler/test_source_build_examples.py** | | |
| `test_install_modes_directories_and_payload` | body-dup: modes and tree copy pinned byte for byte; tree record in recorded digest | `test_install_steps_render_files_modes_and_directories` |
| `test_unpinned_app_builds_fail_naming_lock_and_build` | golden-pinned: taiko-client unpinned line pinned in surge build script; hint kept elsewhere | `compiler/test_surge_golden.py::test_surge_image_matches_the_committed_tree` |
| **compiler/test_tdxs_runtime_init.py** | | |
| `test_build_packages_lower_to_build_packages` | golden-pinned: Package(golang, role=build) is in the recorded tree | `declarative/test_lower.py::test_lowered_recipe_compiles_to_the_recorded_tree` |
| `test_tdxs_adds_build_hook` | layer-dup: pinned tdxs hook there; go flags in the surge marker | `compiler/test_source_build_examples.py::test_every_declaration_that_builds_from_source_is_a_source_build` |
| `test_tdxs_declares_build_packages` | golden-pinned: 30-tdxs.conf BuildPackages pinned in the surge tree | `compiler/test_surge_golden.py::test_surge_image_matches_the_committed_tree` |
| `test_tdxs_generates_config_yaml_and_units` | golden-pinned: tdxs config, service, socket, udev rule, postinst lines pinned in surge | `compiler/test_surge_golden.py::test_surge_image_matches_the_committed_tree` |
| `test_tdxs_renders_issuer_and_validator_types` | body-dup: types pinned there; gcp validator in test_tdxs_platform_validator_verification_flags | `declarative/test_utils.py::test_tdxs_issuer_and_validator_render_the_config` |
| `test_tdxs_waits_for_the_steps_it_needs` | body-dup: needs=(key.ready,) After/Requires asserted there | `declarative/test_utils.py::test_tdxs_needs_orders_its_units_after_those_steps` |
| **compiler/test_unit_render.py** | | |
| `test_runtime_init_step_unit_renders_through_the_typed_table` | golden-pinned: surge runtime-init-disks.service is exactly these bytes | `compiler/test_surge_golden.py::test_surge_image_matches_the_committed_tree` |
| **compiler/test_unit_shapes.py** | | |
| `test_every_renders_monotonic_timers` | body-dup: Every timer full bytes there; first= in the declarative schedules test | `test_example_03_health_check_runs_only_from_its_timer` |
| **declarative/test_azure_target.py** | | |
| `test_azure_glue_lists_its_package_in_its_own_concern_file` | golden-pinned: 40-cloud-glue.conf pinned byte for byte | `compiler/test_native_tree.py::test_payload_subimage_includes_the_payload_directory` |
| `test_azure_provisioning_script_content` | golden-pinned: substring checks of a constant the golden pins whole | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_azure_variant_builds_the_vhd_wrapper` | golden-pinned: wrapper conf, ESP copy and converter pinned there | `compiler/test_native_tree.py::test_wrappers_depend_on_the_payload_and_convert_it` |
| `test_azure_variant_emits_provisioning_script` | golden-pinned: golden pins script bytes and exec bit | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_azure_variant_emits_service_unit` | golden-pinned: golden pins the unit; network-setup branch test stays | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_azure_variant_enables_service_in_postinst` | golden-pinned: golden postinst pins the enable line | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_azure_variant_symlinks_to_minimal_target` | golden-pinned: golden postinst pins the minimal.target.wants link | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_the_payload_of_every_variant_carries_the_azure_glue` | golden-pinned: golden payload carries the glue; one payload for every variant | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| **declarative/test_devtools_fragment.py** | | |
| `test_devtools_postinst_configures_dropbear` | golden-pinned: surge dev postinst pins the dropbear sed | `compiler/test_surge_golden.py::test_surge_image_matches_the_committed_tree` |
| `test_devtools_postinst_configures_openssh` | golden-pinned: surge dev postinst pins the sshd lines | `compiler/test_surge_golden.py::test_surge_image_matches_the_committed_tree` |
| `test_devtools_postinst_sets_root_password` | golden-pinned: surge dev postinst pins the login hook | `compiler/test_surge_golden.py::test_surge_image_matches_the_committed_tree` |
| `test_devtools_variant_adds_debug_packages` | golden-pinned: surge dev profile conf pins them; declared-set test stays | `compiler/test_surge_golden.py::test_surge_image_matches_the_committed_tree` |
| `test_devtools_variant_bash_completion_in_debloat_skip` | layer-dup: same keep_paths_by_variant PROFILES guard asserted there | `compiler/test_emit_mkosi.py::test_compile_debloat_profile_conditional_paths` |
| `test_devtools_variant_does_not_affect_default_variant` | golden-pinned: surge production payload pins no devtools content | `compiler/test_surge_golden.py::test_surge_image_matches_the_committed_tree` |
| `test_devtools_variant_registers_postinst_password_hook` | body-dup: one substring of the root-password test | `compiler/test_surge_golden.py::test_surge_image_matches_the_committed_tree` |
| **declarative/test_dropin_hardening.py** | | |
| `test_none_removes_the_sandbox` | body-dup: the strict-under/none-patch matrix cell | `test_a_preset_replaces_the_units_sandbox` |
| **declarative/test_gcp_target.py** | | |
| `test_gce_disk_naming_rules_content` | golden-pinned: substring checks of a constant the golden pins | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_gcp_hosts_content` | golden-pinned: substring checks of a constant the golden pins | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_gcp_resolv_conf_content` | golden-pinned: substring checks of a constant the golden pins | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_gcp_variant_adds_udev_package` | golden-pinned: udev in golden 10-platform.conf | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_gcp_variant_builds_the_tar_wrapper` | golden-pinned: wrapper conf and tar flags pinned there | `compiler/test_native_tree.py::test_wrappers_depend_on_the_payload_and_convert_it` |
| `test_gcp_variant_emits_hosts_file` | golden-pinned: golden pins /etc/hosts | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_gcp_variant_emits_nvme_id_script` | golden-pinned: golden pins bytes and exec bit | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_gcp_variant_emits_udev_rules` | golden-pinned: golden pins the rules file | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_google_nvme_id_content` | golden-pinned: substring checks of a constant the golden pins | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_the_default_variant_boots_the_same_payload_with_the_glue` | layer-dup: shared payload whatever the selection | `compiler/test_native_tree.py::test_selected_variants_do_not_change_the_payload` |
| **declarative/test_kernel_modules.py** | | |
| `test_a_multi_output_variant_keeps_the_union` | golden-pinned: golden finalize pins the qemu+azure+gcp union | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_the_payload_keeps_the_modules_of_every_target_whatever_is_compiled` | layer-dup: payload identical whatever the selection | `compiler/test_native_tree.py::test_selected_variants_do_not_change_the_payload` |
| **declarative/test_load.py** | | |
| `test_cli_reports_resolution_errors` | layer-dup: check exit code on errors; Recipe-file CLI path | `cli/test_check_lock.py::test_errors_exit_1_in_text_and_json` |
| `test_load_discovers_like_the_cli_and_rejects_other_values` | dead-guard: Recipe discovery; tests/unit/test_recipe_loading.py has each case | dead guard: Recipe-file loading beyond one test |
| `test_load_image_lowers_and_wires_the_backend` | layer-dup: bound backend wiring asserted on an Image file | `image/test_prover.py::test_tundravm_backend_and_an_explicit_backend_win` |
| `test_load_is_load_recipe` | trivial: asserts an alias | no guarantee: tests a helper |
| `test_load_returns_the_recipe` | dead-guard: Recipe discovery; tests/unit/test_recipe_loading.py covers it too | dead guard: Recipe-file loading beyond one test |
| **declarative/test_locations.py** | | |
| `test_a_declaration_records_its_absolute_line` | body-dup: absolute declared_at asserted there | `test_edits_keep_the_declaration_line_and_record_their_own` |
| `test_provenance_carries_origin_locations` | layer-dup: same origins via provenance(); show JSON in tests/cli/test_show.py | `test_edits_keep_the_declaration_line_and_record_their_own` |
| **declarative/test_lower.py** | | |
| `test_build_sources_setting_mounts_per_variant` | layer-dup: BuildSources reaching the tree asserted there | `compiler/test_tdxs_runtime_init.py::test_build_sources_setting_mounts_build_source` |
| `test_lowering_refuses_settings_summary_cannot_describe` | body-dup: same three settings and codes | `compiler/test_native_tree.py::test_settings_that_break_the_summary_or_leak_the_host_are_refused` |
| `test_mkosi_fields_and_policy_reach_the_compiler` | layer-dup: version script, verity, network default each pinned by compiler tests | `compiler/test_emit_mkosi.py::test_compile_version_script` |
| `test_runtime_init_runs_each_step_as_its_own_unit` | layer-dup: per-step units, Requires, scripts asserted there | `compiler/test_needs.py::test_step_units_require_their_direct_dependencies` |
| `test_runtime_tools_default_and_key_handoff` | layer-dup: encryption_key handoff in disk-setup.yaml asserted there | `compiler/test_keys_disks_secrets.py::test_disk_encryption_supports_multiple_disks` |
| `test_service_lowers_to_the_generated_unit` | layer-dup: same Service-to-spec path, asserted rendered | `declarative/test_service_fields.py::test_each_service_field_renders_its_directive` |
| `test_service_needs_picks_the_runtime_init_units_it_waits_for` | layer-dup: needs default, none and step cases in test_needs | `compiler/test_needs.py::test_service_needs_a_builtin_step` |
| `test_variants_with_their_own_settings_cannot_share_a_tree` | body-dup: same refusal, wider there | `declarative/test_lowering_completeness.py::test_variants_with_their_own_settings_or_kernel_are_refused` |
| **declarative/test_lowering_completeness.py** | | |
| `test_one_secrets_keeps_the_runtime_tools_paths` | layer-dup: default secrets.yaml path read there | `compiler/test_keys_disks_secrets.py::test_secret_delivery_writes_config_from_declared_secrets` |
| `test_policy_has_no_inert_integrity_switch` | dead-guard: pins a field list against a removed switch | dead guard: Policy integrity switch removed |
| `test_variant_leaving_a_cloud_target_is_the_qemu_disk_alone` | layer-dup: wrapper not overlay, glue for every target | `declarative/test_lower.py::test_a_variant_that_only_names_outputs_is_a_wrapper_not_an_overlay` |
| **declarative/test_model.py** | | |
| `test_lists_are_frozen_into_tuples` | trivial: list-to-tuple freezing asserted there | `test_packages_is_a_named_bundle_of_packages` |
| `test_records_are_frozen_and_slotted` | trivial: frozen-ness asserted there | `test_packages_is_a_named_bundle_of_packages` |
| **declarative/test_no_side_effects.py** | | |
| `test_explicit_output_operations_create_files` | dead-guard: bake(Backend('local')) writes build/default/output | dead guard: in-process bake into per-variant output directories |
| **declarative/test_resolve.py** | | |
| `test_builtin_steps_carry_their_dependencies` | layer-dup: keys->disks->secrets edges asserted there | `testing_toolkit/test_image.py::test_init_steps_follow_the_graph_with_before_edges` |
| `test_chained_variants_remove_and_edit_any_kind` | body-dup: edit/remove by identity; the ancestry test covers the chain | `test_changes_edit_and_remove_by_identity` |
| `test_service_and_template_identities` | layer-dup: profile edits by reference; identities in the refs test | `image/test_components_and_profiles.py::test_a_profile_adds_and_changes_by_reference` |
| `test_several_targets_inherit_and_stay_consistent` | body-dup: target-inconsistent there; output inheritance in the ancestry test | `test_hook_unit_and_target_checks` |
| **declarative/test_secrets.py** | | |
| `test_secret_declaration_supports_schema_and_multiple_targets` | trivial: echoes constructor arguments | `declarative/test_model.py::test_valid_declarations_construct` |
| `test_secret_targets_support_file_and_global_env` | layer-dup: file and global env manifest targets asserted there | `compiler/test_keys_disks_secrets.py::test_secret_delivery_writes_config_from_declared_secrets` |
| **declarative/test_ssh_access.py** | | |
| `test_baked_keys_give_key_only_root_login_and_a_firewall_opening` | golden-pinned: golden dev profile pins sshd config and keys; port collection in test_network | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| `test_host_keys_are_made_at_boot_before_sshd` | golden-pinned: golden pins ssh-host-keys.service and the needs drop-in | `compiler/test_native_tree.py::test_fixture_compiles_to_the_committed_tree` |
| **declarative/test_tee_base.py** | | |
| `test_defaults_declare_time_journal_sysctl_dns_and_masks` | golden-pinned: surge golden pins every TeeBase file, package and mask | `image/test_starter.py::test_the_tee_baseline_is_there` |
| **declarative/test_typed_mkosi.py** | | |
| `test_a_kernel_with_a_config_passes_no_host_variable` | layer-dup: no bare Environment= in any example, nethermind_base included | `compiler/test_native_tree.py::test_every_tree_sets_the_reproducibility_knobs` |
| `test_defaults_write_the_opinionated_keys` | golden-pinned: golden pins the default keys | `compiler/test_native_tree.py::test_main_image_is_the_qemu_disk_with_every_reproducibility_setting` |
| `test_typed_fields_write_their_mkosi_keys` | body-dup: tree digest equals the Settings tree | `test_typed_fields_write_the_tree_the_settings_wrote` |
| **declarative/test_unit_shapes.py** | | |
| `test_shapes_have_refs_keyed_by_unit` | layer-dup: Task and Mount refs edited and removed there | `compiler/test_unit_shapes.py::test_variant_overlay_adds_edits_and_removes_shapes` |
| **declarative/test_utils.py** | | |
| `test_backports_pins_mirror_and_release` | layer-dup: mirror, release and pins asserted there | `compiler/test_emit_mkosi.py::test_compile_backports_sandbox_sources_take_the_fragments_fields` |
| `test_composite_subclass_works_as_a_fragment` | layer-dup: plain-dataclass Composite lowered there | `declarative/test_authoring_helpers.py::test_go_binary_lowers_like_the_hand_written_build` |
| `test_tdxs_accounts_use_the_account_prelude` | golden-pinned: surge postinst lines 4-5 pin it | `compiler/test_surge_golden.py::test_surge_image_matches_the_committed_tree` |
| `test_tdxs_issuer_and_validator_render_the_config` | layer-dup: issuer/validator config keys asserted in compiler tests | `compiler/test_tdxs_runtime_init.py::test_tdxs_validator_config_supports_expected_measurements` |
| `test_tdxs_needs_orders_its_units_after_those_steps` | layer-dup: same needs ordering; no-init case there too | `compiler/test_tdxs_runtime_init.py::test_tdxs_waits_for_the_steps_it_needs` |
| `test_utils_are_fragments_configured_by_their_fields` | trivial: repr, signature, isinstance and equality trivia | no guarantee: tests a helper |
| **image/test_components_and_profiles.py** | | |
| `test_a_component_keeps_its_class_name_in_provenance` | dead-guard: fragment class name served the removed explore and why verbs | dead guard: explore/why provenance printing |
| **image/test_service.py** | | |
| `test_an_endpoint_carries_the_readiness_it_was_exported_with` | body-dup: endpoint URL in argv and Requires on the ready unit | `image/test_components_and_profiles.py::test_exports_resolve_to_the_units_behind_them` |
| **image/test_starter.py** | | |
| `test_expose_opens_udp_too` | body-dup: misnamed: asserts a TCP accept rule only | `test_the_firewall_opens_the_exposed_port_and_the_management_ingress` |
| **lifecycle/test_bake_artifacts.py** | | |
| `test_bake_respects_global_and_variant_output_targets` | layer-dup: per-variant outputs; file names pinned by cli test_targets_add_the_cloud_disks | `lifecycle/test_lifecycle_api.py::test_bake_writes_the_outputs_each_variant_declares` |
| `test_verify_artifact_passes_for_untouched_bytes` | body-dup: measure() verifies untouched bytes first; two-line pass case | `lifecycle/test_measure.py::test_measure_of_a_real_uki_round_trips_through_the_policy` |
| **lifecycle/test_bake_progress.py** | | |
| `test_bake_forwards_logger_records_only_while_attached` | layer-dup: attach-only forwarding tested on the logger; ops feed report.json | `test_structured_logger_forwards_warnings` |
| `test_bake_without_reporter_records_digests_and_durations` | dead-guard: digests covered by test_bake_writes_the_outputs_each_variant_declares | dead guard: BakeResult durations (builds.py times itself) |
| `test_local_backend_maps_streamed_failure_to_backend_error` | body-dup: same fake_mkosi failure mapped to BackendExecutionError | `lifecycle/test_local_linux_backend.py::test_local_execute_reports_mkosis_failure_with_the_log_tail` |
| `test_public_bake_reports_progress_lines` | body-dup: same bake(progress=) callback, asserts progress lines and sha256 | `lifecycle/test_lifecycle_api.py::test_bake_writes_the_outputs_each_variant_declares` |
| **lifecycle/test_bake_report.py** | | |
| `test_bake_report_schema_contains_observability_fields` | dead-guard: pins report.json keys no reader consumes | dead guard: report.json (written by _bake.py, read by nothing) |
| `test_structured_logs_include_profile_phase_module_and_builder` | dead-guard: reads report.json logs, a vestigial staging file | dead guard: report.json logs section |
| **lifecycle/test_diff.py** | | |
| `test_diff_reports_new_package` | body-dup: same compile, write, +extra diff, tree-untouched assertions | `test_diff_leaves_the_tree_alone_until_written` |
| **lifecycle/test_lifecycle_api.py** | | |
| `test_bake_refuses_a_stale_lock` | body-dup: stale refusal; also in test_bake_one_variant_against_a_lock_of_every_variant | `lifecycle/test_lock_drift.py::test_frozen_bake_error_carries_drift` |
| **lifecycle/test_lima_backend.py** | | |
| `test_lima_mount_plan_is_deterministic` | trivial: calls a pure function twice | `test_lima_mount_plan_single_mount` |
| **lifecycle/test_lock_drift.py** | | |
| `test_section_digests_cover_top_level_and_profile_sections` | layer-dup: section naming asserted on a real lock there | `test_lockfile_records_section_digests_and_keeps_whole_digest` |
| **lifecycle/test_lock_failures.py** | | |
| `test_cli_lock_success_path_is_unchanged` | layer-dup: CLI lock success on a Recipe file; pins via test_lock_attempts_every_source | `cli/test_check_lock.py::test_lock_writes_then_check_reports_drift` |
| **lifecycle/test_lockfile.py** | | |
| `test_bake_frozen_fails_when_lock_is_stale` | body-dup: third copy of the stale-lock refusal | `lifecycle/test_lock_drift.py::test_frozen_bake_error_carries_drift` |
| `test_bake_frozen_succeeds_with_current_lock` | body-dup: frozen bake success asserted there with more fields | `lifecycle/test_lifecycle_api.py::test_bake_writes_the_outputs_each_variant_declares` |
| `test_lockfile_roundtrip_parser_serializer` | body-dup: same build/serialize/parse equality | `lifecycle/test_lock_drift.py::test_lockfile_round_trips_its_toolchain` |
| `test_offline_build_fails_when_lock_missing` | layer-dup: same CLI refusal, through a Recipe file | `cli/test_build.py::test_offline_without_a_lock_is_refused` |
| **lifecycle/test_measure.py** | | |
| `test_measure_verification_reports_mismatched_registers` | layer-dup: Measurements.verify mismatch tested on a direct value | `lifecycle/test_lifecycle_api.py::test_measurements_to_dict_and_verify` |
| `test_the_command_line_and_stub_version_reach_rtmr2` | layer-dup: cmdline and stub version feed RTMR2, asserted at replay | `measure/test_replay.py::test_uki_sections_feed_rtmr2_only_for_a_recent_stub` |
| **lifecycle/test_measure_provenance.py** | | |
| `test_a_changed_artifact_is_refused_before_measuring` | body-dup: same swapped-bytes refusal by measure() | `lifecycle/test_bake_artifacts.py::test_changed_artifact_is_refused_by_measure` |
| **lifecycle/test_reproducible_bake.py** | | |
| `test_repeated_bakes_with_same_recipe_have_stable_artifact_digests` | layer-dup: fake-mkosi determinism; build --reproduce is the guarantee | `cli/test_build.py::test_reproduce_records_a_match` |
| **lifecycle/test_sbom.py** | | |
| `test_artifact_without_lock_lists_no_sources` | dead-guard: lockless SBOM only reachable from the removed verb | dead guard: sbom verb without a lock (builds.py always passes one) |
| `test_cyclonedx_document_structure` | dead-guard: no command renders CycloneDX since the sbom verb went | dead guard: sbom verb CycloneDX format (build writes spdx-json only) |
| `test_lowered_sbom_lists_declared_sources_pinned_by_the_lock` | dead-guard: builds.py only documents built artifacts | dead guard: sbom of a Lowered image (sbom verb without a bake) |
| `test_text_and_markdown_tables` | dead-guard: no command renders text or markdown SBOMs | dead guard: sbom verb text/markdown formats |
| **lifecycle/test_sbom_embedded.py** | | |
| `test_a_binary_recording_nothing_is_only_noted` | dead-guard: note path only via binaries= | dead guard: sbom(binaries=) of the sbom verb; builds.py passes none |
| `test_a_stale_binary_is_unbound` | dead-guard: unbound-binary path only via binaries= | dead guard: sbom(binaries=) of the sbom verb; builds.py passes none |
| `test_a_uki_without_a_readable_initrd_records_no_installed_digests` | layer-dup: unreadable initrd yields None, tested at the reader | `lifecycle/test_initrd_digests.py::test_unreadable_initrds_record_nothing` |
| `test_an_unrelated_binary_found_by_file_name_is_unbound` | dead-guard: unbound-binary path only via binaries= | dead guard: sbom(binaries=) of the sbom verb; builds.py passes none |
| `test_cargo_auditable_section_lists_runtime_crates` | dead-guard: cargo auditable parser only reached via binaries= | dead guard: sbom(binaries=) of the sbom verb; builds.py passes none |
| `test_go_build_information_lists_modules_with_replacements` | dead-guard: Go buildinfo parser only reached via binaries= | dead guard: sbom(binaries=) of the sbom verb; builds.py passes none |
| `test_go_module_purl_keeps_the_module_path` | dead-guard: embedded purls only via binaries= | dead guard: sbom(binaries=) of the sbom verb; builds.py passes none |
| `test_nothing_recorded_leaves_every_binary_unbound` | dead-guard: unbound-binary path only via binaries= | dead guard: sbom(binaries=) of the sbom verb; builds.py passes none |
| `test_sbom_nests_each_binarys_dependencies_under_its_build` | dead-guard: embedded components only via binaries= | dead guard: sbom(binaries=) of the sbom verb; builds.py passes none |
| **lint/test_check_rules.py** | | |
| `test_aarch64_qemu_target_is_unsupported` | message-pin: sibling asserts the qemu finding on aarch64; only hint text extra | `test_x86_64_is_fine_and_aarch64_always_builds_the_qemu_disk` |
| `test_bake_refuses_error_level_findings` | layer-dup: bake raises LintError E_LINT on file-path-relative there too | `lifecycle/test_bake_progress.py::test_bake_lint_failure_emits_failed_phase_and_done` |
| `test_boot_step_cycle_names_the_steps` | layer-dup: resolve test asserts step-cycle with the same cycle message | `declarative/test_resolve.py::test_boot_step_references_and_cycles` |
| `test_bootable_setting_takes_only_no` | body-dup: same 'writes Bootable= itself' refusal | `compiler/test_emit_mkosi.py::test_compile_not_bootable_writes_a_disk_without_uki` |
| `test_clean_recipe_bakes` | layer-dup: public bake of a clean recipe yields the default artifact | `lifecycle/test_bake_progress.py::test_public_bake_reports_progress_lines` |
| `test_clean_recipe_has_no_findings` | body-dup: baseline report()==[] is the merged table's no-Debloat row | `test_debloat_settings_that_report_nothing` |
| `test_cli_clean_recipe` | dead-guard: Recipe file through check; Image-file test covers clean exit 0 | `cli/test_check_lock.py::test_a_clean_image_checks_clean_and_says_there_is_no_lock` |
| `test_cli_error_exit_and_json` | dead-guard: same exit 1, text line and tundravm.check/1 JSON assertions | `cli/test_check_lock.py::test_errors_exit_1_in_text_and_json` |
| `test_cli_warnings_are_reported_without_failing` | dead-guard: starter check exits 0 with two source-unpinned warnings (verified) | `cli/test_check_lock.py::test_a_clean_image_checks_clean_and_says_there_is_no_lock` |
| `test_secret_requires_a_delivery_target` | body-dup: identical Secret(targets=()) ValidationError | `declarative/test_secrets.py::test_secret_delivery_rejects_unnamed_or_targetless_secrets` |
| `test_unknown_step_references_are_errors` | layer-dup: step-undefined there; needs-undefined in test_resolve::test_needs_names_declared_steps | `declarative/test_resolve.py::test_boot_step_references_and_cycles` |
| `test_variant_cannot_drop_an_inherited_cloud_target` | layer-dup: same target-inconsistent rule on a child narrowing inherited targets | `declarative/test_resolve.py::test_several_targets_inherit_and_stay_consistent` |
| **lint/test_disk_device.py** | | |
| `test_a_disk_is_never_formatted_by_default` | trivial: asserts a default; the never-format row proves it behaviourally | `test_a_stable_device_or_no_formatting_is_clean` |
| `test_formatting_without_a_device_is_an_error` | same-path-param: every format other than never takes one branch; error asserted there | `test_storage_safety_warn_demotes_it` |
| **lint/test_secret_rules.py** | | |
| `test_allow_secret_exempts_service_and_unit_alike` | body-dup: per-declaration exemption of Service and Unit already asserted | `test_allow_secret_silences_each_declaration` |
| **lint/test_unit_lint.py** | | |
| `test_style_and_correctness_codes_are_reported` | body-dup: all four codes are rows of the table | `test_each_code_fires_on_raw_unit_text` |
| **measure/test_policy_v2.py** | | |
| `test_a_register_matches_any_accepted_value_and_names_its_label` | layer-dup: label matching asserted through attest | `cli/test_attest.py::test_a_register_matches_any_accepted_value_and_reports_its_label` |
| `test_a_schema_1_policy_is_refused_with_a_re_export_hint` | body-dup: third schema-1 refusal (also in test_attest) | `cli/test_policy_export.py::test_schema_1_policies_are_refused_with_a_re_export_hint` |
| `test_every_covered_register_is_accepted_or_unverifiable` | body-dup: neither/both there; empty list in test_labelled_values_keep_their_labels | `cli/test_policy_export.py::test_export_rejects_uncovered_registers_and_unknown_schemes` |
| `test_firmware_and_label_pick_the_values_for_the_validator` | layer-dup: firmware= pick and rendered config asserted there | `cli/test_policy_export.py::test_from_policy_picks_one_value_per_register` |
| `test_mrtd_and_rtmr0_of_different_firmwares_do_not_pair` | layer-dup: same name, same firmware pairing rule | `cli/test_attest.py::test_mrtd_and_rtmr0_of_different_firmwares_do_not_pair` |
| `test_the_validator_gets_one_value_per_register` | layer-dup: multi-value refusal through Tdxs.from_policy | `cli/test_policy_export.py::test_from_policy_picks_one_value_per_register` |
| **test_toolchain.py** | | |
| `test_the_installed_mkosi_is_the_pinned_commit` | body-dup: local backend reports installed_mkosi() == PINNED | `test_backends_report_the_mkosi_they_run` |
| **testing_toolkit/test_toolkit_helpers.py** | | |
| `test_assert_clean_warnings_allow_fails_on_errors_only` | body-dup: both assertions repeat it; strict= refusal guards a removed kwarg | `test_assert_clean_fails_on_warnings_for_every_input` |
| `test_assert_diagnostic_returns_the_match` | body-dup: same code/severity/subject match on service-user-missing | `testing_toolkit/test_image.py::test_lint_needs_no_compile_and_feeds_the_assertions` |
| `test_compile_tree_defaults_to_a_fresh_temp_dir` | helper-only: compile_tree is documented as for testing tundravm itself | no guarantee: tests a helper |
| `test_compile_tree_selects_variants` | helper-only: fixture test asserts variant selection and conf(variant=) | `testing_toolkit/test_pytest_plugin.py::test_compiled_factory_uses_fresh_dirs` |
| `test_fake_fragment_checks_run_per_variant` | layer-dup: fragment checks run per variant there | `declarative/test_resolve.py::test_fragment_requires_and_checks` |
| `test_fake_fragment_cycle_is_diagnosed` | layer-dup: same step-cycle message | `declarative/test_resolve.py::test_boot_step_references_and_cycles` |
| `test_fake_fragment_lands_only_in_its_variant` | helper-only: fake_fragment has one user; variant add semantics tested elsewhere | no guarantee: tests a helper |
| `test_fake_fragment_requires_a_fragment_by_name` | helper-only: requires by name, satisfied by include= | `testing_toolkit/test_image.py::test_include_satisfies_requires` |
| `test_fake_fragment_requires_other_fragments` | helper-only: fragment-requires-missing and include= satisfying it | `testing_toolkit/test_image.py::test_include_satisfies_requires` |
| `test_recipe_file_and_run_cli` | dead-guard: Recipe-file CLI loading; recipe_file and run_cli still tested there | `test_run_cli_captures_usage_errors_and_sdk_errors` |
| **unit/test_models_and_errors.py** | | |
| `test_bake_result_artifact_lookup` | trivial: dict lookup on a dataclass | dead guard: BakeResult (bake results) |
| `test_recipe_state_initializes_default_profile` | trivial: every lower() initializes it; base/arch/variant asserted | `unit/test_describe.py::test_describe_structure` |
| **unit/test_recipe_loading.py** | | |
| `test_ambiguous_instances_error` | dead-guard: Recipe ambiguity branch; Image ambiguity is a separate branch | dead guard: Recipe-file loading (several internal models) |
| `test_attr_missing_lists_candidates` | body-dup: missing --attr lists candidates there | `test_attr_selects_an_image_or_a_factory_returning_one` |
| `test_attr_override_selects_instance_or_factory` | layer-dup: same --attr selection, Image file instead of Recipe | `test_attr_selects_an_image_or_a_factory_returning_one` |
| `test_discovers_factory_by_return_annotation` | dead-guard: -> Recipe annotation discovery, a Recipe-only path | dead guard: Recipe-file loading (kept: test_loads_module_level_recipe) |
| `test_discovers_single_anonymous_instance` | dead-guard: single anonymous Recipe instance, a Recipe-only path | dead guard: Recipe-file loading (kept: test_loads_module_level_recipe) |
| `test_discovers_single_build_prefixed_factory` | dead-guard: Recipe factory discovery by build_ prefix | dead guard: Recipe-file loading (kept: test_loads_module_level_recipe) |
| `test_examples_are_loadable_recipes` | layer-dup: every example loads there (protected file) | `lint/test_examples_api.py::test_example_binds_an_image_or_the_internal_recipe` |
| `test_loads_build_factory` | layer-dup: build() factory discovery asserted there | `declarative/test_load.py::test_load_discovers_like_the_cli_and_rejects_other_values` |
| `test_no_recipe_error` | layer-dup: 'does not define an Image' asserted there | `declarative/test_load.py::test_load_discovers_like_the_cli_and_rejects_other_values` |
| `test_surge_example_recipe_loads` | layer-dup: surge loads with production/dev profiles there | `compiler/test_surge_golden.py::test_surge_image_declares_the_golden_profiles` |
| **unit/test_unit_parse.py** | | |
| `test_timer_pair_converts_to_a_scheduled_task` | body-dup: Task Every(1h) row: one conversion, same type and text | `test_canonical_renderings_parse_back` |
| `test_unit_without_user_converts_to_root` | body-dup: the identical glue Task(user=Root()) row | `test_canonical_renderings_parse_back` |
| **unit/test_unit_text.py** | | |
| `test_no_trailing_newline_is_kept` | trivial: 'no trailing newline' row round-trips byte for byte | `test_tricky_text_round_trips` |

## Appendix B. Merges

Each group becomes one parametrized test in the file named; every case is a row.

| New test | Replaces | Rows |
|---|---|---|
| `lint/test_check_rules.py::test_debloat_settings_that_report_nothing` | `test_debloat_keep_extra_or_disabled_is_fine`, `test_kept_or_knowingly_removed_modules_are_fine` | networkd service + Debloat(keep_units_extra=networkd, keep_modules=True); networkd service + Debloat(enabled=False); Debloat(keep_modules=True); Debloat(keep_modules=False); Debloat(keep_paths=('/usr/lib/modules',)); Debloat(enabled=False); no Debloat declared (implicit default) |
| `cli/test_secrets.py::test_release_predicate` | `test_release_predicate`, `cli/test_fail_closed.py::test_production_delivery_refuses_simulated_evidence`, `cli/test_fail_closed.py::test_production_delivery_refuses_partial_coverage`, `cli/test_fail_closed.py::test_a_production_build_in_a_simulated_instance_gets_nothing_whatever_it_answers`, `cli/test_fail_closed.py::test_a_development_build_in_a_simulated_instance_accepts_simulated_only` | tdx/production/trusted released; tdx/production/measurements-match refused; tdx/production/partial refused; tdx/production/simulated refused 'a TDX instance answered with simulated evidence'; tdx/development/measurements-match released; tdx/development/partial refused; simulated/production/trusted refused fail-closed; simulated/production/measurements-match refused fail-closed; simulated/production/partial refused fail-closed; simulated/production/simulated refused fail-closed; simulated/development/simulated released; simulated/development/trusted refused; simulated/development/measurements-match refused; simulated/development/partial refused |
| `cli/test_manifest_verify.py::test_tampering_fails_the_named_check` | `test_a_changed_artifact_fails`, `test_an_unexpected_file_fails`, `test_a_missing_file_fails`, `test_an_edited_tree_file_fails`, `test_a_changed_tree_mode_fails`, `test_a_changed_symlink_target_fails` | qemu.raw appended -> only artifacts fails 'qemu.raw differs'; extra.bin added -> artifacts 'extra.bin is not in the manifest'; sbom.spdx.json removed -> artifacts 'sbom.spdx.json is missing'; tree index.html edited -> only tree fails; tree index.html chmod u+x -> only tree fails; tree symlink retargeted (or one added) -> only tree fails |
| `compiler/test_emit_mkosi.py::test_compile_kernel_with_config_emits_build_script` | `test_compile_kernel_with_config_emits_build_script`, `test_compile_kernel_config_file_copied`, `test_compile_kernel_passes_no_host_environment` | build script body with the unpinned kernel hint; payload/kernel/kernel.config holds the config; no Environment= in the payload conf and only ESP_MKFS_ENVIRONMENT in the root conf |
| `compiler/test_emit_mkosi.py::test_a_variants_sandbox_file_is_only_in_its_profile` | `test_compile_overlay_repository_is_only_in_its_profiles_sandbox`, `test_compile_backports_only_in_the_variant_that_adds_it` | Repository in a variant fragment -> mkosi.profiles/extra/mkosi.sandbox/.../extra.sources, absent at the root; Backports in an inheriting variant -> mkosi.profiles/bp/.../debian-backports.sources, absent at the root |
| `compiler/test_execution_gate.py::test_the_gate_script_records_or_refuses_per_mode` | `test_without_tdx_a_production_profile_refuses`, `test_a_tdx_cpu_flag_without_a_quote_path_is_still_refused`, `test_without_tdx_a_development_profile_records_simulated`, `test_with_a_quote_path_either_profile_records_tdx` | production, no cpu flag, no report -> (1, REFUSED), no mode file; production, cpu flag, no report -> (1, REFUSED); development, no cpu flag -> (0, ALLOWED), mode simulated; production, flag and report -> 0, quote path, mode tdx; development, flag and report -> 0, quote path, mode tdx |
| `compiler/test_keys_disks_secrets.py::test_runtime_tools_override_the_tool_sources` | `test_key_generation_custom_repo_and_branch`, `test_disk_encryption_custom_repo` | Key + RuntimeTools(custom/fork, v2.0) -> key-generation source, one build; Disk + RuntimeTools(custom/disk, v3) -> disk-encryption source, one build |
| `compiler/test_keys_disks_secrets.py::test_keys_and_disks_refuse_inconsistent_arguments` | `test_key_generation_pipe_strategy_requires_pipe_path`, `test_key_generation_pipe_path_only_valid_with_pipe_strategy`, `test_disk_encryption_rejects_mapper_name_for_plain_disks` | Key(strategy=pipe) without pipe= -> 'the pipe strategy requires pipe='; Key(pipe=) without the pipe strategy -> 'pipe= is only valid'; plain Disk(mapper=) -> 'a plain disk cannot set mapper=' |
| `compiler/test_source_build.py::test_drift_reports_added_moved_changed_and_removed_sources` | `test_drift_reports_new_and_moved_sources`, `test_drift_reports_removed_sources` | unlocked source -> lock-added; fresh lock -> (); source_drift moved pin -> sources.tool a->b; retargeted ref -> lock-changed; source dropped from the recipe -> lock-removed |
| `compiler/test_source_build.py::test_show_lists_sources_and_hooks_at_their_pins` | `test_explain_lists_sources_with_pin`, `test_inspect_renders_hooks_at_the_pins_it_shows` | unlocked: text line pinned=-, json hook 'not pinned' and pinned None; locked: json sources record with pinned sha7, hook keyed build}/tool-<16 hex>, text 'Sources (1):' and pinned=sha7 |
| `compiler/test_tdxs_runtime_init.py::test_tdxs_needing_runtime_init_waits_only_when_steps_exist` | `test_tdxs_needing_all_of_runtime_init_waits_for_runtime_init`, `test_tdxs_no_init_dependency_when_no_init_scripts` | Key + Tdxs(needs=RUNTIME_INIT) -> service Requires=runtime-init.service tdxs.socket, socket has no runtime-init; Tdxs(needs=RUNTIME_INIT) alone -> no runtime-init in service or socket |
| `compiler/test_tdxs_runtime_init.py::test_tdxs_refuses_bad_roles` | `test_tdxs_rejects_invalid_issuer_type`, `test_tdxs_rejects_no_roles` | issuer='invalid' -> 'Unsupported tdxs type'; issuer=None, validator=None -> 'at least one of issuer_type or validator_type' |
| `declarative/test_azure_target.py::test_a_recipe_without_cloud_targets_carries_no_glue` | `test_a_recipe_without_the_azure_target_carries_no_azure_glue`, `declarative/test_gcp_target.py::test_a_recipe_without_the_gcp_target_carries_no_gcp_glue` | azure: no dmidecode, no provisioning script, no mkosi.images/azure; gcp: no DNS unit, no /etc/hosts, no mkosi.images/gcp |
| `declarative/test_authoring_helpers.py::test_spellings_are_the_constructor` | `test_file_text_and_copy_are_the_constructor`, `test_unit_from_file_is_the_constructor` | File.text == File (+ same lowering); File.copy == File (+ same lowering); Unit.from_file == Unit |
| `declarative/test_component.py::test_exports_follow_their_provider` | `test_http_endpoint`, `test_exports_require_their_provider`, `test_bound_exports_follow_the_provider_instance` | HttpEndpoint url and address; HttpEndpoint refusals (4); export requires its provider (clean / fragment-requires-missing); bound export provider and origin; export-stale error for another instance; export-stale warning for an edit inside it |
| `declarative/test_config_file.py::test_each_format_renders_its_text` | `test_yaml_quotes_strings_another_type_would_claim`, `test_yaml_scalars_and_nesting`, `test_insertion_order_on_request`, `test_json_ini_and_env` | yaml quotes ambiguous strings; yaml floats/bools/null/empty/nested; yaml sorted keys; yaml insertion order; json insertion order; json unicode and null; ini repeated keys; env quoting |
| `declarative/test_kernel_modules.py::test_the_selection_lists_modules_and_debloat_follows` | `test_qemu_keeps_the_tdx_guest_set`, `test_a_tuple_adds_to_the_curated_set`, `test_all_keeps_every_module`, `test_a_built_kernel_selects_nothing_and_debloat_removes_modules` | qemu default -> TDX set, module loop, no rm; tuple -> TDX set + vsock,tun; 'all' -> nothing listed, no module section, no rm; built kernel -> nothing listed, rm modules |
| `declarative/test_kernel_modules.py::test_invalid_selections_are_rejected` | `test_invalid_selections_are_rejected`, `test_modules_without_a_distribution_kernel_are_rejected` | virtio*; 'some'; x.ko; kernel_package=None with modules |
| `declarative/test_lower.py::test_inject_after_init` | `test_inject_after_init`, `test_inject_after_init_prepends_several_units_in_order` | the five single-unit rows; two units prepended in order |
| `declarative/test_lower.py::test_service_validation` | `test_service_rejects_bad_fields`, `declarative/test_service_fields.py::test_service_validation` | the 20 existing rows; restart=sometimes; limits NOFILE=True; limits as tuple pairs; name x.socket |
| `declarative/test_lowering_completeness.py::test_variants_with_their_own_settings_or_kernel_are_refused` | `test_variants_with_their_own_settings_or_kernel_are_refused`, `test_only_variants_with_their_own_settings_are_listed` | small (CompressOutput) listed; unseeded (Seed removal) listed; seeded (Seed add) listed; lts and bare (Kernel) listed; dev, slim, rescue, ok not listed; hint says an image of its own |
| `declarative/test_model.py::test_invalid_declarations_raise` | `test_invalid_declarations_raise`, `declarative/test_secrets.py::test_secret_delivery_rejects_unnamed_or_targetless_secrets` | every existing INVALID row; secret with an empty name (the targetless row exists) |
| `declarative/test_service_fields.py::test_historical_fields_render_the_historical_unit` | `test_historical_fields_render_the_historical_unit`, `test_restart_sec_replaces_or_drops_the_historical_line`, `test_disabled_service_has_no_install_section` | {} -> BASE+INSTALL; restart on-failure + 5; restart always + 2s; restart always + restart_sec None; enabled False -> BASE |
| `declarative/test_resolve.py::test_order_boot_steps_follows_after_and_before` | `test_order_boot_steps_follows_after_and_before`, `test_unconstrained_steps_keep_builtins_first_then_declaration_order` | after/before chain -> keys,disks,first,secrets,late,second; unconstrained -> keys,secrets,zeta,alpha; cycle x<->y refused |
| `lifecycle/test_bake_tree_state.py::test_collect_artifacts_takes_selected_files_only` | `test_collect_artifacts_skips_directories_and_missing_output`, `test_collect_artifacts_takes_only_the_selected_targets` | missing output dir -> {}; directory named like a target is skipped; qemu+azure selection keeps qemu.raw with payload.efi; azure-only selection; azure+gcp names and the shared boot payload |
| `lifecycle/test_deploy_adapters.py::test_each_adapter_requires_its_cli` | `test_qemu_adapter_requires_qemu_binary`, `test_azure_adapter_requires_az_cli`, `test_gcp_adapter_requires_gcloud_cli` | qemu: QEMU binary not found; azure: Azure CLI; gcp: gcloud |
| `lifecycle/test_hermetic_fetch.py::test_a_prefetch_that_cannot_run_records_why` | `test_fetch_without_the_host_toolchain_skips_the_deps_and_succeeds`, `test_a_failing_prefetch_is_recorded_and_the_build_downloads_online`, `test_script_builds_and_kernels_have_no_dependency_cache` | no host go -> 'skipped (no host go)', no calls, no deps dir; go exits 1 -> 'failed (go mod: boom)', no marker; script build -> deps None, no calls |
| `lifecycle/test_lima_backend.py::test_lima_instance_name` | `test_lima_instance_name_is_hash_based`, `test_lima_instance_name_deterministic`, `test_lima_instance_name_differs_per_path`, `test_lima_instance_name_override` | tdx-builder- + 8 hex; stable for one request; differs per build path; instance_name override wins |
| `lifecycle/test_lima_backend.py::test_lima_mkosi_args_are_paths_selection_then_extra` | `test_lima_build_mkosi_command_is_paths_and_the_selection`, `test_lima_build_mkosi_command_extra_args` | paths, cache, output, then selection args in order; backend mkosi_args last; _in_state prefix |
| `lifecycle/test_lima_backend.py::test_lima_guest_is_x86_64_with_the_pinned_mkosi` | `test_lima_resources_are_configurable`, `test_lima_guest_is_x86_64_with_the_pinned_mkosi` | x86_64 qemu guest, amd64 image, no rosetta/nix; pinned mkosi in the yaml and VM_MKOSI, fingerprint; cpus/memory/disk rendered into lima.yaml |
| `lifecycle/test_lock_drift.py::test_drift_names_the_section_and_the_change` | `test_drift_after_install_names_packages_with_detail`, `test_drift_after_file_names_files_section_and_path` | +Package(htop) -> variants.default.packages '+htop' (Diagnostic and render); +File(/etc/issue) -> variants.default.files '+/etc/issue' |
| `lifecycle/test_lock_failures.py::test_cli_lock_failure_exits_2_and_leaves_the_lockfile` | `test_cli_lock_failure_exits_2_and_writes_nothing`, `test_cli_lock_failure_keeps_the_existing_lockfile` | no lock: exit 2, nothing written, error lines and hint; existing lock + --update raiko: exit 2, bytes unchanged |
| `lifecycle/test_measure_provenance.py::test_an_artifact_that_is_no_uki_is_refused_for_every_scheme` | `test_an_artifact_that_is_no_uki_refuses_with_hint`, `test_no_scheme_measures_what_is_no_uki`, `lifecycle/test_measure.py::test_an_artifact_that_is_no_uki_or_disk_is_refused_for_every_scheme` | qemu: E_MEASUREMENT, 'is not a PE/EFI image', hint 'UKI (.efi)'; gcp; azure; portable; gcp-variant disk artifact without a UKI (gcp, azure, portable) |
| `unit/test_describe.py::test_describe_is_pure_and_serializable` | `test_describe_is_json_serializable_and_stable`, `test_describe_does_not_mutate_state` | json.dumps round trip; same output twice on one image and across lowerings; image.state and digest unchanged |
