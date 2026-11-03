# Docs clarity: the pages read by a stranger

Date: 2026-11-03. Base: master at `f450eff`. Reader: knows Linux and Docker, not mkosi or
TDX. Run here: `check`, `show --diff` of every stage, `lock --check`, `convert`, `doctor`.

## The path today

The README (517 words) defines tundravm through mkosi and TDX, installs, lists stage 1 and
links the guide. The guide (7999 words) is the only other page: a status table keyed to
plan gates, project setup and backends, the six stages, then fifteen reference sections.
The diagnosis held: one page is a tutorial, a concept explainer and a reference at once.
The stage story sits in its middle and leans on definitions hundreds of lines further down.

## The ten worst stumbles

1. README: "tundravm is mkosi made opinionated for confidential VMs". No page defines
   mkosi or a confidential VM.
2. README: "a measured UKI ... and the `tdxs` evidence service, whose quote `attest`
   checks". Five undefined terms in one sentence.
3. Guide intro: "The work follows the gates of the v2 plan". Internal status comes second.
4. "compare the image with the committed `mkosi/` tree, which depends on the lock". Tree
   and lock are used before either is defined.
5. "`build:go` blocks without a host `go`": backend rows come before the first boot.
6. Stage 1: "[`dev`](#run) builds any stage's dev profile". Profile is undefined, and the
   link jumps 900 lines ahead.
7. Stage 2: "Nethermind brings three things the boot did not have". Volume, secret,
   attestation and control plane arrive in one paragraph.
8. Stage 2 lists a 60-line `Component` class before saying what a component is.
9. No stage shows what it changed; only stage 1 names an output.
10. The dev/production split, the main safety idea, is scattered over five sections.

The probe table also lacked `rtmr`, `key:instance` and `owner:`.

## The structure

| Page | For |
|---|---|
| `README.md` | what, who, the five-command first run, where next |
| `docs/terms.md` | each term once, in the order the stages need it |
| `docs/guide.md` | the six stages: one idea, the diff, a command and its real output each |
| `docs/howto.md` | fourteen tasks outside the story, each under 120 words |
| `docs/manual.md` | the reference prose: slots, rules, files, tables, errors |

The five pages stay under today's 8516 words. The cuts are duplicates of the generated
command reference (deploy settings, `run` modes) and API reference (testing helpers).
`terms.md` is not `concepts.md` because that name is on the removed-doc denylist of
`tests/lint/test_user_strings.py`. The guide keeps its name: `new` and the reference call
stage 1 "stage 1 of the guide". The tests pin a ceiling per page, one budget for the five,
the stage order, the task ceiling, and a definition in `terms.md` for each bold term of
the guide.

`check`, `show --diff` and `convert` output was run here. The stage 1 probe lines come
from CI run 41508188448's `boot` job, the build report from its `reproduce` job.
