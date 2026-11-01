# Test speed: where the gate's time goes

Date: 2026-11-01. Status: measured; the fixes follow in their own commits. Scope: the commit
gate every agent runs before a commit (`gate.sh`: ruff, ruff format, mypy, the unit suite,
the example check, the reference diff). The host has 12 cores shared with nine other agents
running the same gate, so every figure is the median of three runs and states the load.

## 1. Before

The gate, three runs at `653975b` (load average 3.5, 2.9, 2.4):

| Run | Wall |
|---|---|
| 1 | 78.3 s |
| 2 | 67.7 s |
| 3 | 68.2 s |
| **median** | **68.2 s** |

Per stage at `6701445` (load 3.4), and the lint stages three times each at `ae8401d`:

| Stage | Wall | Note |
|---|---|---|
| `ruff check .` | 0.03 s | 0.06 s with `--no-cache` |
| `ruff format --check` | 0.02 s | |
| `mypy .` | 0.24 s warm, 9.9 s cold | already incremental: `.mypy_cache` lives in each worktree; a one-line edit costs 0.5 to 0.8 s |
| `pytest -m "not integration"` | 68.8 s | 2 213 tests, serial |
| `tundravm check` the example | 0.42 s | |
| reference generation and diff | 0.33 s | |

The unit suite is 91% of the gate. Its own figures at `ae8401d`: 70.9 s wall, 66.4 s of test
time (57.3 s calls, 9.0 s fixture setup), collection 0.74 s. By directory: compiler 20.3 s,
cli 20.2 s, unit 7.2 s, lint 6.8 s, image 3.7 s, lifecycle 3.1 s, testing toolkit 2.2 s,
declarative 2.0 s, docs 0.8 s. One core runs all of it; the other eleven idle.

The 20 slowest tests (serial, `--durations=40`, `ae8401d`):

| Test | Time | Why |
|---|---|---|
| `compiler/test_determinism.py::test_the_converters_reproduce_their_outputs` | 9.10 s | four `sgdisk` calls make four identical disks, each 1.15 s because `sgdisk` syncs after writing; the GCP converter's `tar --hole-detection=raw` reads its 1 GiB sparse disk, 2.1 s a run; all of it one after the other |
| `unit/test_list_arguments.py::test_mypy_accepts_every_list_argument` | 3.59 s | `mypy --strict --no-incremental` re-checks the whole package cold for a short snippet |
| `unit/test_package_layout.py::test_the_sdist_and_the_wheel_hold_no_go_module` | 1.22 s | `uv build` of both distributions (a boundary test) |
| `lint/test_user_strings.py::test_package_strings_use_the_current_recipe_spellings` | 1.17 s | parses every package module |
| `lint/test_user_strings.py::test_package_strings_cite_only_the_declarative_api` | 0.90 s | parses every package module and example again |
| `compiler/test_native_tree.py::test_summary_resolves_the_selection_mkosi_argv_names` | 0.80 s | four real `mkosi summary` runs |
| `unit/test_unit_parse.py::test_emitted_units_round_trip[semantic]` | 0.78 s | compiles every example, the surge recipe and every template; `emitted_units` is `@cache`d, but each test passes a fresh `mktemp` path, so it never hits |
| `lint/test_rule_registry.py::test_every_reported_code_is_declared_and_every_declared_code_is_reported` | 0.74 s | parses every package module (and edits the trees, so they cannot be shared) |
| `compiler/test_profile_overlays.py::test_a_chain_with_an_edit_and_a_removal_resolves_for_every_profile` | 0.70 s | `mkosi summary` per profile |
| `lint/test_error_hints.py::test_every_sdk_error_raise_passes_a_hint` | 0.69 s | parses every package module |
| `compiler/test_profile_overlays.py::test_each_profile_resolves_to_the_shared_payload_plus_its_overlay[surge]` | 0.58 s | `mkosi summary` per profile |
| `lint/test_user_strings.py::test_lint_fixes_and_hints_name_the_public_api` | 0.57 s | parses every package module |
| `compiler/test_profile_overlays.py::test_each_profile_resolves_to_the_shared_payload_plus_its_overlay[fixture-verity]` | 0.57 s | `mkosi summary` per profile |
| `unit/test_unit_parse.py::test_emitted_units_round_trip[exact]` | 0.56 s | the same corpus compiled again |
| `compiler/test_profile_overlays.py::test_each_profile_resolves_to_the_shared_payload_plus_its_overlay[fixture-initramfs]` | 0.53 s | `mkosi summary` per profile |
| `testing_toolkit/test_boot.py::test_an_http_url_that_never_answers_fails_the_boot` | 0.51 s | waits out its 0.5 s timeout, by design |
| `unit/test_unit_text.py::test_every_emitted_unit_round_trips` | 0.48 s | the same corpus compiled a third time |
| `lint/test_examples_api.py::test_teaching_example_builds_the_same_identity_twice[04_storage_and_secrets.py]` | 0.42 s | two full builds, by design |
| `compiler/test_native_tree.py::test_stale_history_is_never_replayed` | 0.41 s | compile plus two `mkosi summary` runs |
| `compiler/test_native_tree.py::test_summary_of_every_node_is_the_committed_evidence[verity]` | 0.40 s | `mkosi summary` of every node |

Below those is a long flat tail: 2 000 tests under 0.3 s each, and the CLI tests' `built`
fixture (lock and build per test, 0.25 s). The fixes follow from that shape: the suite is
wide, not deep, so parallelism buys most of it; then the one test that would be the long
pole on its own worker; then repeated work.
