# frame_capture_basler

Single-camera Basler FrameCapture with atomic hand-off (D-031). Owner chat: W-frame_capture_basler.

Per frame: write once to `_tmp`, hard-link into `INF` (and `UI` when empty), `os.replace` into `live/IMG_1.jpg`. See `docs/ARCHITECTURE.md` section 3a and `docs/CONTRACTS.md` sections 3 and 4.1.

Run (config file chosen by env, else `BASLER_CONFIG1.xml` in the working directory):
```
BASLER_CONFIG=~/INSIGHTZZ/config/BASLER_CONFIG1.xml python3 FrameCapture_Basler.py
```
Copy `BASLER_CONFIG1.example.xml`, fill in the serial and DB login, keep the real file out of git (chmod 600). Open issues: `docs/CODE_AUDIT.md` section 6.
