# Site commissioning checklist (v1)

How to bring a new site (or a new machine) from nothing to "frames flowing". It is written for a person doing it by hand today, and it is the specification for the commissioning wizard later (section 6).

**Status:** covers the layout, code, camera capture and secrets. The algorithms, mail, cleanup, PLC and UI are not config-driven yet (D-023), so their steps are listed as "not yet". Update this file whenever a worker becomes config-driven.

## 0. Roles
| Who | Does |
|---|---|
| Installer (developer or trained colleague) | All steps below |
| Customer / plant IT | Provides network details (PLC IP, mail account, recipients), power, camera cabling, a mounting position for each camera |
| Developer (central) | Provides a tagged release, the model files and the site data sheet defaults |

Bring: the release tag to install, the model files, a filled site data sheet (section 1), and a way to reach GitHub from the machine.

## 1. Site data sheet (collect before or during the visit)
Copy the Tadipatri column as the starting point; mark anything not yet known.

| Value | Where it comes from | Tadipatri |
|---|---|---|
| Site id, hostname, login user | you choose | tadipatri, dpc-tadipatri, dpc_tadipatri |
| Release tag to install | developer | (first: v0.1) |
| Camera serials | detected (step 4) | 24824427 and 24874775 (both Basler, detected) |
| Which serial is CAM1 / CAM2, which side (LEFT/RIGHT) | physical mounting: look at the image | CAM1 = LEFT = 24824427, CAM2 = RIGHT = 24874775 |
| fps, exposure, rotation per camera | defaults from a similar site, tuned on site | CAM1: 4.0 fps, 1500 us; CAM2: 4.0 fps, 2000 us; no rotation (rotate 0, needs D-041 in code) |
| DB name | you choose | tadipatri4 |
| DB user and password | a new password per site; a dedicated user is recommended, `root` is allowed (D-050) | to create |
| Health table name | per site (hardcoded in code today, D-038) | tadipatri_health_status_table |
| PLC type, IP, registers | plant network and PLC program | to fill |
| Mail sender, credentials, recipients | customer or IT | to fill |
| Wheel count, expected cycle total, object sizes, wheel-stand counts | machine specification and calibration | to fill |
| Model files and thresholds | developer | to fill |

Passwords and secrets are written only into that machine's `site.yaml` (chmod 600, D-048): never into this repo or SITES.md; share it only as a `--redacted` copy.

## 2. Procedure
The step-by-step installation lives in **`docs/INSTALL.md`** (kept current with every change). This file keeps the site data sheet (section 1), how a new model is packaged (section 2c), the acceptance checklist (section 3) and troubleshooting (section 4).

## 2c. Package an existing model (first install, D-056, D-061)
Model files are never in git (too large; distributed through OneDrive later, D-057). On a site that already has a trained model:
1. Make its Detectron config self-contained (model-zoo configs point to a `_BASE_` file that is not copied), in the Python that has Detectron2:
   ```
   python3 - <<'EOF'
   from detectron2.config import get_cfg
   cfg = get_cfg()
   cfg.merge_from_file("<detectron2>/configs/COCO-InstanceSegmentation/mask_rcnn_R_101_FPN_3x.yaml")
   cfg.MODEL.ROI_HEADS.NUM_CLASSES = <number of classes>
   open("/tmp/<model>.yaml", "w").write(cfg.dump())
   EOF
   ```
2. Package it (classes in the model's output order = the old `ALL_CLASS_NAMES`):
   `python3 scripts/make_model_package.py --name <name> --version <yyyy.mm.dd> --weights <.pth> --detectron-yaml /tmp/<model>.yaml --classes <c1,c2,...> --legacy-label-map`
3. `python3 scripts/dpc_check.py`: line `model` = PASS.
**Tadipatri, exact commands used (2026-10-22), result `dpc_maskrcnn/2026.10.12`:**
```
python3 - <<'EOF'
from detectron2.config import get_cfg
cfg = get_cfg()
cfg.merge_from_file("/home/dpc_tadipatri/detectron2-0.6/detectron2/configs/COCO-InstanceSegmentation/mask_rcnn_R_101_FPN_3x.yaml")
cfg.MODEL.ROI_HEADS.NUM_CLASSES = 19
open("/tmp/dpc_maskrcnn_R101.yaml", "w").write(cfg.dump())
EOF

cd ~/INSIGHTZZ/code
python3 scripts/make_model_package.py --name dpc_maskrcnn --version 2026.10.12 --weights /home/dpc_tadipatri/INSIGHTZZ/old/MODELS/FIRST_MODE/Mahir_Oct_12_Int_Model/model_0041159.pth --detectron-yaml /tmp/dpc_maskrcnn_R101.yaml --classes half_bottom_chain,bottom_chain_bolt_clip,wheel,bottom_chain_bolt,top_chain_bolt,wheel_stand,wheel_bolt,top_chain,nut_bolt,half_bucket_clip,top_chain_bolt_clip_missing,bottom_chain,half_top_chain,half_wheel,bucket_clip,top_chain_bolt_clip,high_dusty_bucket_clip,plate,wheel_magnet --legacy-label-map
```
Output: 19 classes, label map {} (the legacy dusty_* classes are not in this model), trained_at 2026-10-12, `current -> 2026.10.12`.

## 3. Acceptance checklist (sign-off)
- [ ] Layout script printed `OK`; code is at the intended release tag (`git describe --tags`)
- [ ] Both serials detected; CAM1/CAM2 and sides confirmed by looking at the images
- [ ] Each camera: live image updates, `INF` fills at the configured fps, `_tmp` empty, rotation correct, log clean
- [ ] Config files and `site.yaml` are `chmod 600`, outside the repo, and not in `git status`
- [ ] DB password unique to this site (`root` allowed, D-050)
- [ ] `INF` queues emptied after the test; disk usage noted
- [ ] `docs/SITES.md` updated and committed

## 4. Troubleshooting
| Symptom | Likely cause | What to do |
|---|---|---|
| Camera list is empty | power, cable, network (GigE: not on the same subnet), pylon runtime missing | fix the physical or network link first; check with Basler's pylon Viewer |
| `imports OK` fails on pypylon | pylon runtime not installed | install Basler's pylon runtime, retry |
| `os.link failed` or "different filesystem" in the log | frame folders on different mounts | all of `frames/CAMn` must be on one filesystem; rerun the layout script check |
| `_tmp` fills up | writes failing after the temp file is created | check disk space and permissions; read the log |
| `INF` count not growing | camera not grabbing, wrong serial, config path wrong | check the serial in the XML, the log, and `BASLER_CONFIG` |
| Image is sideways | rotation setting | set element 18 `ROTATE_DEG` in the camera's XML (D-041), restart |
| Log shows `CHANGE_ME` (DB user or health table) or `Access denied for user 'CHANGE_ME'` | the capture read a template copy, not the site config: started without `BASLER_CONFIG`, so it took `BASLER_CONFIG1.xml` from the working folder | always start with `BASLER_CONFIG=$HOME/INSIGHTZZ/config/BASLER_CONFIGn.xml`; delete config copies from the code folder; the `Started pid` log line shows `config=` |
| Health row changes only at start | by design: written at start, at once on a state change, otherwise every 10 min (`HEALTH_INTERVAL_S`) | wait 10 min or pull a cable to see `NOK` at once |
| `git push` or fetch fails with authentication | HTTPS and no token, or key not added | use the SSH deploy key (step 2) |

## 5. Update and rollback of an installed site
```bash
cd ~/INSIGHTZZ/code && git fetch --tags && git checkout v0.<n>      # then restart the workers
```
Roll back by checking out the previous tag. Never edit code on the site; update `docs/SITES.md` with the new tag.

## 6. What the wizard will automate (specification)
| Step | Wizard |
|---|---|
| 5 | Detects cameras and lists the serials; the installer assigns CAM1/CAM2 and the side |
| 5, 7 | Test-opens each camera with the chosen fps and exposure, and shows a preview image to confirm the side and rotation |
| 6 | Writes `site.yaml` (with the secrets, chmod 600), runs the renderer to produce the legacy XMLs and `workers.yaml`; paths are derived from one root, never typed |
| 8 | Creates the DB, a limited user and a generated password, and the tables |
| 9 | Checks that the model files exist and match their checksum |
| 3, 11 | Records the installed release tag and the site data sheet |
| 7, 3 | Runs the acceptance checks and prints a sign-off report |
