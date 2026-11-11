"""transfer.json reading, and GraphClient's HTTP handling with a fake requests session."""
import json
import os
import subprocess
import sys
import unittest

import requests

from workers.transfer.tests import helpers as H
import graph_client as G
import transfer_settings as TS
from transfer_errors import PermanentError, TemporaryError


class Settings(unittest.TestCase):
    def setUp(self):
        self.root = H.temp_root(self)

    def test_full_file(self):
        s = TS.build_settings(H.raw_config(self.root))
        self.assertEqual(s.models_folder, "DPC/models/tadipatri", "<site.id> resolved")
        self.assertEqual(s.upload.remote_folder, "DPC/data/tadipatri")
        self.assertIsNone(s.upload.max_mbps, "null = unlimited")
        self.assertEqual([x.side for x in s.sides], ["LEFT", "RIGHT"])
        self.assertEqual(s.sides[1].raw_images, f"{self.root}/data/frames/CAM2/RAW_IMAGES")
        self.assertEqual(s.warnings, [])

    def test_defaults_with_one_warning_each(self):
        raw = H.raw_config(self.root)
        del raw["paths"]["staging"]
        raw["transfer"]["upload"] = {}
        del raw["transfer"]["models_folder"]
        s = TS.build_settings(raw)
        self.assertEqual(s.staging, f"{self.root}/data/transfer/staging")
        self.assertEqual((s.upload.chunk_mb, s.upload.staging_max_gb, s.upload.max_mbps, s.upload.plate_images),
                         (500, 20.0, 20.0, 100))
        self.assertEqual(s.models_folder, "DPC/models/tadipatri")
        self.assertEqual(len(s.warnings), 7)

    def test_no_upload_section_means_download_only(self):
        raw = H.raw_config(self.root)
        raw["transfer"]["upload"] = None
        self.assertIsNone(TS.build_settings(raw).upload)

    def test_secrets_and_drive_are_never_defaulted(self):
        raw = H.raw_config(self.root)
        del raw["graph"]["client_secret"]
        del raw["transfer"]["drive"]
        with self.assertRaises(TS.ConfigError) as ctx:
            TS.build_settings(raw)
        self.assertIn("graph.client_secret", str(ctx.exception))
        self.assertIn("transfer.drive", str(ctx.exception))

    def test_bad_values(self):
        for change, text in ((lambda r: r["transfer"].update(drive={"site": "no-colon"}), "transfer.drive.site"),
                             (lambda r: r["transfer"].update(drive={"group": "x"}), "transfer.drive"),
                             (lambda r: r["transfer"]["upload"].update(chunk_mb=0), "chunk_mb"),
                             (lambda r: r["transfer"]["upload"].update(max_mbps=-1), "max_mbps"),
                             (lambda r: r["transfer"].update(models_folder="DPC/../x"), "models_folder")):
            raw = H.raw_config(self.root)
            change(raw)
            with self.assertRaises(TS.ConfigError, msg=text) as ctx:
                TS.build_settings(raw)
            self.assertIn(text, str(ctx.exception))

    def test_a_config_error_writes_last_error_without_the_secret(self):
        raw = H.raw_config(self.root)
        raw["transfer"]["drive"] = {"site": H.GRAPH_SECRET}        # an error message that would echo it
        path = os.path.join(self.root, "transfer.json")
        with open(path, "w") as f:
            json.dump(raw, f)
        os.makedirs(os.path.join(self.root, "run"), exist_ok=True)
        env = dict(os.environ, DPC_WORKER_CONFIG=path)
        result = subprocess.run([sys.executable, os.path.join(H.WORKER_DIR, "transfer.py")], env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1)
        with open(os.path.join(self.root, "run", "transfer.last_error.json")) as f:
            record = f.read()
        self.assertIn("ConfigError", record)
        self.assertNotIn(H.GRAPH_SECRET, record + result.stdout + result.stderr)


class Response:
    def __init__(self, status, body=None, headers=None, content=b""):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.content = content
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class Session:
    """Answers requests from a list; records what was asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def _next(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def post(self, url, **kwargs):
        return self._next("POST", url, **kwargs)

    def request(self, method, url, **kwargs):
        return self._next(method, url, **kwargs)


TOKEN = Response(200, {"access_token": "tok", "expires_in": 3600})
GRAPH = {"tenant_id": "t", "client_id": "c", "client_secret": H.GRAPH_SECRET}


class Client(unittest.TestCase):
    def client(self, *answers, drive=None):
        self.session = Session(*answers)
        return G.GraphClient(GRAPH, drive or {"user": "dpc@contoso.com"}, session=self.session, clock=H.Clock())

    def test_token_is_reused_and_user_drive_needs_no_lookup(self):
        c = self.client(TOKEN, Response(404), Response(404))
        self.assertIsNone(c.get_item("a/b"))
        self.assertIsNone(c.get_item("a/c"))
        self.assertEqual(sum(1 for m, *_ in self.session.calls if m == "POST"), 1)
        self.assertIn("/users/dpc%40contoso.com/drive/root:/a/c", self.session.calls[-1][1])

    def test_site_drive_is_looked_up_once(self):
        c = self.client(TOKEN, Response(200, {"id": "site-id"}), Response(404), Response(404),
                        drive={"site": "contoso.sharepoint.com:/sites/DPC"})
        c.get_item("x")
        c.get_item("y")
        self.assertIn("/sites/contoso.sharepoint.com:/sites/DPC", self.session.calls[1][1])
        self.assertIn("/sites/site-id/drive/root:/y", self.session.calls[-1][1])

    def test_classification(self):
        cases = [(Response(503, headers={"Retry-After": "7"}), TemporaryError),
                 (Response(429), TemporaryError),
                 (Response(403, {"error": {"code": "accessDenied", "message": "no"}}), PermanentError),
                 (Response(400, {"error": {"code": "invalidRequest", "message": "bad"}}), PermanentError),
                 (requests.ConnectionError("down"), TemporaryError)]
        for answer, kind in cases:
            c = self.client(TOKEN, answer)
            with self.assertRaises(kind) as ctx:
                c.get_item("x")
            if isinstance(answer, Response) and answer.status_code == 503:
                self.assertEqual(ctx.exception.retry_after_s, 7.0)
            if isinstance(answer, Response) and answer.status_code == 403:
                self.assertIn("README", str(ctx.exception))

    def test_refused_token_is_permanent_and_never_shows_the_secret(self):
        c = self.client(Response(401, {"error": "invalid_client", "secret": H.GRAPH_SECRET}))
        with self.assertRaises(PermanentError) as ctx:
            c.token()
        self.assertNotIn(H.GRAPH_SECRET, str(ctx.exception))
        self.assertIn("client_secret", str(ctx.exception))

    def test_upload_fragment_outcomes(self):
        c = self.client(Response(202, {"nextExpectedRanges": ["327680-"]}),
                        Response(201, {"size": 10, "file": {"hashes": {"quickXorHash": "h"}}}),
                        Response(416), Response(404))
        self.assertEqual(c.upload_fragment("https://up/1", b"x" * 327680, 0, 1000000), ("more", 327680))
        outcome, item = c.upload_fragment("https://up/1", b"x", 327680, 327681)
        self.assertEqual((outcome, G.item_quickxor(item)), ("done", "h"))
        self.assertEqual(c.upload_fragment("https://up/1", b"x", 0, 1), ("resync", None))
        self.assertEqual(c.upload_fragment("https://up/1", b"x", 0, 1), ("expired", None))
        method, url, kwargs = self.session.calls[0]
        self.assertNotIn("Authorization", kwargs["headers"], "uploadUrl is pre-authorized")
        self.assertEqual(kwargs["headers"]["Content-Range"], "bytes 0-327679/1000000")

    def test_next_expected_start(self):
        self.assertEqual(G.next_expected_start(["12345-"]), 12345)
        self.assertEqual(G.next_expected_start(["500-600", "100-200"]), 100)
        self.assertEqual(G.next_expected_start([]), 0)


if __name__ == "__main__":
    unittest.main()


class Check(unittest.TestCase):
    def test_permission_check_reports_each_step(self):
        root = H.temp_root(self)
        s = H.make_settings(root)
        graph = H.FakeGraph()
        graph.token = lambda: "tok"
        graph.drive_base = lambda: "https://graph.microsoft.com/v1.0/sites/x/drive"
        lines = []
        import transfer
        self.assertTrue(transfer.check_graph(s, graph, out=lines.append))
        self.assertEqual([line.split()[0] for line in lines], ["OK"] * 4)
        self.assertEqual(graph.files, {}, "the test file is deleted again")

    def test_permission_refused_is_a_fail_line(self):
        s = H.make_settings(H.temp_root(self))
        graph = H.FakeGraph()

        def refused():
            raise PermanentError("Graph token refused: HTTP 401")
        graph.token = refused
        lines = []
        import transfer
        self.assertFalse(transfer.check_graph(s, graph, out=lines.append))
        self.assertTrue(lines[0].startswith("FAIL  token"))
