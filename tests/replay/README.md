# Replay: old vs new inspection on saved frames (STATUS P7)

`replay_inspection.py` runs the same saved frames through the old algorithm (`LeftAlgorithm.py` /
`RightAlgorithm.py` from git) and the new `workers/inspection/inspection.py`, each into its own
scratch folder under `/tmp` and its own scratch database, then compares the defects.

**Where:** the site machine (GPU, Detectron2, the model package, MySQL). It shares the GPU, so stop
that side's live inspection first (`python3 supervisor/supervisor.py stop inspection_left`, or the old
service), and start it again afterwards.

**Frames:** copy a range from `~/INSIGHTZZ/data/frames/CAM1/RAW_IMAGES` (CAM2 for RIGHT) to a folder
of your own first (cleanup deletes RAW_IMAGES after 24 h). Take at least two plates: about 3,000
frames (12-13 min at 4 fps) for a 190-wheel chain.

```bash
cd ~/INSIGHTZZ/code
mkdir -p ~/replay_frames_left && cp ~/INSIGHTZZ/data/frames/CAM1/RAW_IMAGES/2026_10_27_10_*.jpg ~/replay_frames_left/
python3 tests/replay/replay_inspection.py --side LEFT --frames ~/replay_frames_left --limit 3000
```

Options: `--config` (default `~/INSIGHTZZ/config/rendered/inspection_<side>.json`), `--old-ref`
(git ref of the old algorithm, default `HEAD`), `--first <frame name>`, `--limit N`, `--keep` (keep the
scratch databases `<db>_replay_old` / `<db>_replay_new`; the scratch folder and `report.json` are always kept).

**Result:** prints a JSON summary and `PASS` / `FAIL`; exit 0 on pass.
- Pass = every old `Nut_Bolt_Missing_In_Bucket_Clip` (wheel + box within 3 px) has one in the new run, and
  the new run has none extra.
- RIGHT: the old program never inserted bolt defects (R:843), so its side is read from its DEFECTIVE
  image names (wheel only).
- `new_types` lists the defect types that are new in P7 (`WheelMissing`, `Wheel_Skidding`,
  `Batch_Wheels_Missing`). For LEFT, check `Batch_Wheels_Missing` against the plates you know passed:
  extra ones come from B5 (`workers/inspection/INSPECTION.md` section 10).

Safety: the script only creates and drops databases named `*_replay_old` / `*_replay_new`, never the live
DB; the DB password is read from the rendered JSON and only written into the scratch folder (chmod 600).
