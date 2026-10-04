# Policy

A `Policy` makes the lifecycle stricter without changing what the recipe declares. Set it on the recipe:

```python
from tundravm import Fragment, Package, Policy, Recipe

recipe = Recipe(
    name="node",
    common=Fragment("node", items=(Package("curl"),)),
    policy=Policy(require_frozen_lock=True, mutable_ref_policy="error"),
)
```

`Recipe(policy=None)`, the default, is `Policy()`.

## Options

| Option | Default | Effect |
|---|---|---|
| `require_frozen_lock` | `False` | `True`: a bake without a lockfile fails with `E_POLICY` instead of baking unpinned |
| `mutable_ref_policy` | `"warn"` | How an unpinned source build (a `Git` branch or tag, an `Http` without `sha256`, and no pin in the lockfile) is treated: `"warn"` reports `source-unpinned` as a warning; `"error"` makes it a lint error and makes `compile` and `bake` fail with `E_POLICY`; `"allow"` reports it as info |
| `require_integrity` | `True` | Require integrity values for external fetch inputs |
| `network_mode` | `"online"` | `"offline"`: `lock` behaves as `lock --offline` and fails with `E_LOCKFILE` for any source it would have to resolve |

A pin in `build/tundravm.lock` satisfies `mutable_ref_policy` for that source, so the usual flow under `"error"` is `tundravm lock` first, then compile and bake.

```console
$ tundravm compile m.py --out mk
error [E_POLICY]: Unpinned source builds are not allowed by policy: tdxs@master.
Hint: Run `tundravm lock RECIPE` to pin them, or relax mutable_ref_policy.
  operation: compile
  sources: tdxs@master
```

## CI

For CI, refuse anything that is not pinned:

```python
Policy(require_frozen_lock=True, mutable_ref_policy="error", require_integrity=True, network_mode="online")
```

With it, `tundravm ci` fails at `lint` for every unpinned source, and a bake without a committed lockfile fails before building. `inspect` prints the effective policy on its `Policy:` line.
