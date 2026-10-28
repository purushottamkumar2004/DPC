"""Why the program stopped, for the Supervisor (D-073 D7, CONTRACTS 2, D-079).

Copy of workers/inspection/last_error.py (W-inspection owns that file; moving one shared copy
to common/ is requested in the P9 handoff). Before exiting on an unexpected error the program
writes `<root>/run/<worker_id>.last_error.json` {at, type, message, where (`file:line in
function`), traceback (last lines)}. Secret values are replaced by '***'. Written atomically.
"""

import datetime
import json
import os
import traceback

TRACEBACK_LINES = 12
OWN_CODE_DIR = os.path.dirname(os.path.abspath(__file__))


def _scrub(text: str, secrets) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def error_record(exc: BaseException, secrets=()) -> dict:
    frames = traceback.extract_tb(exc.__traceback__)
    # `where` = the deepest line in this program's own code (not in pymysql or cv2), else the last line
    own = [f for f in frames if os.path.abspath(f.filename).startswith(OWN_CODE_DIR + os.sep)]
    last = (own or frames)[-1] if frames else None
    where = f"{os.path.basename(last.filename)}:{last.lineno} in {last.name}" if last else "unknown"
    lines = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip().splitlines()
    return {
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
        "type": type(exc).__name__,
        "message": _scrub(str(exc), secrets)[:500],
        "where": where,
        "traceback": [_scrub(line, secrets) for line in lines[-TRACEBACK_LINES:]],
    }


def write_last_error(root: str, worker_id: str, exc: BaseException, secrets=()) -> str:
    """Write the file and return its path. Never raises (the program is exiting anyway)."""
    run_dir = os.path.join(root, "run")
    path = os.path.join(run_dir, f"{worker_id}.last_error.json")
    try:
        os.makedirs(run_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(error_record(exc, secrets), f, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass
    return path
