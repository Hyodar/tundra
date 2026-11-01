# API review: what a user writes, after a day of landings

Date: 2026-11-01. Base: master at `39c31ec`. Branch: `api-review`. The question, from
Franco's goals: which changes to the public API most reduce what a user writes or has to
know, for mkosi made accessible, opinionated (mkosi, systemd, Debian), easily usable for
TDX, with no compatibility kept?

Method: one prompt (the goals, the whole `docs/reference/api.md`, the guide's image,
services, components, profiles and sources sections, the `service` and `prover` templates
as `tundravm new` writes them, the surge prover) to Codex (`gpt-6-astra`, effort high,
read-only), asking for the ten changes ranked by reduction over size and risk, each with
before and after code. Its ranking is below, with a verdict on each. Refused: anything that
reverses a decision already taken, weakens an opinion, or touches a section 6 contract.
"Do later" names the file that owns it, where another branch is in flight.

## The ranking and the verdicts

| # | Codex's change | Size | Verdict |
|---|---|---|---|
| 1 | The common values (`TCP`, `Secret`, `Volume`, probes, ingress, `GoBinary`) in the root | M | **Refuse**: it reverses the namespaces decision of 2026-10-31 (one home per name, the root is the image and its foundation), taken a day ago on the same trade-off |
| 2 | `Service.endpoint(port)`: the `Endpoint` a plain service answers at, with its readiness | S | **Do** |
| 3 | `GoBinary(package=".")` as the default | S | **Do later**: `GoBinary` is defined in `declarative/utils.py`, outside this branch's files |
| 4 | A volume mounts at a path derived from its name | S | **Do** |
| 5 | `hardening="python"`: a preset by name | S | **Do later**: `_image/_lower.py` passes `service.hardening` on as a `Hardening`, so the field cannot widen to a name here |
| 6 | `Service(quotes=True)` for `requires=[EVIDENCE]` plus `groups=["tdx"]` | S | **Do later**: `EVIDENCE` and the Raiko service that uses it live in `components/_base.py` and `components/_taiko.py`; both move with it |
| 7 | The image collects the volumes and secrets its services bind | L | **Do**: S here, since lowering already reads `image.volumes` and `image.secrets` |
| 8 | A secret reference binds itself | L | **Do later**, in a smaller form (a `Secret` as an argv element, as an `Endpoint` already is): an edit of `argv=` could then add a binding, which `_image/_change.py` must refuse as it refuses `credentials=` |
| 9 | A profile edit may change readiness, credentials and units, regenerating them | L | **Do later**: `_image/_change.py` and `_image/_lower.py` (the unit graph is rebuilt per edit) |
| 10 | `AttestedTLS` binds its key to the quote, with a verified client | L | **Do later**: gate 3 (tundra-tools, `attestation.py`); the image side does not change |

Codex's rejections, which stand: one mixed content list instead of slots; exposed ports
inferred from argv or probes; a default unlock policy; a sandbox or execution mode chosen
by inference. It counted template cleanup as possible today rather than an API change; it
is done here anyway, since the templates are what a user copies.

## What changes

**Templates say only what differs from a default** (no API change). The `service` and
`prover` templates drop `base=Debian("trixie")` (the default), the prover's snapshot and
EFI-stub constants (the locked snapshot is the default and the lock records it),
`Package("ca-certificates")` (neither program makes TLS calls) and `rotation="restart"`
(the default). The `cloud` template keeps its pin: showing one is its point.

**7. The image collects its volumes and secrets.** `Image(volumes=, secrets=)` are gone.
`image.volumes` and `image.secrets` are derived: every `Volume` a service's `state=` sits
on and every `Secret` a service binds (components expanded, observability and every
profile's additions included), plus each `TLS` certificate, in order of first use. Two
different values under one name are refused, as two declarations were.

```python
# before
image = Image("surge-tdx-prover", components=[execution, driver, prover],
              volumes=[data], secrets=[jwt, chain, config], ingress=[...])
# after
image = Image("surge-tdx-prover", components=[execution, driver, prover], ingress=[...])
```

The `does not declare` refusals go with the slots. A volume or secret nothing binds no
longer exists in the image, which is what it did before at run time: nothing waited for it.

**4. A volume's mount is derived from its name.** `Volume(name, *, unlock, mount=None)`;
no `mount=` is `/persistent/<name>`. `unlock=` keeps no default: it is the trust decision.

```python
data = Volume("data", mount="/persistent", unlock=OperatorKey())  # before
data = Volume("data", unlock=OperatorKey())                      # after: /persistent/data
```

The surge prover takes the default, so its tree moves from `/persistent/<dir>` to
`/persistent/data/<dir>`.

**2. `Service.endpoint(port, *, path="/", scheme="http")`** returns
`Endpoint("127.0.0.1", port, path, scheme, ready=service.ready)`: what an ingress fronts or
a dependent requires. It opens no port and changes no listen address. Example 06:

```python
ingress=[AttestedTLS(443, upstream=Endpoint("127.0.0.1", 8080, ready=app.ready))]  # before
ingress=[AttestedTLS(443, upstream=app.endpoint(8080))]                            # after
```

A component's `self.service("app")` is a `ServiceRef` (`_image/_component.py`); giving it
the same method is the follow-up that shortens the templates' `http` property.

## Also seen, not ranked

- `examples/03_components.py` imports `Repository`, `Unit`, `Task`, `ConfigFile` and `Run`
  from `tundravm.declarative`: a packaged unit, an extra apt repository and a build-time
  command have no public spelling yet. Do later, as its own design note.
- `readiness=TcpProbe(8080)` repeats the port of `expose=[TCP(8080)]`. Inferring it would
  add a probe unit to every exposed service, which changes what `requires=` waits for:
  refused, for the reason Codex refused inferring ports.
