# Public namespaces: one home per name

Date: 2026-10-31. Status: decided. Section 9 records the three decisions taken on the
study's open questions; section 8 is the change that implements them. Base: master at
`6a65991`. No backward compatibility: the old import paths go.

## 1. Where we were

`tundravm.__all__` had 73 names; `tundravm.components` added 13, five of them the same
objects as root names (`DevAccess`, `Network`, `Observability`, `JournalExport`,
`PrometheusRemoteWrite`), and `tundravm.image` re-exported every SDK name a third time.
`docs/reference/api.md` was one alphabetical `## tundravm` section of about 1 200 lines in
which `Calendar` sat between `bake` and `CargoBinary`. `IMAGE_OBJECT_NAMES` was public but
only `load` reads it. Example 05 imported `Git` from `tundravm.declarative` although the
root exported the same object.

Imports across the examples, the templates, the README and the guide's blocks: `Image` 17,
`Service` 13, `File` 10, `TCP` 10, `DevAccess`, `Endpoint`, `Package` 6; 28 other names
appear five times or fewer. Use supports a small root.

## 2. Rules

1. **One home per name.** Each public object is in exactly one `__all__`; no re-exports, no
   aliases. Defining modules are private (`_`-prefixed), so the home is the only spelling
   the docs, hints and reference use.
2. **The namespace is a noun the reader already has**: the subsystem the value configures,
   named the way the slot, the `Service` keyword or the tool names it (plan principle 4).
3. **No namespace under three names**, unless its concept is distinct (decision 3).
4. **The root is the image, its contents and its foundation**: the values of `packages=`,
   `files=`, `services=`, `users=`, `groups=`, `profiles=`, `base=`, `kernel=`, `rootfs=`
   and `mkosi=`, plus the lifecycle functions.
5. **A shipped component's configuration values live beside it** (`Egress` with
   `Network`, `JournalExport` with `Observability`).
6. **The import block is a table of contents**: one statement per subsystem the file
   touches, each on one line at 100 columns.

## 3. The layout

| Namespace | Names | Why here |
|---|---|---|
| `tundravm` | `Image`, `Profile`, `Service`; `File`, `Tree`, `Package`, `User`, `Group`; `Debian`, `Kernel`, `RootFS`, `Mkosi`; `load`, `lower`, `compile`, `bake`, `doctor`, `__version__` (18) | The image, what it contains and what it stands on. `tundravm.load(path)` is the spelling the test file `new` writes. |
| `tundravm.ports` | `TCP`, `UDP`, `AttestedTLS`, `TLS` (4) | A port and what it carries. `expose=` and `ingress=` open one inbound, `Egress(ports=)` outbound: the firewall is derived from exactly these. |
| `tundravm.systemd` | `Hardening`, `Resources`, `Shutdown`, `Notify`, `HttpProbe`, `TcpProbe`, `Every`, `Calendar`, `Listen`, `DropIn` (10) | The values of `Service` keywords: sandbox, limits, stop, readiness, schedule, socket. Each lowers to unit directives or a generated unit (a probe); `DropIn` is systemd's escape. |
| `tundravm.components` | `Component`, `Endpoint`, `ServiceRef`, `ServiceReady`, `ComponentReady`, `Resolved`, `Diagnostic`; `TeeBase`, `Network`, `StaticAddress`, `Egress`, `Firewall`, `Tdxs`, `EVIDENCE`, `SshAccess`, `DevAccess`, `Observability`, `JournalExport`, `PrometheusRemoteWrite`, `Nethermind`, `TaikoClient`, `Raiko` (22) | The base class, its export types, its `check(resolved) -> Diagnostic` contract, and the shipped library. |
| `tundravm.storage` | `Volume`, `VolumeDirectory`, `OperatorKey`, `TpmSeal`, `BrokerKey` (5) | The encrypted volumes and the policies `Volume(unlock=)` takes. |
| `tundravm.secrets` | `Secret`, `Credential` (2) | What `credentials=` takes: a value delivered after attestation, or a file loaded as a credential. |
| `tundravm.sources` | `Build`, `Install`, `Git`, `Http`, `GoBinary`, `CargoBinary` (6) | Software built from source: exactly what `tundravm lock` pins and `source-unpinned` reports. |
| `tundravm.errors` | `TdxError`, its 12 subclasses, `ErrorCode` (14) | Already the defining module. Image files never raise or catch them; scripts calling `bake` do. |
| `tundravm.testing` | unchanged (33) | Already a namespace. |

81 public names: the 80 distinct root and component names of `0b0df95`, without
`IMAGE_OBJECT_NAMES`, plus `ErrorCode`. Every other path that exported goes private, so
nothing gets a second home by accident: `tundravm.image` is `tundravm._image` (with
`BootReady` and `recipe_of`, which only its `__all__` listed), the workloads live in
`tundravm.components._nethermind` and `._taiko`, and the internal `tundravm.observability`
and `tundravm.platforms` are `_reporting` and `_platforms`.

## 4. Before and after

| File (length before) | Before: statements / lines / names | As built | Study's proposal | Fine-grained (5.1) | Coarse (5.2) |
|---|---|---|---|---|---|
| README quickstart (14 lines) | 1 / 1 / 4 | 2 / 2 / 4 | 2 / 2 | 3 / 3 | 1 / 1 |
| `service` template (81 lines) | 1 / 14 / 12 | 4 / 4 / 12 | 5 / 5 | 7 / 7 | 2 / 2 |
| `cloud` template (84 lines) | 1 / 14 / 12 | 4 / 4 / 12 | 5 / 5 | 7 / 7 | 2 / 2 |
| `prover` template (104 lines) | 1 / 18 / 16 | 6 / 6 / 16 | 7 / 7 | 9 / 9 | 3 / 3 |
| Surge prover (33 lines) | 2 / 2 / 10 | 5 / 5 / 10 | 5 / 5 | 6 / 6 | 3 / 3 |
| **Total** | **6 / 49** | **21 / 21** | **24 / 24** | **32 / 32** | **11 / 11** |

The templates' blocks shrink from 14 to 18 physical lines to 4 to 6; the quickstart and
Surge grow. Names are unchanged.

README quickstart, before `from tundravm import TCP, File, Image, Service`:

```python
from tundravm import File, Image, Service
from tundravm.ports import TCP
```

`service` and `cloud` templates, before one parenthesized block of `TCP, Component, Debian,
DevAccess, Diagnostic, Endpoint, File, Image, Package, Resolved, Service, TcpProbe`:

```python
from tundravm import Debian, File, Image, Package, Service
from tundravm.components import Component, DevAccess, Diagnostic, Endpoint, Resolved
from tundravm.ports import TCP
from tundravm.systemd import TcpProbe
```

`prover` template, before the same block with `AttestedTLS` for `TCP` plus `OperatorKey,
Secret, Volume, VolumeDirectory`:

```python
from tundravm import Debian, File, Image, Package, Service
from tundravm.components import Component, DevAccess, Diagnostic, Endpoint, Resolved
from tundravm.ports import AttestedTLS
from tundravm.secrets import Secret
from tundravm.storage import OperatorKey, Volume, VolumeDirectory
from tundravm.systemd import TcpProbe
```

Surge, before `from tundravm import AttestedTLS, DevAccess, Image, OperatorKey, RootFS, Secret,
Volume` and `from tundravm.components import Nethermind, Raiko, TaikoClient`:

```python
from tundravm import Image, RootFS
from tundravm.components import DevAccess, Nethermind, Raiko, TaikoClient
from tundravm.ports import AttestedTLS
from tundravm.secrets import Secret
from tundravm.storage import OperatorKey, Volume
```

Surge's block now says what the image is made of before its first line of code; five
statements, and a file of 36 lines instead of 33, is the cost.

## 5. Rejected alternatives

1. **Fine-grained, one namespace per concern** (`files`, `ingress`, `volumes`, `platform`,
   `network`, `observability`, `sdk` as well). The quickstart becomes three lines and the
   prover template nine, and four of those namespaces hold two or three names.
2. **Coarse** (root of 36 names, `components`, one `secrets` bucket for volumes, secrets and
   ingress, `errors`). Fewest lines, but the root stays a list to scan and `TCP` stays flat,
   which is what Franco asked to change.
3. **Flat.** One line everywhere; the namespace tells the reader nothing.
4. **`tundravm.files`** (`File`, `Tree`, `Package`, `User`, `Group`). These are the image's
   own contents; moving them costs the quickstart and every template a statement.
5. **`tundravm.ingress`** (`AttestedTLS`, `TLS`). Two names. An `ingress` namespace that also
   held `TCP` would invite `ingress=[TCP(8080)]`, which the image refuses; `ports` does not.
6. **`tundravm.volumes`.** `storage` holds the unlock policies as well as the volumes.
7. **`storage` and `secrets` merged.** One statement fewer in the prover template and Surge,
   but nobody looks for `Volume` or `TpmSeal` under secrets.
8. **`tundravm.mkosi`** (`Debian`, `Kernel`, `Mkosi`: the study's proposal). It reads as a
   wrapper of the mkosi dependency, and choosing the OS should not require knowing what
   builds it (decision 2).
9. **`tundravm.build`** for the sources. A trap: `tundravm build` builds the image, not a
   source; `tundravm.builds` (build directories) is one letter away; `tundravm.build.Build`
   stutters.
10. **`tundravm.service`** for the `Service` keywords. A module named after a class that
    lives in the root: `from tundravm.service import TcpProbe` beside `from tundravm import
    Service` reads like a mistake.
11. **`tundravm.platform`** for `Debian` and `Kernel`. In this codebase a platform is a cloud
    target (the Azure and GCP glue, `targets=`); Debian is not one.
12. **`tundravm.base`.** It matches `base=`, but in Python `base` reads as base classes, and
    `Component` is not there.
13. **`tundravm.network`** (`Network`, `StaticAddress`, `Egress`). `Network` is a shipped
    component: the namespace would either split the library or leave out `Network`. Its
    values go beside it (rule 5), and `TCP` goes to `ports`.
14. **`tundravm.observability`.** Three names configuring one component.
15. **`tundravm.sdk`.** The package is the SDK: `tundravm.load(path)`, not `tundravm.sdk.load`.
16. **`Diagnostic` and `Resolved` in `errors`.** A lint finding is not an exception; both
    exist only for `Component.check`.
17. **Workload submodules** (`tundravm.components.taiko`, plan 3.1). Two statements for
    Surge's three workloads; 22 names need no second level.
18. **Root re-exports.** One line again, but two spellings per name, and hints drift.

## 6. Prior art

- **FastAPI**: the everyday names at the root (`FastAPI`, `Depends`, `HTTPException`),
  topical submodules for the occasional (`fastapi.responses`, `fastapi.security`,
  `fastapi.testclient`). The closest model, minus its duplicates (`fastapi.Response` is
  also `fastapi.responses.Response`).
- **Pulumi**: the core in `pulumi`; resources namespaced by the service the user already
  names (`pulumi_aws.s3.Bucket`, `pulumi_aws.ec2.Instance`). That is rule 2.
- **Pydantic v2**: types defined in `pydantic.types` and `pydantic.networks`, re-exported
  at the root by a lazy `__getattr__`: one import line, two spellings per name, a reference
  by module while code imports from the root. Rule 1 avoids that.
- **Dagster**: nearly everything flat in `dagster`, internals in `dagster._core`: hundreds
  of root names, docs organised by concept. tundravm before this change.

## 7. Names that collide

- **`tundravm.secrets` and `tundravm secrets`**: a feature. `secrets deliver` delivers
  `Secret`s; `secrets unlock` acts on a `storage` volume, which the `storage` docstring
  says. Spell it `from tundravm.secrets import Secret`: `from tundravm import secrets`
  shadows the standard library's `secrets` in that file.
- **`tundravm.sources`**: `source-unpinned`, `lock --update` and the lockfile's source pins
  all say "source": a feature.
- **`tundravm.systemd`** and the internal `tundravm.declarative.systemd`: no reader sees
  the second.
- **Internal modules that looked public**: the build event reporters and the Azure and GCP
  glue are `tundravm._reporting` and `tundravm._platforms`.

## 8. What changed

1. `src/tundravm/image/` is `src/tundravm/_image/`; docstring roles name each value's home.
2. New `src/tundravm/{ports,systemd,storage,secrets,sources}.py`: a docstring opening with
   the table's rationale, the imports, `__all__`.
3. `src/tundravm/__init__.py` exports the 18 root names; `IMAGE_OBJECT_NAMES` stays in
   `_image._api`.
4. `src/tundravm/components/__init__.py` adds the seven authoring names, `StaticAddress` and
   `Egress`; every module under it is private.
5. `src/tundravm/templates.py` writes the blocks of section 4. `convert --from mkosi` needs
   no change: `Image`, `Debian`, `Tree` and `Mkosi` are all root names.
6. The eight example files; 05 takes `Git` from `tundravm.sources`. The package-file
   `tundravm.declarative.Resources` of 06 and `nethermind_base` stays internal.
7. `README.md` and every python block of `docs/guide.md`; the guide's slot table names each
   value's namespace, and a new "Imports" section lists them.
8. `src/tundravm/_reference.py`: `MODULES` is the nine namespaces in the table's order, each
   a `##` section opening with its docstring; an object two of them export is an error.
   `docs/reference/api.md` is regenerated.
9. `tests/unit/test_public_surface.py`: one frozen `__all__` per namespace, one home per
   public object, no root attribute for a namespaced name, no public-looking internal module.
10. `tests/lint/test_examples_api.py`: each `from tundravm... import X` in an example names
    X's home, which also refuses an internal path such as `tundravm.declarative.Git`.
11. `tests/lint/test_user_strings.py`: `RENAMED` refuses, in hints, templates and docs, a
    flat import of a namespaced name, `tundravm.<namespaced name>`, `tundravm.image`,
    `tundravm.components.taiko` and `.nethermind`, `tundravm.observability` and
    `tundravm.platforms`.
12. `tests/unit/test_package_layout.py` and every test that imported a moved name from the
    root.

## 9. Decisions (Franco, 2026-10-31)

1. `TCP` and `UDP` live in `tundravm.ports` with `AttestedTLS` and `TLS`; the quickstart
   is two import lines, as Franco's own example implied.
2. `Debian`, `Kernel`, `Mkosi` and `RootFS` stay in the root: they are the image's
   foundation slots (`base=`, `kernel=`, `rootfs=`, the `mkosi=` escape). There is no
   `tundravm.mkosi`: Codex's point that it reads as a wrapper of the dependency stands. It
   saves every template a statement (21 statements in total, not 24).
3. `tundravm.storage` and `tundravm.secrets` stay separate. `BrokerKey` is a
   `Volume(unlock=)` policy beside `OperatorKey` and `TpmSeal` (it also feeds
   `Secret(source=)`), so it lives in `storage`; `secrets` holds `Secret` and `Credential`,
   two names for a distinct concept.
