"""GitHub connection: authorization, repository creation and publishing projects.

Authorization is explicit and stored in ``data/github.json``:

* ``mode = "git-credential"`` — reuse the GitHub login Git already has (Git Credential Manager);
  no token is copied into the Hub.
* ``mode = "token"`` — a personal access token the user pasted; on Windows it is encrypted with
  DPAPI (only this Windows user can decrypt it).
* The ``GITHUB_TOKEN`` / ``GH_TOKEN`` environment variable overrides both (CI, scripts).

``grants`` decides what agents may do through the MCP server: ``createRepo`` and ``push``.
Nothing here ever puts a token on a command line or in an error message.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

from . import core

API = os.environ.get("HUB_GITHUB_API", "https://api.github.com")  # overridable for tests
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def _config_file():
    return core.DATA_DIR / "github.json"


# ---------------------------------------------------------------- secret storage (DPAPI)

def _protect(data: bytes) -> str:
    if os.name != "nt":
        return "plain:" + base64.b64encode(data).decode()
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    src = Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    out = Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(src), "ProjectHub", None, None, None, 0x1,
                                                  ctypes.byref(out)):
        raise core.HubError("无法加密保存令牌（DPAPI 失败）")
    try:
        return "dpapi:" + base64.b64encode(ctypes.string_at(out.pbData, out.cbData)).decode()
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _unprotect(text: str) -> bytes:
    kind, _, payload = text.partition(":")
    raw = base64.b64decode(payload)
    if kind == "plain":
        return raw
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    src = Blob(len(raw), ctypes.cast(ctypes.create_string_buffer(raw, len(raw)), ctypes.POINTER(ctypes.c_char)))
    out = Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 0x1,
                                                    ctypes.byref(out)):
        raise core.HubError("无法解密已保存的令牌，请重新连接 GitHub", code="github_token_unreadable")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _load() -> dict:
    try:
        return json.loads(_config_file().read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _save(cfg: dict) -> None:
    core.DATA_DIR.mkdir(parents=True, exist_ok=True)
    f = _config_file()
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, f)
    if os.name != "nt":
        os.chmod(f, 0o600)


# ---------------------------------------------------------------- tokens

def _git_credential() -> str | None:
    """Ask Git's credential helper for the github.com login (no prompt; nothing is stored)."""
    try:
        r = subprocess.run(["git", "credential", "fill"], input=b"protocol=https\nhost=github.com\n\n",
                           capture_output=True, timeout=20, creationflags=core.NO_WINDOW,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode:
        return None
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("password="):
            return line[9:] or None
    return None


def token(required: bool = True) -> str | None:
    env = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if env:
        return env
    cfg = _load()
    tok = None
    if cfg.get("mode") == "token" and cfg.get("token"):
        tok = _unprotect(cfg["token"]).decode()
    elif cfg.get("mode") == "git-credential":
        tok = _git_credential()
    if not tok and required:
        raise core.HubError("还没有连接 GitHub。请在 Project Hub 的「GitHub」设置中授权，"
                            "或运行 python hub.py github connect。", code="github_not_connected")
    return tok


def _request(method: str, path: str, body: dict | None = None, tok: str | None = None) -> tuple[dict, dict]:
    tok = tok or token()
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ProjectHub",
                                          **({"Content-Type": "application/json"} if body is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
            return (json.loads(data) if data else {}), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}")
        except ValueError:
            detail = {}
        msg = detail.get("message", e.reason)
        errors = "; ".join(x.get("message") or x.get("code", "") for x in detail.get("errors", []) if isinstance(x, dict))
        code = {401: "github_unauthorized", 403: "github_forbidden", 404: "github_not_found",
                422: "github_invalid"}.get(e.code, "github_error")
        hint = {401: "令牌无效或已过期，请重新连接 GitHub。",
                403: "令牌权限不足（需要 repo / Contents 读写 + Administration 写权限），或触发了频率限制。",
                404: "找不到该资源，或令牌无权访问它。"}.get(e.code, "")
        raise core.HubError(f"GitHub {e.code}：{msg}{('（' + errors + '）') if errors else ''}。{hint}".rstrip("。") + "。",
                            code=code, status=e.code) from None
    except urllib.error.URLError as e:
        raise core.HubError(f"无法连接 GitHub：{e.reason}", code="github_offline") from None


# ---------------------------------------------------------------- connection

DEFAULT_GRANTS = {"createRepo": True, "push": True}


def status(verify: bool = True) -> dict:
    cfg = _load()
    env = bool(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    res = {"connected": bool(cfg.get("mode")) or env, "mode": "env" if env else cfg.get("mode"),
           "login": cfg.get("login"), "grants": {**DEFAULT_GRANTS, **cfg.get("grants", {})} if cfg or env else
           {"createRepo": False, "push": False}, "defaultPrivate": cfg.get("defaultPrivate", True)}
    if res["connected"] and verify:
        try:
            user, headers = _request("GET", "/user")
            res.update(login=user.get("login"), name=user.get("name"), avatar=user.get("avatar_url"), ok=True,
                       scopes=[s.strip() for s in headers.get("X-OAuth-Scopes", "").split(",") if s.strip()])
        except core.HubError as e:
            res.update(ok=False, error=str(e), code=e.details.get("code"))
    return res


def connect(mode: str, tok: str | None = None, grants: dict | None = None,
            default_private: bool = True) -> dict:
    """Authorize the Hub (and agents using it) to act on GitHub. Verifies before saving."""
    if mode not in ("token", "git-credential"):
        raise core.HubError("授权方式只能是 token 或 git-credential")
    if mode == "token":
        tok = (tok or "").strip()
        if not re.match(r"^(ghp_|github_pat_|gho_|ghu_|ghs_)?[A-Za-z0-9_]{20,255}$", tok):
            raise core.HubError("令牌格式不正确，请粘贴完整的 GitHub 个人访问令牌")
    else:
        tok = _git_credential()
        if not tok:
            raise core.HubError("Git 中没有可用的 GitHub 登录。请先用 git 推送一次任意 GitHub 仓库完成登录，"
                                "或改用个人访问令牌。", code="github_no_git_credential")
    user, _ = _request("GET", "/user", tok=tok)
    cfg = {"mode": mode, "login": user.get("login"),
           "grants": {**DEFAULT_GRANTS, **{k: bool(v) for k, v in (grants or {}).items() if k in DEFAULT_GRANTS}},
           "defaultPrivate": bool(default_private)}
    if mode == "token":
        cfg["token"] = _protect(tok.encode())
    _save(cfg)
    return status(verify=False) | {"login": user.get("login"), "ok": True}


def disconnect() -> None:
    try:
        _config_file().unlink()
    except FileNotFoundError:
        pass


def set_grants(grants: dict | None = None, default_private: bool | None = None) -> dict:
    cfg = _load()
    if not cfg.get("mode"):
        raise core.HubError("还没有连接 GitHub", code="github_not_connected")
    cfg["grants"] = {**DEFAULT_GRANTS, **cfg.get("grants", {}),
                     **{k: bool(v) for k, v in (grants or {}).items() if k in DEFAULT_GRANTS}}
    if default_private is not None:
        cfg["defaultPrivate"] = bool(default_private)
    _save(cfg)
    return status(verify=False)


def require(grant: str) -> None:
    st = status(verify=False)
    if not st["connected"]:
        raise core.HubError("还没有连接 GitHub，请在 Project Hub 中授权后重试。", code="github_not_connected")
    if not st["grants"].get(grant):
        label = {"createRepo": "创建仓库", "push": "推送"}[grant]
        raise core.HubError(f"用户没有授权 Agent「{label}」。请在 Project Hub 的 GitHub 设置中开启后重试。",
                            code="github_not_granted", grant=grant)


# ---------------------------------------------------------------- git credentials for push

def _is_github(url: str) -> bool:
    return bool(re.match(r"^(https://([^@/]*@)?github\.com/|git@github\.com:|ssh://git@github\.com/)", url))


def push_env(path: str) -> dict:
    """Environment that lets ``git push`` to github.com authenticate with the stored token.
    Empty when not connected via token/env, or the project has no https github.com remote."""
    cfg = _load()
    env_tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not env_tok and cfg.get("mode") != "token":
        return {}  # git-credential mode: Git's own helper already authenticates
    try:
        urls = core.git(path, "remote", "-v", check=False)
    except Exception:
        return {}
    if "https://github.com/" not in urls and "@github.com/" not in urls:
        return {}
    try:
        tok = token(required=False)
    except core.HubError:
        return {}
    if not tok:
        return {}
    auth = base64.b64encode(f"x-access-token:{tok}".encode()).decode()
    n = int(os.environ.get("GIT_CONFIG_COUNT", "0") or 0)
    return {"GIT_CONFIG_COUNT": str(n + 1), f"GIT_CONFIG_KEY_{n}": "http.https://github.com/.extraheader",
            f"GIT_CONFIG_VALUE_{n}": f"AUTHORIZATION: basic {auth}"}


# ---------------------------------------------------------------- repositories

def create_repo(name: str, private: bool | None = None, description: str = "", org: str | None = None,
                *, check_grant: bool = True) -> dict:
    if check_grant:
        require("createRepo")
    if not NAME_RE.match(name or "") or name in (".", ".."):
        raise core.HubError("仓库名只能包含字母、数字、. _ -，且不超过 100 个字符", code="github_bad_name")
    if private is None:
        private = _load().get("defaultPrivate", True)
    body = {"name": name, "description": (description or "")[:350], "private": bool(private), "auto_init": False}
    path = f"/orgs/{org}/repos" if org else "/user/repos"
    try:
        repo, _ = _request("POST", path, body)
    except core.HubError as e:
        if e.details.get("status") == 422 and "already exists" in str(e):
            raise core.HubError(f"GitHub 上已存在仓库 {org or _load().get('login') or ''}/{name}。"
                                "换一个名字，或用 publish --existing 把项目接到这个已有仓库。",
                                code="github_repo_exists", name=name) from None
        raise
    return _repo_summary(repo)


def _repo_summary(repo: dict) -> dict:
    return {"fullName": repo.get("full_name"), "url": repo.get("html_url"), "cloneUrl": repo.get("clone_url"),
            "private": repo.get("private"), "defaultBranch": repo.get("default_branch")}


def get_repo(full_name: str) -> dict:
    repo, _ = _request("GET", f"/repos/{full_name}")
    return _repo_summary(repo)


def _slug_repo_name(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")[:100]
    return slug or "project"


def publish(ref: str, repo_name: str | None = None, private: bool | None = None, description: str | None = None,
            org: str | None = None, remote: str = "origin", branch: str | None = None,
            existing: bool = False, *, check_grant: bool = True) -> dict:
    """Create a GitHub repository for a registered project (or attach an existing one),
    add it as a remote and push the branch. Safe to re-run: an attached remote is reused."""
    if check_grant:
        require("push")
    p = core.find_project(ref)
    path = p["path"]
    if not core.is_git(path):
        raise core.HubError("项目还不是 git 仓库。先初始化并提交一次，再发布。", code="not_git")
    branch = branch or core.git(path, "branch", "--show-current", check=False).strip()
    if not branch:
        raise core.HubError("当前处于 detached HEAD，请用 --branch 指定要发布的分支")
    if not core.git(path, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).strip():
        raise core.HubError(f"分支 {branch} 还没有提交。先提交一次再发布。", code="no_commits")
    remotes = core.git(path, "remote").split()
    created = None
    url = core.git(path, "remote", "get-url", remote, check=False).strip() if remote in remotes else ""
    if url and not _is_github(url):
        raise core.HubError(f"远端 {remote} 已指向其他地址（{core._display_url(url)}）。用 --remote 指定另一个远端名。",
                            code="remote_taken")
    if not url:
        name = repo_name or _slug_repo_name(p["name"])
        owner = org or status(verify=False).get("login") or ""
        if existing:
            info = get_repo(f"{owner}/{name}")
        else:
            if check_grant:
                require("createRepo")
            info = create_repo(name, private, description if description is not None else p.get("description", ""),
                               org, check_grant=False)
            created = info
        url = info["cloneUrl"]
        core.git(path, "remote", "add", remote, url)
    result = core.push_project(p, branch, remote, None, False, None)
    full = re.sub(r"^.*github\.com[/:]", "", url).removesuffix(".git")
    return {"project": p["id"], "repo": full, "url": f"https://github.com/{full}", "remote": remote,
            "branch": branch, "sha": result.get("sha"), "created": bool(created),
            "upToDate": result.get("upToDate"), "upstreamSet": result.get("upstreamSet")}


if __name__ == "__main__":  # tiny manual check: python -m hub.github
    print(json.dumps(status(), ensure_ascii=False, indent=2), file=sys.stderr)
