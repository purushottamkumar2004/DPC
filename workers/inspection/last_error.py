"""Why the program stopped, for the Supervisor (D-073 D7, CONTRACTS 2, STATUS F21).

Before exiting on an unexpected error the program writes `<root>/run/<worker_id>.last_error.json`
{at, type, message, where (file:line), traceback (last lines)}. Secret values (DB / MQTT
passwords) are replaced by '***'. Written atomically: the Supervisor never reads half a file.
"""

import datetime
import json
import os
import traceback

TRACEBACK_LINES = 12


def _scrub(text: str, secrets) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def error_record(exc: BaseException, secrets=()) -> dict:
    frames = traceback.extract_tb(exc.__traceback__)
    where = f"{os.path.basename(frames[-1].filename)}:{frames[-1].lineno}" if frames else "unknown"
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
