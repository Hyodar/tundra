# Lint fixes in the image file's words

Date: 2026-11-07. Base: master at `9f2cd316`. Item 3 of `status-2026-11-01.md` said about
25 rows of `docs/reference/lint.md` named the internal model (`Unit(...)`, `Key`, `Disk`,
`SecretEnv`, `ExternalDelivery`, `Setting`, `Debloat`, `needs=`, `Firewall(collect=)`).

## What master already did

`0d8768d4` removed the 28 codes only an internal-model file could reach, with
`Rule(internal=)` and lint.md's internal section, and `tests/lint/test_user_strings.py`
already refuses every name item 3 lists (`INTERNAL_MODEL`) in each rule's summary and fix,
each finding's hint and each error. Read row by row, all 54 registry rows and all 13
shipped-component rows are reachable from an `Image` file (a shipped unit file is a
`File` under a unit directory, a project's component reports `rule-undeclared`), and none
names an internal word. lint.md keeps one table: no row is internal-only, so the generator
does not change.

## What is left

The guard does not read a finding's message, and a few messages and internal-only errors
still speak the lowering. Each one, and its new wording:

| Code or error | Reachable? | Internal words | New wording |
|---|---|---|---|
| `file-path-duplicate` (message) | yes | `mkosi.extra`, `mkosi.skeleton`, `template`, `skeleton` | `shipped N times with different content; only the last one ends up in the image` |
| `debloat-removes-declared-file` (message) | yes | `at finalize` | `the debloat empties PATTERN at the end of the build, after this file is placed` |
| `service-runs-as-root` (message) | yes; `Root()` only from tundravm's own units | `user=Root()` | `user="root"`, or `a unit tundravm composes` for its own root plumbing |
| `hardening-relaxed` (message) | yes; `security=` only from a parsed unit file | `security= is deprecated` | `the sandbox a parsed unit file renders; an Image file's Service sets hardening=` |
| build-script order (error, `order_hooks`) | no: only a shipped component orders its scripts | `phase`, `hook`, `after=` | `build script NAME (STAGE) runs after DEP, a script of the later STAGE stage`; hint: a shipped component wrote it, `Mkosi(scripts=)` runs in the order given, report it |
| ordering cycle (error) | no: steps and scripts are ordered by tundravm | `after=` | hint: a shipped component wrote the order, report it |

The guard then reads a finding's message as well as its hint, and refuses the lowering's
tree and stage names (`in mkosi.extra`, `in mkosi.skeleton`, `at finalize`).

"debloat" stays as a word: `docs/trust.md` names it, and it is three codes' prefix.
