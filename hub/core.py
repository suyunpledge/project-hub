"""Project Hub core: registry, git operations, file access, checks and runners.

Standard library only. All git calls go through ``git()`` so encoding and
non-interactive settings are uniform.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import push_lock, push_preview

ROOT = Path(__file__).resolve().parent.parent
# An installed copy (marker written by the installer) keeps its data outside the program folder,
# so upgrades and reinstalls never touch the registry. A source checkout keeps it in ./data.
INSTALLED = (ROOT / ".installed").exists()
_DEFAULT_DATA = (Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ProjectHub" / "data") if INSTALLED else ROOT / "data"
DATA_DIR = Path(os.environ.get("PROJECT_HUB_DATA", _DEFAULT_DATA))
REGISTRY_FILE = DATA_DIR / "hub.json"

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env", "dist", "build",
    ".gradle", ".idea", ".next", ".nuxt", "target", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "coverage", ".cache", "out",
}
MAX_FILES = 20000
MAX_READ = 2 * 1024 * 1024
WIN = sys.platform == "win32"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if WIN else 0


class HubError(Exception):
    """User-facing error (bad input, git refusal, ...)."""

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.details = details


# ---------------------------------------------------------------- registry

_reg_lock = threading.RLock()


def _default_registry() -> dict:
    return {"groups": [{"id": "default", "name": "未分组", "color": "#64748b"}], "projects": []}


def _parse_registry(text: str) -> dict:
    reg = json.loads(text)
    if not isinstance(reg, dict) or not isinstance(reg.get("projects", []), list) \
            or not isinstance(reg.get("groups", []), list):
        raise ValueError("registry has the wrong shape")
    for p in reg.get("projects", []):
        if not isinstance(p, dict) or not isinstance(p.get("id"), str) or not isinstance(p.get("path"), str):
            raise ValueError("registry has a malformed project entry")
        p.setdefault("name", p["id"])
        p.setdefault("group", "default")
        for k in ("run", "check", "preview", "description"):
            p.setdefault(k, "")
    return reg


def load_registry() -> dict:
    """Read the registry. A corrupt file (crash mid-write, bad hand edit) falls back to the
    last good backup; the broken file is kept as ``hub.corrupt-<time>.json`` for inspection."""
    with _reg_lock:
        if not REGISTRY_FILE.exists():
            return _default_registry()
        try:
            reg = _parse_registry(REGISTRY_FILE.read_text("utf-8-sig"))
        except (ValueError, UnicodeDecodeError) as err:
            backup = REGISTRY_FILE.with_suffix(".json.bak")
            try:
                reg = _parse_registry(backup.read_text("utf-8-sig"))
            except (OSError, ValueError, UnicodeDecodeError):
                raise HubError(f"登记文件 {REGISTRY_FILE} 已损坏且没有可用备份（{err}）。"
                               "请修复该 JSON 文件，或将其改名后重新登记项目。", code="registry_corrupt") from err
            try:
                os.replace(REGISTRY_FILE, REGISTRY_FILE.with_name(f"hub.corrupt-{int(time.time())}.json"))
                save_registry(reg)
            except OSError:
                pass  # still usable read-only; the next successful save repairs it
        if not any(g.get("id") == "default" for g in reg.get("groups", [])):
            reg.setdefault("groups", []).insert(0, _default_registry()["groups"][0])
        reg.setdefault("projects", [])
        return reg


def save_registry(reg: dict) -> None:
    with _reg_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if REGISTRY_FILE.exists():  # keep the previous good version as a fallback
            try:
                _parse_registry(REGISTRY_FILE.read_text("utf-8-sig"))
                shutil.copyfile(REGISTRY_FILE, REGISTRY_FILE.with_suffix(".json.bak"))
            except (OSError, ValueError, UnicodeDecodeError):
                pass
        tmp = REGISTRY_FILE.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2), "utf-8")
        for attempt in range(20):  # Windows: a reader may briefly hold the file open
            try:
                os.replace(tmp, REGISTRY_FILE)
                return
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
        os.replace(tmp, REGISTRY_FILE)


@contextmanager
def _file_lock():
    """Lock one file across processes. Closing it also releases the lock after a crash."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / "hub.lock", "a+b") as fh:
        deadline = time.monotonic() + 15
        if WIN:
            import msvcrt
            fh.seek(0)
        else:
            import fcntl
        while True:
            try:
                if WIN:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fh, fcntl.LOCK_EX)
                break
            except (PermissionError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise HubError("登记文件被占用，稍后重试")
                time.sleep(0.05)
        yield


@contextmanager
def transaction():
    """Load → modify → save the registry atomically (in-process and cross-process)."""
    with _reg_lock, _file_lock():
        reg = load_registry()
        yield reg
        save_registry(reg)


def _slug(text: str, taken: set[str]) -> str:
    base = re.sub(r"[^\w\-]+", "-", text.strip().lower()).strip("-") or "project"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def _path_key(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def find_project(ref: str, *, prefer_id: bool = False) -> dict:
    """Look a project up by id, name, or path."""
    return _resolve_project(load_registry()["projects"], ref, prefer_id=prefer_id)


def _resolve_project(projects: list[dict], ref: str, *, prefer_id: bool = False) -> dict:
    explicit = ref.startswith("id:")
    if explicit or prefer_id:
        pid = ref[3:] if explicit else ref
        for p in projects:
            if p["id"] == pid:
                return p
        if explicit:
            raise HubError(f"找不到项目 ID：{pid}")
    matches = [p for p in projects if ref in (p["name"], p["id"])]
    if len(matches) == 1:
        return matches[0]
    if matches:
        candidates = [{"ref": "id:" + p["id"], "id": p["id"], "name": p["name"],
                       "group": p["group"], "path": p["path"]} for p in matches]
        options = "\n".join(f"  {p['ref']}  [{p['group']}]  {p['path']}" for p in candidates)
        raise HubError(f"「{ref}」匹配多个项目，请按分组和路径选择项目引用：\n{options}",
                       code="ambiguous_project", candidates=candidates)
    if os.path.exists(ref):
        norm = _path_key(ref)
        for p in projects:
            if _path_key(p["path"]) == norm:
                return p
    raise HubError(f"找不到项目：{ref}")


def search_projects(query: str, group: str | None, tags: list[str], kind: str | None,
                    missing: bool, fuzzy: bool = True) -> list[dict]:
    """Ranked project search; see :func:`hub.search.search_projects`."""
    from .search import search_projects as ranked
    return ranked(query, group, tags, kind, missing, fuzzy)


def edit_tags(ref: str, add: list[str], remove: list[str]) -> dict:
    additions = [tag.strip() for tag in add]
    removals = {tag.strip().casefold() for tag in remove}
    keys = {t.casefold() for t in additions}
    if "" in keys | removals or keys & removals:
        raise HubError("请使用非空标签，添加和移除的标签不能重叠")
    with transaction() as reg:
        p = _resolve_project(reg["projects"], ref)
        labels = {t.casefold(): t for t in p.get("tags", []) if t.casefold() not in removals}
        for t in additions:
            labels.setdefault(t.casefold(), t)
        p["tags"] = list(labels.values())
        return p


def add_project(path: str, group: str = "default", name: str | None = None) -> dict:
    projects, _ = register_projects([path], group, name=name)
    return projects[0]


def register_projects(paths: list[str], group: str = "default", *, name: str | None = None,
                      group_id: str | None = None) -> tuple[list[dict], list[str]]:
    """Register a selection atomically. Return its projects and the IDs newly added."""
    if not isinstance(paths, list) or not all(isinstance(p, str) and p.strip() for p in paths):
        raise HubError("项目目录无效")
    if not paths:
        return [], []
    selected, added = [], []
    with transaction() as reg:
        known = {_path_key(p["path"]): p for p in reversed(reg["projects"])}
        ids = {p["id"] for p in reg["projects"]}
        for path in paths:
            full = os.path.abspath(os.path.expanduser(path))
            if not os.path.isdir(full):
                raise HubError(f"目录不存在：{full}")
            key = _path_key(full)
            proj = known.get(key)
            if proj is None:
                gid = _existing_group_id(reg, group_id) if group_id is not None else ensure_group(reg, group)
                label = name or os.path.basename(full)
                det = detect(full)
                proj = {"id": _slug(label, ids), "name": label, "path": full, "group": gid,
                        "run": det["run"], "check": det["check"], "preview": det["preview"], "description": ""}
                ids.add(proj["id"])
                known[key] = proj
                reg["projects"].append(proj)
                added.append(proj["id"])
            selected.append(proj)
    return selected, added


def _existing_group_id(reg: dict, gid: str) -> str:
    if not any(g["id"] == gid for g in reg["groups"]):
        raise HubError("目标分组已不存在，请重新选择")
    return gid


def find_group(reg: dict, ref: str) -> dict | None:
    for field in ("id", "name"):
        for g in reg["groups"]:
            if g[field] == ref:
                return g
    return None


def ensure_group(reg: dict, name_or_id: str, color: str | None = None) -> str:
    name_or_id = name_or_id.strip()
    if not name_or_id:
        raise HubError("分组名不能为空")
    g = find_group(reg, name_or_id)
    if g:
        return g["id"]
    palette = ["#6366f1", "#0ea5e9", "#10b981", "#f59e0b", "#ef4444", "#a855f7", "#14b8a6", "#ec4899"]
    gid = _slug(name_or_id, {g["id"] for g in reg["groups"]})
    reg["groups"].append({"id": gid, "name": name_or_id,
                          "color": color or palette[len(reg["groups"]) % len(palette)]})
    return gid


def update_project(pid: str, fields: dict) -> dict:
    allowed = {"name", "group", "run", "check", "preview", "description", "pinned"}
    with transaction() as reg:
        for p in reg["projects"]:
            if p["id"] == pid:
                for k, v in fields.items():
                    if k == "groupId":
                        k, v = "group", _existing_group_id(reg, v)
                    elif k == "group":
                        v = ensure_group(reg, v or "default")
                    elif k == "pinned":
                        if not isinstance(v, bool):
                            raise HubError("pinned 必须是 true 或 false")
                    elif k in allowed:
                        if not isinstance(v, str) or len(v) > 4000:
                            raise HubError(f"{k} 必须是不超过 4000 字的文本")
                        v = v.strip()
                        if k == "name" and not v:
                            raise HubError("显示名不能为空")
                    if k in allowed:
                        p[k] = v
                return p
    raise HubError(f"找不到项目：{pid}")


def remove_project(pid: str) -> None:
    with transaction() as reg:
        reg["projects"] = [p for p in reg["projects"] if p["id"] != pid]
        reg.get("pushPreviews", {}).pop(pid, None)
        reg.get("pushPreviewHistory", {}).pop(pid, None)


def set_group(project_refs: list[str], group: str, color: str | None = None,
              description: str | None = None) -> dict:
    """Create ``group`` if needed (by id or name) and move the projects into it."""
    with transaction() as reg:
        ids = [_resolve_project(reg["projects"], r)["id"] for r in project_refs]
        gid = ensure_group(reg, group, color)
        g = next(x for x in reg["groups"] if x["id"] == gid)
        if color:
            g["color"] = color
        if description is not None:
            g["description"] = description
        for p in reg["projects"]:
            if p["id"] in ids:
                p["group"] = gid
        return {"group": gid, "moved": ids}


def edit_group(gid: str, name: str | None = None, color: str | None = None,
               description: str | None = None, delete: bool = False) -> None:
    with transaction() as reg:
        g = find_group(reg, gid)
        if not g:
            raise HubError(f"找不到分组：{gid}")
        if delete:
            if g["id"] == "default":
                raise HubError("默认分组不能删除")
            reg["groups"].remove(g)
            for p in reg["projects"]:
                if p["group"] == g["id"]:
                    p["group"] = "default"
            return
        if name is not None:
            name = name.strip()
            if not name or any(other is not g and name in (other["id"], other["name"]) for other in reg["groups"]):
                raise HubError("请输入非空且未被占用的分组名")
            g["name"] = name
        if color:
            g["color"] = color
        if description is not None:
            g["description"] = description


def readme_head(path: str, limit: int = 1200) -> str:
    for name in ("README.zh.md", "README.md", "README.txt", "README", "readme.md"):
        f = os.path.join(path, name)
        if os.path.isfile(f):
            try:
                return Path(f).read_text("utf-8", errors="replace")[:limit]
            except OSError:
                return ""
    return ""


def scan(root: str, depth: int = 2) -> list[dict]:
    """Find candidate project folders under ``root`` (git repos or folders with project markers)."""
    markers = ("package.json", "pyproject.toml", "setup.py", "requirements.txt", "build.gradle",
               "build.gradle.kts", "Cargo.toml", "go.mod", "index.html", "pom.xml", "CMakeLists.txt")
    root = os.path.abspath(os.path.expanduser(root))
    registered = {_path_key(p["path"]) for p in load_registry()["projects"]}
    seen: set[str] = set()
    found: list[dict] = []

    def walk(d: str, level: int) -> None:
        try:
            with os.scandir(d) as children:
                entries = list(children)
        except OSError as err:
            if level == 0:
                raise HubError(f"无法扫描目录：{d}（{err.strerror}）") from err
            return
        names = {e.name for e in entries}
        is_git = ".git" in names
        if d != root and (is_git or any(m in names for m in markers)):
            key = _path_key(d)
            if key in seen:
                return
            seen.add(key)
            found.append({"path": d, "name": os.path.basename(d), "git": is_git,
                          "registered": key in registered,
                          "kind": detect(d)["kinds"]})
            return  # don't descend into a project
        if level >= depth:
            return
        for e in entries:
            if e.is_dir(follow_symlinks=False) and not e.name.startswith((".", "$")) \
                    and e.name not in SKIP_DIRS and e.name not in ("AppData", "Application Data"):
                walk(e.path, level + 1)

    walk(root, 0)
    return sorted(found, key=lambda f: f["name"].lower())


def detect(path: str) -> dict:
    """Guess project kinds and default run / check / preview settings."""
    p = Path(path)
    kinds: list[str] = []
    run = check = preview = ""
    pkg = p / "package.json"
    if pkg.exists():
        kinds.append("node")
        try:
            manifest = json.loads(pkg.read_text("utf-8-sig"))
            scripts = manifest.get("scripts") if isinstance(manifest, dict) else None
            if not isinstance(scripts, dict):
                scripts = {}
        except (ValueError, OSError):
            scripts = {}
        for s in ("dev", "start", "serve"):
            if s in scripts:
                run = f"npm run {s}"
                break
        for s in ("typecheck", "lint", "check"):
            if s in scripts:
                check = f"npm run {s}"
                break
        if not check and (p / "tsconfig.json").exists():
            check = "npx tsc --noEmit"
    if any((p / f).exists() for f in ("pyproject.toml", "setup.py", "requirements.txt")) \
            or any(p.glob("*.py")):
        kinds.append("python")
        if not run:
            for f in ("main.py", "app.py", "run.py", "server.py", "manage.py"):
                if (p / f).exists():
                    run = f"python {f}" + (" runserver" if f == "manage.py" else "")
                    break
    if (p / "build.gradle").exists() or (p / "build.gradle.kts").exists():
        kinds.append("gradle")
        if not check:
            check = "gradlew.bat compileDebugKotlin" if WIN else "./gradlew compileDebugKotlin"
    if (p / "Cargo.toml").exists():
        kinds.append("rust")
        run, check = run or "cargo run", check or "cargo check"
    if (p / "go.mod").exists():
        kinds.append("go")
        run, check = run or "go run .", check or "go vet ./..."
    if (p / "index.html").exists():
        kinds.append("web")
        preview = "index.html"
    if (p / ".git").exists():
        kinds.append("git")
    return {"kinds": kinds, "run": run, "check": check, "preview": preview}


# --------------------------------------------------------------------- git

_git_extra = threading.local()


@contextmanager
def git_environment(extra: dict | None):
    """Add environment variables to every git call made by this thread (e.g. push credentials).
    Values go through the environment, never the command line, so they don't show in process lists."""
    previous = getattr(_git_extra, "env", None)
    _git_extra.env = {**(previous or {}), **(extra or {})}
    try:
        yield
    finally:
        _git_extra.env = previous


def _run_git(path: str, *args: str, env: dict | None = None,
             input: str | None = None, lock_fd: int | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C.UTF-8",
                **(getattr(_git_extra, "env", None) or {}), **(env or {})}
    with push_lock.child_options(lock_fd) as options:
        return subprocess.run(["git", "-c", "core.quotepath=false", "-C", path, *args],
                              capture_output=True, env=full_env, input=input.encode() if input else None,
                              creationflags=NO_WINDOW, **options)


def git(path: str, *args: str, env: dict | None = None, check: bool = True,
        input: str | None = None) -> str:
    r = _run_git(path, *args, env=env, input=input)
    out = r.stdout.decode("utf-8", "replace")
    if check and r.returncode != 0:
        detail = r.stderr.decode("utf-8", "replace").strip() or out.strip()
        if "nothing to commit" in detail or "no changes added" in detail:
            detail = "没有可提交的改动"
        raise HubError(detail or f"git {args[0]} 失败")
    return out


def is_git(path: str) -> bool:
    return subprocess.run(["git", "-C", path, "rev-parse", "--is-inside-work-tree"],
                          capture_output=True, creationflags=NO_WINDOW).returncode == 0


def git_init(path: str) -> None:
    git(path, "init")


def _display_url(url: str) -> str:
    if re.match(r"^[\w+.-]+::", url):  # custom remote helper arguments are opaque
        return url.split("::", 1)[0] + "::<…>"
    if "://" in url:
        address = re.split(r"[?#]", url, maxsplit=1)[0]
        return re.sub(r"^(\w[\w+.-]*://)[^/]*@", r"\1", address)
    return url


def remote_targets(path: str, remotes: list[str]) -> dict[str, list[str]]:
    return {name: [_display_url(url) for url in git(path, "remote", "get-url", "--push", "--all",
                                                    "--", name).splitlines()] for name in remotes}


def _stale_preview(changes: list[str], context: dict) -> HubError:
    labels = {"local_sha": "本地分支的提交", "push_urls": "远端推送地址", "remote_tip": "远端目标分支的提交",
              "branch": "本地分支", "remote": "远端名称", "target": "目标分支", "preview_id": "预演记录"}
    hint = "请核对变化，使用 --dry-run 重新预演后再推送。"
    message = f"项目 {context['project']} 的预演已失效，" + "、".join(labels[c] for c in changes) + "发生变化。"
    if "previewSha" in context:
        message += f"预演提交为 {context['previewSha'][:12]}，当前为 {context['sha'][:12] or '无提交'}。"
    if "previewUrls" in context:
        message += f"预演地址为 {', '.join(context['previewUrls'])}，当前为 {', '.join(context['pushUrls'])}。"
    if "remote_tip" in changes:
        message += f"请检查 {context['remote']}/{context['target']} 的最新历史。"
    return HubError(message + hint,
                    code="preview_stale", changes=changes, hint=hint, **context)


def push_project(project: dict, branch: str | None, remote: str | None, target: str | None,
                 dry_run: bool, preview_id: str | None = None) -> dict:
    # The lock covers the Git operation and its receipt, but never other projects' Git operations.
    key = hashlib.sha256(project["id"].encode("utf-8")).hexdigest()
    from . import github  # local import: github imports core
    try:
        with push_lock.acquire(DATA_DIR / f"push-{key}.lock", wait=dry_run or preview_id is not None) as fd, \
                git_environment(github.push_env(project["path"])):
            return _push_project_locked(project, branch, remote, target, dry_run, preview_id, fd)
    except push_lock.PushBusy:
        raise HubError("此项目的预演或 Git 推送仍在执行，请等待结束后用原参数重试。", code="push_busy")


def _record_preview(reg: dict, snapshot: dict, status: str) -> None:
    history = reg.setdefault("pushPreviewHistory", {}).setdefault(snapshot["project"], [])
    receipt = {k: snapshot[k] for k in ("previewId", "sha", "remote", "target", "branch", "pushUrls")}
    if snapshot.get("recovered"):
        receipt["recovered"] = True
    if status == "pushed" and "destinations" in snapshot:
        receipt["destinations"] = snapshot["destinations"]
    history.append({**receipt, "status": status})
    del history[:-64]


PREVIEW_TTL = 30 * 60  # seconds; an unused dry run stops blocking plain pushes after this


def _expire_preview(pid: str) -> None:
    with transaction() as reg:
        pending = reg.get("pushPreviews", {}).get(pid)
        if pending and not pending.get("attempted") and \
                time.time() - pending.get("createdAt", 0) > PREVIEW_TTL:
            _record_preview(reg, pending, "expired")
            del reg["pushPreviews"][pid]


def _pending_preview(pid: str, preview_id: str | None) -> dict | None:
    _expire_preview(pid)
    reg = load_registry()
    pending = reg.get("pushPreviews", {}).get(pid)
    if preview_id is None:
        if pending:
            raise HubError("此项目有待推送的预演。请用 --preview-id 传入你自己预演返回的编号。",
                           code="preview_id_required", project=pid)
        return None
    if pending and pending["previewId"] == preview_id:
        return pending
    history = reg.get("pushPreviewHistory", {}).get(pid, [])
    receipt = next((r for r in reversed(history) if r["previewId"] == preview_id), None)
    if receipt and receipt["status"] == "pushed":
        action = "已核实目标分支收到" if receipt.get("recovered") else "已经成功推送"
        raise HubError(f"该预演编号{action} {receipt['sha'][:12]}（{receipt['remote']}/{receipt['target']}）。"
                       "本次未重复推送。", code="preview_already_pushed", project=pid, **receipt)
    reason = receipt["status"] if receipt and receipt["status"] == "expired" else "superseded" if receipt else "unknown"
    message = ("该预演已超过 30 分钟未使用，已失效" if reason == "expired"
               else "该编号已被后续成功的预演替代" if receipt else "此项目没有该编号的预演记录")
    hint = "请用 --dry-run 重新预演，并传回自己收到的编号。"
    raise HubError(message + "。" + hint, code="preview_stale", changes=["preview_id"],
                   reason=reason, hint=hint, project=pid, previewId=preview_id)


def _set_push_attempt(saved: dict, *, attempted: bool) -> None:
    try:
        with transaction() as reg:
            pending = reg.get("pushPreviews", {}).get(saved["project"])
            if not pending or pending["previewId"] != saved["previewId"]:
                raise _stale_preview(["preview_id"], saved)
            if attempted:
                pending["attempted"] = True
            else:
                pending.pop("attempted", None)
    except OSError as exc:
        raise HubError("无法保存本地推送状态，请恢复登记文件的写入权限后用原预演编号重试。",
                       code="push_state_failed", previewId=saved["previewId"]) from exc


def _finish_push(saved: dict, context: dict, *, recovered: bool = False) -> None:
    try:
        with transaction() as reg:
            previews = reg.get("pushPreviews", {})
            if previews.get(saved["project"], {}).get("previewId") != saved["previewId"]:
                raise _stale_preview(["preview_id"], saved)
            _record_preview(reg, {**saved, **context, "recovered": recovered}, "pushed")
            del previews[saved["project"]]
    except OSError as exc:
        raise HubError("远端已达到预演的提交，本地回执保存失败。请用原预演编号重试以补记结果。",
                       code="push_receipt_failed", **context) from exc


_DESTINATION_LABELS = {
    "at_target": "已到达目标提交，跳过推送", "pending": "待推送", "pushed": "推送成功",
    "rejected": "推送被拒", "unknown": "结果待确认", "changed": "预演状态已变化",
    "not_attempted": "本次未推送",
}


def format_destinations(rows: list[dict]) -> str:
    lines = []
    for row in rows:
        label = (f"响应中断，已只读核实目标提交 {row['remoteTip'][:12]}" if row.get("verifiedAfterDisconnect")
                 else _DESTINATION_LABELS[row["status"]])
        line = f"  {row['url']}  {label}"
        if row["status"] == "changed" and "previewTip" in row and "remoteTip" in row:
            before = (row["previewTip"] or "分支不存在")[:12]
            now = (row["remoteTip"] or "分支不存在")[:12]
            line += f"  预演时 {before}，当前 {now}"
        if row.get("error"):
            line += f"  {row['error']}"
        lines.append(line)
    return "\n".join(lines)


def _read_destinations(path: str, urls: list[str], context: dict, expected: list | None,
                       *, recovering: bool = False) -> list[dict]:
    ref = "refs/heads/" + context["target"]
    rows = []
    for index, url in enumerate(urls):
        row = {"url": context["pushUrls"][index], "status": "unknown"}
        if expected is not None:
            row["previewTip"] = expected[index]
        rows.append(row)
        # The read must reach the reviewed push address, including when fetch URL rewrites exist.
        resolved = _run_git(path, "ls-remote", "--get-url", "--", url)
        if resolved.returncode or resolved.stdout.decode("utf-8", "replace").rstrip("\r\n") != url:
            row["error"] = "读取地址与推送地址不一致"
            continue
        probe = _run_git(path, "ls-remote", "--refs", "--", url, ref)
        if probe.returncode:
            row["error"] = "无法读取目标分支"
            continue
        tip = next((line.split("\t")[0] for line in probe.stdout.decode("utf-8", "replace").splitlines()
                    if line.endswith("\t" + ref)), None)
        row["remoteTip"] = tip
        row["status"] = "at_target" if tip == context["sha"] else "pending"
        if expected is not None and tip != expected[index] and not (recovering and tip == context["sha"]):
            row["status"] = "changed"
    return rows


def _destination_error(error: HubError, rows: list[dict]) -> HubError:
    if error.details.get("code") == "preview_stale" and not error.details.get("dryRun"):
        rows = [{**row, "status": "not_attempted"} if row["status"] == "pending" else row for row in rows]
    return HubError(f"{error}\n{format_destinations(rows)}", **{**error.details, "destinations": rows})


def _reconcile_push(path: str, urls: list[str], saved: dict, context: dict) -> bool:
    rows = _read_destinations(path, urls, context, saved["tips"], recovering=True)
    if any(r["status"] == "unknown" for r in rows):
        raise _destination_error(HubError("上次推送结果待确认，请恢复访问后用原预演编号重试。",
                                          code="push_outcome_unknown", **context), rows)
    tips = [r["remoteTip"] for r in rows]
    if tips and all(tip == saved["sha"] for tip in tips):
        context["destinations"] = rows
        return True
    if tips == saved["tips"]:
        return False
    if any(r["status"] == "changed" for r in rows):
        error = _stale_preview(["remote_tip"], {**context, "remoteTips": tips})
    else:
        hint = "请重新预演，再用新编号补推剩余地址。"
        error = HubError(f"经只读核对，部分地址已达到预演提交 {saved['sha'][:12]}。本次没有推送。{hint}",
                         code="push_partially_applied", hint=hint, remoteTips=tips, **context)
    raise _destination_error(error, rows)


def _send_push(path: str, url: str, context: dict, *, simulate: bool, guarded: bool,
               expected: list | None, config: list[str], lock_fd: int | None) -> tuple[bool, str | None]:
    remote, sha, target = context["remote"], context["sha"], context["target"]
    args = [*config, "-c", f"remote.{remote}.mirror=false", "push", "--porcelain", "--no-follow-tags",
            "--recurse-submodules=no"]
    if simulate:
        args.append("--dry-run")
    args.extend(["--", remote, f"{sha}:refs/heads/{target}"])
    tip = None
    if guarded:
        hooks = Path(path) / git(path, "rev-parse", "--git-path", "hooks").strip()
        snapshot = {**context, "urlHashes": [push_preview.url_hash(url)]}
        with push_preview.observe(hooks, snapshot, expected) as (hook_config, log):
            result = _run_git(path, *hook_config, *args, lock_fd=lock_fd)
            observations = [json.loads(line) for line in log.read_text("utf-8").splitlines()] if log.exists() else []
        changes = list(dict.fromkeys(key for obs in observations for key in obs["changes"]))
        if changes or (not result.returncode and len(observations) != 1):
            raise _stale_preview(changes or ["push_urls"], context)
        if observations:
            tip = observations[0]["tip"]
    else:
        result = _run_git(path, *args, lock_fd=lock_fd)
    updates = [line for line in result.stdout.decode("utf-8", "replace").splitlines()
               if len(line) > 1 and line[1] == "\t"]
    if result.returncode:
        rejected = [line.split("\t", 2)[-1] for line in updates if line.startswith("!")]
        code = "push_failed"
        hint = "请核对推送地址、连接和写入权限。"
        if any(s in ("[rejected] (non-fast-forward)", "[rejected] (fetch first)") for s in rejected):
            code = "non_fast_forward"
            hint = "请先获取目标分支的最新提交，比较两边历史并整合改动，再预演推送。"
        elif rejected and all(s.startswith(("[rejected]", "[remote rejected]")) for s in rejected):
            code = "push_rejected"
            hint = "请检查 Git 给出的拒绝原因以及目标仓库的分支规则。"
        detail = "\n".join(rejected + [result.stderr.decode("utf-8", "replace").strip()]).strip()
        ref_changed = re.search(r"cannot lock ref .* (?:expected |reference already exists)", detail)
        if expected is not None and (code == "non_fast_forward" or ref_changed):
            raise _stale_preview(["remote_tip"], context)
        detail = detail.replace(url, _display_url(url))
        detail = re.sub(r"[\w+.-]+://[^\s'\"<>]+", lambda m: _display_url(m[0]), detail)
        if expected is not None and not simulate and code == "push_failed":
            code = "push_outcome_unknown"
            hint = "上次推送结果待确认。请用原预演编号重试，Hub 会先只读核对远端。"
        action = "预演" if simulate else "推送"
        raise HubError(f"{context['project']} {context['branch']} → {remote}/{target} {action}失败。{hint}\n{detail}".rstrip(),
                       code=code, hint=hint, **context)
    return bool(updates) and all(s[0] == "=" for s in updates), tip



def _check_destinations(path: str, urls: list[str], context: dict, expected: list | None) -> list[dict]:
    rows = _read_destinations(path, urls, context, expected)
    if any(row["status"] == "changed" for row in rows):
        raise _destination_error(_stale_preview(["remote_tip"], context), rows)
    if any(row["status"] == "unknown" for row in rows):
        raise _destination_error(HubError("无法核对所有推送地址，请恢复读取权限或连接后重试。本次没有推送。",
                                          code="push_outcome_unknown", **context), rows)
    return rows


def _push_multiple(path: str, urls: list[str], context: dict, saved: dict | None,
                   lock_fd: int) -> tuple[bool, list]:
    expected = saved["tips"] if saved else None
    rows = _check_destinations(path, urls, context, expected)
    tips = [row["remoteTip"] for row in rows]
    first_index = {}
    for i, url in enumerate(urls):
        first_index.setdefault(url, i)
    pending = [i for i in first_index.values() if rows[i]["status"] == "pending"]
    remote = context["remote"]
    configs = {}
    for i in pending:
        # Keep the real remote name, transport settings and hook arguments, but replace its URL
        # list for just this Git process. Verify the effective destination before any transfer.
        key = f"remote.{remote}.pushurl"
        config = ["-c", key + "=", "-c", key + "=" + urls[i]]
        effective = git(path, *config, "remote", "get-url", "--push", "--all", "--", remote).splitlines()
        if effective != [urls[i]]:
            rows[i].update(status="unknown", error="Git 未能单独选中此推送地址")
            raise _destination_error(HubError("无法将此 Git 推送限定到单个地址，请检查 Git 版本和 URL 重写配置。",
                                              code="push_url_selection_failed", **context), rows)
        configs[i] = config

    # Validate every pending destination before writing to the first. Skipped destinations
    # have only their target ref read; neither receive-pack nor pre-push runs for them.
    passes = [True, False] if saved else [context["dryRun"]]
    for simulate in passes:
        failures = []
        if not simulate and saved and pending:
            # A skipped mirror or an earlier checked candidate may have moved during the dry runs.
            rows = _check_destinations(path, urls, context, expected)
            _set_push_attempt(saved, attempted=True)
        for i in pending:
            row = rows[i]
            try:
                up_to_date, observed = _send_push(path, urls[i], context, simulate=simulate, guarded=True,
                                                  expected=None if simulate and not saved else [tips[i]],
                                                  config=configs[i], lock_fd=None if simulate else lock_fd)
            except HubError as exc:
                code = exc.details.get("code")
                status = "rejected" if code in ("push_rejected", "non_fast_forward") else "unknown"
                if code == "preview_stale":
                    status = "changed"
                row.update(status=status, error=str(exc))
                # A ref race may surface as a generic refusal or a lost acknowledgement.
                uncertain = code == "push_outcome_unknown"
                check_failed_write = not simulate and (code == "push_rejected" or uncertain)
                stop = status == "changed" or uncertain
                if check_failed_write or (stop and "remote_tip" in exc.details.get("changes", ())):
                    selected = {**context, "pushUrls": [row["url"]]}
                    latest, = _read_destinations(path, [urls[i]], selected, [tips[i]], recovering=uncertain)
                    row.pop("remoteTip", None)
                    if "remoteTip" in latest:
                        row["remoteTip"] = latest["remoteTip"]
                        if uncertain and latest["status"] == "at_target":
                            row.update(status="at_target", verifiedAfterDisconnect=True)
                            row.pop("error", None)
                        elif check_failed_write and latest["status"] == "changed":
                            row["status"] = "changed"
                            # Keep Git's diagnostic; the new group-level retry advice supersedes the old one.
                            row["error"] = str(exc).partition("\n")[2]
                            exc = _stale_preview(["remote_tip"], context)
                            stop = True
                    else:
                        row["error"] += " " + latest["error"]
                        stop = True
                elif status in ("unknown", "changed") and not simulate:
                    row.pop("remoteTip", None)
                failures.append(exc)
                if stop:
                    break
            else:
                if simulate and not saved:
                    tips[i] = observed
                    row.update(status="at_target" if up_to_date else "pending", remoteTip=observed)
                elif not simulate:
                    row.update(status="at_target" if up_to_date else "pushed", remoteTip=context["sha"])
        for i, url in enumerate(urls):
            source = first_index[url]
            if i != source:
                rows[i] = rows[source].copy()
                tips[i] = tips[source]
                if rows[i]["status"] == "pushed":
                    rows[i]["status"] = "at_target"
        # The last reply can be lost after every destination already reached the requested SHA.
        if failures and not all(r["status"] in ("pushed", "at_target") for r in rows):
            if not simulate:
                for row in rows:
                    if row["status"] == "pending":
                        row["status"] = "not_attempted"
            error = failures[-1]
            if error.details.get("code") == "preview_stale":
                if saved and not simulate and not any(r["status"] in ("pushed", "unknown") for r in rows):
                    _set_push_attempt(saved, attempted=False)
                raise _destination_error(error, rows)
            if not simulate and any(r["status"] == "unknown" for r in rows):
                hint = ("部分推送结果待确认，请恢复访问后用原预演编号重试，Hub 会先只读核对各地址。"
                        if saved else "部分推送结果待确认，请先核对各地址的目标分支。")
                error = HubError(hint, code="push_outcome_unknown", hint=hint, **context)
            elif not simulate and any(r["status"] in ("at_target", "pushed") for r in rows):
                hint = "部分地址已达到目标提交，本次推送已停止。请核对各地址后重新预演，补推剩余地址。"
                error = HubError(hint, code="push_partially_applied", hint=hint, **context)
            raise _destination_error(error, rows)
    context["destinations"] = rows
    return all(r["status"] == "at_target" and not r.get("verifiedAfterDisconnect") for r in rows), tips


def _push_project_locked(project: dict, branch: str | None, remote: str | None, target: str | None,
                         dry_run: bool, preview_id: str | None, lock_fd: int) -> dict:
    path = project["path"]
    pid = project["id"]
    saved = None if dry_run else _pending_preview(pid, preview_id)
    root = git(path, "rev-parse", "--show-toplevel", check=False).strip()
    if not root or _path_key(root) != _path_key(path):
        raise HubError("请选择登记在 Git 工作树根目录的项目")
    branch = branch or git(path, "branch", "--show-current").strip()
    if saved and saved["branch"] != branch:
        raise _stale_preview(["branch"], {"project": pid, "branch": branch})
    if not branch:
        raise HubError("当前为 detached HEAD，请用 -b 指定本地分支")
    target = target or branch
    for name in (branch, target):
        git(path, "check-ref-format", f"refs/heads/{name}")
    sha = git(path, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}", check=False).strip()
    context = {"project": pid, "branch": branch, "target": target, "sha": sha, "dryRun": dry_run}
    if saved:
        context["previewId"] = saved["previewId"]
    if saved and saved["target"] != target:
        raise _stale_preview(["target"], {**context, "previewTarget": saved["target"]})
    if not sha and not saved:
        raise HubError(f"本地分支不存在或尚无提交：{branch}")
    remotes = git(path, "remote").splitlines()
    if not remotes:
        if saved:
            raise _stale_preview(["remote"], context)
        raise HubError(f"项目 {project['id']} 还没有配置 Git 远端，请先配置推送地址",
                       code="no_remotes", availableRemotes={}, **context)
    if remote is None:
        for key in (f"branch.{branch}.pushRemote", "remote.pushDefault", f"branch.{branch}.remote"):
            remote = git(path, "config", "--get", key, check=False).strip()
            if remote:
                break
        if not remote:
            remote = "origin" if "origin" in remotes else remotes[0] if len(remotes) == 1 else None
    if saved and (remote != saved["remote"] or remote not in remotes):
        raise _stale_preview(["remote"], {**context, "remote": remote, "previewRemote": saved["remote"]})
    if remote not in remotes:
        choices = remote_targets(path, remotes)
        options = "\n".join(f"  {name} → {', '.join(urls)}" for name, urls in choices.items())
        reason = f"找不到远端「{remote}」" if remote else "有多个远端且没有默认推送目标"
        raise HubError(f"项目 {project['id']} {reason}，请核对地址并用 --remote 选择：\n{options}",
                       code="unknown_remote" if remote else "remote_required", remote=remote,
                       availableRemotes=choices, **context)
    urls = git(path, "remote", "get-url", "--push", "--all", "--", remote).splitlines()
    context.update(remote=remote, pushUrls=[_display_url(url) for url in urls])
    hashes = [push_preview.url_hash(url) for url in urls]
    snapshot = {**context, "urlHashes": hashes}
    if saved and saved["urlHashes"] != hashes:
        raise _stale_preview(["push_urls"], {**context, "previewUrls": saved["pushUrls"]})
    if saved and saved.get("attempted"):
        reviewed = {**context, "sha": saved["sha"]}
        if _reconcile_push(path, urls, saved, reviewed):
            _finish_push(saved, reviewed, recovered=True)
            return {**reviewed, "upToDate": True, "recovered": True}
    if saved and saved["sha"] != sha:
        raise _stale_preview(["local_sha"], {**context, "previewSha": saved["sha"]})
    if len(urls) > 1:
        up_to_date, tips = _push_multiple(path, urls, context, saved, lock_fd)
    else:
        if saved:
            _set_push_attempt(saved, attempted=True)
        try:
            up_to_date, tip = _send_push(path, urls[0], context, simulate=dry_run, guarded=bool(dry_run or saved),
                                         expected=saved["tips"] if saved else None, config=[],
                                         lock_fd=None if dry_run else lock_fd)
        except HubError as exc:
            if saved and exc.details.get("code") in ("preview_stale", "push_rejected", "non_fast_forward"):
                _set_push_attempt(saved, attempted=False)
            raise
        tips = [tip]
    if dry_run:
        snapshot.update(tips=tips, previewId=uuid.uuid4().hex, createdAt=time.time())
        with transaction() as reg:
            if not any(p["id"] == pid and p["path"] == path for p in reg["projects"]):
                raise _stale_preview(["preview_id"], context)
            previews = reg.setdefault("pushPreviews", {})
            if pid in previews:
                _record_preview(reg, previews[pid], "superseded")
            previews[pid] = snapshot
        context.update(previewId=snapshot["previewId"], remoteTips=snapshot["tips"],
                       expiresAt=snapshot["createdAt"] + PREVIEW_TTL)
    else:
        if saved:
            _finish_push(saved, context)
        context["upstreamSet"] = _ensure_upstream(path, branch, remote, target)
    return {**context, "upToDate": up_to_date}


def _ensure_upstream(path: str, branch: str, remote: str, target: str) -> bool:
    """After a real push, let the branch track what it was pushed to — only if it tracks nothing yet."""
    if git(path, "config", "--get", f"branch.{branch}.remote", check=False).strip():
        return False
    ok = _run_git(path, "config", f"branch.{branch}.remote", remote).returncode == 0
    ok = ok and _run_git(path, "config", f"branch.{branch}.merge", f"refs/heads/{target}").returncode == 0
    return ok


def git_info(path: str) -> dict:
    if not is_git(path):
        return {"git": False}
    branch = git(path, "branch", "--show-current", check=False).strip()
    head = git(path, "rev-parse", "--short", "HEAD", check=False).strip()
    branches = []
    for line in git(path, "for-each-ref", "--sort=-committerdate",
                    "--format=%(refname:short)\x1f%(objectname:short)\x1f%(committerdate:relative)\x1f%(subject)",
                    "refs/heads", check=False).splitlines():
        name, sha, when, subj = (line.split("\x1f") + ["", "", "", ""])[:4]
        branches.append({"name": name, "sha": sha, "when": when, "subject": subj})
    status = parse_status(git(path, "status", "--porcelain=v1", "-z", "-uall", check=False))
    remote = git(path, "remote", "get-url", "origin", check=False).strip()
    ahead = behind = 0
    if branch:
        ab = git(path, "rev-list", "--left-right", "--count", f"{branch}...@{{u}}", check=False).split()
        if len(ab) == 2:
            ahead, behind = int(ab[0]), int(ab[1])
    return {"git": True, "branch": branch or "(detached)", "head": head, "branches": branches,
            "changes": status, "remote": remote, "ahead": ahead, "behind": behind}


def parse_status(raw: str) -> list[dict]:
    items, parts, i = [], raw.split("\0"), 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        x, y, file = entry[0], entry[1], entry[3:]
        orig = None
        if x in "RC":
            orig = parts[i] if i < len(parts) else None
            i += 1
        items.append({"path": file, "index": x, "worktree": y, "orig": orig,
                      "label": _status_label(x, y)})
    return items


def _status_label(x: str, y: str) -> str:
    if x == "?" and y == "?":
        return "新文件"
    c = y if y != " " else x
    return {"M": "修改", "A": "新增", "D": "删除", "R": "重命名", "C": "复制", "U": "冲突"}.get(c, c)


def git_log(path: str, ref: str = "HEAD", n: int = 60, file: str | None = None) -> list[dict]:
    args = ["log", f"-n{n}", "--format=%H\x1f%h\x1f%an\x1f%ar\x1f%s\x1f%D", ref, "--"]
    if file:
        args.append(file)
    out = git(path, *args, check=False)
    log = []
    for line in out.splitlines():
        f = line.split("\x1f")
        if len(f) >= 6:
            log.append({"sha": f[0], "short": f[1], "author": f[2], "when": f[3],
                        "subject": f[4], "refs": f[5]})
    return log


def git_show_commit(path: str, sha: str) -> dict:
    meta = git(path, "show", "-s", "--format=%H%n%an <%ae>%n%ad%n%B", sha)
    lines = meta.split("\n")
    files = []
    for line in git(path, "show", "--format=", "--numstat", sha).splitlines():
        a, d, f = (line.split("\t") + ["", "", ""])[:3]
        files.append({"path": f, "add": a, "del": d})
    diff = git(path, "show", "--format=", "--no-color", "--stat=0", "-p", sha)
    return {"sha": lines[0], "author": lines[1], "date": lines[2],
            "message": "\n".join(lines[3:]).strip(), "files": files, "diff": diff[:400_000]}


def git_diff(path: str, file: str | None = None, staged: bool = False) -> str:
    args = ["diff", "--no-color"] + (["--cached"] if staged else [])
    if file:
        st = git(path, "status", "--porcelain", "--", file, check=False)
        if st.startswith("??"):
            return git(path, "diff", "--no-color", "--no-index", "--", os.devnull, file, check=False)
        args += ["HEAD", "--", file] if not staged else ["--", file]
        out = git(path, *args, check=False)
        return out or git(path, "diff", "--no-color", "--", file, check=False)
    return git(path, *args, "HEAD", check=False)


def _checked_out_branches(path: str) -> dict[str, str]:
    out = git(path, "worktree", "list", "--porcelain", check=False)
    res, wt = {}, None
    for line in out.splitlines():
        if line.startswith("worktree "):
            wt = line[9:]
        elif line.startswith("branch refs/heads/"):
            res[line[18:]] = wt
    return res


def commit_to_branch(path: str, branch: str, message: str, files: list[str] | None = None,
                     base: str | None = None, author: str | None = None,
                     allow_empty: bool = False) -> dict:
    """Commit working-tree content to ``branch``.

    * If ``branch`` is the checked-out branch, this is a normal ``git add`` + ``git commit``.
    * Otherwise the snapshot is committed *onto that branch without switching the checkout*,
      using a temporary index. The branch is created from ``base`` (default HEAD) if missing.

    ``files`` limits the commit to those paths (additions, edits and deletions); ``None``
    means the whole working tree (respecting .gitignore).
    """
    if not message.strip():
        raise HubError("提交说明不能为空")
    if not is_git(path):
        raise HubError("该项目还不是 git 仓库，先初始化")
    if branch and git(path, "check-ref-format", "--branch", branch, check=False).strip() == "":
        raise HubError(f"分支名不合法：{branch}")
    current = git(path, "branch", "--show-current", check=False).strip()
    branch = branch or current
    if files is not None and (not isinstance(files, list) or not files or
                              not all(isinstance(f, str) and f for f in files)):
        raise HubError("请选择要提交的文件")
    env: dict = {"GIT_LITERAL_PATHSPECS": "1"} if files else {}
    if author:
        m = re.match(r"\s*(.+?)\s*<(.+)>\s*$", author)
        if not m:
            raise HubError("author 格式应为 'Name <email>'")
        env.update(GIT_AUTHOR_NAME=m[1], GIT_AUTHOR_EMAIL=m[2])
    present, missing = [], []
    for f in files or []:
        (present if os.path.lexists(os.path.join(path, f)) else missing).append(f)

    def git_paths(args: list[str], paths: list[str], git_env: dict) -> None:
        # Feeding literal paths on stdin also handles large selections on Windows.
        git(path, *args, "--pathspec-from-file=-", "--pathspec-file-nul",
            env=git_env, input="\0".join(paths) + "\0")

    if branch == current:
        if files is None:
            git(path, "add", "-A", env=env)
        elif present:
            git_paths(["add", "-A"], present, env)
        args = ["commit", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        if files:
            git_paths(args, files, env)
        else:
            git(path, *args, env=env)
        sha = git(path, "rev-parse", "HEAD").strip()
        return {"branch": branch, "sha": sha, "created": False, "switched": False}

    other = _checked_out_branches(path)
    if branch in other:
        raise HubError(f"分支 {branch} 正在另一个工作树中检出（{other[branch]}），请到那里提交")
    exists = git(path, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}",
                 check=False).strip()
    parent = exists or git(path, "rev-parse", "--verify", "--quiet", f"{base or 'HEAD'}^{{commit}}",
                           check=False).strip()
    if base and not exists and not parent:
        raise HubError(f"基准不存在：{base}")
    gitdir = git(path, "rev-parse", "--absolute-git-dir").strip()
    fd, index = tempfile.mkstemp(prefix="hub-index-", dir=gitdir)
    os.close(fd)
    os.unlink(index)
    ienv = {**env, "GIT_INDEX_FILE": index}
    try:
        if parent:
            git(path, "read-tree", parent, env=ienv)
        else:
            git(path, "read-tree", "--empty", env=ienv)
        if files is None:
            git(path, "add", "-A", env=ienv)
        else:
            if present:
                git_paths(["add", "-A"], present, ienv)
            if missing:
                git_paths(["rm", "-rf", "--cached", "--ignore-unmatch"], missing, ienv)
        tree = git(path, "write-tree", env=ienv).strip()
    finally:
        if os.path.exists(index):
            os.unlink(index)
    if parent and not allow_empty and tree == git(path, "rev-parse", f"{parent}^{{tree}}").strip():
        raise HubError(f"与分支 {branch} 相比没有改动")
    ct = ["commit-tree", tree, "-m", message] + (["-p", parent] if parent else [])
    sha = git(path, *ct, env=env).strip()
    git(path, "update-ref", f"refs/heads/{branch}", sha, exists or "0" * 40)
    return {"branch": branch, "sha": sha, "created": not exists, "switched": False}


def checkout(path: str, branch: str, create: bool = False) -> None:
    git(path, "switch", *(["-c"] if create else []), branch)


def create_branch(path: str, name: str, base: str = "HEAD") -> None:
    git(path, "branch", name, base)


# ------------------------------------------------------------------- files

def safe_join(root: str, rel: str) -> str:
    full = os.path.realpath(os.path.join(root, rel))
    root_real = os.path.realpath(root)
    if os.path.commonpath([full, root_real]) != root_real:
        raise HubError("路径越界")
    return full


def list_files(path: str, ref: str | None = None) -> list[str]:
    """Files of the working tree (tracked + untracked, not ignored) or of ``ref``."""
    if ref:
        out = git(path, "ls-tree", "-r", "--name-only", "-z", ref)
        return [f for f in out.split("\0") if f][:MAX_FILES]
    if is_git(path):
        out = git(path, "ls-files", "-co", "--exclude-standard", "-z", check=False)
        files = [f for f in out.split("\0") if f and os.path.exists(os.path.join(path, f))]
        return sorted(set(files))[:MAX_FILES]
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        rel = os.path.relpath(dirpath, path)
        for f in filenames:
            files.append(f if rel == "." else f"{rel}/{f}".replace("\\", "/"))
            if len(files) >= MAX_FILES:
                return sorted(files)
    return sorted(files)


def read_file(path: str, rel: str, ref: str | None = None) -> dict:
    if ref:
        size = int(git(path, "cat-file", "-s", f"{ref}:{rel}"))
        if size > MAX_READ:
            return {"path": rel, "binary": False, "tooLarge": True, "size": size, "content": ""}
        r = subprocess.run(["git", "-C", path, "show", f"{ref}:{rel}"], capture_output=True,
                           creationflags=NO_WINDOW)
        if r.returncode:
            raise HubError(r.stderr.decode("utf-8", "replace").strip())
        data = r.stdout
    else:
        full = safe_join(path, rel)
        size = os.path.getsize(full)
        if size > MAX_READ:
            return {"path": rel, "binary": False, "tooLarge": True, "size": size, "content": ""}
        with open(full, "rb") as fh:
            data = fh.read()
    if b"\0" in data[:8000]:
        return {"path": rel, "binary": True, "size": len(data), "content": ""}
    encoding = _source_encoding(data)
    text = data.decode(encoding or "utf-8", errors="replace")
    newlines = set(re.findall(r"\r\n|\r|\n", text))
    return {"path": rel, "binary": False, "size": len(data), "content": text,
            "version": hashlib.sha256(data).hexdigest(), "editable": bool(encoding) and len(newlines) <= 1}


def _source_encoding(data: bytes) -> str | None:
    for enc in ("utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8", "gbk"):
        try:
            data.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return None


_save_lock = threading.Lock()


def write_file(path: str, rel: str, content: str, version: str | None = None) -> None:
    full = safe_join(path, rel)
    with _save_lock:
        encoding = "utf-8"
        if version is not None:
            with open(full, "rb") as fh:
                original = fh.read(MAX_READ + 1)
            if hashlib.sha256(original).hexdigest() != version:
                raise HubError("磁盘文件已更改。请先复制编辑内容，再重新打开文件核对。")
            encoding = _source_encoding(original)
            endings = set(re.findall(rb"\r\n|\r|\n", original))
            if not encoding or len(endings) > 1:
                raise HubError("此文件请在外部编辑器中保存")
            if endings:
                content = re.sub(r"\r\n|\r|\n", endings.pop().decode("ascii"), content)
        try:
            data = content.encode(encoding)
        except UnicodeEncodeError as e:
            raise HubError(f"原文件编码 {encoding} 无法表示部分字符，请在外部编辑器中转换编码") from e
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(data)


def open_in(path: str, target: str, file: str | None = None, line: int | None = None) -> None:
    where = safe_join(path, file) if file else path
    if target == "vscode":
        code = shutil.which("code")
        if not code:
            raise HubError("没找到 VS Code 的 code 命令")
        args = [code, path] + (["-g", f"{where}:{line or 1}"] if file else [])
        subprocess.Popen(args, creationflags=NO_WINDOW, shell=WIN)
    elif target == "explorer":
        if WIN:
            subprocess.Popen(["explorer", f"/select,{where}"] if file else ["explorer", where])
        else:
            subprocess.Popen(["xdg-open", where])
    elif target == "terminal":
        if WIN:
            wt = shutil.which("wt")
            if wt:
                subprocess.Popen([wt, "-d", path])
            else:
                subprocess.Popen(["cmd", "/c", "start", "cmd", "/K", f"cd /d {path}"])
        else:
            subprocess.Popen(["x-terminal-emulator"], cwd=path)
    else:
        raise HubError(f"未知打开方式：{target}")


# ------------------------------------------------------------------ checks

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PROBLEM_PATTERNS = [
    # Python traceback frame:  File "x.py", line 3, in <module>
    re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+)'),
    # tsc / msbuild:  src/a.ts(3,5): error TS2322: ...
    re.compile(r"^(?P<file>(?:[A-Za-z]:)?[^:()]+?\.\w+)\((?P<line>\d+),(?P<col>\d+)\):\s*(?P<sev>error|warning)\s*(?P<msg>.*)$"),
    # gcc / eslint-unix / kotlinc / ruff:  file:line:col: message
    re.compile(r"^(?:e: |w: )?(?P<file>(?:[A-Za-z]:)?[^:]+?\.\w+):(?P<line>\d+):(?:(?P<col>\d+):?)?\s*(?P<msg>.+)$"),
]
ERROR_HINT = re.compile(r"(error|exception|traceback|failed|fatal|错误|ERR!|cannot find|not found|undefined)", re.I)
CLEAN_SUMMARY = re.compile(r"\b(?:0|no)\s+(?:errors?|exceptions?|failures?|failed)\b", re.I)


class _LogDiagnostics:
    """Parse a stream once, carrying traceback frames between output lines."""
    def __init__(self, root: str):
        self.root = root
        self.frame = None
        self.items = {}

    def feed(self, line: str) -> None:
        s = line.strip()
        for pat in PROBLEM_PATTERNS:
            m = pat.search(s)
            if not m:
                continue
            gd = m.groupdict()
            file = _rel(gd["file"], self.root)
            if file is None:
                return
            if pat is PROBLEM_PATTERNS[0]:
                self.frame = (file, int(gd["line"]))
                return
            msg = gd["msg"].strip()
            sev = gd.get("sev") or ("warning" if s.startswith("w: ") or re.match(r"warn", msg, re.I) else "error")
            row, col = int(gd["line"]), int(gd.get("col") or 1)
            break
        else:
            if not self.frame or not re.match(r"^(?:[\w.]*Error|[\w.]*Exception|KeyboardInterrupt|SystemExit)\b", s):
                return
            file, row = self.frame
            self.frame = None
            msg, sev, col = s, "error", 1
        key = (file, row, col, sev, msg)
        if key not in self.items:
            if len(self.items) >= 20000:
                del self.items[next(iter(self.items))]
            self.items[key] = {"file": file, "line": row, "col": col, "severity": sev,
                               "message": msg, "source": "run"}


def parse_problems(text: str, root: str) -> list[dict]:
    diagnostics = _LogDiagnostics(root)
    for line in text.splitlines():
        diagnostics.feed(ANSI.sub("", line))
    return list(diagnostics.items.values())


def _rel(file: str, root: str) -> str | None:
    f = file.replace("\\", "/")
    if os.path.isabs(file):
        try:
            rel = os.path.relpath(file, root).replace("\\", "/")
        except ValueError:
            return None
        return None if rel.startswith("..") else rel
    return f[2:] if f.startswith("./") else f


def _dedupe(probs: list[dict]) -> list[dict]:
    seen, out = set(), []
    for p in probs:
        key = (p["file"], p["line"], p["message"])
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


CHECKABLE = {".py", ".pyw", ".js", ".mjs", ".cjs", ".json"}


def check_files(root: str, files: list[str], stop: threading.Event | None = None) -> list[dict]:
    """Fast built-in syntax checks: Python (ruff if installed, else compile), JS (node --check), JSON."""
    probs: list[dict] = []
    py = [f for f in files if Path(f).suffix in (".py", ".pyw")]
    js = [f for f in files if Path(f).suffix in (".js", ".mjs", ".cjs")]
    js_files = [f for f in files if Path(f).suffix == ".json"]

    ruff = shutil.which("ruff")
    if py and ruff:
        for chunk in _chunks(py, 200):
            if stop and stop.is_set():
                return probs
            r = subprocess.run([ruff, "check", "--output-format=json", "--no-cache", "--exit-zero",
                                "--select", "E9,F63,F7,F82,F401,F811,F821,F841", *chunk],
                               cwd=root, capture_output=True, creationflags=NO_WINDOW)
            try:
                if r.returncode:
                    probs += _compile_py(root, chunk)
                    continue
                for d in json.loads(r.stdout.decode("utf-8", "replace")):
                    code = d.get("code") or "syntax"
                    sev = "warning" if code in ("F401", "F811", "F841") else "error"
                    probs.append({"file": _rel(d["filename"], root) or d["filename"],
                                  "line": d["location"]["row"], "col": d["location"]["column"],
                                  "severity": sev, "message": f"{code}: {d['message']}",
                                  "source": "ruff"})
            except ValueError:
                py_fallback = chunk
                probs += _compile_py(root, py_fallback)
    elif py:
        probs += _compile_py(root, py)

    node = shutil.which("node")
    if node:
        for f in js:
            if stop and stop.is_set():
                return probs
            r = subprocess.run([node, "--check", f], cwd=root, capture_output=True,
                               creationflags=NO_WINDOW)
            if r.returncode:
                err = r.stderr.decode("utf-8", "replace")
                m = re.search(r":(\d+)\s*$", err.splitlines()[0] if err else "")
                msg = next((ln.strip() for ln in err.splitlines() if "Error" in ln), err.strip()[:200])
                probs.append({"file": f, "line": int(m[1]) if m else 1, "col": 1,
                              "severity": "error", "message": msg, "source": "node"})
    for f in js_files:
        if stop and stop.is_set():
            return probs
        full = os.path.join(root, f)
        try:
            if os.path.getsize(full) > MAX_READ:
                continue
            json.loads(Path(full).read_text("utf-8-sig"))
        except ValueError as e:
            if f.endswith(("tsconfig.json", "jsconfig.json")) or "/.vscode/" in f"/{f}":
                continue  # JSONC allowed
            probs.append({"file": f, "line": getattr(e, "lineno", 1), "col": getattr(e, "colno", 1),
                          "severity": "error", "message": f"JSON: {getattr(e, 'msg', e)}",
                          "source": "json"})
        except OSError:
            pass
    return probs


def _compile_py(root: str, files: list[str]) -> list[dict]:
    out = []
    for f in files:
        try:
            src = Path(root, f).read_bytes()
            compile(src, f, "exec", dont_inherit=True)
        except SyntaxError as e:
            out.append({"file": f, "line": e.lineno or 1, "col": e.offset or 1, "severity": "error",
                        "message": f"{type(e).__name__}: {e.msg}", "source": "python"})
        except (OSError, ValueError):
            pass
    return out


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def run_check_command(proj: dict, timeout: int = 300) -> tuple[int, str, list[dict]]:
    if not proj.get("check"):
        return 0, "", []
    r = subprocess.run(proj["check"], shell=True, cwd=proj["path"], capture_output=True,
                       timeout=timeout, env=_child_env(), creationflags=NO_WINDOW)
    out = ANSI.sub("", (r.stdout + r.stderr).decode("utf-8", "replace"))
    probs = parse_problems(out, proj["path"])
    for p in probs:
        p["source"] = "check"
    if r.returncode and not any(p["severity"] == "error" for p in probs):
        probs.append({"file": "", "line": 0, "col": 0, "severity": "error",
                      "message": f"检查命令退出码 {r.returncode}：{out.strip().splitlines()[-1] if out.strip() else ''}",
                      "source": "check"})
    return r.returncode, out, probs


def full_check(proj: dict, with_command: bool = True) -> dict:
    files = [f for f in list_files(proj["path"]) if Path(f).suffix in CHECKABLE]
    t0 = time.time()
    probs = check_files(proj["path"], files)
    output = ""
    if with_command and proj.get("check"):
        _, output, cmd_probs = run_check_command(proj)
        probs += cmd_probs
    return {"problems": _dedupe(probs), "files": len(files), "seconds": round(time.time() - t0, 2),
            "output": output[-20000:]}


def _child_env() -> dict:
    return {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "FORCE_COLOR": "0",
            "NO_COLOR": "1"}


# ----------------------------------------------------------------- watcher

class Watcher:
    """Polls a project's files and re-checks changed ones; keeps the live problem list."""

    def __init__(self, proj: dict):
        self.proj = proj
        self.problems: dict[str, list[dict]] = {}
        self.version = 0
        self.last_run = 0.0
        self.state = "starting"
        self._mtimes: dict[str, float] = {}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict:
        with self._lock:
            flat = [p for ps in self.problems.values() for p in ps]
            version, state, checked_at = self.version, self.state, self.last_run
        flat.sort(key=lambda p: (p["severity"] != "error", p["file"], p["line"]))
        return {"version": version, "state": state, "problems": flat, "checkedAt": checked_at}

    def _scan(self) -> dict[str, float]:
        if not os.path.isdir(self.proj["path"]):
            raise HubError("项目目录不存在")
        res = {}
        for f in list_files(self.proj["path"]):
            if Path(f).suffix in CHECKABLE:
                try:
                    res[f] = os.path.getmtime(os.path.join(self.proj["path"], f))
                except OSError:
                    pass
        return res

    def _apply(self, files: list[str], probs: list[dict], removed: list[str]) -> None:
        with self._lock:
            for f in files + removed:
                self.problems.pop(f, None)
            for p in probs:
                self.problems.setdefault(p["file"], []).append(p)
            self.version += 1
            self.last_run = time.time()
            self.state = "watching"

    def _loop(self) -> None:
        last_list = 0.0
        while not self._stop.is_set():
            try:
                if time.time() - last_list > 1.5:
                    mt = self._scan()
                    last_list = time.time()
                    changed = [f for f, m in mt.items() if self._mtimes.get(f) != m]
                    removed = [f for f in self._mtimes if f not in mt]
                    if changed or removed or not self.last_run:
                        with self._lock:
                            self.state = "checking"
                        self._apply(changed, check_files(self.proj["path"], changed, self._stop), removed)
                        self._mtimes = mt
                    else:
                        with self._lock:
                            self.state = "watching"
            except Exception as e:  # keep watching even if one round fails
                with self._lock:
                    self.state = f"error: {e}"
            self._stop.wait(0.5)


# ------------------------------------------------------------------ runner

class Run:
    """A long-running command with captured output (stdout+stderr merged)."""

    def __init__(self, proj: dict, command: str):
        self.id = uuid.uuid4().hex[:8]
        self.proj_id = proj["id"]
        self.root = proj["path"]
        self.command = command
        self.lines: list[dict] = []
        self.first_line = 0
        self.diagnostics = _LogDiagnostics(self.root)
        self.stopped = False
        self.finished = threading.Event()
        self.started = time.time()
        self.ended: float | None = None
        self.code: int | None = None
        self._lock = threading.Lock()
        self.proc = subprocess.Popen(command, shell=True, cwd=self.root, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     env=_child_env(), creationflags=NO_WINDOW, start_new_session=not WIN)
        self._eof = threading.Event()
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._wait_exit, daemon=True).start()

    # After the command exits, wait this long for the remaining output before calling it finished.
    # A background child (``start /b``, a daemonised server) can keep the pipe open indefinitely;
    # its later output is still collected, but it must not keep the run "running" or block Stop.
    LOG_GRACE = 2.0

    def _wait_exit(self) -> None:
        code = self.proc.wait()
        self._eof.wait(self.LOG_GRACE)
        with self._lock:
            self.code = code
            self.ended = time.time()
            self.finished.set()

    def _pump(self) -> None:
        assert self.proc.stdout
        for raw in iter(self.proc.stdout.readline, b""):
            for enc in ("utf-8", "gbk"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                text = raw.decode("utf-8", "replace")
            text = ANSI.sub("", text.rstrip("\r\n"))
            with self._lock:
                self.lines.append({"t": text, "err": bool(ERROR_HINT.search(CLEAN_SUMMARY.sub("", text)))})
                self.diagnostics.feed(text)
                if len(self.lines) > 20000:
                    del self.lines[:5000]
                    self.first_line += 5000
        self.proc.stdout.close()
        self._eof.set()

    def stop(self) -> None:
        if self.finished.is_set():
            return
        if WIN:
            if self.proc.poll() is None:
                with self._lock:
                    result = subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                                            capture_output=True, creationflags=NO_WINDOW)
                    self.stopped = result.returncode == 0
                    if not self.stopped and self.proc.poll() is None:
                        raise HubError("停止进程失败，请重试")
        else:
            with self._lock:
                try:
                    os.killpg(self.proc.pid, signal.SIGTERM)
                    self.stopped = True
                except ProcessLookupError:
                    pass
            if not self.finished.wait(1):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if not self.finished.wait(5):
            raise HubError("仍在等待进程结束和日志收齐，请稍后重试")

    @property
    def running(self) -> bool:
        return not self.finished.is_set()

    def output(self, start: int = 0) -> dict:
        with self._lock:
            total = self.first_line + len(self.lines)
            start = min(max(self.first_line, start), total)
            lines = self.lines[start - self.first_line:]
            problems = list(self.diagnostics.items.values())
            running, code, ended, stopped = self.running, self.code, self.ended, self.stopped
        if code and not stopped and not any(p["severity"] == "error" for p in problems):
            problems.append({"file": "", "line": 0, "col": 0, "severity": "error",
                             "message": f"进程退出码 {code}，详见运行输出", "source": "run"})
        return {"id": self.id, "command": self.command, "running": running, "code": code,
                "started": self.started, "ended": ended, "stopped": stopped, "lines": lines,
                "from": start, "next": total, "problems": problems}


def run_once(proj: dict, command: str | None = None, timeout: float = 30) -> dict:
    """Run a command for at most ``timeout`` seconds and report output + problems.

    Long-running servers are fine: if still alive at the timeout and no error was seen, the
    result reports ``alive: true`` and the process is stopped.
    """
    cmd = command or proj.get("run")
    if not cmd:
        raise HubError("项目没有配置运行命令，用 --cmd 指定")
    r = Run(proj, cmd)
    alive = not r.finished.wait(timeout)
    if alive:
        r.stop()
    out = r.output()
    out["alive"] = alive
    out["ok"] = (alive or out["code"] == 0) and not any(p["severity"] == "error" for p in out["problems"])
    return out
