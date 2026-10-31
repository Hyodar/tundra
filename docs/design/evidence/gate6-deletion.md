# Gate 6 evidence: deletion (the code half)

On a branch rebased onto `62edc57` (master with the rewritten docs). Plan section 8's gate 6
row asks for section 7's removals, each behind its replacement's test, with "the test that
covered each deleted guarantee, named in the commit". Each commit below passes the gate
chain on its own (`ruff check`, `ruff format --check`, `mypy`, the unit suite, `tundravm
check` of the surge example against its committed tree, and the generated reference equal
to `python -m tundravm._reference docs/reference`), and CI's fixture-boot, verity,
verity-cloud and reproduce jobs ran green on the scratch branch for the final content.

## The commits

| Commit | Deleted | Covered by |
|---|---|---|
| `refactor!: the in-process backend and simulated artifacts are gone` | `backends/inprocess.py`; the `inprocess` backend kind (`Backend`, `BACKEND_KINDS`, `TUNDRAVM_BACKEND`, doctor's build row, `render_recipe_template(backend="inprocess")`); `Artifact.simulated`, the manifest's `simulated` flag and its "simulated build" unchecked entry and measurement state; `measure(allow_placeholder=)`, `deploy(allow_simulated=)` and their refusals; placeholder measurements (`measure/placeholder.py`, `PlaceholderMeasurementWarning`, `schemes.derive(allow_placeholder=)`, `Tdxs.from_measurements(allow_placeholder=)`); `tundravm.testing.fake_bake` and `bake_in_process` | `tests/testing_toolkit/test_toolkit_helpers.py::test_the_fakes_run_a_whole_build_with_nothing_forked_or_fetched`, `tests/testing_toolkit/test_pytest_plugin.py::test_recipe_fixture_builds_against_the_fake_mkosi`, `tests/cli/test_build.py` (with `test_offline_builds_from_what_an_online_build_fetched`), `test_show.py`, `test_manifest_verify.py`, `test_secrets.py`, `tests/lifecycle/test_measure.py::test_an_artifact_that_is_no_uki_or_disk_is_refused_for_every_scheme`, `tests/lifecycle/test_measure_provenance.py::test_an_artifact_that_is_no_uki_refuses_with_hint` |
| `refactor: show keeps what it needs of explain; the rest is deleted` | `explain.py` (1 228 lines): `describe()` and `render()` move to the private `_describe.py`; `render_markdown`, `diff_variants`, `explain_why` and their renderers go | `tests/unit/test_describe.py` (was `test_explain.py`), `tests/cli/test_show.py`, `tests/declarative/test_locations.py::test_provenance_carries_origin_locations` (now through `show`'s provenance) |
| `refactor: the lifecycle code gate 5 left unreachable is deleted` | `bake-result.json` (its writer, `BakeResult.save/load`, `read_artifacts`, `variant_provenance`, the per-variant record); `bake(verify_reproducible=, offline=)` and the second-build machinery; `lifecycle.deploy` with `Qemu`/`Azure`/`Gcp`/`Deployment` and the parameters it built, `Artifact.ports/ssh_port/data_disks`, `Lowered.ssh_ports`; `lifecycle.fetch` and `fetch_image(force=)`; `lifecycle.diff`, `select_artifact`, `variant_entries`, `variant_tree_digest`; `_hashes.py`, `diff.cmd_diff`, `backend_fingerprint`, `RELEASE_NAMESPACE`, `JsonReporter`, `render_bake_summary`, `describe_changes`, `write_policy`, `schemes.measurable`, `deps_prefetched`, `spdx_packages`, `app_template.py` | `tests/cli/test_build.py` (build, `--reproduce`, `--offline`, `--clean-cache`), `tests/cli/test_run.py` and `test_deploy.py`, `tests/lifecycle/test_bake_artifacts.py`, `tests/lifecycle/test_host_fetch.py::test_a_modified_git_checkout_fails_until_a_clean_cache_fetches_it_again`, `tests/lifecycle/test_diff.py`, `tests/lifecycle/test_sbom.py::test_moving_the_bake_directory_changes_no_document` |
| `fix: build scripts and source errors name tundravm build, not the removed fetch` | the `tundravm fetch` hints in emitted build scripts and source errors (and the lint's exemption for them); links to the deleted `docs/api.md` | `tests/lint/test_user_strings.py::test_removed_verbs_are_caught`, `::test_removed_docs_are_caught`, `tests/compiler/test_surge_golden.py`, the regenerated `examples/surge-tdx-prover/mkosi` |

## How the unit suite builds without the in-process backend

The root `tests/conftest.py` used to set `TUNDRAVM_BACKEND=inprocess` outside integration
tests, so nothing built for real by accident. It now sets `TUNDRAVM_BACKEND=local` and puts
two fakes at the process boundary: `FakeMkosi` (`invoke` and `needs_sudo` of the mkosi
adapter) and the new `tundravm.testing.FakeSources` (`fetch_source`, the git queries that
verify a checkout, and the host-toolchain dependency prefetch). A build then runs the whole
real path: fetch into `.sources` with completion markers, lint, frozen-lock check, compile,
the Linux backend's argv and `summary --json`, artifact collection, measurement, SBOM, the
build directory and its manifest; nothing forks, fetches or needs privileges, on any host.
Tests of the boundary itself (the mkosi adapter, the source fetch against local git and
http, the Linux backend's host checks, `convert`, which reads trees through mkosi's own
parser) are marked `boundary` and get neither fake.

Running the real path exposed one behaviour the in-process backend hid: `build --offline`
right after `lock` refuses (`Cannot fetch 'tdxs': policy network_mode is offline.`), since
nothing was fetched yet; `test_offline_builds_from_what_an_online_build_fetched` pins it.
Measurements of a fake build are `unknown` (the fake UKI is no PE image) where they were
`unchecked: simulated build`.

## Proof that the lifecycle deletions were dead

Two independent checks on the tree before the third commit:

- A call-graph walk over `src/tundravm` (every Name and Attribute resolved through each
  module's imports, function-local names excluded) from these roots: `tundravm.cli:main`,
  `tundravm.__all__`, `tundravm.testing.__all__` and its pytest plugin,
  `tundravm.components.__all__`, `tundravm.image.__all__`, every `__main__` module, and
  what `tests/integration/` and `examples/` import. `tundravm.declarative.__all__` is not a
  root: the package is internal. Every symbol deleted in the third commit was unreached,
  except the `bake-result.json` round trip, which `bake_image` wrote and read back within
  one call and which is now built in memory.
- Coverage of `tests/cli`, `tests/image` and `tests/compiler` (716 tests, the CLI and SDK
  through the real build path) with `pytest-cov`, body lines of each deleted function
  executed:

```
declarative/lifecycle.py:Qemu: 0/13            declarative/lifecycle.py:deploy: 0/21
declarative/lifecycle.py:Azure: 0/11           declarative/lifecycle.py:deploy_parameters: 0/11
declarative/lifecycle.py:Gcp: 0/5              declarative/lifecycle.py:with_secret_ports: 0/5
declarative/lifecycle.py:Deployment: 0/4       declarative/lifecycle.py:qemu_image_parameters: 0/6
declarative/lifecycle.py:diff: 0/9             declarative/lifecycle.py:target_of: 0/1
declarative/lifecycle.py:fetch: 0/8            declarative/lifecycle.py:select_artifact: 0/12
declarative/lifecycle.py:_verify_reproducible: 0/18
declarative/lifecycle.py:_rebake: 0/25         declarative/lifecycle.py:_report_reproducibility: 0/22
declarative/lifecycle.py:_compare_digests: 0/11
declarative/lifecycle.py:_record_reproducibility: 0/7
declarative/lifecycle.py:recorded_reproducibility: 0/4
declarative/lifecycle.py:reproducibility_of: 0/9
declarative/lifecycle.py:variant_entries: 0/1  declarative/lifecycle.py:variant_tree_digest: 0/1
_hashes.py:HashCache: 0/48                     observability.py:JsonReporter: 0/17
observability.py:render_bake_summary: 0/16     declarative/bom.py:spdx_packages: 0/10
diff.py:cmd_diff: 0/9                          diff.py:_wants_color: 0/5
toolchain.py:describe_changes: 0/3             measure/policy.py:write_policy: 0/3
measure/schemes.py:measurable: 0/1             backends/base.py:backend_fingerprint: 0/3
_source.py:deps_prefetched: 0/1                app_template.py: 0 of 81 statements
```

  The round trip ran, as expected, and is replaced rather than dropped:
  `BakeResult.save` 20/33, `BakeResult.load` 7/17, `read_artifacts` 12/36,
  `variant_provenance` 4/4, `_kept_variants` 4/15, `_write_json` 8/11; `data_disk_serials`
  4/4, `secret_ports` 2/2 and `_ssh_port` 5/5 ran only to fill fields of that record that
  only `lifecycle.deploy` read. `lifecycle.bake` ran (6/16) and stays.

## What stays from section 7, and why

- **`models.py` runtime-tools models and `src/tundravm/_modules/`** (about 2 000 lines).
  Their replacement is gate 3's single versioned tundra-tools protocol (plan 6.3) with
  readiness and failure semantics, which is blocked on Go work in tundra-tools. Untouched;
  only `models.py`'s `bake-result.json` persistence (`BakeResult.save/load`, the filename
  and schema constants, the path helpers, two record-only fields) went.
- **The non-TDX half of `testing/_boot.py` → `mkosi vm`**. It needs a boot proof in CI (a
  notify-socket exit unit with the same assertions) that this host cannot run; out of this
  round.
- **`tundravm.declarative` as a package.** Gate 4 recorded it as internal, "gate 6 renames
  or deletes it". Its `__all__` lost what this round deleted (`deploy`, `Qemu`, `Azure`,
  `Gcp`, `Deployment`, `diff`, `fetch`, `read_artifacts`), and nothing else: renaming or
  folding it moves every internal import and test, which is a refactor of its own rather
  than a deletion.
- **`tundravm.bake`.** It is in the frozen public surface (`tests/unit/test_public_surface.py`)
  and builds artifacts into a directory from Python; it keeps the options it already had
  (`lock`, `backend`, `build`, `profiles`, `progress`, `fetch`). It is not the engine under
  `tundravm build` (that is `bake_image`), so whether the SDK should build into an
  immutable build directory instead is a decision, not a deletion.
- **`deploy/qemu.py`'s `QemuDeployAdapter`** (about 250 lines). After `lifecycle.deploy`
  went it is reachable only through `get_adapter("qemu")`, which `deployments.deploy` never
  asks for (`run` builds its own QEMU command in `instances.py`). Section 7 lists "the TDX
  QEMU runner" under what stays, and this adapter is one, so it waits for an explicit
  decision; its path constants are what `run`, `doctor` and the boot harness read.
- **Placeholder verifier policies.** `Tdxs.from_policy(allow_placeholder=True)`,
  `is_placeholder` and `attest`'s refusal of a placeholder policy stay: nothing produces one
  any more, but `examples/peer.policy.json` is one (example 06 builds a verifier from it),
  and the refusal guards against such files.
- **Other unreached code the call-graph walk lists**, not proven here: the attestation
  record readers (`AttestationRecord`, `read_attestation` and helpers, which `tests/cli/
  test_attest.py` still drives), `declarative/_kernel_hardening.py` and
  `declarative/inputs.py` (reached through relative imports the walk may miss),
  `templates.RECIPE_TEMPLATE`/`TEMPLATES`, `recipe.load_recipe` (the scaffolded test module
  calls it), `formats.format_help`. Each needs its own proof; none is a gate-5 leftover of
  the lifecycle.

## Changelog entries

For the Unreleased section of `CHANGELOG.md`:

- The in-process backend is gone: `TUNDRAVM_BACKEND`, `[tool.tundravm] backend` and
  `Backend(...)` take `local` or `lima`. Builds no longer produce simulated artifacts, and
  the manifest has no `simulated` field (`run --mode simulated` is unchanged).
- `tundravm.testing.fake_bake` and `bake_in_process` are removed; `FakeMkosi` (mkosi) and the
  new `FakeSources` (git and http fetches) let a test run a real build with nothing forked or
  fetched.
- `measure()` no longer returns placeholder values: `allow_placeholder=` and
  `PlaceholderMeasurementWarning` are gone, and an artifact that is no UKI or disk is always
  refused. `Tdxs.from_policy(allow_placeholder=True)` still reads a committed placeholder
  policy.
- `bake-result.json` is no longer written; a build's record is its build directory's
  `manifest.json`. `tundravm.bake()` drops `verify_reproducible=` and `offline=` (use `tundravm
  build --reproduce` and `--offline`).
- `tundravm.declarative` loses `deploy`, `Qemu`, `Azure`, `Gcp`, `Deployment`, `diff`,
  `fetch` and `read_artifacts`: `tundravm run`, `tundravm deploy`, `tundravm check --tree`
  and `tundravm build` replace them.
- Emitted build scripts and source errors say `run tundravm lock, then tundravm build` and
  `is not fetched: run tundravm build` instead of naming the removed `fetch`.

## Lines removed

`git diff --shortstat origin/master` for the four code commits: 102 files changed,
1 157 insertions, 4 059 deletions (src: 705 insertions, 3 001 deletions; tests: 411
insertions, 1 020 deletions). This file adds the rest of the insertions of the docs commit.
