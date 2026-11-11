"""Microsoft Graph for the transfer program: one drive, app-only (client credentials).

Same login as workers/mail (graph.tenant_id / client_id / client_secret, token from
login.microsoftonline.com with scope https://graph.microsoft.com/.default). App-only has no
/me/drive, so the drive is named in config (transfer.drive):
    {"site": "contoso.sharepoint.com:/sites/DPC"}  -> the site's default document library
    {"user": "dpc@contoso.com"}                    -> that user's OneDrive
Permissions: workers/transfer/README.md (Q18).

Every method raises TemporaryError (retry later) or PermanentError (stop the job); a 404 is
returned as None where "not there" is a normal answer. Nothing here logs a token or a secret.

Upload sessions (large-file upload): POST .../createUploadSession -> uploadUrl (pre-authorized,
no Authorization header); PUT byte ranges with Content-Range; every range except the last must
be a multiple of 320 KiB; 202 = more expected (nextExpectedRanges), 200/201 = done (the item);
GET uploadUrl = status after a cut; 404 = the session is gone (expired) -> start a new one.
"""

import time
import urllib.parse
from typing import Dict, List, Optional, Tuple

import requests

from transfer_errors import PermanentError, TemporaryError

GRAPH = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
TIMEOUT_S = 60                       # per request; a fragment of 10 MiB at 2 Mbit/s takes 42 s
TOKEN_MARGIN_S = 300                 # renew 5 minutes before the token expires
UPLOAD_FRAGMENT_BYTES = 32 * 320 * 1024          # 10 MiB, a multiple of 320 KiB (Graph rule)


def _quote(path: str) -> str:
    return urllib.parse.quote(path.strip("/"), safe="/")


def _short(response) -> str:
    """'HTTP 403 accessDenied: <message>' from a Graph error body, at most 300 characters."""
    try:
        error = response.json().get("error", {})
        text = f"{error.get('code', '')}: {error.get('message', '')}".strip(": ")
    except ValueError:
        text = (response.text or "")[:200]
    return f"HTTP {response.status_code} {text}"[:300]


def _retry_after(response) -> float:
    try:
        return float(response.headers.get("Retry-After", 0))
    except (TypeError, ValueError):
        return 0.0


def raise_for(response, what: str):
    """Classify a failed response."""
    code = response.status_code
    if code in (408, 429) or code >= 500:
        raise TemporaryError(f"{what}: {_short(response)}", _retry_after(response))
    if code in (401, 403):
        raise PermanentError(f"{what}: {_short(response)} (check the app permission, "
                             f"workers/transfer/README.md)")
    raise PermanentError(f"{what}: {_short(response)}")


class GraphClient:
    def __init__(self, graph: Dict[str, str], drive: Dict[str, str], session=None, clock=time.time):
        self.graph = graph
        self.drive = drive
        self.http = session or requests.Session()
        self.clock = clock
        self._token: Optional[str] = None
        self._token_until = 0.0
        self._drive_base: Optional[str] = None

    # ------------------------------------------------------------------ plumbing

    def _request(self, method: str, url: str, what: str, auth: bool = True, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        if auth:
            headers["Authorization"] = f"Bearer {self.token()}"
        try:
            return self.http.request(method, url, headers=headers, timeout=TIMEOUT_S, **kwargs)
        except requests.RequestException as exc:
            raise TemporaryError(f"{what}: {type(exc).__name__} (network)") from None

    def token(self) -> str:
        if self._token and self.clock() < self._token_until:
            return self._token
        try:
            response = self.http.post(TOKEN_URL.format(tenant=self.graph["tenant_id"]), timeout=TIMEOUT_S, data={
                "grant_type": "client_credentials", "client_id": self.graph["client_id"],
                "client_secret": self.graph["client_secret"], "scope": "https://graph.microsoft.com/.default"})
        except requests.RequestException as exc:
            raise TemporaryError(f"Graph token: {type(exc).__name__} (network)") from None
        if response.status_code != 200:
            if response.status_code >= 500 or response.status_code == 429:
                raise TemporaryError(f"Graph token: HTTP {response.status_code}", _retry_after(response))
            # 400/401: wrong tenant, client id or secret (or the secret expired). Body text only,
            # never the request: it holds the secret.
            raise PermanentError(f"Graph token refused: HTTP {response.status_code} "
                                 f"(check graph.tenant_id / client_id / client_secret in site.yaml)")
        body = response.json()
        self._token = body["access_token"]
        self._token_until = self.clock() + float(body.get("expires_in", 3600)) - TOKEN_MARGIN_S
        return self._token

    def drive_base(self) -> str:
        """URL prefix of the drive: .../users/<upn>/drive or .../sites/<id>/drive."""
        if self._drive_base:
            return self._drive_base
        if "user" in self.drive:
            self._drive_base = f"{GRAPH}/users/{urllib.parse.quote(self.drive['user'])}/drive"
        else:
            host, _, path = self.drive["site"].partition(":/")
            response = self._request("GET", f"{GRAPH}/sites/{host}:/{_quote(path)}", "find the SharePoint site")
            if response.status_code != 200:
                raise_for(response, f"find the SharePoint site {self.drive['site']}")
            self._drive_base = f"{GRAPH}/sites/{response.json()['id']}/drive"
        return self._drive_base

    def _item_url(self, path: str, suffix: str = "") -> str:
        return f"{self.drive_base()}/root:/{_quote(path)}" + (f":/{suffix}" if suffix else "")

    # ------------------------------------------------------------------ items and folders

    def get_item(self, path: str) -> Optional[dict]:
        """The driveItem at path, or None when it does not exist."""
        response = self._request("GET", self._item_url(path), f"read {path}")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise_for(response, f"read {path}")
        return response.json()

    def list_children(self, path: str) -> Optional[List[dict]]:
        """Every item directly in the folder (all pages), or None when the folder does not exist."""
        url = self._item_url(path, "children") + "?$top=200"
        items = []
        while url:
            response = self._request("GET", url, f"list {path}")
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise_for(response, f"list {path}")
            body = response.json()
            items += body.get("value", [])
            url = body.get("@odata.nextLink")
        return items

    def ensure_folder(self, path: str) -> None:
        """Create path and its parents where missing (an existing folder is fine)."""
        parent = ""
        for name in path.strip("/").split("/"):
            current = f"{parent}/{name}" if parent else name
            if self.get_item(current) is None:
                url = (f"{self.drive_base()}/root/children" if not parent
                       else self._item_url(parent, "children"))
                response = self._request("POST", url, f"create folder {current}", json={
                    "name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})
                if response.status_code not in (200, 201, 409):           # 409: someone made it meanwhile
                    raise_for(response, f"create folder {current}")
            parent = current

    def delete_item(self, path: str) -> None:
        response = self._request("DELETE", self._item_url(path), f"delete {path}")
        if response.status_code not in (204, 404):
            raise_for(response, f"delete {path}")

    # ------------------------------------------------------------------ upload sessions

    def create_upload_session(self, path: str) -> Tuple[str, str]:
        """(uploadUrl, expirationDateTime) for a new upload that replaces any file at path."""
        response = self._request("POST", self._item_url(path, "createUploadSession"), f"start upload of {path}",
                                 json={"item": {"@microsoft.graph.conflictBehavior": "replace"}})
        if response.status_code != 200:
            raise_for(response, f"start upload of {path}")
        body = response.json()
        return body["uploadUrl"], body.get("expirationDateTime", "")

    def upload_status(self, upload_url: str) -> Optional[int]:
        """The next byte the session expects, or None when the session is gone (expired/unknown)."""
        response = self._request("GET", upload_url, "upload status", auth=False)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise_for(response, "upload status")
        return next_expected_start(response.json().get("nextExpectedRanges") or ["0-"])

    def upload_fragment(self, upload_url: str, data: bytes, start: int, total: int):
        """Send bytes [start, start+len(data)). Returns ("more", next_start) or ("done", item);
        ("resync", None) when Graph already has these bytes (416): ask upload_status;
        ("expired", None) when the session is gone: start a new one."""
        end = start + len(data) - 1
        response = self._request("PUT", upload_url, "upload fragment", auth=False, data=data, headers={
            "Content-Length": str(len(data)), "Content-Range": f"bytes {start}-{end}/{total}"})
        if response.status_code == 202:
            return "more", next_expected_start(response.json().get("nextExpectedRanges") or [f"{end + 1}-"])
        if response.status_code in (200, 201):
            return "done", response.json()
        if response.status_code == 416:
            return "resync", None
        if response.status_code == 404:
            return "expired", None                                 # the caller starts a new session
        raise_for(response, "upload fragment")

    def cancel_upload(self, upload_url: str) -> None:
        try:
            self._request("DELETE", upload_url, "cancel upload", auth=False)
        except TemporaryError:
            pass                                                  # it expires by itself

    # ------------------------------------------------------------------ download

    def download_range(self, path: str, start: int, end: int) -> bytes:
        """Bytes [start, end] (inclusive) of the file at path. Graph answers with a redirect to a
        pre-authorized URL; requests drops our Authorization header when it changes host."""
        response = self._request("GET", self._item_url(path, "content"), f"download {path}",
                                 headers={"Range": f"bytes={start}-{end}"}, allow_redirects=True)
        if response.status_code == 206:
            return response.content
        if response.status_code == 200:                            # server ignored Range: slice it
            return response.content[start:end + 1]
        if response.status_code == 404:
            raise PermanentError(f"download {path}: the file is no longer on OneDrive")
        raise_for(response, f"download {path}")


def next_expected_start(ranges: List[str]) -> int:
    """'12345-' or '12345-99999' -> 12345 (the lowest missing byte)."""
    starts = []
    for text in ranges:
        try:
            starts.append(int(str(text).split("-", 1)[0]))
        except ValueError:
            continue
    return min(starts) if starts else 0


def item_quickxor(item: dict) -> Optional[str]:
    return ((item or {}).get("file") or {}).get("hashes", {}).get("quickXorHash")
