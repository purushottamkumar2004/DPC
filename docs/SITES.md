# Sites

One entry per site; a SITE chat works from its entry. No secrets here (DB passwords and mail secrets stay in that site's `site.yaml` on the machine, D-048).

All sites run tagged releases of the same `main` branch (D-039): there are no site branches. Record the tag each site runs in its `RELEASE` line and update it whenever the site moves to a new tag.

Update a site: `cd ~/INSIGHTZZ/code && git fetch --tags && git checkout v0.<n>`, restart the Supervisor, then update `RELEASE` here. Roll back: check out the previous tag.

## Template
```
SITE: <id>        HOST: <hostname>     ROOT: /home/<user>/INSIGHTZZ     RELEASE: v0.<n>
DISK: <layout>
CAMERAS: CAM1=<side> <driver> serial <..> exposure_us <..> fps <..> rotate <..>; CAM2=...
DB: name <..>   health table <..>
PLC: type Modbus TCP | S7, ip <..>, light <..>, cleaning <..>, conveyor status <..>
MODEL: folder <..>, yaml <..>, threshold <..>, classes <..>
MAIL: sender SMTP|Graph, recipients managed in <..>
WHEELS/REV: <n>   wheel_stand counts: <..>   object sizes: <..>   disk thresholds: <..>
QUIRKS: <..>; both cameras share one Ethernet port on the server: if that port is down, both cameras stop (check it first when both fail together); MQTT broker: mosquitto on localhost, no login; LEFT and RIGHT use the same model (same weights and class list, confirmed 2026-10-21): one inspection.model per site fits; the previous setup lives in ~/INSIGHTZZ/old (own images and logs, outside the new layout and outside cleanup); its leftConfig.xml still had another site's INPUT_IMG_DIR (/home/dpc_bela/...)
```

## tadipatri (new machine)
```
SITE: tadipatri   HOST: dpc-tadipatri (HP Z2 Tower G9)   ROOT: /home/dpc_tadipatri/INSIGHTZZ   RELEASE: (none yet)
REFERENCE site.yaml (all real non-secret values, validated by the loader): docs/sites/tadipatri.site.reference.yaml
PER-SIDE DIFFERENCES (rendered 2026-10-28; same code both sides, D-069; everything else equal):
  motion LEFT rtl / RIGHT ltr (camera view); bucket_clip_window LEFT 50,1990,750,1085 / RIGHT 10,1270,550,780 (xmin,xmax,ymin,ymax; camera geometry); half_bucket_clip_window y LEFT 470-750 / RIGHT 480-730 (camera geometry); tracker.clip_confirm max_gap_ms / max_dist_px LEFT 300/200 / RIGHT 500/500 and tracker.skidding.time_gap_reset_ms LEFT 500 / RIGHT 1000 (deliberate per-camera tuning, D-080: the cameras sit at different distances); writes_live_wheel_counter LEFT false / RIGHT true (D-073 D3)
DISK: one 938 GB NVMe mounted at /, services run as the login user dpc_tadipatri (D-030)
CAMERAS: CAM1=LEFT Basler serial 24824427 exposure_us 1500 fps 4.0 rotate 0 (target; code still rotates 90 until D-041 is built); CAM2=RIGHT Basler serial 24874775 exposure_us 2000 fps 4.0 rotate 0 (same note). Mapping confirmed, both capture correctly (test 2026-10-12).
DB: name tadipatri4   health table health_status (lowercase; renamed from tadipatri4_health_status_table on 2026-10-20; Basler XML element 16 HEALTH_TABLE and site.yaml db.health_table both = health_status). The old upper-case HEALTH_STATUS (DEVICE_NAME, STATUS) is a different legacy table: never point a program at it
PLC: Modbus TCP, port 502, timeout 3 s; registers light 4109 (0 = ON, 1 = OFF, inverted), cleaning 4106 (1 = ON, 0 = OFF, pulse 10 s), conveyor status 4098 (read); old cleaning registers 4097 / 4197 in the legacy test script, not used by the new register. PLC IP 192.168.1.5 (confirmed 2026-10-22; the legacy PLC test script's 192.168.1.4 is wrong). The algorithm XML's PLC_WRITE/READ_DB_NUMBER 12/13 look like S7 leftovers; the PLC is Modbus.
CAMERA IPs: LEFT 192.168.1.102, RIGHT 192.168.1.103 (from the legacy UI config)
MODEL: dpc_maskrcnn/2026.10.12 packaged on the machine in ~/INSIGHTZZ/data/models/dpc_maskrcnn/ (not in git; 19 classes; from ~/INSIGHTZZ/old/MODELS/FIRST_MODE/Mahir_Oct_12_Int_Model/model_0041159.pth; Detectron config = flattened model-zoo mask_rcnn_R_101_FPN_3x with NUM_CLASSES 19; class order = old ALL_CLASS_NAMES; steps in COMMISSIONING 2c)   WHEELS/REV: 190   THRESHOLD: 0.5   OBJECT SIZES: both sides wheel 530x495, bucket clip 300, chain 880, half bucket clip 150
CLEANUP: raw images kept 20 h, interval 10 s, repeated 20 days / 200 items, plate 10 days, defective 10 days / 200 items, disk % delete 75 / raw delete 85 / mail 90 / INF delete 95 (from the legacy deleter config)
MAIL: Microsoft Graph, sender insightzz.reports@insightzz.com; recipient and CC lists live only in that site's site.yaml (personal addresses are never written to this repo). The mail XML found on the machine is a Maihar copy. Tadipatri: project name Tadipatri_dpc, recipient and CC lists = Maihar's for now (developer, 2026-10-22); the lists change over time and are edited in site.yaml (later through the UI), then applied with a reload (D-062).
QUIRKS: Basler config was under .../CODE/FRAME_CAPTURE/CAM1/ in the old layout; DB login is root (rotate, use a dedicated user); no image rotation wanted (rotate 0): model and object sizes must be for unrotated 1920x1080 frames
```

## maihar (production, legacy layout)
```
SITE: maihar   ROOT: /home/ddpc_maihar/INSIGHTZZ   (legacy layout, CONTRACTS 4.2)
CAMERAS: CAM1 FLIR serial 25134836 exposure_us 15000 fps 4.0; CAM2 FLIR serial 23542439 exposure_us 12000 fps 4.0
DB: name MAHIAR_DB     PLC: Modbus TCP ip 192.168.2.5 (confirmed)     WHEELS/REV: 190
QUIRKS: see CODE_AUDIT sections 2 and 4
```
CLEANUP (legacy deleter config, old layout under /home/ddpc_maihar/INSIGHTZZ/CODE/...): raw images kept 24 h, interval 10 s, repeated 20 days / 200 items, plate 10 days, defective 10 days / 200 items, disk % delete 75 / raw delete 85 / mail 90 / INF delete 95
