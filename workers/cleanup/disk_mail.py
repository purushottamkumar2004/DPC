"""Disk-usage mails through Microsoft Graph (old delete_script.py, texts unchanged).

The Graph login (`graph`) and the addresses come from cleanup.json (site.yaml `graph`,
`mail.sender_address`, `mail.disk_report_to` else `mail.recipients`). No secret is ever
logged: errors log the HTTP status and at most 300 characters of Graph's own answer.
"""

import datetime

TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
SEND_URL = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"


class GraphMailer:
    def __init__(self, mail_settings, log):
        self.m = mail_settings
        self.log = log

    def _token(self):
        import requests                  # only needed when a mail is really sent
        try:
            response = requests.post(TOKEN_URL.format(tenant=self.m.tenant_id), timeout=20, data={
                "grant_type": "client_credentials",
                "client_id": self.m.client_id,
                "client_secret": self.m.client_secret,
                "scope": "https://graph.microsoft.com/.default",
            })
        except Exception as e:
            self.log.error(f"Token Exception: {type(e).__name__}")
            return None
        if response.status_code == 200:
            return response.json().get("access_token")
        self.log.error(f"Token Error: HTTP {response.status_code} {response.text[:300]}")
        return None

    def send(self, subject: str, body_html: str) -> bool:
        if not self.m.enabled:
            self.log.warning(f"Disk mail is off (no Graph login or addresses); not sent: {subject}")
            return False
        import requests
        token = self._token()
        if not token:
            self.log.error("Unable to get MS Graph token")
            return False
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": body_html},
                "toRecipients": [{"emailAddress": {"address": a}} for a in self.m.to],
                "ccRecipients": [{"emailAddress": {"address": a}} for a in self.m.cc],
            },
            "saveToSentItems": True,
        }
        try:
            response = requests.post(SEND_URL.format(sender=self.m.sender_address), json=payload, timeout=30,
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/json"})
        except Exception as e:
            self.log.error(f"Mail Exception: {type(e).__name__}")
            return False
        if response.status_code == 202:
            self.log.info(f"Mail sent successfully: {subject}")
            return True
        self.log.error(f"Mail send failed: HTTP {response.status_code} {response.text[:300]}")
        return False


def _row(label, value):
    return f"""
        <tr>
            <td><b>{label}</b></td>
            <td>{value}</td>
        </tr>
"""


def daily_report(site_name, used_percent, threshold, now=None):
    """Old send_daily_disk_report: (subject, html)."""
    now = now or datetime.datetime.now()
    subject = f"Remainder -  Disk Usage Alert - {site_name}"
    status = "CRITICAL" if used_percent >= threshold else "NORMAL"
    body = f"""
    <html>
    <body>

    <h3>Remainder - Disk Usage Alert</h3>

    <table border="1" cellpadding="8" cellspacing="0">
{_row("Plant Name", site_name)}{_row("Current Disk Usage", f"{used_percent:.2f}%")}{_row("Threshold", f"{threshold}%")}{_row("Status", status)}{_row("Time", now.strftime('%Y-%m-%d %H:%M:%S'))}
    </table>

    <br>

    Thanks & Regards,<br>
    Insightzz System

    </body>
    </html>
    """
    return subject, body


def threshold_alert(site_name, used_percent, threshold, now=None):
    """Old send_threshold_alert: (subject, html)."""
    now = now or datetime.datetime.now()
    subject = f"⚠️ Disk Usage Alert - {site_name}"
    body = f"""
    <html>
    <body>

    <h3>Disk Usage Threshold Alert</h3>

    <table border="1" cellpadding="8" cellspacing="0">
{_row("Plant Name", site_name)}{_row("Current Disk Usage", f"{used_percent:.2f}%")}{_row("Threshold", f"{threshold}%")}{_row("Alert Time", now.strftime('%Y-%m-%d %H:%M:%S'))}
    </table>

    <br>

    <p>Please take immediate action.</p>

    <br>

    Thanks & Regards,<br>
    Insightzz System

    </body>
    </html>
    """
    return subject, body
