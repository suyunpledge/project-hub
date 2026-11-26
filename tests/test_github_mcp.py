"""GitHub authorization / publish, MCP server, search ranking, management and edge cases.

GitHub itself is replaced by a local mock API; pushes to https://github.com/... are redirected to
local bare repositories with ``url.<path>.pushInsteadOf``, so nothing touches the network.
"""
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from hub import core, github, manage, mcp_server, search

TOKEN = "ghp_" + "a1b2c3d4e5" * 4


class MockGitHub(BaseHTTPRequestHandler):
    repos: dict = {}
    root: Path = Path()

    def log_message(self, *a):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-OAuth-Scopes", "repo")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(401, {"message": "Bad credentials"})
            return False
        return True

    def do_GET(self):
        if not self._authorized():
            return
        if self.path == "/user":
            return self._send(200, {"login": "tester", "name": "Test"})
        name = self.path.rsplit("/", 1)[-1]
        if self.path.startswith("/repos/tester/") and name in self.repos:
            return self._send(200, self.repos[name])
        self._send(404, {"message": "Not Found"})

    def do_POST(self):
        if not self._authorized():
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        name = body["name"]
        if name in self.repos:
            return self._send(422, {"message": "Repository creation failed.",
                                    "errors": [{"message": "name already exists on this account"}]})
        subprocess.run(["git", "init", "-q", "--bare", str(self.root / f"{name}.git")], check=True)
        repo = {"full_name": f"tester/{name}", "html_url": f"https://github.com/tester/{name}",
                "clone_url": f"https://github.com/tester/{name}.git", "private": body["private"],
                "default_branch": "main"}
        self.repos[name] = repo
        self._send(201, repo)


class GitHubMcpTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patches = [patch.object(core, "DATA_DIR", self.root / "data"),
                   patch.object(core, "REGISTRY_FILE", self.root / "data" / "hub.json"),
                   patch.dict(os.environ, {"GITHUB_TOKEN": "", "GH_TOKEN": ""})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        remotes = self.root / "remotes"
        remotes.mkdir()
        MockGitHub.repos, MockGitHub.root = {}, remotes
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockGitHub)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        p = patch.object(github, "API", f"http://127.0.0.1:{httpd.server_port}")
        p.start()
        self.addCleanup(p.stop)

    def make_project(self, name="演示项目", commit=True):
        path = self.root / name
        path.mkdir()
        core.git(str(path), "init", "-q", "-b", "main")
        core.git(str(path), "config", "user.email", "t@example.com")
        core.git(str(path), "config", "user.name", "T")
        # pushes to github.com/tester/* land in local bare repos
        core.git(str(path), "config", f"url.{(self.root / 'remotes').as_posix()}/.pushInsteadOf",
                 "https://github.com/tester/")
        if commit:
            (path / "app.py").write_text("print('hi')\n", "utf-8")
            core.git(str(path), "add", "-A")
            core.git(str(path), "commit", "-qm", "init")
        return core.add_project(str(path))

    # ------------------------------------------------------------ authorization
    def test_requires_authorization_and_grants(self):
        with self.assertRaises(core.HubError) as e:
            github.create_repo("demo")
        self.assertEqual(e.exception.details["code"], "github_not_connected")
        with self.assertRaises(core.HubError) as e:
            github.connect("token", "not a token")
        github.connect("token", TOKEN, {"createRepo": False})
        raw = (core.DATA_DIR / "github.json").read_text("utf-8")
        self.assertNotIn(TOKEN, raw, "token must not be stored in plain text")
        st = github.status()
        self.assertTrue(st["ok"])
        self.assertEqual(st["login"], "tester")
        with self.assertRaises(core.HubError) as e:
            github.create_repo("demo")
        self.assertEqual(e.exception.details["code"], "github_not_granted")
        github.set_grants({"createRepo": True})
        self.assertEqual(github.create_repo("demo")["fullName"], "tester/demo")
        with self.assertRaises(core.HubError) as e:
            github.create_repo("demo")
        self.assertEqual(e.exception.details["code"], "github_repo_exists")
        github.disconnect()
        self.assertFalse(github.status()["connected"])

    def test_bad_token_is_rejected_before_saving(self):
        with self.assertRaises(core.HubError) as e:
            github.connect("token", "ghp_" + "z" * 36)
        self.assertEqual(e.exception.details["code"], "github_unauthorized")
        self.assertFalse((core.DATA_DIR / "github.json").exists())

    # ------------------------------------------------------------ publish
    def test_publish_creates_repo_pushes_and_is_rerunnable(self):
        p = self.make_project()
        github.connect("token", TOKEN)
        r = github.publish(p["id"], repo_name="demo-app")
        self.assertTrue(r["created"])
        self.assertEqual(r["repo"], "tester/demo-app")
        head = core.git(p["path"], "rev-parse", "HEAD").strip()
        bare = str(self.root / "remotes" / "demo-app.git")
        self.assertEqual(core.git(bare, "rev-parse", "main").strip(), head)
        self.assertTrue(r["upstreamSet"])
        self.assertEqual(core.git(p["path"], "config", "branch.main.remote").strip(), "origin")
        self.assertFalse(MockGitHub.repos["demo-app"]["private"] is False)  # private by default
        # second run: remote reused, new commit pushed, no second repo
        (Path(p["path"]) / "b.txt").write_text("x", "utf-8")
        core.commit_to_branch(p["path"], "", "add b", ["b.txt"])
        r2 = github.publish(p["id"])
        self.assertFalse(r2["created"])
        self.assertEqual(core.git(bare, "rev-parse", "main").strip(), core.git(p["path"], "rev-parse", "HEAD").strip())

    def test_publish_edge_cases(self):
        github.connect("token", TOKEN)
        empty = self.make_project("空仓库", commit=False)
        with self.assertRaises(core.HubError) as e:
            github.publish(empty["id"])
        self.assertEqual(e.exception.details["code"], "no_commits")
        p = self.make_project("别处")
        core.git(p["path"], "remote", "add", "origin", "https://gitlab.example.com/x/y.git")
        with self.assertRaises(core.HubError) as e:
            github.publish(p["id"])
        self.assertEqual(e.exception.details["code"], "remote_taken")
        with self.assertRaises(core.HubError):
            github.create_repo("bad name!")

    # ------------------------------------------------------------ MCP
    def call(self, name, args=None):
        reply = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": name, "arguments": args or {}}})
        res = reply["result"]
        return res["isError"], json.loads(res["content"][0]["text"])

    def test_mcp_protocol_and_grants(self):
        init = mcp_server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                                  "params": {"protocolVersion": "2024-11-05"}})
        self.assertEqual(init["result"]["protocolVersion"], "2024-11-05")
        self.assertIsNone(mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        tools = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertIn("github_publish_project", {t["name"] for t in tools})
        unknown = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope"}})
        self.assertEqual(unknown["error"]["code"], -32602)
        p = self.make_project()
        github.connect("token", TOKEN, {"push": False})
        err, body = self.call("github_publish_project", {"project": p["ref"] if "ref" in p else p["id"]})
        self.assertTrue(err)
        self.assertEqual(body["code"], "github_not_granted")
        github.set_grants({"push": True})
        err, body = self.call("github_publish_project", {"project": p["id"], "repoName": "via-mcp"})
        self.assertFalse(err, body)
        self.assertEqual(body["repo"], "tester/via-mcp")
        err, body = self.call("hub_commit", {"project": p["id"], "message": "x"})
        self.assertTrue(err)  # nothing to commit -> clean error, server keeps running
        err, body = self.call("hub_search", {"query": "演示"})
        self.assertFalse(err)
        self.assertEqual(body["items"][0]["id"], p["id"]) if isinstance(body, dict) and "items" in body else \
            self.assertEqual(body[0]["id"], p["id"])

    # ------------------------------------------------------------ search & management
    def test_ranking_fuzzy_and_grep(self):
        a = self.make_project("tool")
        b = self.make_project("my-tool-kit")
        c = self.make_project("other")
        core.update_project(c["id"], {"description": "a tool for others"})
        ids = [r["id"] for r in search.search_projects("tool")]
        self.assertEqual(ids[0], a["id"], "exact name match ranks first")
        self.assertEqual(set(ids), {a["id"], b["id"], c["id"]})
        fuzzy = search.search_projects("mtk")
        self.assertEqual([r["id"] for r in fuzzy], [b["id"]])
        self.assertTrue(fuzzy[0]["fuzzy"])
        self.assertEqual(search.search_projects("zzzz"), [])
        (Path(a["path"]) / "notes.md").write_text("needle here\n", "utf-8")
        g = search.grep("NEEDLE")
        self.assertEqual(g["total"], 1)
        self.assertEqual(g["results"][0]["hits"][0]["file"], "notes.md")
        with self.assertRaises(core.HubError):
            search.grep("(", regex=True)

    def test_management_and_registry_recovery(self):
        a = self.make_project("alpha")
        b = self.make_project("beta")
        manage.batch([a["id"], b["id"]], group="后端", add_tags=["服务"], pinned=True)
        reg = core.load_registry()
        self.assertTrue(all(p["pinned"] and p["tags"] == ["服务"] for p in reg["projects"]))
        ov = manage.overview()
        self.assertEqual(ov["summary"]["projects"], 2)
        moved = self.root / "alpha-moved"
        os.replace(a["path"], moved)
        self.assertEqual(manage.prune()["missing"][0]["id"], a["id"])
        manage.relocate(a["id"], str(moved))
        self.assertEqual(manage.prune()["missing"], [])
        with self.assertRaises(core.HubError):
            manage.relocate(a["id"], b["path"])  # already registered
        # a crash leaves broken JSON: the backup takes over
        core.REGISTRY_FILE.write_text("{not json", "utf-8")
        self.assertEqual(len(core.load_registry()["projects"]), 2)
        with self.assertRaises(core.HubError):
            core.update_project(a["id"], {"name": "  "})
        with self.assertRaises(core.HubError):
            core.update_project(a["id"], {"run": 42})

    def test_unused_preview_expires(self):
        p = self.make_project("推送")
        bare = self.root / "remotes" / "plain.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        core.git(p["path"], "remote", "add", "origin", str(bare))
        old = core.push_project(p, None, None, None, True)["previewId"]
        with self.assertRaises(core.HubError) as e:
            core.push_project(p, None, None, None, False)
        self.assertEqual(e.exception.details["code"], "preview_id_required")
        with patch.object(core, "PREVIEW_TTL", -1):
            r = core.push_project(p, None, None, None, False)
            self.assertTrue(r["upstreamSet"])
            with self.assertRaises(core.HubError) as e:
                core.push_project(p, None, None, None, False, old)
            self.assertEqual(e.exception.details.get("reason"), "expired")


if __name__ == "__main__":
    unittest.main()
