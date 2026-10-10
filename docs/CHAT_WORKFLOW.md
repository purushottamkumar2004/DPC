# How to work with Claude on this repo (v3: roles, GitHub, handoffs)

Chats cannot talk to each other and cannot push to GitHub. Everything they share lives in the repo docs, and code reaches GitHub through you (commits) or Claude Code. Contracts are the glue (`docs/CONTRACTS.md`); folder ownership is in `docs/OWNERS.md`.

## Roles
| Chat | Owns | Never touches | Attach |
|---|---|---|---|
| **MASTER** (one, long-lived) | `docs/*`, backlog, contract changes, handoff review | code | nothing extra (Project knowledge has the docs) |
| **SUPERVISOR** | `supervisor/`, MQTT parts of `common/` | worker internals | `supervisor/` |
| **W-<worker>** (one per task) | `workers/<worker>/` | any other folder | that folder + `docs/CONTRACTS.md` |
| **UI** | `ui/` | workers, supervisor | `ui/` |
| **CONFIG** | `config/`, config loader in `common/`, `scripts/` | worker logic | `config/`, `common/` |
| **SITE-<name>** | that site's entry in `docs/SITES.md` | code | site profile + sanitized example config |

Rules: one task per worker chat; start a new chat when the task is done or the chat gets long; never mix two workers in one chat. If a task is the interface between two owners (a topic, a DB column, a path), it goes to MASTER first.

Name chats `<role> | <task>`, e.g. `W-frame_capture_basler | device-lost recovery`.

## The loop for every task
1. **MASTER**: pick the next backlog item, write the brief (task, files, contract points, done-when).
2. Open a **new chat inside the Project**, paste the opener below.
3. Work on a branch `feat/<worker>-<task>`.
4. At the end ask the chat to **produce the handoff** (block below).
5. You test, open a PR, merge to `main`.
6. Paste the handoff into **MASTER**; it updates `DECISIONS.md` and `CONTRACTS.md`; you commit them.
7. **Sync now** in the Project so the next chat sees the new code and docs.

## Openers (copy, fill in)
Worker:
```
ROLE: W-<worker>   (frame_capture_basler | frame_capture | inspection | mail | cleanup | plc)
TASK: <one sentence>
OWNS: workers/<worker>/
CONTRACT: <which parts of CONTRACTS.md apply>
DO NOT CHANGE: <business logic / contracts that must stay>
DONE WHEN: <testable condition>
```
Supervisor:
```
ROLE: SUPERVISOR
TASK: <e.g. worker mode (service | oneshot | periodic), queue_lag health, richer heartbeat>
OWNS: supervisor/, common/mqtt*
CONTRACT: MQTT section of CONTRACTS.md (propose changes, never invent topics)
DO NOT CHANGE: worker code, DB schema
DONE WHEN: <testable condition>
```
UI:
```
ROLE: UI
TASK: <e.g. live frames and worker health>
OWNS: ui/
READS ONLY: DB tables, data/ui_live and data/frames/*/UI, MQTT status topics (per CONTRACTS.md)
DO NOT CHANGE: how workers write data; propose contract changes to MASTER
DONE WHEN: <testable condition>
```
Config:
```
ROLE: CONFIG
TASK: <e.g. site.yaml schema and loader with tests>
OWNS: config/, common/config*, scripts/
CONTRACT: CONTRACTS section 7 (site.yaml) and section 3 (legacy XML tag order)
DONE WHEN: <testable condition>
```

## Handoff (ask the chat to produce it, paste into MASTER)
```
HANDOFF <role> <date>
Changed files: ...
Behaviour change: ...
Contract impact (topics, config keys, DB columns, paths): none | ...
Decision to record (D-xxx): ...
How I tested: ...
Open risks / follow-ups: ...
```

## Git conventions
- **One `main` for all code (D-039).** `main` = deployed or ready to deploy; every site runs the same code. Work branches: `feat/<worker>-<task>`. A site that needs a code fix before the next release gets a short-lived `hotfix/<site>-<what>`, merged to `main` quickly and released as a new tag. No long-lived site branches: sites differ by config, not by code.
- One logical change per commit; message `<worker>: <what> (D-xxx)`.
- Tag releases `v0.<n>`. A site pins a tag (`git fetch && git checkout v0.<n>`); updating = a newer tag, rollback = an older one. Golden images pull a tag. Record the tag each site runs in `docs/SITES.md`.
- Docs change in the same PR as the code that changes them.
- Never in git: `secrets.env`, `site.yaml`, real XML configs, models, images, logs (see `.gitignore`). Commit only `*.example.*` files. Site settings (no secrets) may live in a separate private `dpc-sites` repo, one folder per site.

## GitHub and Claude
- **Project knowledge**: instructions = `docs/PROJECT_INSTRUCTIONS.md`; knowledge = the small docs in `docs/`. Add the repo with **"+" > GitHub**; it is a read-only snapshot, refreshed with **Sync now** after each merge. Use **Configure files** to leave out large files (`LeftAlgorithm.py`, `RightAlgorithm.py` are about 125-155 KB each): attach those only in the chat that edits them.
- **Chat to repo**: the chat produces files, you copy them in, review the diff, commit.
- **Claude Code** on your machine, in the repo, for multi-file work (supervisor, config loader, renames, merges): it reads files itself, runs tests and can commit, and does not fill a chat.
- Rotate any secret that appears in a chat; never paste `secrets.env`.
