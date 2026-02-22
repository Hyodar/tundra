# Policy Modes

The SDK policy engine controls strictness without changing recipe code.

## Options

- `require_frozen_lock`: Require `bake(frozen=True)`; non-frozen bakes fail with `E_POLICY`.
- `mutable_ref_policy`:
  - `warn`: mutable refs emit warnings.
  - `error`: mutable refs fail.
  - `allow`: mutable refs are accepted silently.
- `require_integrity`: Require explicit integrity values for external fetch inputs.
- `network_mode`:
  - `online`: network operations allowed.
  - `offline`: network operations fail with `E_POLICY`.

## CI Recommendation

For CI, use:

```python
from tdx.policy import Policy

ci_policy = Policy(
    require_frozen_lock=True,
    mutable_ref_policy="error",
    require_integrity=True,
    network_mode="online",
)
```

This configuration enforces lock fidelity, immutable refs, and integrity checks while preserving deterministic failure behavior.
