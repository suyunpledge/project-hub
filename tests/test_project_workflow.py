import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from hub import core, server


class ProjectWorkflowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "项目"
        self.project.mkdir()
        self.path = str(self.project)

    def git(self, *args):
        return core.git(self.path, *args)

    def test_encoded_project_and_group_routes(self):
        with patch.object(core, "DATA_DIR", self.root), patch.object(core, "REGISTRY_FILE", self.root / "hub.json"):
            core.add_project(self.path, group="工具")
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            self.addCleanup(httpd.server_close)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            self.addCleanup(httpd.shutdown)
            base = f"http://127.0.0.1:{httpd.server_port}/api/"
            with urlopen(base + "projects/" + quote("项目")) as response:
                self.assertEqual(json.load(response)["name"], "项目")
            endpoint = base + "groups/" + quote("工具")
            headers = {"X-Hub": "1", "Content-Type": "application/json"}
            with urlopen(Request(endpoint, b'{"description":"ok"}', headers)) as response:
                self.assertTrue(json.load(response)["ok"])
            for body in (b"[1]", b"{"):
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(endpoint, body, headers))
                with error.exception as response:
                    self.assertEqual(response.code, 400)
                    self.assertIn("error", json.load(response))

    def test_save_preserves_source_format_and_detects_conflicts(self):
        file = self.project / "说明.txt"
        for encoding in ("gbk", "utf-8-sig", "utf-8"):
            with self.subTest(encoding=encoding):
                file.write_bytes("第一行\r\n".encode(encoding))
                opened = core.read_file(self.path, file.name)
                core.write_file(self.path, file.name, "第二行\n", opened["version"])
                expected = "第二行\r\n".encode(encoding)
                self.assertEqual(file.read_bytes(), expected)
                with self.assertRaises(core.HubError):
                    core.write_file(self.path, file.name, "过时的内容", opened["version"])
                self.assertEqual(file.read_bytes(), expected)
        file.write_bytes(b"a\r\nb\n")
        self.assertFalse(core.read_file(self.path, file.name)["editable"])

    def test_selected_commit_across_branches_and_current_index(self):
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Hub Test")
        self.git("config", "user.email", "hub@example.invalid")
        for name in ("old.txt", "drop.txt", "untouched.txt", "note[1].txt", "note1.txt"):
            (self.project / name).write_text("base")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.git("switch", "-c", "review")
        (self.project / "branch-only.txt").write_text("keep")
        (self.project / "drop.txt").write_text("target version")
        self.git("add", ".")
        self.git("commit", "-m", "branch files")
        self.git("switch", "main")
        self.git("mv", "old.txt", "新名.txt")
        self.git("rm", "drop.txt")
        (self.project / "untouched.txt").write_text("already staged")
        self.git("add", "untouched.txt")
        (self.project / "note[1].txt").write_text("literal filename")
        (self.project / "note1.txt").write_text("unselected")
        files = ["old.txt", "新名.txt", "drop.txt", "note[1].txt"]
        index = self.git("write-tree")
        head = self.git("rev-parse", "HEAD")
        core.commit_to_branch(self.path, "review", "selected", files)
        self.assertEqual(self.git("branch", "--show-current").strip(), "main")
        self.assertEqual(self.git("write-tree"), index)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("show", "review:branch-only.txt"), "keep")
        self.assertEqual(self.git("show", "review:note1.txt"), "base")
        self.assertEqual(self.git("show", "review:note[1].txt"), "literal filename")
        self.assertEqual(self.git("show", "review:新名.txt"), "base")
        self.assertEqual(self.git("ls-tree", "review", "--", "drop.txt", "old.txt"), "")
        core.commit_to_branch(self.path, "main", "selected", files)
        self.assertEqual(self.git("diff", "--cached", "--name-only").strip(), "untouched.txt")
        self.assertEqual(self.git("show", "main:untouched.txt"), "base")
        with self.assertRaises(core.HubError):
            core.commit_to_branch(self.path, "main", "empty selection", [])


if __name__ == "__main__":
    unittest.main()
