#!/usr/bin/env python3
"""Immediate defect-alert mail (one program for every site).

Migrated from the Baga Graph mail variant (the reference, developer 2026-10-30). The business
rules are kept as they were; what changed is where the settings come from and how it runs.

Every POLL_SECONDS:
  1. read every row of defects_table with is_alert_sent = 0
  2. a row older than mail.max_alert_age_hours is marked 2 (ignored) and never mailed (F34)
  3. dedupe as before (D-044): a row is a duplicate when a row with the same wheel_no +
     defect_type + defect_side was already SENT (is_alert_sent = 1) in the last 24 h, or when
     the same (type, wheel, side) was already taken earlier in this poll -> marked 2
  4. the rest goes out as ONE mail (subject, HTML table, defect images from view_defect)
  5. flood breaker (F35, optional mail.flood): if (rows sent in the last window_hours + this
     batch) > max_per_window, the mail goes only to mail.flood.internal_to, without CC
  6. rows are marked 1 only after Graph answers 202 (or SMTP accepted the mail). A failed send
     is logged and retried at the next poll; the rows stay 0.

Never twice: the IDs of a confirmed mail are written to <state>/mail/sent_ids.json before the
UPDATE; at start any IDs left there (crash between send and UPDATE) are marked 1 first.

Settings come ONLY from the rendered JSON in env DPC_WORKER_CONFIG
(<root>/config/rendered/mail.json, CONTRACTS 13). Until CONFIG renders it, safe keys get a
default with ONE warning each; a recipient, a secret or the DB name is never defaulted.
Heartbeat (D-034, STATUS F69): common/heartbeat.py publishes dpc/worker/mail/health every 10 s
with last_poll_at, pending_rows, sent_today and flood_active; healthy = a poll finished within
3 x POLL_SECONDS. Without an `mqtt` block in mail.json the heartbeat is off (one warning).
Test mail for the setup wizard and commissioning (STATUS F92, D-103 step 8):
    python3 mail_worker.py --test --json --sender graph --from reports@x.com --to a@x.com,b@x.com [--cc c@x.com]
Graph login from env DPC_MS_TENANT_ID, DPC_MS_CLIENT_ID, DPC_MS_CLIENT_SECRET (SMTP: --smtp-host,
--smtp-port, env DPC_SMTP_PASSWORD). A value not given falls back to mail.json when DPC_WORKER_CONFIG
points to one. Sends ONE mail without attachments and never opens the database (defects_table is
never read or written); no log file, no last_error. Prints {"ok", "status", "message", ...}; status
(exit code): ok (0), failed (1, refused by Graph/SMTP), bad_request (2), unreachable (3, network).
Any unexpected error (config, DB) writes <root>/run/mail.last_error.json (secrets scrubbed)
and exits 1; the Supervisor restarts the program (D-073 D7, D-079). SIGTERM / SIGINT: exit 0.
"""

import argparse
import base64
import datetime
import json
import logging
import os
import signal
import smtplib
import sys
import time
from dataclasses import dataclass, field
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from logging.handlers import TimedRotatingFileHandler
from typing import Callable, List, Optional

import pymysql
import pymysql.cursors
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))          # <root>/code
sys.path.insert(0, REPO_ROOT)

from common.heartbeat import Heartbeat                               # noqa: E402
from common.last_error import clear_last_error, write_last_error   # noqa: E402

WORKER_ID = "mail"
POLL_SECONDS = 10                       # as before
HEALTHY_POLL_AGE_S = 3 * POLL_SECONDS    # heartbeat: healthy while a poll finished this recently

# Dedupe, exactly as the old senders (D-044).
DEDUPE_HOURS = 24
DEDUPE_SIZE_PX = 50

# Attachments, as the old sender; Graph refuses a sendMail request above about 4 MB and the
# images are base64-encoded (+33 %), so Graph mails carry at most 3 MB of images (D-mail-3).
MAX_ATTACH_FILES = 10
MAX_ATTACH_MB = 22
GRAPH_MAX_ATTACH_MB = 3

TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
SEND_URL = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
TOKEN_TIMEOUT_S = 20
SEND_TIMEOUT_S = 60
LOGGED_ROWS = 20                        # rows named in one log line; the rest are counted

STATUS_SENT, STATUS_IGNORED = 1, 2


# =================================================================================
# 1. SETTINGS (only from the rendered JSON)
# =================================================================================

class ConfigError(Exception):
    pass


@dataclass
class Flood:
    max_per_window: int
    window_hours: float
    internal_to: List[str]


@dataclass
class Settings:
    root: str
    logs_dir: str
    state_dir: str
    backup_count: int
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    sender: str                          # graph | smtp
    sender_address: str
    project_name: str
    to: List[str]
    cc: List[str]
    report_url: Optional[str]
    max_alert_age_hours: Optional[float]  # None = off
    flood: Optional[Flood]               # None = off
    mqtt: Optional[dict] = None          # heartbeat broker {host, port, user, password}; None = off
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = ""
    smtp_host: str = ""
    smtp_port: int = 0
    smtp_password: str = ""
    warnings: List[str] = field(default_factory=list)

    def secrets(self) -> List[str]:
        mqtt_password = (self.mqtt or {}).get("password") or ""
        return [self.db_password, self.graph_tenant_id, self.graph_client_id,
                self.graph_client_secret, self.smtp_password, mqtt_password]


_MISSING = object()
_REQUIRED = object()


def root_from_code_location() -> str:
    """<root>/code/workers/mail -> <root> (CONTRACTS 4.1)."""
    return os.path.dirname(REPO_ROOT)


def read_raw_config() -> dict:
    """The rendered JSON as a dict; {} when the env var or the file is missing."""
    path = os.environ.get("DPC_WORKER_CONFIG", "")
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError as e:
        raise ConfigError(f"{path} is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must hold a JSON object")
    return data


def secrets_in(raw: dict) -> List[str]:
    """Secret values from the raw file, so even a config error never shows them."""
    found = []
    for section, keys in (("db", ("password",)), ("graph", ("tenant_id", "client_id", "client_secret")),
                          ("mqtt", ("password",))):
        part = raw.get(section) or {}
        if isinstance(part, dict):
            found += [str(part[k]) for k in keys if part.get(k)]
    smtp = (raw.get("mail") or {}).get("smtp") if isinstance(raw.get("mail"), dict) else None
    if isinstance(smtp, dict) and smtp.get("password"):
        found.append(str(smtp["password"]))
    return found


class _Reader:
    """Reads dotted keys; collects missing required keys and default warnings."""

    def __init__(self, raw: dict):
        self.raw = raw
        self.missing: List[str] = []
        self.warnings: List[str] = []

    def lookup(self, key: str):
        value = self.raw
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return _MISSING
            value = value[part]
        return value

    def get(self, key, kind, default=_REQUIRED, nullable=False, shown=None):
        value = self.lookup(key)
        if value is None and nullable:
            return None
        if value is _MISSING or value is None or value == "":
            if default is _REQUIRED:
                self.missing.append(key)
                return None
            self.warnings.append(f"{key} missing in mail.json, using default {shown or repr(default)}")
            return default
        if kind is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if not isinstance(value, kind) or isinstance(value, bool):
            raise ConfigError(f"{key} must be {kind.__name__}, got {type(value).__name__}")
        return value

    def addresses(self, key, required):
        value = self.lookup(key)
        if value is _MISSING or value is None:
            if required:
                self.missing.append(key)
            else:
                self.warnings.append(f"{key} missing in mail.json, using default []")
            return []
        if not isinstance(value, list) or not all(isinstance(a, str) and a.strip() for a in value):
            raise ConfigError(f"{key} must be a list of e-mail addresses")
        if required and not value:
            self.missing.append(key)
        return [a.strip() for a in value]


def _flood(r: _Reader) -> Optional[Flood]:
    if r.lookup("mail.flood") is _MISSING:
        r.warnings.append("mail.flood missing in mail.json: flood breaker is off")
        return None
    if r.lookup("mail.flood") is None:
        return None
    limit = r.get("mail.flood.max_per_window", int)
    hours = r.get("mail.flood.window_hours", float)
    internal = r.addresses("mail.flood.internal_to", required=True)
    if limit is not None and limit < 1:
        raise ConfigError("mail.flood.max_per_window must be at least 1")
    if hours is not None and hours <= 0:
        raise ConfigError("mail.flood.window_hours must be above 0")
    return Flood(limit, hours, internal)


def _mqtt(r: _Reader) -> Optional[dict]:
    """The heartbeat broker, as rendered for the other workers. Missing or null = heartbeat off
    (common/heartbeat.py says so once); never a config error, mail works without a broker."""
    block = r.lookup("mqtt")
    if not isinstance(block, dict):
        return None
    return {"host": block.get("host") or "localhost", "port": int(block.get("port") or 1883),
            "user": block.get("user") or None, "password": block.get("password") or None}


def build_settings(raw: dict) -> Settings:
    r = _Reader(raw)
    root = r.get("paths.root", str, root_from_code_location(), shown="<root> from the code location")
    logs_dir = r.get("paths.logs", str, os.path.join(root, "logs"), shown="<root>/logs")
    state_dir = r.get("paths.state", str, os.path.join(root, "data", "state"), shown="<root>/data/state")
    sender = r.get("mail.sender", str, "graph")
    if sender not in ("graph", "smtp"):
        raise ConfigError(f"mail.sender must be graph or smtp, got {sender!r}")
    age = r.get("mail.max_alert_age_hours", float, 24.0, nullable=True)
    if age is not None and age <= 0:
        raise ConfigError("mail.max_alert_age_hours must be above 0 (or null = off)")
    site_name = r.lookup("site.name")
    has_site_name = isinstance(site_name, str) and bool(site_name)
    project = r.get("mail.project_name", str, site_name if has_site_name else _REQUIRED,
                    shown=f"site.name {site_name!r}")

    s = Settings(
        root=root, logs_dir=logs_dir, state_dir=state_dir,
        backup_count=r.get("log.backup_count", int, 5),
        db_host=r.get("db.host", str, "localhost"),
        db_port=r.get("db.port", int, 3306),
        db_name=r.get("db.name", str),
        db_user=r.get("db.user", str),
        db_password=r.get("db.password", str),
        sender=sender,
        sender_address=r.get("mail.sender_address", str),
        project_name=project,
        to=r.addresses("mail.to", required=True),
        cc=r.addresses("mail.cc", required=False),
        report_url=r.get("mail.report_url", str, None, nullable=True, shown="none (link line left out)"),
        max_alert_age_hours=age,
        flood=_flood(r),
        mqtt=_mqtt(r),
    )
    if sender == "graph":
        s.graph_tenant_id = r.get("graph.tenant_id", str)
        s.graph_client_id = r.get("graph.client_id", str)
        s.graph_client_secret = r.get("graph.client_secret", str)
    else:
        s.smtp_host = r.get("mail.smtp.host", str)
        s.smtp_port = r.get("mail.smtp.port", int)
        s.smtp_password = r.get("mail.smtp.password", str)
    if r.missing:
        raise ConfigError("missing in mail.json (no safe default): " + ", ".join(r.missing))
    s.warnings = r.warnings
    return s


# =================================================================================
# 2. DATABASE
# =================================================================================

def connect_db(s: Settings):
    return pymysql.connect(host=s.db_host, port=s.db_port, user=s.db_user, password=s.db_password,
                           database=s.db_name, cursorclass=pymysql.cursors.DictCursor,
                           autocommit=True, connect_timeout=10)


def fetch_unsent(cur) -> list:
    cur.execute("SELECT * FROM defects_table WHERE is_alert_sent = 0;")
    return list(cur.fetchall())


def row_id(row):
    """The column is `ID` (CONTRACTS 5); accept `id` for an older site DB."""
    return row["ID"] if "ID" in row else row.get("id")


def is_db_duplicate(cur, row, log) -> bool:
    """The old checkIsDefectAlreadyGenerated(), query unchanged (D-044): the size condition
    compares the row with itself, so wheel + type + side + 24 h decide. An error counts as
    'not a duplicate', as before."""
    try:
        since = (datetime.datetime.now() - datetime.timedelta(hours=DEDUPE_HOURS)).strftime('%Y-%m-%d %H:%M:%S')
        xmin, xmax, ymin, ymax = row['xmin'], row['xmax'], row['ymin'], row['ymax']
        x_lo, x_hi = (xmax - xmin) - DEDUPE_SIZE_PX, (xmax - xmin) + DEDUPE_SIZE_PX
        y_lo, y_hi = (ymax - ymin) - DEDUPE_SIZE_PX, (ymax - ymin) + DEDUPE_SIZE_PX
        query = f"""
            SELECT id FROM defects_table
            WHERE create_datetime > '{since}'
            AND wheel_no = {row['wheel_no']}
            AND defect_type = '{row['defect_type']}'
            AND defect_side = '{row['defect_side']}'
            AND is_alert_sent = 1
            AND ({xmax} - {xmin}) BETWEEN {x_lo} AND {x_hi}
            AND ({ymax} - {ymin}) BETWEEN {y_lo} AND {y_hi}
            LIMIT 1;
        """
        cur.execute(query)
        return len(cur.fetchall()) > 0
    except Exception as e:
        log.error(f"dedupe check failed for row {row_id(row)}, treated as new: {type(e).__name__}: {e}")
        return False


def mark_rows(cur, ids, status):
    if ids:
        placeholders = ", ".join(["%s"] * len(ids))
        cur.execute(f"UPDATE defects_table SET is_alert_sent = %s WHERE ID IN ({placeholders});",
                    (status, *ids))


def count_sent_since(cur, since) -> int:
    """Flood breaker: rows marked sent whose create_datetime is inside the window (as Baga)."""
    cur.execute("SELECT COUNT(*) AS cnt FROM defects_table WHERE is_alert_sent = 1 AND create_datetime > %s;",
                (since.strftime('%Y-%m-%d %H:%M:%S'),))
    result = cur.fetchone()
    return int((result or {}).get("cnt") or 0)


# =================================================================================
# 3. RULES (which rows are mailed)
# =================================================================================

@dataclass
class Sorted:
    to_send: list = field(default_factory=list)
    duplicates: list = field(default_factory=list)
    too_old: list = field(default_factory=list)


def sort_rows(rows, now, max_age_hours, is_duplicate: Callable[[dict], bool]) -> Sorted:
    result = Sorted()
    oldest_allowed = now - datetime.timedelta(hours=max_age_hours) if max_age_hours else None
    taken_this_poll = set()
    for row in rows:
        created = row.get("create_datetime")
        if oldest_allowed and isinstance(created, datetime.datetime) and created < oldest_allowed:
            result.too_old.append(row)
            continue
        signature = (row['defect_type'], row['wheel_no'], row['defect_side'])
        duplicate = is_duplicate(row)
        if duplicate or signature in taken_this_poll:
            result.duplicates.append(row)
        else:
            result.to_send.append(row)
            taken_this_poll.add(signature)
    return result


def flood_restricted(s: Settings, cur, batch_size, now):
    """(restricted?, rows already sent in the window). Always False without mail.flood."""
    if s.flood is None:
        return False, 0
    sent = count_sent_since(cur, now - datetime.timedelta(hours=s.flood.window_hours))
    return (sent + batch_size) > s.flood.max_per_window, sent


# =================================================================================
# 4. MAIL CONTENT (subject, body and images as in the Baga sender)
# =================================================================================

def _number(value):
    return int(value) if float(value).is_integer() else value


def build_subject(project_name, count, restricted):
    prefix = "!! FALSE-ALARM WATCH (Restricted) !!" if restricted else "!! CRITICAL ALERT"
    return f"{prefix}: {count} New Defects Detected in {project_name} !!"


def build_body(project_name, rows, report_url=None, flood=None, restricted=False):
    times = [r.get('create_datetime') for r in rows if r.get('create_datetime')]
    start_time = min(times) if times else "N/A"
    end_time = max(times) if times else "N/A"

    table_html = ""
    for item in rows:
        table_html += (f"<tr><td>{item.get('defect_type')}</td><td>{item.get('defect_side')}</td>"
                       f"<td>{item.get('wheel_no', 'N/A')}</td><td>{item.get('create_datetime')}</td></tr>")

    html = """
        <!DOCTYPE html>
        <html>
        <head>
        <meta charset='utf-8' />
        <style type='text/css'>
        table { background: white; border-radius:3px; border-collapse: collapse; width: 100%; padding:5px; }
        th { color:#D5DDE5; background:#1b1e24; border-bottom: 4px solid #9ea7af;
             font-size:14px;font-weight: 300; padding:10px;text-align:center; }
        tr { border-top: 1px solid #C1C3D1; border-bottom: 1px solid #C1C3D1;
             color:#666B85; font-size:16px; }
        tr:hover td { background:#4E5066; color:#FFFFFF; }
        td { background:#FFFFFF; padding:10px; text-align:center; font-size:13px; }
        </style>
        </head>
        <body>
        <table>
        <thead>
        <tr>
        <th>Defect Type</th>
        <th>Defect Side</th>
        <th>Wheel Counter</th>
        <th>Date Time</th>
        </tr>
        </thead>
        <tbody>
    """ + table_html + """
        </tbody>
        </table>
        </body>
        </html>
    """

    restricted_notice = ""
    if restricted and flood:
        restricted_notice = (
            f"<b>NOTE:</b> More than {flood.max_per_window} defects have been sent in the last "
            f"{_number(flood.window_hours)} hours. To avoid spamming clients in case this is a false-alarm "
            f"flood, this alert has been restricted to the internal team only for review.<br><br>"
        )
    link = ""
    if report_url:              # the old code had a fixed link; now mail.report_url (absent = no link)
        link = (f"You can also check all the defective images from the link below:<br><br>"
                f"<a href='{report_url}' target='_blank'>"
                f"Click here to view the report</a><br><br>")

    return (
        f"Dear All,<br><br>"
        f"{restricted_notice}"
        f"Please find attached the {project_name} Report from: {start_time} to {end_time}.<br>"
        f"Total Number of Defects: {len(rows)}<br><br>"
        f"Below is the detailed list of defects detected during the period:<br><br>"
        f"{link}"
        f"{html}<br>Thanks & Regards,<br>Insightzz Team"
    )


def collect_images(rows, max_bytes, log):
    """[(file name, bytes)] from view_defect, in row order, with the old limits:
    at most MAX_ATTACH_FILES files; a missing file or one that would pass max_bytes is skipped."""
    images, total = [], 0
    for row in rows:
        path = row.get('view_defect')
        if not path:
            continue
        if len(images) >= MAX_ATTACH_FILES:
            log.warning(f"image not attached (max {MAX_ATTACH_FILES} files): {os.path.basename(path)}")
            break
        if not os.path.isfile(path):
            log.warning(f"image not attached (file not found): {path}")
            continue
        size = os.path.getsize(path)
        if total + size > max_bytes:
            log.warning(f"image not attached (would pass {max_bytes // (1024 * 1024)} MB): {os.path.basename(path)}")
            continue
        with open(path, "rb") as f:
            images.append((os.path.basename(path), f.read()))
        total += size
    return images


# =================================================================================
# 5. SENDING (Graph or SMTP). A failure returns False; it never raises for network errors.
# =================================================================================

@dataclass
class SendResult:
    ok: bool
    status: str             # ok | failed (refused) | unreachable (network)
    message: str


def _graph_token(s: Settings):
    """(token, None) or (None, SendResult explaining why not)."""
    try:
        response = requests.post(TOKEN_URL.format(tenant=s.graph_tenant_id), timeout=TOKEN_TIMEOUT_S, data={
            "grant_type": "client_credentials",
            "client_id": s.graph_client_id,
            "client_secret": s.graph_client_secret,
            "scope": "https://graph.microsoft.com/.default",
        })
    except requests.RequestException as e:
        return None, SendResult(False, "unreachable", f"Graph token failed: {type(e).__name__}")
    if response.status_code == 200:
        token = response.json().get("access_token")
        if token:
            return token, None
    return None, SendResult(False, "failed",
                            f"Graph token failed: HTTP {response.status_code} {response.text[:300]}")


def graph_send_result(s: Settings, subject, body, images, to, cc) -> SendResult:
    token, problem = _graph_token(s)
    if problem:
        return problem
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
            "ccRecipients": [{"emailAddress": {"address": a}} for a in cc],
            "attachments": [{"@odata.type": "#microsoft.graph.fileAttachment", "name": name,
                             "contentBytes": base64.b64encode(data).decode()} for name, data in images],
        },
        "saveToSentItems": True,
    }
    try:
        response = requests.post(SEND_URL.format(sender=s.sender_address), json=payload, timeout=SEND_TIMEOUT_S,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    except requests.RequestException as e:
        return SendResult(False, "unreachable", f"Graph send failed: {type(e).__name__}")
    if response.status_code == 202:
        return SendResult(True, "ok", "accepted by Graph (HTTP 202)")
    return SendResult(False, "failed", f"Graph send failed: HTTP {response.status_code} {response.text[:300]}")


def smtp_send_result(s: Settings, subject, body, images, to, cc) -> SendResult:
    message = MIMEMultipart()
    message['From'] = s.sender_address
    message['To'] = ", ".join(to)
    if cc:
        message['Cc'] = ", ".join(cc)
    message['Subject'] = subject
    message.attach(MIMEText(body, 'html'))
    for name, data in images:
        part = MIMEBase('application', 'octet-stream')
        part.set_payload(data)
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f"attachment; filename={name}")
        message.attach(part)
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=SEND_TIMEOUT_S) as server:
            server.starttls()
            server.login(s.sender_address, s.smtp_password)      # login user = sender, as before
            server.sendmail(s.sender_address, to + cc, message.as_string())
        return SendResult(True, "ok", "accepted by the SMTP server")
    except smtplib.SMTPException as e:
        return SendResult(False, "failed", f"SMTP send failed: {type(e).__name__}: {str(e)[:300]}")
    except OSError as e:
        return SendResult(False, "unreachable", f"SMTP send failed: {type(e).__name__}: {str(e)[:300]}")


def send_graph(s: Settings, subject, body, images, to, cc, log) -> bool:
    result = graph_send_result(s, subject, body, images, to, cc)
    if not result.ok:
        log.error(result.message)
    return result.ok


def send_smtp(s: Settings, subject, body, images, to, cc, log) -> bool:
    result = smtp_send_result(s, subject, body, images, to, cc)
    if not result.ok:
        log.error(result.message)
    return result.ok


def send_alert(s: Settings, rows, restricted, log) -> bool:
    to, cc = (s.flood.internal_to, []) if restricted else (s.to, s.cc)
    subject = build_subject(s.project_name, len(rows), restricted)
    body = build_body(s.project_name, rows, s.report_url, s.flood, restricted)
    if s.sender == "graph":
        images = collect_images(rows, min(MAX_ATTACH_MB, GRAPH_MAX_ATTACH_MB) * 1024 * 1024, log)
        return send_graph(s, subject, body, images, to, cc, log)
    images = collect_images(rows, MAX_ATTACH_MB * 1024 * 1024, log)
    return send_smtp(s, subject, body, images, to, cc, log)


# =================================================================================
# 6. SENT JOURNAL (never mail a confirmed row twice)
# =================================================================================

def journal_path(s: Settings) -> str:
    return os.path.join(s.state_dir, "mail", "sent_ids.json")


def write_journal(s: Settings, ids):
    path = journal_path(s)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"at": datetime.datetime.now().isoformat(timespec="seconds"), "ids": list(ids)}, f)
    os.replace(tmp, path)


def clear_journal(s: Settings):
    try:
        os.remove(journal_path(s))
    except FileNotFoundError:
        pass


def replay_journal(s: Settings, cur, log):
    """At start: rows mailed just before a crash are marked sent, not mailed again."""
    path = journal_path(s)
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        ids = json.load(f).get("ids") or []
    mark_rows(cur, ids, STATUS_SENT)
    clear_journal(s)
    log.warning(f"marked {len(ids)} rows sent that were mailed before the last stop: {ids}")


# =================================================================================
# 7. HEARTBEAT FIELDS (written by the poll loop, read by the heartbeat thread)
# =================================================================================

class PollStats:
    """What the heartbeat reports (D-034 extra fields, STATUS F69).

    last_poll_at  local time the last poll finished (None until the first one)
    pending_rows  rows still unsent (is_alert_sent = 0) after the last poll; > 0 means a send failed
    sent_today    rows mailed today (from the DB at start, then counted; back to 0 at midnight)
    flood_active  the flood breaker would send a new defect to the internal list only right now
    healthy       a poll finished within HEALTHY_POLL_AGE_S (counted from the start before the first)
    """

    def __init__(self, clock=time.monotonic, today=datetime.date.today):
        self._clock = clock
        self._today = today
        self._started = clock()
        self._last_poll_mono = None
        self.last_poll_at = None
        self.pending_rows = 0
        self.sent_today = 0
        self.flood_active = False
        self._day = today()

    def _roll_day(self):
        if self._today() != self._day:
            self._day = self._today()
            self.sent_today = 0

    def start_day(self, sent_so_far: int):
        self._day = self._today()
        self.sent_today = sent_so_far

    def poll_done(self, pending_rows: int, sent_rows: int, flood_active: bool):
        self._roll_day()
        self.sent_today += sent_rows
        self.pending_rows = pending_rows
        self.flood_active = flood_active
        self.last_poll_at = datetime.datetime.now().isoformat(timespec="seconds")
        self._last_poll_mono = self._clock()

    def fields(self) -> dict:
        self._roll_day()
        return {"last_poll_at": self.last_poll_at, "pending_rows": self.pending_rows,
                "sent_today": self.sent_today, "flood_active": self.flood_active}

    def healthy(self) -> bool:
        since = self._last_poll_mono if self._last_poll_mono is not None else self._started
        return (self._clock() - since) <= HEALTHY_POLL_AGE_S


def flood_active_now(s: Settings, cur, now) -> bool:
    """True when one more defect would already go to the internal list only (sent >= limit)."""
    if s.flood is None:
        return False
    return count_sent_since(cur, now - datetime.timedelta(hours=s.flood.window_hours)) >= s.flood.max_per_window


def midnight(now):
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


# =================================================================================
# 8. ONE POLL
# =================================================================================

def describe(rows) -> str:
    shown = ", ".join(f"{row_id(r)} {r['defect_type']} {r['defect_side']} wheel {r['wheel_no']}"
                      for r in rows[:LOGGED_ROWS])
    more = f", and {len(rows) - LOGGED_ROWS} more" if len(rows) > LOGGED_ROWS else ""
    return f"{len(rows)} [{shown}{more}]"


def poll_once(s: Settings, cur, log, now=None, stats: Optional[PollStats] = None) -> Sorted:
    now = now or datetime.datetime.now()
    rows = fetch_unsent(cur)
    if not rows:
        if stats is not None:
            stats.poll_done(pending_rows=0, sent_rows=0, flood_active=flood_active_now(s, cur, now))
        return Sorted()
    result = sort_rows(rows, now, s.max_alert_age_hours, lambda r: is_db_duplicate(cur, r, log))
    mark_rows(cur, [row_id(r) for r in result.duplicates + result.too_old], STATUS_IGNORED)

    parts = []
    sent_rows = 0
    if result.to_send:
        restricted, already_sent = flood_restricted(s, cur, len(result.to_send), now)
        if send_alert(s, result.to_send, restricted, log):
            ids = [row_id(r) for r in result.to_send]
            write_journal(s, ids)
            mark_rows(cur, ids, STATUS_SENT)
            clear_journal(s)
            sent_rows = len(ids)
            if restricted:
                parts.append(f"sent RESTRICTED to {len(s.flood.internal_to)} internal address(es) "
                             f"(flood: {already_sent} in {_number(s.flood.window_hours)} h + "
                             f"{len(ids)} > {s.flood.max_per_window}) {describe(result.to_send)}")
            else:
                parts.append(f"sent to {len(s.to)} + {len(s.cc)} cc {describe(result.to_send)}")
        else:
            log.error(f"mail not sent, {len(result.to_send)} rows stay unsent; retry in {POLL_SECONDS} s")
    if result.duplicates:
        parts.append(f"ignored duplicate {describe(result.duplicates)}")
    if result.too_old:
        parts.append(f"ignored older than {_number(s.max_alert_age_hours)} h {describe(result.too_old)}")
    if parts:
        log.info("poll: " + "; ".join(parts))
    if stats is not None:
        pending = len(rows) - len(result.duplicates) - len(result.too_old) - sent_rows
        stats.poll_done(pending_rows=pending, sent_rows=sent_rows, flood_active=flood_active_now(s, cur, now))
    return result


# =================================================================================
# 9. TEST MAIL (setup wizard step 8 and commissioning, STATUS F92). Never touches the DB.
# =================================================================================

TEST_OK, TEST_FAILED, TEST_BAD_REQUEST, TEST_UNREACHABLE = "ok", "failed", "bad_request", "unreachable"
TEST_EXIT_CODES = {TEST_OK: 0, TEST_FAILED: 1, TEST_BAD_REQUEST: 2, TEST_UNREACHABLE: 3}
TEST_ENV = {"graph_tenant_id": "DPC_MS_TENANT_ID", "graph_client_id": "DPC_MS_CLIENT_ID",
            "graph_client_secret": "DPC_MS_CLIENT_SECRET", "smtp_password": "DPC_SMTP_PASSWORD"}


class _Sources:
    """Takes a value from the command line / environment first, else from mail.json; remembers which."""

    def __init__(self, raw: dict):
        self.raw = raw or {}
        self.used_cli = False
        self.used_json = False

    def pick(self, given, json_key):
        if given not in (None, "", []):
            self.used_cli = True
            return given
        value = _Reader(self.raw).lookup(json_key)
        if value not in (_MISSING, None, "", []):
            self.used_json = True
            return value
        return None

    def described(self) -> str:
        if self.used_json and self.used_cli:
            return "mail.json + command line"
        return "mail.json" if self.used_json else "command line"


def _address_list(values) -> List[str]:
    """--to a,b --to c -> [a, b, c]; a list from mail.json stays a list."""
    out = []
    for value in values or []:
        out += [part.strip() for part in str(value).split(",") if part.strip()]
    return out


def _bad_addresses(addresses) -> List[str]:
    return [a for a in addresses if "@" not in a or " " in a]


def test_mail_settings(args, env, raw):
    """(Settings for one test send, settings_from) or raise ConfigError naming what is missing."""
    src = _Sources(raw)
    sender = src.pick(args.sender, "mail.sender") or "graph"
    if sender not in ("graph", "smtp"):
        raise ConfigError(f"sender must be graph or smtp, got {sender!r}")
    sender_address = src.pick(args.sender_address, "mail.sender_address")
    to = _address_list(src.pick(_address_list(args.to), "mail.to"))
    cc = _address_list(src.pick(_address_list(args.cc), "mail.cc"))
    project = src.pick(args.project_name, "mail.project_name") or _Reader(raw or {}).lookup("site.name")
    project = project if isinstance(project, str) and project else "DPC"

    missing = []
    if not sender_address:
        missing.append("--from")
    if not to:
        missing.append("--to")
    bad = _bad_addresses(([sender_address] if sender_address else []) + to + cc)
    if bad:
        raise ConfigError(f"not an e-mail address: {', '.join(bad)}")

    s = Settings(root="", logs_dir="", state_dir="", backup_count=0, db_host="", db_port=0, db_name="",
                 db_user="", db_password="", sender=sender, sender_address=sender_address or "",
                 project_name=project, to=to, cc=cc, report_url=None, max_alert_age_hours=None, flood=None)
    if sender == "graph":
        for attr, json_key in (("graph_tenant_id", "graph.tenant_id"), ("graph_client_id", "graph.client_id"),
                               ("graph_client_secret", "graph.client_secret")):
            value = src.pick(env.get(TEST_ENV[attr]), json_key)
            if not value:
                missing.append(TEST_ENV[attr])
            setattr(s, attr, value or "")
    else:
        s.smtp_host = src.pick(args.smtp_host, "mail.smtp.host") or ""
        port = src.pick(args.smtp_port, "mail.smtp.port")
        s.smtp_port = int(port) if port else 0
        s.smtp_password = src.pick(env.get(TEST_ENV["smtp_password"]), "mail.smtp.password") or ""
        missing += [name for name, value in (("--smtp-host", s.smtp_host), ("--smtp-port", s.smtp_port),
                                             (TEST_ENV["smtp_password"], s.smtp_password)) if not value]
    if missing:
        raise ConfigError("missing: " + ", ".join(missing))
    return s, src.described()


def test_mail_content(project_name, now=None):
    now = now or datetime.datetime.now()
    subject = f"DPC test mail: {project_name}"
    body = (f"Dear All,<br><br>"
            f"This is a test mail from the {project_name} defect-alert program, sent "
            f"{now.strftime('%Y-%m-%d %H:%M:%S')}.<br>"
            f"It confirms that defect alerts can reach this address. It reports no defect.<br><br>"
            f"Thanks & Regards,<br>Insightzz Team")
    return subject, body


def run_test_mail(args, env=None, raw=None, raw_error=None) -> dict:
    """Send one test mail. Returns the result dict; never raises for mail or settings trouble."""
    env = os.environ if env is None else env
    result = {"ok": False, "status": TEST_BAD_REQUEST, "action": "test_mail", "message": "",
              "sender": None, "to_count": 0, "cc_count": 0, "settings_from": None}
    try:
        s, settings_from = test_mail_settings(args, env, raw)
    except ConfigError as e:
        result["message"] = str(e) + (f" (mail.json: {raw_error})" if raw_error else "")
        return result
    result.update(sender=s.sender, to_count=len(s.to), cc_count=len(s.cc), settings_from=settings_from)
    subject, body = test_mail_content(s.project_name)
    send = graph_send_result if s.sender == "graph" else smtp_send_result
    sent = send(s, subject, body, [], s.to, s.cc)
    message = sent.message
    for secret in sorted({x for x in s.secrets() if x}, key=len, reverse=True):
        message = message.replace(secret, "***")
    result.update(ok=sent.ok, status=TEST_OK if sent.ok else sent.status, message=message)
    return result


def _optional_mail_json():
    """(raw mail.json or None, error text or None). For the test mail the file is optional."""
    path = os.environ.get("DPC_WORKER_CONFIG", "")
    if not path or not os.path.isfile(path):
        return None, None
    try:
        return read_raw_config(), None
    except ConfigError as e:
        return None, str(e)


def _parse_args(argv):
    parser = argparse.ArgumentParser(description="DPC immediate defect-alert mail (no options = the service)")
    test = parser.add_argument_group("test mail (setup wizard / commissioning; never touches the DB)")
    test.add_argument("--test", action="store_true", help="send one test mail and exit")
    test.add_argument("--json", action="store_true", help="print the result as JSON (for the UI)")
    test.add_argument("--sender", choices=("graph", "smtp"), help="default: mail.json, else graph")
    test.add_argument("--from", dest="sender_address", help="sender address")
    test.add_argument("--to", action="append", help="recipient(s), comma-separated, may repeat")
    test.add_argument("--cc", action="append", help="cc address(es), comma-separated, may repeat")
    test.add_argument("--project-name", dest="project_name", help="name in the subject and text")
    test.add_argument("--smtp-host", dest="smtp_host")
    test.add_argument("--smtp-port", dest="smtp_port", type=int)
    args = parser.parse_args(argv)
    test_only = ("json", "sender", "sender_address", "to", "cc", "project_name", "smtp_host", "smtp_port")
    if not args.test and any(getattr(args, name) not in (None, False) for name in test_only):
        parser.error("these options belong to --test")
    return args


def test_mail_main(args) -> int:
    raw, raw_error = _optional_mail_json()
    result = run_test_mail(args, raw=raw, raw_error=raw_error)
    if args.json:
        print(json.dumps(result))
    else:
        print(f"{result['status']}: {result['message']}")
    return TEST_EXIT_CODES[result["status"]]


# =================================================================================
# 10. SERVICE
# =================================================================================

_stop = False


def _on_signal(signum, _frame):
    global _stop
    _stop = True


def setup_logging(logs_dir, backup_count) -> logging.Logger:
    os.makedirs(logs_dir, exist_ok=True)
    log = logging.getLogger("mail")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    for handler in (logging.StreamHandler(),
                    TimedRotatingFileHandler(os.path.join(logs_dir, "mail.log"), when='midnight',
                                             backupCount=backup_count)):
        handler.setFormatter(formatter)
        log.addHandler(handler)
    return log


def start_heartbeat(s: Settings, stats: PollStats, log, **heartbeat_kwargs) -> Heartbeat:
    """heartbeat_kwargs: for tests only (a fake MQTT client, a short interval)."""
    heartbeat = Heartbeat(WORKER_ID, mqtt=s.mqtt, extra=stats.fields, healthy=stats.healthy, logger=log,
                          **heartbeat_kwargs)
    heartbeat.start()           # off (one warning) without an mqtt block or without paho-mqtt
    return heartbeat


def run(s: Settings, log):
    conn = connect_db(s)
    cur = conn.cursor()
    stats = PollStats()
    heartbeat = None
    try:
        replay_journal(s, cur, log)
        now = datetime.datetime.now()
        stats.start_day(count_sent_since(cur, midnight(now)))
        heartbeat = start_heartbeat(s, stats, log)
        log.info(f"mail started: sender {s.sender}, {len(s.to)} + {len(s.cc)} cc addresses, "
                 f"flood breaker {'on' if s.flood else 'off'}, max age "
                 f"{_number(s.max_alert_age_hours) if s.max_alert_age_hours else 'off'} h")
        while not _stop:
            poll_once(s, cur, log, stats=stats)
            deadline = time.monotonic() + POLL_SECONDS
            while not _stop and time.monotonic() < deadline:
                time.sleep(0.5)
        log.info("stop signal: mail stopped")
    finally:
        if heartbeat is not None:
            heartbeat.stop()
        try:
            cur.close()
            conn.close()
        except Exception:
            pass


def main(argv=None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.test:
        return test_mail_main(args)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    raw, secrets = {}, []
    root = root_from_code_location()
    try:
        raw = read_raw_config()
    except ConfigError as exc:            # unreadable file: report it with the root we know
        clear_last_error(root, WORKER_ID)
        write_last_error(root, WORKER_ID, exc)
        print(f"mail: {exc}", file=sys.stderr)
        return 1
    secrets = secrets_in(raw)
    paths = raw.get("paths") if isinstance(raw.get("paths"), dict) else {}
    root = paths.get("root") or root
    clear_last_error(root, WORKER_ID)
    backup = (raw.get("log") or {}).get("backup_count") if isinstance(raw.get("log"), dict) else None
    log = setup_logging(paths.get("logs") or os.path.join(root, "logs"),
                        backup if isinstance(backup, int) else 5)
    if not os.environ.get("DPC_WORKER_CONFIG"):
        log.warning("DPC_WORKER_CONFIG is not set (mail.json not rendered yet)")
    try:
        settings = build_settings(raw)
        for warning in settings.warnings:
            log.warning(warning)
        run(settings, log)
        return 0
    except Exception as exc:
        record_path = write_last_error(root, WORKER_ID, exc, secrets)
        message = str(exc)
        for secret in sorted(set(secrets), key=len, reverse=True):
            message = message.replace(secret, "***")
        log.error(f"stopped by {type(exc).__name__}: {message[:500]} (details in {record_path})")
        return 1


if __name__ == "__main__":
    sys.exit(main())
