"""transfer.py --check: rendered and direct mode (F91), JSON result, secrets never shown."""
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest

from workers.transfer.tests import helpers as H
import graph_check as C
from transfer_errors import PermanentError, TemporaryError

LOGIN = {"tenant_id": "tenant-guid", "client_id": "client-guid", "client_secret": H.GRAPH_SECRET}
ENV = {"DPC_GRAPH_TENANT_ID": "tenant-guid", "DPC_GRAPH_CLIENT_ID": "client-guid",
       "DPC_GRAPH_CLIENT_SECRET": H.GRAPH_SECRET}
DIRECT = ["--check", "--json", "--site-id", "tadipatri", "--drive-site", "contoso.sharepoint.com:/sites/DPC"]


class Graph(H.FakeGraph):
    """FakeGraph + login and drive lookup, each can be made to fail."""

    def __init__(self, token_error=None, drive_error=None):
        super().__init__()
        self.token_error = token_error
        self.drive_error = drive_error
        self.write_error = None
        self.made_with = None

    def token(self):
        if self.token_error:
            raise self.token_error
        return "tok"

    def drive_base(self):
        if self.drive_error:
            raise self.drive_error
        return "https://graph.microsoft.com/v1.0/sites/site-guid/drive"

    def create_upload_session(self, path):
        if self.write_error:
            raise self.write_error
        return super().create_upload_session(path)


class Run:
    """check_main in-process with a fake Graph; returns (exit code, parsed JSON or text lines)."""

    def __init__(self, graph=None):
        self.graph = graph or Graph()

    def factory(self, login, drive):
        self.graph.made_with = (login, drive)
        return self.graph

    def __call__(self, argv, stdin="", environ=None):
        lines = []
        code = C.check_main(argv, stdin=io.StringIO(stdin), environ=ENV if environ is None else environ,
                            graph_factory=self.factory, out=lines.append)
        return code, (json.loads(lines[0]) if "--json" in argv else lines), "\n".join(lines)


class Direct(unittest.TestCase):
    def test_all_steps_pass_without_any_rendered_file(self):
        run = Run()
        code, result, text = run(DIRECT)
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "direct")
        self.assertEqual([(s["step"], s["status"]) for s in result["steps"]],
                         [("token", "ok"), ("drive", "ok"), ("models_folder", "ok"), ("upload_write", "ok")])
        self.assertEqual((result["models_folder"], result["upload_folder"]),
                         ("DPC/models/tadipatri", "DPC/data/tadipatri"), "site.yaml defaults")
        self.assertFalse(result["steps"][2]["exists"], "a new site has no models folder yet: still OK")
        self.assertEqual(run.graph.made_with, (LOGIN, {"site": "contoso.sharepoint.com:/sites/DPC"}))
        self.assertEqual(run.graph.files, {}, "the test file is deleted again")
        self.assertIn(("DPC/data/tadipatri/_check/check.txt", 0, len(C.CHECK_FILE_TEXT) - 1),
                      run.graph.download_calls, "read back")
        self.assertNotIn(H.GRAPH_SECRET, text)

    def test_login_from_stdin_wins_and_accepts_the_site_yaml_wrapper(self):
        run = Run()
        code, result, _ = run(DIRECT + ["--graph-stdin"], stdin=json.dumps({"graph": LOGIN}), environ={})
        self.assertEqual(code, 0)
        self.assertEqual(run.graph.made_with[0], LOGIN)

    def test_explicit_folders_user_drive_and_upload_off(self):
        run = Run()
        run.graph.ensure_folder("Custom/models")
        run.graph.put_file("Custom/models/dpc_maskrcnn/2026.11.20.zip", b"z")
        code, result, _ = run(["--check", "--json", "--drive-user", "dpc@contoso.com",
                               "--models-folder", "Custom/models", "--no-upload"])
        self.assertEqual(code, 0)
        self.assertEqual(result["drive"], {"user": "dpc@contoso.com"})
        self.assertIsNone(result["upload_folder"])
        self.assertEqual(result["steps"][3]["status"], "skipped")
        self.assertTrue(result["steps"][2]["exists"])
        self.assertIn("1 item", result["steps"][2]["detail"])

    def test_refused_login_skips_the_rest(self):
        run = Run(Graph(token_error=PermanentError("Graph token refused: HTTP 401 (check graph.client_secret)")))
        code, result, _ = run(DIRECT)
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertEqual([s["status"] for s in result["steps"]], ["fail", "skipped", "skipped", "skipped"])

    def test_site_not_granted(self):
        run = Run(Graph(drive_error=PermanentError("find the SharePoint site: HTTP 403 accessDenied")))
        code, result, _ = run(DIRECT)
        self.assertEqual(code, 1)
        self.assertEqual([s["status"] for s in result["steps"]], ["ok", "fail", "skipped", "skipped"])
        self.assertIn("403", result["steps"][1]["detail"])

    def test_read_only_grant_fails_the_write_step(self):
        run = Run()
        run.graph.write_error = PermanentError("start upload: HTTP 403 accessDenied (check the app permission)")
        code, result, _ = run(DIRECT)
        self.assertEqual(code, 1)
        self.assertEqual([s["status"] for s in result["steps"]], ["ok", "ok", "ok", "fail"])

    def test_network_trouble_is_a_fail_with_the_reason(self):
        run = Run(Graph(token_error=TemporaryError("Graph token: ConnectionError (network)")))
        code, result, _ = run(DIRECT)
        self.assertEqual(code, 1)
        self.assertIn("network", result["steps"][0]["detail"])

    def test_a_secret_in_an_error_is_never_shown(self):
        run = Run(Graph(token_error=PermanentError(f"weird answer echoing {H.GRAPH_SECRET}")))
        code, result, text = run(DIRECT)
        self.assertNotIn(H.GRAPH_SECRET, text)
        self.assertIn("***", result["steps"][0]["detail"])

    def test_text_output(self):
        code, lines, _ = Run()([a for a in DIRECT if a != "--json"])
        self.assertEqual(code, 0)
        self.assertEqual([line.split()[0] for line in lines], ["OK"] * 4)


class DirectArgumentErrors(unittest.TestCase):
    def error(self, argv, stdin="", environ=None):
        code, result, text = Run()(argv, stdin, environ)
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["steps"], [])
        self.assertNotIn(H.GRAPH_SECRET, text)
        return result["error"]

    def test_missing_login_names_the_variables_not_values(self):
        error = self.error(DIRECT, environ={"DPC_GRAPH_TENANT_ID": "t"})
        self.assertIn("DPC_GRAPH_CLIENT_ID", error)
        self.assertIn("DPC_GRAPH_CLIENT_SECRET", error)

    def test_bad_stdin(self):
        self.assertIn("not one JSON object", self.error(DIRECT + ["--graph-stdin"], stdin="tenant=x"))
        self.assertIn("client_secret", self.error(DIRECT + ["--graph-stdin"],
                                                  stdin=json.dumps({"tenant_id": "t", "client_id": "c"})))

    def test_site_id_needed_for_default_folders(self):
        self.assertIn("--site-id", self.error(["--check", "--json", "--drive-site", "h:/sites/x"]))

    def test_bad_values(self):
        self.assertIn("drive.site", self.error(["--check", "--json", "--site-id", "s", "--drive-site", "no-colon"]))
        self.assertIn("--site-id", self.error(["--check", "--json", "--site-id", "a b", "--drive-site", "h:/sites/x"]))
        self.assertIn("--models-folder", self.error(DIRECT + ["--models-folder", "DPC/../x"]))

    def test_direct_options_without_a_drive(self):
        self.assertIn("need --drive-site", self.error(["--check", "--json", "--site-id", "tadipatri"]))

    def test_both_drives_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
            Run()(DIRECT + ["--drive-user", "x@y"])
        self.assertEqual(ctx.exception.code, 2)


class Rendered(unittest.TestCase):
    def test_rendered_file_still_works(self):
        root = H.temp_root(self)
        path = os.path.join(root, "transfer.json")
        with open(path, "w") as f:
            json.dump(H.raw_config(root), f)
        os.environ["DPC_WORKER_CONFIG"] = path
        self.addCleanup(os.environ.pop, "DPC_WORKER_CONFIG")
        code, result, _ = Run()(["--check", "--json"], environ={})
        self.assertEqual(code, 0)
        self.assertEqual(result["mode"], "rendered")
        self.assertEqual(result["upload_folder"], "DPC/data/tadipatri")

    def test_nothing_given(self):
        saved = os.environ.pop("DPC_WORKER_CONFIG", None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, "DPC_WORKER_CONFIG", saved)
        code, result, _ = Run()(["--check", "--json"])
        self.assertEqual(code, 1)
        self.assertIn("--drive-site", result["error"])


class Program(unittest.TestCase):
    def test_real_program_prints_one_json_object_and_no_secret(self):
        """The wizard's call, end to end in a subprocess: login on stdin, an unreachable tenant
        (no network in tests), exactly one JSON line on stdout."""
        env = {k: v for k, v in os.environ.items() if k not in ("DPC_WORKER_CONFIG",) and not k.startswith("DPC_GRAPH")}
        env["HTTPS_PROXY"] = env["https_proxy"] = "http://127.0.0.1:9"        # make the network fail fast
        result = subprocess.run([sys.executable, os.path.join(H.WORKER_DIR, "transfer.py")] + DIRECT
                                + ["--graph-stdin"], input=json.dumps(LOGIN), env=env,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 1)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1, result.stdout + result.stderr)
        answer = json.loads(lines[0])
        self.assertEqual(answer["steps"][0], {"step": "token", "status": "fail",
                                              "detail": answer["steps"][0]["detail"]})
        self.assertIn("network", answer["steps"][0]["detail"])
        self.assertNotIn(H.GRAPH_SECRET, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
