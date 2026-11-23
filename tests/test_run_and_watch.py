import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hub import core, server


class RunAndWatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        self.project = {"id": "sample", "path": str(self.path), "run": f'"{sys.executable}" main.py'}

    def script(self, code):
        (self.path / "main.py").write_text(code, encoding="utf-8")

    def wait_for(self, read, matches):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            snapshot = read()
            if matches(snapshot):
                return snapshot
            time.sleep(0.04)
        self.fail(f"Timed out waiting for state: {snapshot}")

    def test_restart_and_late_stop_are_bound_to_one_run(self):
        self.script("import time\nprint('ready', flush=True)\ntime.sleep(60)")
        endpoint = "/api/projects/sample/"
        with patch.object(server, "RUNS", {}), patch.object(server, "_project", return_value=self.project):
            try:
                first_id = server.api_post(endpoint + "run", {})["id"]
                first = server.RUNS["sample"]
                self.wait_for(first.output, lambda r: bool(r["lines"]))
                second_id = server.api_post(endpoint + "run", {})["id"]
                self.assertNotEqual(second_id, first_id)
                self.assertTrue(first.finished.is_set())
                self.assertTrue(first.output()["stopped"])
                server.api_post(endpoint + "stop", {"id": first_id})
                def read_new():
                    return server.api_get(endpoint + "run", {"id": [first_id], "from": ["999"]})
                second = self.wait_for(read_new, lambda r: bool(r["lines"]))
                self.assertTrue(second["running"])
                self.assertEqual([s["t"] for s in second["lines"]], ["ready"])
                self.assertEqual(second["from"], 0)
                server.api_post(endpoint + "stop", {"id": second_id})
                final = read_new()
                self.assertFalse(final["running"])
                self.assertTrue(final["stopped"])
                self.assertIsNotNone(final["code"])
                self.assertIsNotNone(final["ended"])
                self.assertEqual(final["problems"], [])
            finally:
                server._stop_all("sample")

    def test_log_rollover_preserves_cursor_errors_and_last_line(self):
        self.script("print('some folder/tool.py:7:2: error: early')\n"
                    "for i in range(21000): print(i)\n"
                    "print('0 errors in next stage')\nraise SystemExit(3)")
        run = core.Run(self.project, self.project["run"])
        self.addCleanup(run.stop)
        self.assertTrue(run.finished.wait(10))
        result = run.output()
        self.assertEqual(result["code"], 3)
        self.assertFalse(result["stopped"])
        self.assertEqual(result["from"], 5000)
        self.assertEqual(result["next"], 21002)
        self.assertEqual(result["problems"][0]["file"], "some folder/tool.py")
        self.assertEqual(result["problems"][0]["line"], 7)
        self.assertEqual(run.output(21000)["lines"], [
            {"t": "20999", "err": False}, {"t": "0 errors in next stage", "err": False},
        ])
        self.assertEqual(run.output(result["next"])["lines"], [])

    def test_warning_and_unstructured_failure(self):
        self.script("print('main.py:1:1: warning: example')")
        result = core.run_once(self.project)
        self.assertTrue(result["ok"])
        self.assertEqual(result["problems"][0]["severity"], "warning")
        self.script("print('details in log')\nraise SystemExit(9)")
        result = core.run_once(self.project)
        self.assertFalse(result["ok"])
        self.assertIn("9", result["problems"][0]["message"])

    def test_empty_first_scan_then_error_and_recovery(self):
        watcher = core.Watcher(self.project)
        self.addCleanup(watcher.stop)
        first = self.wait_for(watcher.snapshot, lambda s: s["state"] == "watching")
        self.assertGreater(first["checkedAt"], 0)
        file = self.path / "config.json"
        file.write_text('{\n"bad": }')
        error = self.wait_for(watcher.snapshot, lambda s: bool(s["problems"]))
        self.assertEqual(error["problems"][0]["line"], 2)
        file.write_text('{"repaired": true}')
        clean = self.wait_for(watcher.snapshot, lambda s: s["version"] > error["version"])
        self.assertEqual(clean["problems"], [])
        self.assertEqual(clean["state"], "watching")


if __name__ == "__main__":
    unittest.main()
