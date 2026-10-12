# Sites

One entry per site; a SITE chat works from its entry. No secrets here (DB passwords, mail secrets stay in that site's `secrets.env`).

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
QUIRKS: <..>
```

## tadipatri (new machine)
```
SITE: tadipatri   HOST: dpc-tadipatri (HP Z2 Tower G9)   ROOT: /home/dpc_tadipatri/INSIGHTZZ   RELEASE: (none yet)
DISK: one 938 GB NVMe mounted at /, services run as the login user dpc_tadipatri (D-030)
CAMERAS: two Basler cameras detected, serials 24824427 and 24874775; CAM1/CAM2 and side to confirm by looking at the images; first config: 24824427 exposure_us 1500, fps 4.0, rotate 90; the second camera uses the same settings until tuned
DB: name tadipatri4   health table tadipatri_health_status_table (hardcoded in code, D-038)
PLC / MODEL / MAIL / WHEELS: to fill
QUIRKS: Basler config was under .../CODE/FRAME_CAPTURE/CAM1/ in the old layout; DB login is root (rotate, use a dedicated user)
```

## maihar (production, legacy layout)
```
SITE: maihar   ROOT: /home/ddpc_maihar/INSIGHTZZ   (legacy layout, CONTRACTS 4.2)
CAMERAS: CAM1 FLIR serial 25134836 exposure_us 15000 fps 4.0; CAM2 FLIR serial 23542439 exposure_us 12000 fps 4.0
DB: name MAHIAR_DB     PLC: Modbus TCP ip 192.168.2.5 (confirmed)     WHEELS/REV: 190
QUIRKS: see CODE_AUDIT sections 2 and 4
```
