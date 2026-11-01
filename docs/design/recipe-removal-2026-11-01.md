# Removing Recipe files from the CLI

Date: 2026-11-01. Base: master at `24f09c8`, then `leftovers-8`. Franco: "you do not need
to support the previous syntax". `check`, `build`, `lock`, `dev` and the rest still load a
file that binds a declarative `Recipe`, kept for one caller: CI's native-tree fixture. This
note says what the removal touches. It is larger than one sitting, so `leftovers-8` stops
here.

## Why the fixture cannot just be rewritten

`tests/fixtures/native-tree/recipe.py` has to become an `Image` file that lowers to the
committed `tree-initramfs/` and `tree-verity/`. Six of its declarations have no `Image`
spelling, and two of those change the tree's bytes:

| Fixture declaration | What an `Image` has | Tree change |
|---|---|---|
| `Mkosi(seed=SEED, root_size="4G")` | a fixed seed (`Seed` is refused, `_image/_values.py`); no root tmpfs cap | every partition UUID, the `summary-*.json` files, the initramfs boot test's size check |
| `Variant("azure", outputs=("azure",))`, `Variant("gcp", ...)` | `Image(targets=[...])`: one payload, one profile | the profile layout of both trees, and the `verity-cloud` and `reproduce` jobs' `for profile in azure gcp` loops and `--profile azure` |
| `BootProbe`: `Task(user=Root(), hardening=Hardening.none())` | none: no public root oneshot | its unit |
| `Service("sink", user=Root(), hardening=Hardening.none())` | none for a generated service | its unit |
| `Hook("dev-slim", "finalize", ...)` | `Mkosi(scripts=[("finalize", ...)])` | the script's name and place |
| `Secrets(entries=(Secret(..., SecretFile(...)),), port=8745)` | `Service(credentials=[Secret(...)])` | the delivery config, and a waiting service |

So the conversion either regenerates the committed trees (and the boot assertions that
read them, `tests/integration/test_fixture_boot.py`, `test_verity_boot.py`,
`test_verity_cloud.py`), or first gives an `Image` a spelling for an unsandboxed root
boot task. The second is a design decision (it is the one thing the boot probe needs that
a secure default refuses); the first loses the seed the boot test pins.

## The files

1. The fixture: `tests/fixtures/native-tree/recipe.py` to `image.py` (keep
   `.gitignore`'s `!tests/fixtures/native-tree/**/lib/` exception if the trees move),
   `tests/compiler/test_native_tree.py` and `tests/compiler/test_profile_overlays.py`
   (they import `recipe_with`), the three integration tests above, and
   `.github/workflows/ci.yml` lines 2, 167-169, 199, 246, 309-310 and the azure and gcp
   loops at 353, 401 and 411 (`--attr verity_recipe` becomes a second file or a profile).
2. Loading: `src/tundravm/recipe.py` keeps `load_file` for `Image` files only. Delete
   `RECIPE_OBJECT_NAMES`, `RECIPE_FACTORY_NAMES`, `load_recipe`/`load`, `_as_recipe`'s
   `Recipe` branch, `_discover`'s Recipe and factory search and `_looks_like_factory`;
   a file binding a `Recipe` gets `_image/_api.py`'s `_no_image` refusal.
   `RecipeFile.recipe` stays (the internal value an `Image` lowers to) and
   `RecipeFile.declared` stops being optional, which `cli.py`'s `_loaded` callers and
   `show.py` then rely on. `tundravm.load` leaves `__all__`
   (`tests/unit/test_public_surface.py`).
3. The 28 internal rules in `src/tundravm/check.py` (`internal=True`):
   `debloat-removes-modules disk-device-guessed disk-device-unstable
   disk-key-path-mismatch disk-key-undefined edit-invalid external-delivery-unwired
   file-path-relative hook-after-undefined hook-order image-arch-unsupported
   kernel-missing kernel-modules-ignored key-pipe-outside-run mkosi-configure-script
   mkosi-environment-passthrough needs-undefined needs-without-init network-missing
   port-unreachable secret-env-service-unknown secret-store-undefined
   secret-target-unsupported setting-raw step-cycle step-undefined target-inconsistent
   unit-raw`. Each is reported from `declarative/resolve.py`, `declarative/state.py` or a
   module's `check` (`_modules/key_generation.py` for `key-pipe-outside-run`). Before
   deleting one, run the `Image` corpus (`tests/image`, `examples/`, the templates) with
   the rule turned into an assertion: a rule an `Image` still reaches as a lowering bug
   (`step-cycle`, `hook-order` from the image's own boot steps) stays as an
   `AssertionError` naming the invariant, not as a lint code. Then regenerate
   `docs/reference/lint.md` (`python -m tundravm._reference docs/reference`).
4. The tests that write or load a Recipe file through the CLI or `load_recipe`:
   `tests/unit/test_recipe_loading.py`, `tests/unit/test_policy.py`,
   `tests/declarative/test_load.py`, `tests/declarative/test_locations.py`,
   `tests/cli/test_check_lock.py`, `tests/lifecycle/test_lock_failures.py`,
   `tests/compiler/test_source_build.py`, `tests/compiler/test_source_build_examples.py`,
   and `tests/fixtures/kernel/recipe.py` with its callers. Tests that build a `Recipe` in
   memory (`planned_model`, `tests/declarative/*`) test the internal model and stay.
5. `CHANGELOG.md` Unreleased, removed: "`check`, `build`, `lock` and the other commands
   load `Image` files only; a file binding a declarative `Recipe` is refused, and the 28
   lint codes only such a file could reach are gone" (breaking).

## Order

The fixture first (it is the only thing CI runs that needs the old loading), in its own
commit with the regenerated trees and the CI lines; then the loader and the tests of item
4; then the rules with `lint.md`; the changelog with the loader.
