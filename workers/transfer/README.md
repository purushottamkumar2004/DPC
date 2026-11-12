# Transfer service (P12, D-104)

One program, `transfer.py`, under the Supervisor. It moves data to and from OneDrive/SharePoint
through Microsoft Graph, **only when a person asks for it** (a UI button, or a request file by
hand). Each request runs one job to the end; an interruption continues where it stopped.

| Button (UI-4) | Request file in `<root>/run/` | What happens |
|---|---|---|
| Upload one cycle | `transfer.upload_cycle` | The next full revolution (plate to plate) of LEFT and RIGHT plus `plate_images` plate frames per side is staged while it happens, packed into chunks and uploaded |
| Update model | `transfer.model_update` | The newest dated zip in this site's models folder is downloaded, packaged, verified and marked `ready` |
| Cancel | `transfer.cancel` (content `upload_cycle` or `model_update`; empty = both) | The job stops and its partial data is deleted |

Without the UI: `touch ~/INSIGHTZZ/run/transfer.model_update`. The file disappears when the job is
accepted. A second click while the same job runs is ignored.

**Retries.** Temporary problems (no network, timeouts, Graph 5xx/429, an expired upload session,
a restart of the program or the machine) retry by themselves after 1, 2, 5, 10, then every 30
minutes, from the byte where they stopped. Permanent problems (permission refused, a bad model
zip, staging full, not enough disk) stop the job as `failed` with the reason in the heartbeat and
`logs/transfer.log`; after fixing the cause, clicking the same button again continues it.

**Models are never switched here.** After `ready`, the Supervisor's `models.auto_update`
decides: `download` = a person switches (`supervisor.py model switch`), `install` = the
Supervisor switches with the self-test; a pinned model is never switched (D-060, D-087).

## Where things are

```
<root>/run/transfer.*                       request files
<root>/data/state/transfer/                 upload_cycle.json, model_update.json (the jobs),
                                            counters.json (cycle numbers per date),
                                            failed_models.json, status.json
<root>/data/transfer/staging/<cycle>/       a staged cycle until its upload is confirmed
<root>/data/transfer/downloads/<model>/     a model zip while it downloads
<root>/logs/transfer.log
```
Staging holds hard links (no extra disk while RAW still has the frame), capped by
`transfer.upload.staging_max_gb`. Cleanup must not touch `data/transfer/` (owned and capped
here). RAW, PLATE and every other folder are only read.

### Upload layout on OneDrive
```
<transfer.upload.remote_folder = DPC/data/<site.id>>/2026-11-10_cycle1/
    LEFT/part_0001.tar ...      the cycle's frames, plain tar, about chunk_mb each
    LEFT/plate_0001.tar         the spread plate frames
    LEFT/cycle.json             site, side, camera serial/fps/exposure, start/end frame and time,
                                frames, plate frames, wheel counts, model name/version
    RIGHT/...
    manifest.json               uploaded LAST: every chunk with its sha256 and frame range
```
A cycle is complete on OneDrive when `manifest.json` is there. Check a chunk after download with
`sha256sum` against the manifest.

### Model layout on OneDrive
```
<transfer.models_folder = DPC/models/<site.id>>/<model name>/2026.11.20.zip
```
- One folder per site; a site reads only its own. One folder per model name it uses
  (`dpc_maskrcnn`, or `dpc_maskrcnn_left` / `_right` for per-side models, D-086).
- Zip name = the version, year first: `2026.11.20.zip`, `2026-11-20.zip` or `2026_11_20.zip`;
  a second one the same day `2026.11.20b.zip`. Other names are ignored.
- Zip content: exactly one `.pth` (normally `model_final.pth`) and exactly one class `.json`
  (the training tool's `{"0": "nut_bolt", ...}`, or COCO), at the top or inside one folder
  (Windows "Send to > Compressed folder" is fine). The Detectron config is not needed: the
  shared one in git is used (D-088).
- Only the newest zip, only when newer than the newest installed version, only when it was
  last changed at least 10 minutes ago (upload finished). A zip that failed is skipped until it
  is replaced.

## Microsoft Graph permission (Q18): steps for the tenant admin

The transfer uses the **same app registration as the mail** (`graph.tenant_id`, `client_id`,
`client_secret` in `site.yaml`), app-only (no user signs in). It needs one more **application**
permission. Every option needs **admin consent**, which only a Global Administrator or a
Privileged Role Administrator can give. Choose one:

### Option A (recommended): a SharePoint site, `Sites.Selected`
The app can reach only the site it is granted, nothing else in the tenant.

1. Create (or pick) a SharePoint site for DPC data, e.g. `https://contoso.sharepoint.com/sites/DPC`.
   Its default document library holds `DPC/data/...` and `DPC/models/...`.
2. Microsoft Entra admin center > App registrations > the DPC app > API permissions >
   Add a permission > Microsoft Graph > **Application permissions** > `Sites.Selected` > Add.
3. On the same page: **Grant admin consent for <tenant>**.
4. Give the app **write** on that one site (a SharePoint Administrator does this; `write` also
   allows reading). Either with PnP PowerShell:
   ```powershell
   Connect-PnPOnline -Url "https://contoso.sharepoint.com/sites/DPC" -Interactive -ClientId <pnp-admin-app-id>
   Grant-PnPEntraIDAppSitePermission -AppId <DPC app client id> -DisplayName "DPC transfer" -Permissions Write
   ```
   or with Graph Explorer, signed in as the SharePoint admin with `Sites.FullControl.All` consented:
   ```
   GET  https://graph.microsoft.com/v1.0/sites/contoso.sharepoint.com:/sites/DPC      -> note "id"
   POST https://graph.microsoft.com/v1.0/sites/<id>/permissions
        {"roles": ["write"],
         "grantedToIdentities": [{"application": {"id": "<DPC app client id>", "displayName": "DPC transfer"}}]}
   ```
5. In `site.yaml`: `transfer.drive: {site: "contoso.sharepoint.com:/sites/DPC"}`.

### Option B: a folder in one user's OneDrive, `Files.SelectedOperations.Selected`
Access to chosen files/folders only. Steps as A, but consent `Files.SelectedOperations.Selected`,
and grant the app `write` on the `DPC` folder with `POST /drives/<drive-id>/items/<folder-id>/permissions`
(Microsoft's "driveItem: create permissions" page shows the body). Then
`transfer.drive: {user: "dpc@contoso.com"}`. **Not yet tested on a real tenant.**

### Option C (simple, broad): `Files.ReadWrite.All`
Consent `Files.ReadWrite.All` (application). No per-site step, but the app can read and write
**every** OneDrive and SharePoint library in the tenant; a security review may refuse it. Use
`transfer.drive` as in A or B.

### Check it
On a configured site (rendered `transfer.json`):
```bash
cd ~/INSIGHTZZ/code/workers/transfer
DPC_WORKER_CONFIG=~/INSIGHTZZ/config/rendered/transfer.json python3 transfer.py --check
```
Before Apply (setup wizard step 8b, F91: no `site.yaml`, no rendered file), the settings go on the
command line and the Graph login on stdin, never as an argument (`ps` shows arguments to everyone):
```bash
echo '{"tenant_id": "...", "client_id": "...", "client_secret": "..."}' | \
  python3 transfer.py --check --json --graph-stdin --site-id tadipatri \
      --drive-site contoso.sharepoint.com:/sites/DPC        # or --drive-user dpc@contoso.com
      # optional: --models-folder P  --upload-folder P | --no-upload  (defaults DPC/models/<site.id>,
      # DPC/data/<site.id>); without --graph-stdin: env DPC_GRAPH_TENANT_ID / _CLIENT_ID / _CLIENT_SECRET
```
Steps: `token`, `drive`, `models_folder` (listing; a missing folder is fine on a new site,
`exists` says which), `upload_write` (a test file written, read back and deleted in
`<upload folder>/_check/`; `skipped` with `--no-upload`). Each is `ok`, `fail` or `skipped`
(skipped after a failed token or drive). `--json` prints exactly one object:
```json
{"ok": true, "mode": "direct", "drive": {"site": "..."}, "models_folder": "DPC/models/tadipatri",
 "upload_folder": "DPC/data/tadipatri",
 "steps": [{"step": "token", "status": "ok", "detail": "login accepted"}, ...], "error": null}
```
`error` is set (and `steps` empty) when the check could not run: wrong arguments, missing login.
Exit 0 = every step that ran passed; 1 = a step failed or `error`; 2 = wrong call (argparse usage).
Reading: `HTTP 403 accessDenied` = consent or the site grant (step 3 or 4) is missing; `token
refused` = wrong tenant/client id/secret, or the secret expired; `network` = no route to Microsoft.

Mail keeps its own permission (`Mail.Send`, assumed, not checked here).

## Heartbeat and events
`dpc/worker/transfer/health` every 10 s (common/heartbeat.py): `state` (idle | staging | uploading
| downloading), `upload_cycle` {state, phase, cycle, frames {LEFT, RIGHT}, bytes_done, bytes_total,
attempts, next_retry_at, message}, `model_update` {state, phase, bytes_done, bytes_total, result,
downloaded, attempts, next_retry_at, message}, `bytes_uploaded`, `bytes_total`, `last_upload_at`,
`last_model_check_at`, `last_error`.

`dpc/worker/transfer/event`: `cycle_staged` {cycle, frames, plate_frames, bytes},
`cycle_uploaded` {cycle, remote, chunks, bytes, seconds}, `upload_failed` {cycle, reason, phase},
`model_downloaded` {name, version}, `model_download_failed` {name, version, reason},
`model_update_done` {result: downloaded | up_to_date, models}, `job_cancelled` {job}.

## Needs from other parts (P12 handoff)
- **Plate signal (W-inspection, F76):** heartbeat fields `plates_seen`, `last_plate_frame`,
  `last_plate_at` (+ optional `last_cycle_wheels`). Until then an upload job waits and says so.
- **transfer.json (CONFIG):** see the docstring of `transfer_settings.py`.
- **Supervisor:** the `transfer` worker entry; reacting to `model_downloaded` when
  `auto_update: install`.

## Tests
```bash
python3 -m unittest discover -s workers/transfer/tests -t . -v     # from the repo root
```
A fake Graph (in memory, with cut connections, expired sessions and corrupted uploads) and fake
plate heartbeats; no network, no real drive.
