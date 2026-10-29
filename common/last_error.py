"""Why a worker stopped, for the Supervisor (D-073 D7, D-079, CONTRACTS 2, STATUS F21, F31).

One shared writer for every worker (inspection, cleanup, the captures next), replacing the
copies in workers/inspection/last_error.py and workers/cleanup/cleanup_last_error.py.

    from common.last_error import clear_last_error, write_last_error

    clear_last_error(root, worker_id)            # at start: a file from an older run is never reported
    try:
        main_loop()
    except Exception as exc:
        write_last_error(root, worker_id, exc, secrets=[db_password, ...])
        sys.exit(1)

File: <root>/run/<worker_id>.last_error.json, written atomically with mode 600:
    {"at": "2026-10-28T09:15:02", "type": "OperationalError", "message": "...",
     "where": "inspection.py:412 in main_loop", "traceback": ["...", ...]}
`where` = `<file>:<line> in <function>` of the deepest frame in our own code (the repo
checkout), not in a library such as pymysql or cv2 (D-079). Field sizes follow D-079's caps
(type 100, message 500, where 200 characters; last 12 traceback lines of 300). Every secret
value passed in is replaced by '***' in message and traceback.
"""

import datetime
import json
import os
import traceback
from typing import Iterable, Optional

TRACEBACK_LINES = 12
LINE_CHARS = 300
TYPE_CHARS, MESSAGE_CHARS, WHERE_CHARS = 100, 500, 200
# The repo checkout (<root>/code): common/ sits directly in it.
CODE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def last_error_path(root: str, worker_id: str) -> str:
    return os.path.join(root, "run", f"{worker_id}.last_error.json")


def _scrub(text: str, secrets: Iterable[str]) -> str:
    # longest first, so a secret that contains another one is replaced whole
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def _is_own(filename: str, own_code_dir: str) -> bool:
    path = os.path.abspath(filename)
    return path.startswith(own_code_dir + os.sep) and f"{os.sep}site-packages{os.sep}" not in path


def error_record(exc: BaseException, secrets: Iterable[str] = (), own_code_dir: Optional[str] = None) -> dict:
    """The D-079 record for an exception (pure: nothing is written)."""
    secrets = [s for s in (secrets or ()) if isinstance(s, str) and s]
    own_dir = os.path.abspath(own_code_dir or CODE_ROOT)
    frames = traceback.extract_tb(exc.__traceback__)
    own = [f for f in frames if _is_own(f.filename, own_dir)]
    last = (own or frames)[-1] if frames else None
    where = f"{os.path.basename(last.filename)}:{last.lineno} in {last.name}" if last else "unknown"
    lines = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip().splitlines()
    return {
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
        "type": type(exc).__name__[:TYPE_CHARS],
        "message": _scrub(str(exc), secrets)[:MESSAGE_CHARS],
        "where": where[:WHERE_CHARS],
        "traceback": [_scrub(line, secrets)[:LINE_CHARS] for line in lines[-TRACEBACK_LINES:]],
    }


def write_last_error(root: str, worker_id: str, exc: BaseException, secrets: Iterable[str] = (),
                     own_code_dir: Optional[str] = None) -> str:
    """Write the file and return its path. Never raises: the program is exiting anyway."""
    path = last_error_path(root, worker_id)
    tmp = f"{path}.tmp-{os.getpid()}"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        record = error_record(exc, secrets, own_code_dir)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=1)
        os.replace(tmp, path)
    except Exception:                       # never hide the original error behind a write error
        try:
            os.remove(tmp)
        except OSError:
            pass
    return path


def clear_last_error(root: str, worker_id: str) -> None:
    """At start: remove a file left by an earlier run (D-076: the rule for every worker)."""
    try:
        os.remove(last_error_path(root, worker_id))
    except FileNotFoundError:
        pass
    except OSError:
        pass
