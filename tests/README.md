# Tests

| Package | Covers |
|---|---|
| `declarative/` | the `Recipe` model, resolution, lowering, `utils` fragments, loading, variant and target shapes, secrets |
| `compiler/` | the mkosi emitter, debloat, runtime-init (tdxs, keys/disks/secrets), source builds, recompiles, surge golden parity |
| `lifecycle/` | diff, lock/drift, bake (progress, reports, result io), measure, deploy, doctor, backends |
| `cli/` | grammar, `--format`/CI kit, completions and UX, lifecycle verbs, an end-to-end recipe-file workflow |
| `lint/` | check rules, example-module API checks, user-facing strings, error hints |
| `testing_toolkit/` | `tundravm.testing` helpers and the pytest plugin |
| `integration/` | real mkosi builds and the upstream nethermind-tdx comparison; marked `integration`, deselect with `-m "not integration"` |
| `unit/` | models, errors, policy, explain, package layout, public surface, recipe loading |

Conventions: no network (the root `conftest.py` makes source resolution fail; pass `resolver=` to `lock()`).
Shared fixtures live in `conftest.py` (`inprocess_backend`, `isolated_cwd`); shared helpers (repo paths, `run_main`, `write_recipe_file`, `conf_list`) in `helpers.py`.
Golden trees are the oracle for compiler output: the surge tests pin `update=False` against `examples/surge-tdx-prover/mkosi/` (regenerate it with `tundravm compile ... --out`); other `assert_tree` goldens rewrite under `TUNDRAVM_UPDATE_GOLDEN=1`. Review the git diff either way.
