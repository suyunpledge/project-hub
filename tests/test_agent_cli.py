import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from hub import core


class AgentCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / "registry"
        self.repo = self.root / "本地 项目"
        self.repo.mkdir()
        self.env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        self.pid = self.cli("add", str(self.repo), "--group", "研发")["id"]

    def cli(self, *args, expected=0, env=None):
        result = subprocess.run([sys.executable, str(core.ROOT / "hub.py"), "--data", str(self.data),
                                 *args, "--json"], env=env or self.env, capture_output=True, timeout=25)
        self.assertEqual(result.returncode, expected, (result.stdout, result.stderr))
        return json.loads(result.stdout)

    def git(self, *args, repo=None):
        return subprocess.check_output(["git", "-C", str(repo or self.repo), *args], env=self.env,
                                       encoding="utf-8", timeout=10).strip()

    def test_search_and_tag_management(self):
        (self.repo / "main.py").write_text("pass")
        self.cli("set", self.pid, "description", "订单 API")
        self.assertEqual(self.cli("tag", self.pid)["tags"], [])
        self.cli("tag", self.pid, "--add", "Release", "--add", "服务端")
        self.cli("tag", self.pid, "--add", "release")
        found = self.cli("find", "订单", "api", "--group", "研发", "--tag", "RELEASE",
                         "--tag", "服务端", "--kind", "python")
        self.assertEqual([p["id"] for p in found], [self.pid])
        self.assertEqual(found[0]["tags"], ["Release", "服务端"])
        self.assertEqual(self.cli("find", "--tag", "前端"), [])
        self.assertEqual(self.cli("tag", self.pid, "--remove", "release")["tags"], ["服务端"])
        self.cli("tag", self.pid, "--add", "A", "--remove", "a", expected=2)
        self.repo.rename(self.root / "新位置")
        missing = self.cli("find", "--missing")
        self.assertEqual([p["id"] for p in missing], [self.pid])
        self.assertFalse(missing[0]["exists"])

    def test_ambiguous_display_name_returns_candidate_ids(self):
        other = self.root / "other"
        other.mkdir()
        second = self.cli("add", str(other))["id"]
        for pid in (self.pid, second):
            self.cli("set", pid, "name", "同名")
        error = self.cli("push", "同名", expected=2)["error"]
        self.assertIn(self.pid, error)
        self.assertIn(second, error)

    def test_preview_publish_retry_and_rejection(self):
        remote = self.root / "远端.git"
        self.git("init", "--bare", str(remote))
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Hub Test")
        self.git("config", "user.email", "hub@example.invalid")
        (self.repo / "file.txt").write_text("base")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.git("remote", "add", "local", str(remote))
        self.git("switch", "-c", "agent/task")
        (self.repo / "file.txt").write_text("feature")
        self.git("commit", "-am", "feature")
        sha = self.git("rev-parse", "HEAD")
        self.git("tag", "-a", "unrelated", "-m", "tag")
        self.git("config", "push.followTags", "true")
        self.git("switch", "main")
        (self.repo / "notes.txt").write_text("staged locally")
        self.git("add", "notes.txt")
        index = self.git("write-tree")
        args = ("push", self.pid, "-b", "agent/task", "--to", "review/task")
        self.assertEqual(self.cli("info", self.pid)["remotes"], ["local"])
        preview = self.cli(*args, "--dry-run")
        self.assertEqual((preview["sha"], preview["remote"], preview["dryRun"]), (sha, "local", True))
        self.assertEqual(self.git("for-each-ref", repo=remote), "")
        self.assertFalse(self.cli(*args, "--preview-id", preview["previewId"])["upToDate"])
        self.assertTrue(self.cli(*args)["upToDate"])
        self.assertEqual(self.git("for-each-ref", "--format=%(refname)", repo=remote), "refs/heads/review/task")
        self.assertEqual(self.git("rev-parse", "refs/heads/review/task", repo=remote), sha)
        self.assertEqual(self.git("branch", "--show-current"), "main")
        self.assertEqual(self.git("write-tree"), index)
        self.git("commit", "-m", "divergent main")
        self.cli("push", self.pid, "--remote", "local", "--to", "review/task", expected=2)
        self.assertEqual(self.git("rev-parse", "refs/heads/review/task", repo=remote), sha)
        self.git("checkout", "--detach")
        self.assertIn("detached", self.cli("push", self.pid, expected=2)["error"])
        self.cli("push", self.pid, "-b", "missing", expected=2)
        self.cli("push", self.pid, "-b", "main", "--remote", "missing", expected=2)

    def test_agent_workflow_from_duplicate_name_to_recovered_push(self):
        other = self.root / "另一个项目"
        other.mkdir()
        self.cli("set", self.pid, "name", self.pid)
        other_id = self.cli("add", str(other), "--name", self.pid, "--group", "试验")["id"]
        for repo in (self.repo, other):
            self.git("init", "-qb", "main", repo=repo)
            self.git("config", "user.name", "Hub Test", repo=repo)
            self.git("config", "user.email", "hub@example.invalid", repo=repo)
            (repo / "费用.txt").write_text("base", encoding="utf-8")
            self.git("add", ".", repo=repo)
            self.git("commit", "-qm", "base", repo=repo)
        branch, target = "agent/billing", "review/billing"
        for args in (("tag", self.pid, "--add", "待发布"),
                     ("commit", self.pid, "-b", branch, "-m", "ambiguous", "费用.txt"),
                     ("push", self.pid, "-b", branch)):
            error = self.cli(*args, expected=2)
            self.assertEqual(error["code"], "ambiguous_project")
            self.assertEqual({p["path"] for p in error["candidates"]}, {str(self.repo), str(other)})
        matches = self.cli("find", self.pid)
        self.assertEqual(len(matches), 2)
        ref = next(p["ref"] for p in matches if p["groupName"] == "研发" and p["path"] == str(self.repo))
        self.cli("tag", ref, "--add", "待发布")
        self.assertEqual([p["ref"] for p in self.cli("find", "--tag", "待发布")], [ref])
        self.assertEqual(self.cli("tag", other_id)["tags"], [])
        base = self.git("rev-parse", "HEAD")
        other_head = self.git("rev-parse", "HEAD", repo=other)
        (self.repo / "notes.txt").write_text("already staged")
        self.git("add", "notes.txt")
        index = self.git("write-tree")
        (self.repo / "费用.txt").write_text("release 1", encoding="utf-8")
        commit = self.cli("commit", ref, "-b", branch, "-m", "调整费用", "费用.txt")
        self.assertEqual(self.git("-c", "core.quotepath=false", "show", "--format=", "--name-only", branch), "费用.txt")
        self.assertEqual(self.git("rev-parse", "HEAD"), base)

        release, staging = self.root / "发布.git", self.root / "测试.git"
        for name, path in (("release", release), ("staging", staging)):
            self.git("init", "--bare", "-b", "main", str(path))
            self.git("remote", "add", name, str(path))
        args = ("push", ref, "-b", branch, "--to", target)
        destinations = self.cli("info", ref)["remoteTargets"]
        self.assertEqual(destinations, {"release": [str(release)], "staging": [str(staging)]})
        for opts, code in [((), "remote_required"), (("--remote", "relase"), "unknown_remote")]:
            failure = self.cli(*args, *opts, expected=2)
            self.assertEqual(failure["code"], code)
            self.assertEqual(failure["availableRemotes"], destinations)
        wrong = self.cli(*args, "--remote", "staging", "--dry-run")
        self.assertEqual(wrong["pushUrls"], [str(staging)])
        publish = (*args, "--remote", "release")
        preview = self.cli(*publish, "--dry-run")
        self.assertEqual((preview["pushUrls"], preview["sha"]), ([str(release)], commit["sha"]))
        self.assertEqual(self.git("for-each-ref", repo=release), "")
        self.assertEqual(self.cli(*publish, "--preview-id", preview["previewId"])["sha"], self.git("rev-parse", target, repo=release))

        peer = self.root / "另一份工作区"
        self.git("clone", "-qb", target, str(release), str(peer))
        (peer / "peer.txt").write_text("remote change")
        self.git("add", "peer.txt", repo=peer)
        self.git("-c", "user.name=Peer", "-c", "user.email=peer@example.invalid",
                 "commit", "-qm", "remote change", repo=peer)
        self.git("push", "-q", "origin", target, repo=peer)
        peer_sha = self.git("rev-parse", "HEAD", repo=peer)
        self.git("config", "advice.pushNonFastForward", "false")
        (self.repo / "费用.txt").write_text("release 2")
        local = self.cli("commit", ref, "-b", branch, "-m", "调整费用第二版", "费用.txt")
        for opts in (("--dry-run",), ()):
            rejected = self.cli(*publish, *opts, expected=2)
            self.assertEqual(rejected["code"], "non_fast_forward")
            self.assertEqual((rejected["remote"], rejected["target"], rejected["sha"]),
                             ("release", target, local["sha"]))
            self.assertEqual(rejected["dryRun"], bool(opts))
            self.assertEqual(self.git("rev-parse", target, repo=release), peer_sha)

        integration = self.root / "整合区"
        self.git("worktree", "add", "-q", str(integration), branch)
        self.git("fetch", "-q", "release", "refs/heads/" + target, repo=integration)
        self.git("rebase", "FETCH_HEAD", repo=integration)
        retry = self.cli(*publish, "--dry-run")
        self.assertEqual(self.cli(*publish, "--preview-id", retry["previewId"])["sha"], retry["sha"])
        self.assertEqual(self.git("rev-parse", target, repo=release), retry["sha"])
        self.assertEqual(self.git("show", target + ":peer.txt", repo=release), "remote change")
        self.assertEqual(self.git("show", target + ":费用.txt", repo=release), "release 2")
        self.assertEqual(self.git("for-each-ref", repo=staging), "")
        self.assertEqual(self.git("branch", "--show-current"), "main")
        self.assertEqual(self.git("rev-parse", "HEAD"), base)
        self.assertEqual(self.git("write-tree"), index)
        self.assertEqual(self.git("rev-parse", "HEAD", repo=other), other_head)
        self.assertEqual(self.git("branch", "--list", branch, repo=other), "")

    def test_formal_push_detects_changes_since_preview(self):
        self.git("init", "-qb", "main")
        self.git("config", "user.name", "Tester")
        self.git("config", "user.email", "test@example.invalid")
        self.git("commit", "--allow-empty", "-qm", "base")
        base = self.git("rev-parse", "HEAD")
        original, replacement = self.root / "original.git", self.root / "replacement.git"
        for path in (original, replacement):
            self.git("init", "--bare", "-qb", "main", str(path))
        self.git("remote", "add", "origin", str(original))
        for scenario in ("local_sha", "push_urls", "remote_tip"):
            with self.subTest(scenario=scenario):
                self.git("remote", "set-url", "--push", "origin", str(original))
                args = ("push", self.pid, "-b", "main", "--remote", "origin", "--to", scenario)
                previous = self.cli(*args, "--dry-run")
                guarded = (*args, "--preview-id", previous["previewId"])
                if scenario == "local_sha":
                    self.git("commit", "--allow-empty", "-qm", "after preview")
                elif scenario == "push_urls":
                    self.git("remote", "set-url", "--push", "origin", str(replacement))
                else:
                    # Another writer creates the target at an ancestor; ordinary Git permits the push.
                    self.git("push", str(original), base + ":refs/heads/" + scenario)
                error = self.cli(*guarded, expected=2)
                self.assertEqual(error["code"], "preview_stale")
                self.assertIn(scenario, error["changes"])
                self.assertIn("重新预演", error["error"])
                tip = self.git("for-each-ref", "--format=%(objectname)", "refs/heads/" + scenario, repo=original)
                self.assertEqual(tip, base if scenario == "remote_tip" else "")
                self.assertEqual(self.git("for-each-ref", "refs/heads/" + scenario, repo=replacement), "")
                self.assertEqual(self.cli(*guarded, expected=2)["code"], "preview_stale")
                fresh = self.cli(*args, "--dry-run")
                self.assertEqual(self.cli(*args, "--preview-id", fresh["previewId"])["sha"], self.git("rev-parse", "HEAD"))
                destination = replacement if scenario == "push_urls" else original
                self.assertEqual(self.git("rev-parse", "refs/heads/" + scenario, repo=destination), fresh["sha"])

    def test_preview_pins_each_push_url_and_preserves_hooks(self):
        self.git("init", "-qb", "main")
        self.git("config", "user.name", "Tester")
        self.git("config", "user.email", "test@example.invalid")
        self.git("commit", "--allow-empty", "-qm", "base")
        base = self.git("rev-parse", "HEAD")
        self.git("commit", "--allow-empty", "-qm", "next")
        destinations = [self.root / name for name in ("one.git", "two.git")]
        self.git("remote", "add", "origin", str(self.root / "fetch-only"))
        for destination in destinations:
            self.git("init", "--bare", "-qb", "main", str(destination))
            self.git("remote", "set-url", "--add", "--push", "origin", str(destination))
            self.git("push", "-q", str(destination), base + ":refs/heads/main")
        args = ("push", self.pid, "--remote", "origin")
        old = self.cli(*args, "--dry-run")
        latest = self.cli(*args, "--dry-run")
        self.assertEqual(latest["remoteTips"], [base, base])
        self.assertEqual(self.cli(*args, "--preview-id", old["previewId"], expected=2)["changes"], ["preview_id"])
        self.git("push", "-q", str(destinations[1]), "main")
        self.assertEqual(self.cli(*args, "--preview-id", latest["previewId"], expected=2)["changes"], ["remote_tip"])
        self.assertEqual(self.git("rev-parse", "main", repo=destinations[0]), base)
        fresh = self.cli(*args, "--dry-run")
        self.assertEqual(fresh["remoteTips"], [base, fresh["sha"]])
        hooks = self.repo / "custom hooks"
        hooks.mkdir()
        self.git("config", "core.hooksPath", str(hooks))
        hook = hooks / "pre-push"
        hook.write_bytes(b'#!/bin/sh\nread src new dst old || exit 0\necho "user hook blocked $dst" >&2\nexit 1\n')
        hook.chmod(0o700)
        self.assertIn("user hook blocked refs/heads/main", self.cli(*args, "--preview-id", fresh["previewId"], expected=2)["error"])
        self.assertEqual(self.git("rev-parse", "main", repo=destinations[0]), base)
        hook.unlink()
        self.assertEqual(self.cli(*args, "--preview-id", fresh["previewId"])["sha"], fresh["sha"])
        self.assertEqual(self.git("rev-parse", "main", repo=destinations[0]), fresh["sha"])

    def test_overlapping_agents_cannot_share_or_replay_previews(self):
        self.git("init", "-qb", "main")
        self.git("-c", "user.name=Tester", "-c", "user.email=t@example.invalid",
                 "commit", "--allow-empty", "-qm", "base")
        remote = self.root / "remote.git"
        self.git("init", "--bare", "-qb", "main", str(remote))
        self.git("remote", "add", "origin", str(remote))
        hook = self.repo / ".git" / "hooks" / "pre-push"
        hook.write_bytes(b'#!/bin/sh\necho entered >> calls\nif test -n "$HUB_TEST_GATE"; then\n'
                         b'  touch "$HUB_TEST_GATE.ready"\n'
                         b'  while ! test -f "$HUB_TEST_GATE.go"; do sleep 0.05; done\nfi\n')
        hook.chmod(0o700)

        def wait_for_hook(gate):
            deadline = time.monotonic() + 8
            while not (self.repo / (gate + ".ready")).exists():
                if time.monotonic() >= deadline:
                    self.fail("Git did not reach test hook")
                time.sleep(0.02)

        first = ("push", self.pid, "--to", "first")
        second = ("push", self.pid, "--to", "second")
        with ThreadPoolExecutor(max_workers=3) as agents:
            # Both previews overlap. The one that runs second supersedes the first.
            preview_a = agents.submit(self.cli, *first, "--dry-run", env={**self.env, "HUB_TEST_GATE": "a"})
            try:
                wait_for_hook("a")
                preview_b = agents.submit(self.cli, *second, "--dry-run")
                self.assertEqual(self.cli(*first, expected=2)["code"], "push_busy")
            finally:
                (self.repo / "a.go").touch()
            a, b = preview_a.result(), preview_b.result()
            old = (*first, "--preview-id", a["previewId"])
            current = (*second, "--preview-id", b["previewId"])
            self.assertNotEqual(a["previewId"], b["previewId"])
            self.assertEqual(self.cli(*old, expected=2)["reason"], "superseded")
            self.assertEqual(self.cli(*second, expected=2)["code"], "preview_id_required")
            self.assertEqual(self.cli(*second, "--preview-id", "", expected=2)["reason"], "unknown")
            self.assertEqual(self.git("for-each-ref", repo=remote), "")

            # Hold the formal push after Git has selected its update, then send a duplicate
            # and request a new preview before letting the first write finish.
            push = agents.submit(self.cli, *current, env={**self.env, "HUB_TEST_GATE": "b"})
            try:
                wait_for_hook("b")
                replay = agents.submit(self.cli, *current, expected=2)
                new = agents.submit(self.cli, *second, "--dry-run")
                self.assertEqual(self.cli(*second, expected=2)["code"], "push_busy")
            finally:
                (self.repo / "b.go").touch()
            self.assertEqual(push.result()["previewId"], b["previewId"])
            duplicate, pending = replay.result(), new.result()
        self.assertEqual(duplicate["code"], "preview_already_pushed")
        self.assertEqual((duplicate["previewId"], duplicate["sha"], duplicate["target"]),
                         (b["previewId"], b["sha"], "second"))
        self.assertEqual(pending["remoteTips"], [b["sha"]])
        self.assertEqual(self.cli(*old, expected=2)["reason"], "superseded")
        self.assertEqual(self.cli(*current, expected=2)["code"], "preview_already_pushed")
        self.assertEqual((self.repo / "calls").read_text().splitlines(), ["entered"] * 4)
        self.assertEqual(self.git("for-each-ref", "--format=%(refname) %(objectname)", repo=remote),
                         "refs/heads/second " + b["sha"])
        self.assertTrue(self.cli(*second, "--preview-id", pending["previewId"])["upToDate"])

    def test_agent_exit_after_remote_update_recovers_without_another_push(self):
        self.git("init", "-qb", "main")
        self.git("-c", "user.name=Tester", "-c", "user.email=t@example.invalid",
                 "commit", "--allow-empty", "-qm", "base")
        remote = self.root / "remote.git"
        self.git("init", "--bare", "-qb", "main", str(remote))
        self.git("remote", "add", "origin", str(remote))
        preview = self.cli("push", self.pid, "--dry-run")
        args = ("push", self.pid, "--preview-id", preview["previewId"])
        hook = remote / "hooks" / "post-receive"
        hook.write_bytes(b"#!/bin/sh\necho updated >> writes\nwhile ! test -f resume; do sleep 0.05; done\n")
        hook.chmod(0o700)
        proc = subprocess.Popen([sys.executable, str(core.ROOT / "hub.py"), "--data", str(self.data),
                                 *args, "--json"], env={**self.env, "TMPDIR": str(self.root)},
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 8
            while not (remote / "writes").exists():
                if time.monotonic() >= deadline or proc.poll() is not None:
                    self.fail("Remote did not reach post-receive")
                time.sleep(.05)
            self.assertEqual(self.git("rev-parse", "main", repo=remote), preview["sha"])
            proc.kill()
            proc.wait(timeout=5)
            self.assertEqual(self.cli("push", self.pid, expected=2)["code"], "push_busy")
        finally:
            (remote / "resume").touch()
            proc.wait(timeout=8)
        self.git("-c", "user.name=Tester", "-c", "user.email=t@example.invalid",
                 "commit", "--allow-empty", "-qm", "local work after the lost acknowledgement")
        local_hook = self.repo / ".git" / "hooks" / "pre-push"
        local_hook.write_bytes(b"#!/bin/sh\necho unexpected >> extra-push\nexit 1\n")
        local_hook.chmod(0o700)
        recovered = self.cli(*args)
        self.assertTrue(recovered["recovered"])
        self.assertEqual((recovered["previewId"], recovered["sha"]), (preview["previewId"], preview["sha"]))
        self.assertEqual(self.cli(*args, expected=2)["code"], "preview_already_pushed")
        self.assertFalse((self.repo / "extra-push").exists())
        self.assertEqual(self.git("rev-parse", "main", repo=remote), preview["sha"])
        self.assertEqual((remote / "writes").read_text().splitlines(), ["updated"])

    @contextmanager
    def git_http_server(self):
        cgi_env = {**self.env, "GIT_PROJECT_ROOT": str(self.root), "GIT_HTTP_EXPORT_ALL": "1"}

        class GitHTTP(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def serve_git(self):
                url = urlsplit(self.path)
                repo = url.path.split("/")[1]
                mode = self.server.modes.get(repo, "online")
                self.server.requests.append((repo, self.command, url.path, url.query))
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if mode == "offline":
                    self.send_error(503)
                    return
                writing = url.path.endswith("/git-receive-pack")
                if writing and mode == "before":
                    return  # Close the connection before giving the pack to Git.
                env = {**cgi_env, "REQUEST_METHOD": self.command, "PATH_INFO": url.path,
                       "QUERY_STRING": url.query, "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                       "CONTENT_LENGTH": str(len(body)), "REMOTE_ADDR": "127.0.0.1"}
                response = subprocess.check_output(["git", "http-backend"], env=env, input=body, timeout=8)
                if writing and mode in ("after", "after-offline"):
                    if mode == "after-offline":
                        self.server.modes[repo] = "offline"
                    return  # Git committed the ref. Drop its status report.
                headers, payload = response.split(b"\r\n\r\n", 1)
                self.send_response(200)
                for line in headers.decode("latin1").splitlines():
                    name, value = line.split(":", 1)
                    self.send_header(name, value.strip())
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = serve_git

        with ThreadingHTTPServer(("127.0.0.1", 0), GitHTTP) as server:
            server.modes = {}
            server.requests = []
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield server
            finally:
                server.shutdown()
                thread.join()

    def test_http_disconnect_before_and_after_receive(self):
        self.git("init", "-qb", "main")
        self.git("-c", "user.name=Tester", "-c", "user.email=t@example.invalid",
                 "commit", "--allow-empty", "-qm", "base")
        remote = self.root / "http.git"
        self.git("init", "--bare", "-qb", "main", str(remote))
        self.git("config", "http.receivepack", "true", repo=remote)
        hook = remote / "hooks" / "post-receive"
        hook.write_bytes(b"#!/bin/sh\ncat >> received\n")
        hook.chmod(0o700)
        with self.git_http_server() as server:
            self.git("remote", "add", "origin", f"http://127.0.0.1:{server.server_port}/http.git")
            for mode in ("before", "after"):
                with self.subTest(mode=mode):
                    args = ("push", self.pid, "--to", mode)
                    preview = self.cli(*args, "--dry-run")
                    retry = (*args, "--preview-id", preview["previewId"])
                    server.modes[remote.name] = mode
                    error = self.cli(*retry, expected=2)
                    self.assertEqual(error["code"], "push_outcome_unknown", error)
                    server.modes[remote.name] = "offline"
                    self.assertEqual(self.cli(*retry, expected=2)["code"], "push_outcome_unknown")
                    server.modes[remote.name] = "online"
                    server.requests.clear()
                    result = self.cli(*retry)
                    writes = [path for _, _, path, _ in server.requests if path.endswith("/git-receive-pack")]
                    self.assertEqual(len(writes), 1 if mode == "before" else 0)
                    self.assertEqual(bool(result.get("recovered")), mode == "after")
                    self.assertEqual(self.git("rev-parse", mode, repo=remote), preview["sha"])
                    self.assertEqual(self.cli(*retry, expected=2)["code"], "preview_already_pushed")
        self.assertEqual([row.split()[-1] for row in (remote / "received").read_text().splitlines()],
                         ["refs/heads/before", "refs/heads/after"])

    def test_multi_address_repair_after_lost_acknowledgement(self):
        self.git("init", "-qb", "main")
        self.git("-c", "user.name=Tester", "-c", "user.email=t@example.invalid",
                 "commit", "--allow-empty", "-qm", "release")
        sha = self.git("rev-parse", "HEAD")
        mirrors = [self.root / (name + ".git") for name in ("A", "B", "C")]
        previous = self.root / "previous.git"
        self.git("init", "--bare", "-q", str(previous))
        self.git("remote", "add", "origin", str(previous))
        self.git("remote", "set-url", "--add", "--push", "origin", str(previous))
        with self.git_http_server() as server:
            for mirror in mirrors:
                self.git("init", "--bare", "-q", str(mirror))
                self.git("config", "http.receivepack", "true", repo=mirror)
                hook = mirror / "hooks" / "post-receive"
                hook.write_bytes(b"#!/bin/sh\ncat >> received\n")
                hook.chmod(0o700)
                url = f"http://127.0.0.1:{server.server_port}/{mirror.name}"
                self.git("remote", "set-url", "--add", "--push", "origin", url)

            def push_requests():
                return [(name, method) for name, method, path, query in server.requests
                        if path.endswith("/git-receive-pack") or query == "service=git-receive-pack"]

            for mode in ("after-offline", "after"):
                with self.subTest(mode=mode):
                    ref = "refs/heads/" + mode
                    self.git("push", "-q", str(previous), sha + ":" + ref)
                    args = ("push", self.pid, "--to", mode)
                    preview = self.cli(*args, "--dry-run")
                    self.assertEqual(preview["remoteTips"], [sha, None, None, None])
                    old = (*args, "--preview-id", preview["previewId"])
                    server.modes["B.git"] = mode
                    server.requests.clear()
                    lost = self.cli(*old, expected=2)
                    self.assertEqual([name for name, method in push_requests() if method == "POST"], ["A.git", "B.git"])
                    self.assertEqual(self.git("rev-parse", ref, repo=mirrors[1]), sha)
                    self.assertEqual(self.git("for-each-ref", ref, repo=mirrors[2]), "")
                    server.requests.clear()
                    if mode == "after-offline":
                        offline = self.cli(*old, expected=2)
                        self.assertEqual(offline["code"], "push_outcome_unknown")
                        self.assertEqual(offline["destinations"][2]["status"], "unknown")
                        self.assertNotIn("remoteTip", offline["destinations"][2])
                    server.modes["B.git"] = "online"
                    checked = self.cli(*old, expected=2)
                    self.assertEqual(checked["code"], "push_partially_applied")
                    self.assertIn(sha[:12], checked["error"])
                    self.assertIn("新编号", checked["hint"])
                    self.assertEqual(checked["remoteTips"], [sha, sha, sha, None])
                    self.assertEqual([r["status"] for r in checked["destinations"]],
                                     ["at_target", "at_target", "at_target", "pending"])
                    self.assertEqual(push_requests(), [])
                    fresh = self.cli(*args, "--dry-run")
                    self.assertNotEqual(fresh["previewId"], preview["previewId"])
                    self.assertEqual(fresh["remoteTips"], checked["remoteTips"])
                    self.assertEqual(self.cli(*old, expected=2)["reason"], "superseded")
                    if mode == "after":
                        # If the last outstanding address also loses its reply, that is full delivery.
                        server.modes["C.git"] = "after"
                    done = self.cli(*args, "--preview-id", fresh["previewId"])
                    final_status = "at_target" if mode == "after" else "pushed"
                    self.assertEqual([r["status"] for r in done["destinations"]],
                                     ["at_target", "at_target", "at_target", final_status])
                    self.assertFalse(done["upToDate"])
                    self.assertEqual(bool(done["destinations"][3].get("verifiedAfterDisconnect")), mode == "after")
                    self.assertEqual(self.cli(*args, "--preview-id", fresh["previewId"], expected=2)["code"],
                                     "preview_already_pushed")
                    self.assertEqual({name for name, _ in push_requests()}, {"C.git"})
                    self.assertEqual([name for name, method in push_requests() if method == "POST"], ["C.git"])
                    for mirror in mirrors:
                        records = (mirror / "received").read_text().splitlines()
                        self.assertEqual([row.split()[1] for row in records if row.split()[2] == ref], [sha])
                    if mode == "after-offline":
                        self.assertEqual(lost["code"], "push_outcome_unknown")
                        self.assertEqual([r["status"] for r in lost["destinations"]],
                                         ["at_target", "pushed", "unknown", "not_attempted"])
                    else:
                        self.assertEqual(lost["destinations"][2]["remoteTip"], sha)
                        self.assertTrue(lost["destinations"][2]["verifiedAfterDisconnect"])
                        self.assertIn("已只读核实目标提交", lost["error"])
                        self.assertEqual(lost["code"], "push_partially_applied")
                        self.assertEqual([r["status"] for r in lost["destinations"]],
                                         ["at_target", "pushed", "at_target", "not_attempted"])

    def test_original_id_reports_partial_delivery_without_writing(self):
        self.git("init", "-qb", "main")
        self.git("-c", "user.name=Tester", "-c", "user.email=t@example.invalid",
                 "commit", "--allow-empty", "-qm", "base")
        first, second, third = (self.root / name for name in ("one.git", "two.git", "three.git"))
        addresses = [first, second, third]
        self.git("remote", "add", "origin", str(self.root / "fetch-only"))
        receiver = self.root / "audit receive"
        receiver.write_bytes(b'#!/bin/sh\necho open >> "$1/receive-opens"\nexec git-receive-pack "$@"\n')
        self.git("config", "remote.origin.receivepack", 'sh "' + receiver.as_posix() + '"')
        for destination in addresses:
            self.git("init", "--bare", "-qb", "main", str(destination))
            self.git("remote", "set-url", "--add", "--push", "origin", str(destination))
            hook = destination / "hooks" / "post-receive"
            hook.write_bytes(b"#!/bin/sh\ncat >> writes\n")
            hook.chmod(0o700)
        hook = second / "hooks" / "pre-receive"
        hook.write_bytes(b"#!/bin/sh\necho 'release paused' >&2\nexit 1\n")
        hook.chmod(0o700)

        def counts():
            return [len((path / "receive-opens").read_text().splitlines()) for path in addresses]

        args = ("push", self.pid)
        preview = self.cli(*args, "--dry-run")
        guarded = (*args, "--preview-id", preview["previewId"])
        first_result = self.cli(*guarded, expected=2)
        self.assertEqual(first_result["code"], "push_partially_applied")
        rows = first_result["destinations"]
        self.assertEqual([r["url"] for r in rows], list(map(str, addresses)))
        self.assertEqual([r["status"] for r in rows], ["pushed", "rejected", "pushed"])
        self.assertIn("release paused", rows[1]["error"])
        hook.unlink()
        before = counts()
        partial = self.cli(*guarded, expected=2)
        sha = preview["sha"]
        self.assertEqual(partial["code"], "push_partially_applied")
        self.assertEqual(partial["remoteTips"], [sha, None, sha])
        self.assertEqual(counts(), before)
        self.assertEqual(self.git("for-each-ref", repo=second), "")
        offline = self.root / "offline.git"
        second.rename(offline)
        try:
            unknown = self.cli(*guarded, expected=2)
            self.assertEqual(unknown["code"], "push_outcome_unknown")
            self.assertEqual([r["status"] for r in unknown["destinations"]], ["at_target", "unknown", "at_target"])
            self.assertNotIn("remoteTip", unknown["destinations"][1])
        finally:
            offline.rename(second)
        fresh = self.cli(*args, "--dry-run")
        self.assertEqual([r["status"] for r in fresh["destinations"]], ["at_target", "pending", "at_target"])
        self.assertEqual(fresh["remoteTips"], [sha, None, sha])
        after_preview = counts()
        self.assertEqual(after_preview, [before[0], before[1] + 1, before[2]])
        result = self.cli(*args, "--preview-id", fresh["previewId"])
        self.assertEqual([r["status"] for r in result["destinations"]], ["at_target", "pushed", "at_target"])
        self.assertEqual(counts(), [before[0], after_preview[1] + 2, before[2]])
        self.assertEqual(self.git("remote", "get-url", "--push", "--all", "origin").splitlines(), list(map(str, addresses)))
        for path in addresses:
            self.assertEqual(self.git("rev-parse", "main", repo=path), sha)
            self.assertEqual(len((path / "writes").read_text().splitlines()), 1)
        before = counts()
        done = self.cli(*args, "--dry-run")
        self.assertTrue(self.cli(*args, "--preview-id", done["previewId"])["upToDate"])
        self.assertEqual(counts(), before)

        # The same physical URL listed twice still gets only one formal transfer.
        self.git("remote", "set-url", "--add", "--push", "origin", str(second))
        duplicate = (*args, "--to", "repeat-url")
        rerun = self.cli(*duplicate, "--dry-run")
        delivered = self.cli(*duplicate, "--preview-id", rerun["previewId"])
        self.assertEqual([r["status"] for r in delivered["destinations"]],
                         ["pushed", "pushed", "pushed", "at_target"])
        self.assertEqual([n - old for n, old in zip(counts(), before)], [3, 3, 3])


    def test_changed_destination_stops_partial_push_repair(self):
        self.git("init", "-qb", "main")
        self.git("config", "user.name", "Tester")
        self.git("config", "user.email", "t@example.invalid")
        self.git("commit", "--allow-empty", "-qm", "base")
        base = self.git("rev-parse", "HEAD")
        self.git("commit", "--allow-empty", "-qm", "peer")
        peer = self.git("rev-parse", "HEAD")
        self.git("commit", "--allow-empty", "-qm", "release")
        sha = self.git("rev-parse", "HEAD")
        addresses = [self.root / (n + ".git") for n in ("A", "B", "C")]
        first, second, third = addresses
        ref = "refs/heads/release"
        args = ("push", self.pid, "--to", "release")
        self.git("remote", "add", "origin", "fetch-only")
        for dest in addresses:
            self.git("init", "--bare", "-qb", "main", str(dest))
            self.git("push", "-q", str(dest), base + ":" + ref)
            self.git("remote", "set-url", "--add", "--push", "origin", str(dest))
            if dest != first:
                hook = dest / "hooks" / "pre-receive"
                hook.write_bytes(b"#!/bin/sh\nexit 1\n")
                hook.chmod(0o700)
        initial = self.cli(*args, "--dry-run")
        self.assertEqual(self.cli(*args, "--preview-id", initial["previewId"], expected=2)["code"],
                         "push_partially_applied")
        for dest in (second, third):
            (dest / "hooks" / "pre-receive").unlink()

        hook = self.repo / ".git" / "hooks" / "pre-push"
        hook.write_bytes(b"#!/bin/sh\necho contact >> push-calls\n")
        hook.chmod(0o700)
        calls = self.repo / "push-calls"
        # Detect both directions of drift, including an independent push of the exact desired SHA.
        for dest, old, new in ((first, sha, base), (second, base, peer), (second, base, sha)):
            with self.subTest(address=dest.name, new=new):
                preview = self.cli(*args, "--dry-run")
                self.assertEqual(preview["remoteTips"], [sha, base, base])
                if dest == first:
                    self.git("update-ref", ref, new, old, repo=dest)
                else:
                    self.git("push", "-q", str(dest), new + ":" + ref)
                count_before = calls.read_bytes()
                result = self.cli(*args, "--preview-id", preview["previewId"], expected=2)
                self.assertEqual(calls.read_bytes(), count_before)
                self.assertEqual(result["code"], "preview_stale")
                rows = result["destinations"]
                self.assertEqual([r["url"] for r in rows if r["status"] == "changed"], [str(dest)])
                self.assertEqual([self.git("rev-parse", ref, repo=p) for p in addresses],
                                 [new if p == dest else (sha if p == first else base) for p in addresses])
                self.git("update-ref", ref, old, new, repo=dest)
                row = next(r for r in rows if r["status"] == "changed")
                self.assertEqual((row.get("previewTip"), row["remoteTip"]), (old, new))
                self.assertIn(f"{str(dest)}  预演状态已变化  预演时 {old[:12]}，当前 {new[:12]}", result["error"])
                self.assertIn("重新预演", result["error"])
                self.assertIn("not_attempted", [r["status"] for r in rows])

        # Roll back A while Hub is testing B, after the first all-address check.
        repair = self.cli(*args, "--dry-run")
        hook.write_bytes(b'#!/bin/sh\ngit --git-dir="$HUB_TEST_DEST" update-ref "$HUB_TEST_REF" "$HUB_TEST_BASE"\n')
        hook.chmod(0o700)
        result = self.cli(*args, "--preview-id", repair["previewId"], expected=2,
                          env={**self.env, "HUB_TEST_DEST": str(first), "HUB_TEST_REF": ref, "HUB_TEST_BASE": base})
        self.assertEqual(result["code"], "preview_stale")
        self.assertEqual([r["status"] for r in result["destinations"]], ["changed", "not_attempted", "not_attempted"])
        self.assertEqual([self.git("rev-parse", ref, repo=p) for p in addresses], [base] * 3)
        hook.unlink()

        # A peer wins B's ref transaction after Git has advertised the old tip on the real push.
        self.git("update-ref", ref, sha, base, repo=first)
        repair = self.cli(*args, "--dry-run")
        hook.write_bytes(b'#!/bin/sh\necho check >> repair-transfers\n'
                         b'if test "$(wc -l < repair-transfers)" -eq 3; then\n'
                         b'  git --git-dir="$HUB_TEST_DEST" update-ref "$HUB_TEST_REF" "$HUB_TEST_PEER"\nfi\n')
        hook.chmod(0o700)
        raced = self.cli(*args, "--preview-id", repair["previewId"], expected=2,
                         env={**self.env, "HUB_TEST_DEST": str(second), "HUB_TEST_REF": ref, "HUB_TEST_PEER": peer})
        self.assertEqual(raced["code"], "preview_stale")
        self.assertEqual([r["status"] for r in raced["destinations"]], ["at_target", "changed", "not_attempted"])
        self.assertEqual(raced["destinations"][1]["previewTip"], base)
        self.assertEqual(raced["destinations"][1]["remoteTip"], peer)
        self.assertEqual([self.git("rev-parse", ref, repo=p) for p in addresses], [sha, peer, base])
        hook.unlink()
        fresh = self.cli(*args, "--dry-run")
        self.cli(*args, "--preview-id", fresh["previewId"])
        self.assertEqual([self.git("rev-parse", ref, repo=p) for p in addresses], [sha] * 3)

    def test_race_after_first_repair_write_stops_and_skips_it_on_retry(self):
        self._check_repair_race(disconnect=False)

    def test_race_with_interrupted_receive_stops_remaining_repair(self):
        self._check_repair_race(disconnect=True)

    def _check_repair_race(self, *, disconnect):
        self.git("init", "-qb", "main")
        self.git("config", "user.name", "Tester")
        self.git("config", "user.email", "t@example.invalid")
        self.git("commit", "--allow-empty", "-qm", "base")
        base = self.git("rev-parse", "HEAD")
        self.git("commit", "--allow-empty", "-qm", "peer change")
        peer_sha = self.git("rev-parse", "HEAD")
        self.git("commit", "--allow-empty", "-qm", "release")
        target = self.git("rev-parse", "HEAD")
        peer = self.root / "peer checkout"
        self.git("clone", "-q", str(self.repo), str(peer))
        addresses = [self.root / (name + ".git") for name in ("A", "B", "C", "D")]
        a, b, c, d = addresses
        self.git("remote", "add", "origin", "fetch-only")
        receiver = self.root / "audit receive"
        receiver.write_bytes(b'#!/bin/sh\necho opened >> "$1/opens"\n'
                             b'if test -f "$1/no-receive"; then exit 1; fi\nexec git-receive-pack "$@"\n')
        self.git("config", "remote.origin.receivepack", 'sh "' + receiver.as_posix() + '"')
        for dest in addresses:
            self.git("init", "--bare", "-qb", "main", str(dest))
            self.git("push", "-q", str(dest), base + ":refs/heads/main")
            self.git("remote", "set-url", "--add", "--push", "origin", str(dest))
            hook = dest / "hooks" / "post-receive"
            hook.write_bytes(b"#!/bin/sh\ncat >> writes\n")
            hook.chmod(0o700)
            if dest != d:
                reject = dest / "hooks" / "pre-receive"
                reject.write_bytes(b"#!/bin/sh\nexit 1\n")
                reject.chmod(0o700)
        args = ("push", self.pid)
        initial = self.cli(*args, "--dry-run")
        partial = self.cli(*args, "--preview-id", initial["previewId"], expected=2)
        self.assertEqual([r["status"] for r in partial["destinations"]], ["rejected"] * 3 + ["pushed"])
        for dest in (a, b, c):
            (dest / "hooks" / "pre-receive").unlink()
        repair = self.cli(*args, "--dry-run")
        gate = a if disconnect else b
        pause = b'touch ready\nwhile ! test -f proceed; do sleep 0.05; done\n'
        if disconnect:
            hook = a / "hooks" / "post-receive"
            hook.write_bytes(b"#!/bin/sh\ncat >> writes\n" + pause)
        else:
            hook = b / "hooks" / "pre-receive"
            # This server refuses stale updates without sending Git's lock diagnostic.
            hook.write_bytes(b'#!/bin/sh\nread old new ref\nif test -n "$HUB_TEST_RACE"; then\n' + pause +
                             b'test "$(git rev-parse "$ref")" = "$old" || { echo "update declined" >&2; exit 1; }\nfi\n')
        hook.chmod(0o700)
        with ThreadPoolExecutor(max_workers=1) as pool:
            push = pool.submit(self.cli, *args, "--preview-id", repair["previewId"],
                               env={**self.env, "HUB_TEST_RACE": "1"}, expected=2)
            try:
                deadline = time.monotonic() + 12
                while not (gate / "ready").exists():
                    if push.done() or time.monotonic() >= deadline:
                        self.fail(f"{gate} did not reach its receive hook")
                    time.sleep(0.05)
                self.assertEqual(self.git("rev-parse", "main", repo=a), target)
                c_opens = (c / "opens").read_bytes()
                self.git("push", "-q", str(b), peer_sha + ":refs/heads/main", repo=peer)
                if disconnect:
                    (b / "no-receive").touch()
            finally:
                (gate / "proceed").touch()
            stopped = push.result()
        self.assertEqual([self.git("rev-parse", "main", repo=dest) for dest in addresses],
                         [target, peer_sha, base, target])
        self.assertEqual((c / "opens").read_bytes(), c_opens)
        self.assertEqual(stopped["code"], "preview_stale")
        rows = stopped["destinations"]
        self.assertEqual([r["url"] for r in rows], list(map(str, addresses)))
        self.assertEqual([r["status"] for r in rows], ["pushed", "changed", "not_attempted", "at_target"])
        self.assertEqual((rows[1]["previewTip"], rows[1]["remoteTip"]), (base, peer_sha))
        self.assertIn("重新预演", stopped["error"])
        hook.unlink()
        if disconnect:
            (b / "no-receive").unlink()

        a_opens = (a / "opens").read_bytes()
        recheck = self.cli(*args, "--dry-run")
        self.assertEqual([r["status"] for r in recheck["destinations"]], ["at_target", "pending", "pending", "at_target"])
        done = self.cli(*args, "--preview-id", recheck["previewId"])
        self.assertEqual([r["status"] for r in done["destinations"]], ["at_target", "pushed", "pushed", "at_target"])
        self.assertEqual((a / "opens").read_bytes(), a_opens)
        self.assertEqual(len((a / "writes").read_text().splitlines()), 1)
        self.assertEqual([self.git("rev-parse", "main", repo=dest) for dest in addresses], [target] * 4)

    def test_remote_update_after_advertisement_requires_new_preview(self):
        self.git("init", "-qb", "main")
        self.git("config", "user.name", "Tester")
        self.git("config", "user.email", "test@example.invalid")
        destination = self.root / "origin.git"
        self.git("init", "--bare", "-qb", "main", str(destination))
        self.git("remote", "add", "origin", str(destination))
        self.git("commit", "--allow-empty", "-qm", "base")
        self.git("push", "-q", "origin", "main")
        self.git("commit", "--allow-empty", "-qm", "peer")
        peer = self.git("rev-parse", "HEAD")
        self.git("push", "-q", "origin", "main:peer")
        self.git("commit", "--allow-empty", "-qm", "publish")
        args = ("push", self.pid)
        preview = self.cli(*args, "--dry-run")
        hook = self.repo / ".git" / "hooks" / "pre-push"
        hook.write_bytes(b'#!/bin/sh\ngit --git-dir="$2" update-ref refs/heads/main refs/heads/peer\n')
        hook.chmod(0o700)
        rejected = self.cli(*args, "--preview-id", preview["previewId"], expected=2)
        self.assertEqual(rejected.get("changes"), ["remote_tip"], rejected)
        self.assertEqual(self.git("rev-parse", "main", repo=destination), peer)
        hook.unlink()
        fresh = self.cli(*args, "--dry-run")
        self.assertEqual(self.cli(*args, "--preview-id", fresh["previewId"])["sha"], fresh["sha"])

    def test_server_refusal_and_redacted_push_address(self):
        self.git("init", "-qb", "main")
        self.git("-c", "user.name=Hub", "-c", "user.email=hub@example.invalid",
                 "commit", "--allow-empty", "-qm", "first")
        dest = self.root / "checked-out"
        self.git("init", "-qb", "main", str(dest))
        self.git("config", "receive.denyCurrentBranch", "refuse", repo=dest)
        self.git("remote", "add", "origin", str(dest))
        denied = self.cli("push", self.pid, expected=2)
        self.assertEqual(denied["code"], "push_rejected")
        self.assertIn("branch is currently checked out", denied["error"])
        self.assertEqual(self.git("for-each-ref", repo=dest), "")
        self.git("remote", "set-url", "--push", "origin", "https://user:example@host.invalid/repo?ticket=example")
        self.assertEqual(self.cli("info", self.pid)["remoteTargets"], {"origin": ["https://host.invalid/repo"]})
