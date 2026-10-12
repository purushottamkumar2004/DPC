# Claude Project setup (one time) and routine

Claude cannot change Project settings itself. The one-time setup below is about ten minutes of clicks; after it, every new chat in the Project starts with the instructions, the docs and the repo already loaded.

## A. One-time setup
1. **Get the latest docs into GitHub.** Apply the newest `dpc-docs-v3.zip` to `~/INSIGHTZZ/code` (`apply_to_repo.sh`), commit, push. In the repo's Settings, confirm it is **Private**.
2. **Instructions.** Open the DPC Project and paste the whole of `docs/PROJECT_INSTRUCTIONS.md` into **Instructions**. If the box refuses the length, paste sections 1 to 7 and leave 8 and 9 in the repo docs.
3. **Knowledge.** Under Project knowledge click **+ > GitHub**, choose the `DPC` repo and press **Sync now**. Use **Configure files** to **exclude**:
   - `workers/inspection/LeftAlgorithm.py` and `RightAlgorithm.py` (about 125-155 KB each; attach them only in the chat that edits them)
   - any `tests/replay/**` sample images, `.dpc_backup_*`, models, logs, real configs (none should be in git anyway)
   Keep everything else: `docs/`, `supervisor/`, the other workers, `common/`, `config/examples/`, `scripts/`, `README.md`.
4. **Check it loaded.** Open a new chat in the Project and send the verification prompt in section C. It should answer from the docs without you pasting anything.
5. **Create the standing chats** (section B) and star the MASTER chat so it is easy to find.

## B. Chats to create now
| Chat | Purpose | First message |
|---|---|---|
| `MASTER` (long-lived, starred) | state, backlog, contracts, decisions | the MASTER starter below |
| `W-frame_capture_basler` | the open fixes in CODE_AUDIT section 6 | opener from `CHAT_WORKFLOW.md` |
| `CONFIG` | `site.yaml` schema and loader, then `dpc-check` | opener from `CHAT_WORKFLOW.md` |
| `SUPERVISOR` | mode, queue_lag, heartbeat, start-after | opener from `CHAT_WORKFLOW.md` |
| `UI` | later, after the MQTT and DB contracts are settled | opener from `CHAT_WORKFLOW.md` |

Name a chat `<role> | <task>` and open a **new chat** per task; do not keep reusing one.

### MASTER starter (paste as the first message)
```
ROLE: MASTER
Read docs/STATUS.md, docs/DECISIONS.md (open questions) and docs/CODE_AUDIT.md section 6 from the Project knowledge.
TASK:
1. Summarise the current state in 10 lines.
2. Propose the next 5 backlog items in order, each with owner chat, files to change, contract impact and a testable done-when.
3. List decision or contract gaps that block them.
OWNS: docs/*. Do not write code in this chat.
OUTPUT: markdown I can paste into docs/STATUS.md.
```

## C. Verification prompt (checks the environment works)
```
Without me pasting anything: (1) list the doc files you can see in Project knowledge, (2) state the release/branching model we decided, (3) state the deployed folder layout root and where secrets live, (4) name the first three backlog items.
```
Expected: the docs listed (including `COMMISSIONING.md`, `OWNERS.md`, `SITES.md`, `STATUS.md`); one `main` branch with tagged releases (D-039); root `~/INSIGHTZZ` with secrets in `config/secrets.env`; backlog led by the config loader and `site.yaml` schema. If it cannot see the docs, press **Sync now** again and check **Configure files**.

## D. Routine after every task
1. The worker chat produces the handoff block (`CHAT_WORKFLOW.md`).
2. You test, commit on a branch, merge to `main`.
3. Paste the handoff into MASTER; it updates `STATUS.md`, `DECISIONS.md` and `CONTRACTS.md`.
4. Commit those docs, push, press **Sync now** in the Project.
5. Tag a release when a tested set of changes should go to sites (`v0.<n>`).

## E. Secrets and repo access (D-042)
- **Chats may hold secrets.** To let Claude work on the repo directly, paste the GitHub token at the start of a chat (or keep it in a Project knowledge text file added by hand, not through GitHub). Claude clones, edits, commits and pushes; you do not download or upload code.
- **Token:** fine-grained, only the `DPC` repo, Contents read and write, an expiry you choose. Replace it when it expires or if the chat history is ever shared.
- **Never in the repo:** passwords, tokens, `secrets.env`, real configs with credentials, models, images, logs. Git history is permanent and every site installs from this repo.
- **What Claude pushes where:** docs (MASTER) go straight to `main`. Code goes to a `feat/` or `hotfix/` branch; you test it on a machine and merge it, because code runs on the plants.
