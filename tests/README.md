# Tests

| Package | Covers |
|---|---|
| `declarative/` | the `Recipe` model, resolution, lowering, `utils` fragments, loading, variant and target shapes, secrets |
| `compiler/` | the mkosi emitter, debloat, runtime-init (tdxs, keys/disks/secrets), source builds, recompiles, the attested node golden parity |
| `lifecycle/` | diff, lock/drift, bake (progress, reports, result io), measure, deploy, doctor, backends |
| `cli/` | grammar, `--format`/CI kit, completions and UX, lifecycle verbs, an end-to-end recipe-file workflow |
| `lint/` | check rules, example-module API checks, user-facing strings, error hints |
| `testing_toolkit/` | `tundravm.testing`: fragment plans and `Image` queries, helpers, the boot harness (fake QEMU), the pytest plugin |
| `docs/` | the docs run as written: code blocks, commands, links; the generated reference is current |
| `integration/` | real mkosi builds, QEMU boots of baked templates (also marked `boot`) and the upstream nethermind-tdx comparison; marked `integration`, deselect with `-m "not integration"` |
| `unit/` | models, errors, policy, `show`'s image description, package layout, public surface, recipe loading |

Conventions: no network and no real builds. Outside `integration` tests the root `conftest.py` makes source resolution fail (pass `resolver=` to `lock()`) and puts `FakeMkosi` and `FakeSources` (`tundravm.testing`) at the process boundary, so a build runs the Linux backend's whole path with nothing forked or fetched; tests of that boundary itself are marked `boundary`.
Shared fixtures live in `conftest.py` (`isolated_cwd`); shared helpers (repo paths, `run_main`, `write_recipe_file`, `conf_list`) in `helpers.py`.
Golden trees are the oracle for compiler output: the attested node tests pin `update=False` against `examples/attested-node/mkosi/` (regenerate it with `tundravm build examples/attested-node/image.py --tree-only examples/attested-node/mkosi`); other `assert_tree` goldens rewrite under `TUNDRAVM_UPDATE_GOLDEN=1`. Review the git diff either way.
