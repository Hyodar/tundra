# Test speed: where the gate's time goes

Date: 2026-11-01. Status: done; section 2 records what landed, section 3 the measured
result. Scope: the commit gate every agent runs before a commit (`gate.sh`: ruff, ruff
format, mypy, the unit suite, the example check, the reference diff). The host has 12 cores
shared with nine other agents running the same gate, so every figure is the median of three
runs and states the load.

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

## 2. What changed

| Commit | What | Measured |
|---|---|---|
| `test: the unit suite runs on every core with pytest-xdist` | `pytest-xdist` in the dev group, `-n auto` in `addopts`; a `pytest_xdist_auto_num_workers` hook in `tests/conftest.py` returns 0 workers unless the run deselects `integration`, so CI's image builds and boots stay serial; the CLI tests' `tmp_path` comes from a new `short_tmp_path` fixture; the `qemu` fixture's `launch` returns once the fake has bound its monitor; mkosi's fork-in-a-threaded-process warning is filtered | unit suite 106.6 s to 31.6 s (interleaved with master, load 16 to 21; `-n 6` 31.7 s, `-n 4` 40.7 s) |
| `test: the converter test makes its disk once and converts both runs at once` | one `sgdisk` disk copied into all four places; the four converter runs in a thread pool | 12.0 s to 3.9 s |
| `test: the emitted unit corpus compiles once per session` | session fixture `emitted_unit_corpus` | the three tests 2.81 s to 1.47 s, serial |
| `test: the list-argument mypy check starts from a copy of the repository's warm cache` | copies `.mypy_cache` into `tmp_path`, checks only the snippet; cold when there is none | 4.30 s to 0.83 s |

What the parallel run surfaced, all in the tests:

- **`AF_UNIX` paths.** `tmp_path` under a worker is `.../popen-gwN/<test name>/`; the fake
  QEMU's monitor socket at `.tundravm/instances/demo-1/monitor.sock` below it passed the
  108-byte limit. It was at 106 bytes serially, so a five-digit pytest run counter or a
  longer `$TMPDIR` would have broken it serially too.
- **A hang under load.** The fake QEMU opens its console with `"w"`. `run --detach` waits
  0.05 s (`LAUNCH_GRACE`, shortened by the CLI conftest), so on a loaded host the fake could
  start after the test had appended its probe lines and truncate them. `run probe` then
  waited out `PROBE_TIMEOUT`, 900 s: the 14-minute hang of
  `test_run_probe_reads_the_probe_off_the_console_and_refuses_production` seen under load
  elsewhere. It was there serially too, only rarer.
- **Nothing writes to the repository.** After a full parallel run `git status` is clean, and
  no test depends on order: the suite passed 6 of 6 full parallel runs and the CLI tests
  10 of 10. One failure in a ninth full run, at load 13, did not reproduce and left no
  output; it is not identified.

Looked at and left:

- **The CLI `built` fixture** (lock and build per test, 9 s of setup across the suite) could
  build once per session and be copied, but receipts carry absolute build paths, and under
  `-n auto` its 9 s spread over twelve workers is under a second of wall time.
- **The lint tests that parse every package module** (`test_user_strings.py`,
  `test_rule_registry.py`, `test_error_hints.py`) could share parsed trees, but
  `test_rule_registry.py` edits its trees, and the tests land on different workers anyway.
- **Deleting tests.** None: nothing measured slow duplicates a golden or another layer.
- **mypy.** It is already incremental per worktree (0.24 s warm, 0.5 to 0.8 s after a
  one-line edit). Seeding a fresh worktree's cache with a copy of the main checkout's does
  not help (10.5 s seeded, 10.5 s cold): the cache records absolute source paths.
- **`sfdisk` instead of `sgdisk`** saves 0.7 s per disk but writes different bytes.

## 3. After

The gate, interleaved three rounds at `24f09c8` (load 10 to 23): master's tree, this branch,
and this branch under the gate proposed in section 4.

| Round | master, `gate.sh` | branch, `gate.sh` | branch, proposed gate |
|---|---|---|---|
| 1 | 111.3 s | 32.2 s | 30.7 s |
| 2 | 109.9 s | 35.9 s | 33.0 s |
| 3 | 114.9 s | 33.1 s | 31.2 s |
| **median** | **111.3 s** | **33.1 s (−70%)** | **31.2 s (−72%)** |

On the quiet host of section 1 the same gate took 68.2 s; the extra time is the other nine
agents' gates. Per stage after: ruff and ruff format unchanged (0.03 s, 0.02 s); mypy
unchanged (0.24 s warm); the unit suite 23.6 to 33.6 s under `-n auto` at load 17 to 22
(six runs at `24f09c8`, 2 285 tests), against 104 to 118 s serial at load 16. The wall time
is now worker start-up (twelve interpreters importing tundravm on a loaded host) plus total
CPU over twelve; no single test is the floor.

The slowest tests at `9edb8d6` under `-n auto` (load 22, 2 312 tests in 28.4 s): the
converter test 10.5 s (it competes with eleven workers for `tar` and `sync`), the lint tests
that parse the package 1.9 to 4.1 s, `uv build` 3.9 s, `mkosi summary` in the profile and
native-tree tests 1.3 to 2.3 s, the list-argument mypy check 2.1 s, the FAT32 cluster-count
table 1.8 s, the convert fixtures 1.6 to 1.9 s.

## 4. The gate

Recommended change to `gate.sh`: the pytest stage takes `-n auto` from `addopts` and needs
no flag. Keep `mypy .` before the tests: the list-argument check copies its warm cache. The
example check and the reference generation run behind the unit suite, worth about 2 s:

```diff
 uv run mypy . --exclude "^\.claude/"
-uv run pytest -q -m "not integration" 2>&1 | tail -1
 example=examples/attested-node; [ -d "$example" ] || ...
-uv run tundravm check "$example/image.py" --tree "$example/mkosi"
 tmp=$(mktemp -d)
-uv run python -m tundravm._reference "$tmp" >/dev/null
-diff -r docs/reference "$tmp" && echo "reference clean"
+{ uv run tundravm check "$example/image.py" --tree "$example/mkosi" && uv run python -m tundravm._reference "$tmp/reference" >/dev/null; } >"$tmp/side.log" 2>&1 &
+side=$!
+uv run pytest -q -m "not integration" 2>&1 | tail -1
+wait "$side" || { cat "$tmp/side.log"; exit 1; }
+cat "$tmp/side.log"
+diff -r docs/reference "$tmp/reference" && echo "reference clean"
 rm -rf "$tmp"
```

A focused run (`uv run pytest tests/cli/test_run.py`) selects no marker, so the hook keeps
it serial and it starts at once; `-n 4` forces workers, `-n0` one process.

Found outside the tests, not changed here: `instances.launch` puts the QEMU monitor socket
under the project, so a project deep enough in the filesystem cannot start an instance
(`AF_UNIX path too long`); and `probe()` binds `PROBE_TIMEOUT` as a default argument, so
shortening the constant in a test has no effect.
