"""Project Hub HTTP server (127.0.0.1 only) — JSON API + static UI + project preview."""
from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import core, github, manage, search

STATIC = os.path.join(os.path.dirname(__file__), "static")
RUNS: dict[str, core.Run] = {}          # project id -> latest run
WATCHERS: dict[str, core.Watcher] = {}  # project id -> Watcher
_lock = threading.Lock()
PREVIEW_PORT = 0


def _project(pid: str) -> dict:
    return core.find_project(pid, prefer_id=True)


# ---------------------------------------------------------------- handlers

def api_get(path: str, q: dict) -> object:
    parts = [unquote(part) for part in path.strip("/").split("/")[1:]]
    one = lambda k, d=None: q.get(k, [d])[0]  # noqa: E731
    if parts == ["config"]:
        return {"previewPort": PREVIEW_PORT, "root": str(core.ROOT)}
    if parts == ["registry"]:
        reg = core.load_registry()
        reg.pop("pushPreviews", None)
        reg.pop("pushPreviewHistory", None)
        for p in reg["projects"]:
            p["exists"] = os.path.isdir(p["path"])
            p["watching"] = p["id"] in WATCHERS
            run = RUNS.get(p["id"])
            p["running"] = bool(run and run.running)
        return reg
    if parts == ["scan"]:
        return core.scan(one("root", os.path.expanduser("~")), int(one("depth", "2")))
    if parts == ["search"]:
        return search.search_projects(one("q", ""), one("group") or None, q.get("tag", []), one("kind") or None)[:50]
    if parts == ["grep"]:
        return search.grep(one("pattern", ""), q.get("project") or None, one("group") or None, q.get("tag", []),
                           one("regex") == "1", one("case") != "1", q.get("glob", []), int(one("max", "30")))
    if parts == ["overview"]:
        return manage.overview(one("group") or None)
    if parts == ["github"]:
        return github.status(verify=one("verify", "1") == "1")
    if parts == ["mcp-config"]:
        from . import mcp_server
        return mcp_server.config_snippets(sys.executable.replace("pythonw.exe", "python.exe"),
                                          str(core.ROOT / "hub.py"))
    if len(parts) >= 2 and parts[0] == "projects":
        proj = _project(parts[1])
        root = proj["path"]
        sub = parts[2] if len(parts) > 2 else ""
        if sub == "":
            return {**proj, "detect": core.detect(root), "gitInfo": core.git_info(root)}
        if sub == "files":
            return core.list_files(root, one("ref") or None)
        if sub == "file":
            return core.read_file(root, one("path"), one("ref") or None)
        if sub == "log":
            return core.git_log(root, one("ref", "HEAD"), int(one("n", "80")), one("path"))
        if sub == "commit":
            return core.git_show_commit(root, one("sha"))
        if sub == "diff":
            return {"diff": core.git_diff(root, one("path"), one("staged") == "1")}
        if sub == "problems":
            w = WATCHERS.get(proj["id"])
            return w.snapshot() if w else {"state": "off", "problems": [], "version": 0}
        if sub == "run":
            run = RUNS.get(proj["id"])
            if not run:
                return {"running": False, "lines": [], "next": 0, "problems": [], "id": None}
            cursor = int(one("from", "0")) if one("id", run.id) == run.id else 0
            return run.output(cursor)
    raise core.HubError(f"未知接口：{path}")


def api_post(path: str, body: dict) -> object:
    parts = [unquote(part) for part in path.strip("/").split("/")[1:]]
    if parts == ["projects"]:
        return core.add_project(body["path"], body.get("group", "default"), body.get("name"))
    if parts == ["scan"]:
        _, added = core.register_projects(body["paths"], body.get("group", "default"),
                                          group_id=body.get("groupId"))
        return {"added": added}
    if parts == ["groups"]:
        return {"id": core.set_group([], body["name"], body.get("color"))["group"]}
    if len(parts) == 2 and parts[0] == "groups":
        core.edit_group(parts[1], body.get("name"), body.get("color"), body.get("description"),
                        bool(body.get("delete")))
        return {"ok": True}
    if parts == ["pick-folder"]:
        return {"path": pick_folder(body.get("title", "选择项目文件夹"))}
    if parts == ["batch"]:
        return {"projects": manage.batch(body.get("projects") or [], body.get("group"), body.get("addTags"),
                                         body.get("removeTags"), body.get("pinned"))}
    if parts == ["prune"]:
        return manage.prune(dry_run=not body.get("yes"))
    if parts == ["github", "connect"]:
        return github.connect(body.get("mode", "token"), body.get("token"), body.get("grants"),
                              body.get("defaultPrivate", True))
    if parts == ["github", "disconnect"]:
        github.disconnect()
        return {"ok": True}
    if parts == ["github", "grants"]:
        return github.set_grants(body.get("grants"), body.get("defaultPrivate"))
    if len(parts) >= 3 and parts[0] == "projects":
        proj = _project(parts[1])
        root, action = proj["path"], parts[2]
        if action == "pin":
            manage.set_pinned([proj["id"]], bool(body.get("pinned", True)))
            return {"ok": True}
        if action == "relocate":
            return manage.relocate(proj["id"], body["path"])
        if action == "publish":
            # the user clicked it in the Hub: no agent grant needed, the GitHub connection still is
            return github.publish("id:" + proj["id"], body.get("repoName") or None, body.get("private"),
                                  body.get("description"), body.get("org") or None, body.get("remote") or "origin",
                                  None, bool(body.get("existing")), check_grant=False)
        if action == "update":
            return core.update_project(proj["id"], body)
        if action == "remove":
            _stop_all(proj["id"])
            core.remove_project(proj["id"])
            return {"ok": True}
        if action == "git-init":
            core.git_init(root)
            return {"ok": True}
        if action == "commit":
            return core.commit_to_branch(root, body.get("branch", ""), body["message"],
                                         body.get("files"), body.get("base"),
                                         body.get("author"))
        if action == "checkout":
            core.checkout(root, body["branch"], bool(body.get("create")))
            return {"ok": True}
        if action == "branch":
            core.create_branch(root, body["name"], body.get("base", "HEAD"))
            return {"ok": True}
        if action == "save":
            core.write_file(root, body["path"], body["content"], body.get("version"))
            return {"ok": True}
        if action == "preview":
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{PREVIEW_PORT}/preview/{proj['id']}/{proj.get('preview') or 'index.html'}")
            return {"ok": True}
        if action == "open":
            core.open_in(root, body.get("target", "vscode"), body.get("file"), body.get("line"))
            return {"ok": True}
        if action == "run":
            with _lock:
                cmd = body.get("command") or proj.get("run")
                if not isinstance(cmd, str) or not cmd.strip():
                    raise core.HubError("没有运行命令")
                old = RUNS.get(proj["id"])
                if old and old.running:
                    old.stop()
                r = core.Run(proj, cmd)
                RUNS[proj["id"]] = r
            return {"id": r.id}
        if action == "stop":
            with _lock:
                run = RUNS.get(proj["id"])
                if run and body.get("id", run.id) == run.id:
                    run.stop()
            return {"ok": True}
        if action == "watch":
            with _lock:
                if body.get("on", True):
                    if proj["id"] not in WATCHERS:
                        WATCHERS[proj["id"]] = core.Watcher(proj)
                elif proj["id"] in WATCHERS:
                    WATCHERS.pop(proj["id"]).stop()
            return {"watching": proj["id"] in WATCHERS}
        if action == "check":
            return core.full_check(proj, with_command=body.get("command", True))
    raise core.HubError(f"未知接口：{path}")


def pick_folder(title: str) -> str:
    """Native folder dialog (tkinter in a child process, so it never blocks the server)."""
    code = ("import tkinter as t, tkinter.filedialog as f, sys;"
            "r=t.Tk(); r.withdraw(); r.attributes('-topmost', True);"
            "sys.stdout.buffer.write((f.askdirectory(title=sys.argv[1]) or '').encode('utf-8'))")
    import importlib.util
    if importlib.util.find_spec("tkinter") is None and os.name == "nt":
        # The bundled (embeddable) Python has no tkinter: use the Windows folder dialog instead.
        ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;Add-Type -AssemblyName System.Windows.Forms;"
              "$d=New-Object System.Windows.Forms.FolderBrowserDialog;$d.Description=$env:HUB_TITLE;"
              "$d.ShowNewFolderButton=$true;"
              "if($d.ShowDialog((New-Object System.Windows.Forms.Form -Property @{TopMost=$true}))-eq'OK'){$d.SelectedPath}")
        r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True, timeout=600,
                           env={**os.environ, "HUB_TITLE": title}, creationflags=core.NO_WINDOW)
        out = r.stdout.decode("utf-8", "replace").strip()
        return os.path.normpath(out) if out else ""
    exe = sys.executable.replace("python.exe", "pythonw.exe") if os.name == "nt" else sys.executable
    if not os.path.exists(exe):
        exe = sys.executable
    r = subprocess.run([exe, "-c", code, title], capture_output=True, timeout=600)
    return os.path.normpath(r.stdout.decode("utf-8", "replace").strip()) if r.stdout.strip() else ""


def _stop_all(pid: str) -> None:
    run = RUNS.pop(pid, None)
    if run:
        run.stop()
    w = WATCHERS.pop(pid, None)
    if w:
        w.stop()


class Handler(BaseHTTPRequestHandler):
    server_version = "ProjectHub/1.0"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _host_ok(self) -> bool:
        # Blocks DNS rebinding: only localhost names may address this server.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1")

    def _send(self, code: int, data: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code: int, obj: object) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _guard(self, fn):
        if not self._host_ok():
            return self._json(403, {"error": "forbidden host"})
        try:
            self._json(200, fn())
        except core.HubError as e:
            self._json(400, {"error": str(e)})
        except (KeyError, ValueError, OSError) as e:
            self._json(400, {"error": f"{type(e).__name__}: {e}"})
        except Exception as e:  # pragma: no cover
            traceback.print_exc()
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path.startswith("/api/"):
            return self._guard(lambda: api_get(u.path, parse_qs(u.query)))
        if not self._host_ok():
            return self._json(403, {"error": "forbidden host"})
        rel = u.path.lstrip("/") or "index.html"
        return self._static(STATIC, rel)

    def do_POST(self):  # noqa: N802
        # Custom header forces a CORS preflight, which we never grant -> no cross-site POSTs.
        if self.headers.get("X-Hub") != "1":
            return self._json(403, {"error": "missing X-Hub header"})
        def dispatch():
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0:
                raise core.HubError("请求长度无效")
            body = json.loads(self.rfile.read(length)) if length else {}
            if not isinstance(body, dict):
                raise core.HubError("请求内容必须是 JSON 对象")
            return api_post(urlparse(self.path).path, body)

        self._guard(dispatch)

    def _static(self, base: str, rel: str) -> None:
        try:
            full = core.safe_join(base, rel)
        except core.HubError:
            return self._json(403, {"error": "forbidden"})
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            self._send(200, fh.read(), ctype)

    def _preview(self, rest: str) -> None:
        pid, _, rel = rest.partition("/")
        try:
            proj = _project(pid)
        except core.HubError as e:
            return self._json(404, {"error": str(e)})
        self._static(proj["path"], rel or proj.get("preview") or "index.html")


class PreviewHandler(Handler):
    """Serves project files on a *separate origin*, so previewed pages can't call the API."""

    def do_GET(self):  # noqa: N802
        if not self._host_ok():
            return self._json(403, {"error": "forbidden host"})
        u = urlparse(self.path)
        if not u.path.startswith("/preview/"):
            return self._send(404, b"not found", "text/plain")
        self._preview(unquote(u.path[len("/preview/"):]))

    def do_POST(self):  # noqa: N802
        self._json(403, {"error": "read-only"})


def start(port: int = 8790) -> tuple[ThreadingHTTPServer, str]:
    """Bind both servers and start them on background threads. Returns (httpd, url)."""
    global PREVIEW_PORT
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("application/javascript", ".mjs")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    preview = ThreadingHTTPServer(("127.0.0.1", port + 1 if port else 0), PreviewHandler)
    preview.daemon_threads = True
    PREVIEW_PORT = preview.server_address[1]
    threading.Thread(target=preview.serve_forever, daemon=True).start()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{port}/"


def shutdown() -> None:
    for r in RUNS.values():
        r.stop()
    for w in WATCHERS.values():
        w.stop()


def serve(port: int = 8790, open_browser: bool = False) -> None:
    httpd, url = start(port)
    print(f"Project Hub 已启动：{url}  (Ctrl+C 退出)", flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()
