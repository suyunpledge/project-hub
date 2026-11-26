"""Project Hub MCP server (stdio, JSON-RPC 2.0, standard library only).

Register it with an agent, e.g. Claude Code:
    claude mcp add project-hub -- python C:\\path\\to\\project-hub\\hub.py mcp
or Codex (~/.codex/config.toml):
    [mcp_servers.project-hub]
    command = "python"
    args = ["C:\\path\\to\\project-hub\\hub.py", "mcp"]

GitHub tools only work after the user authorized GitHub in Project Hub, and only for the
actions granted there (create repositories / push).
"""
from __future__ import annotations

import json
import sys
import traceback

from . import core, github, manage, search

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER = {"name": "project-hub", "title": "Project Hub", "version": "1.1.0"}

S = {"type": "string"}
B = {"type": "boolean"}
I = {"type": "integer"}
ARR = {"type": "array", "items": {"type": "string"}}
PROJECT = {"type": "string", "description": "项目引用：id:<id>、项目 id、显示名或目录路径"}


def _tool(name, title, description, props, required=(), read_only=False, destructive=False, open_world=False):
    return {"name": name, "title": title, "description": description,
            "inputSchema": {"type": "object", "properties": props, "required": list(required),
                            "additionalProperties": False},
            "annotations": {"readOnlyHint": read_only, "destructiveHint": destructive,
                            "openWorldHint": open_world, "idempotentHint": read_only}}


TOOLS = [
    _tool("hub_search", "查找项目",
          "按关键词（名称/标签/备注/分组/路径，全部命中）、分组、标签、类型查找已登记项目，按相关度排序；"
          "精确匹配不到时返回模糊匹配（fuzzy=true）。返回的 ref 可直接传给其他工具。",
          {"query": S, "group": S, "tags": ARR, "kind": {"type": "string", "description": "python/node/web/git/…"},
           "missing": {**B, "description": "只看目录已失效的登记"}}, read_only=True),
    _tool("hub_grep", "跨项目搜索代码",
          "在多个项目的文件内容中搜索（git grep，包含未跟踪文件，遵守 .gitignore）。",
          {"pattern": S, "projects": {**ARR, "description": "限定项目引用；省略则按 group/tags 选择，再省略则全部"},
           "group": S, "tags": ARR, "regex": B, "ignoreCase": B,
           "globs": {**ARR, "description": "文件通配，如 *.py"}, "maxHits": {**I, "description": "每个项目最多返回的行数"}},
          ["pattern"], read_only=True),
    _tool("hub_overview", "项目总览",
          "所有项目的分支、未提交改动数、领先/落后上游、最近提交、远端；可按分组过滤。",
          {"group": S}, read_only=True),
    _tool("hub_info", "项目详情", "项目配置、类型、标签、远端地址、README 开头和最近提交。",
          {"project": PROJECT}, ["project"], read_only=True),
    _tool("hub_status", "Git 状态", "当前分支、分支列表和未提交改动。", {"project": PROJECT}, ["project"], read_only=True),
    _tool("hub_commit", "提交到分支",
          "把工作区文件提交到指定分支。分支不是当前分支时不会切换工作区；分支不存在会从 HEAD 新建。"
          "建议列出自己改过的 files。",
          {"project": PROJECT, "message": S, "branch": S, "files": ARR, "base": S, "author": {**S, "description": "Name <email>"}},
          ["project", "message"]),
    _tool("hub_check", "检查错误", "内置语法检查（Python/JS/JSON）+ 项目配置的检查命令。",
          {"project": PROJECT, "runCommand": {**B, "description": "是否运行项目的检查命令，默认 true"}},
          ["project"], read_only=True),
    _tool("hub_run", "运行并捕获报错", "运行项目命令最多 timeout 秒，返回输出尾部和解析出的报错位置。",
          {"project": PROJECT, "command": S, "timeout": {"type": "number"}}, ["project"]),
    _tool("hub_push", "推送分支",
          "推送本地分支到已配置的远端。先 dryRun=true 预演拿到 previewId，再带 previewId 正式推送。"
          "推送 github.com 远端需要用户已授权「推送」。",
          {"project": PROJECT, "branch": S, "remote": S, "to": S, "dryRun": B, "previewId": S},
          ["project"], destructive=True, open_world=True),
    _tool("github_status", "GitHub 授权状态", "是否已连接 GitHub、账号、以及用户授予 Agent 的权限（建仓 / 推送）。",
          {}, read_only=True, open_world=True),
    _tool("github_create_repo", "新建 GitHub 仓库",
          "在用户的 GitHub 账号（或 org）下新建一个空仓库。需要用户授权「创建仓库」。默认私有。",
          {"name": S, "private": B, "description": S, "org": S}, ["name"], open_world=True),
    _tool("github_publish_project", "发布项目到 GitHub",
          "为已登记项目新建 GitHub 仓库（或接到已有仓库 existing=true），添加远端并推送当前分支。"
          "可重复调用：远端已存在时直接推送更新。需要用户授权「推送」（新建时还需「创建仓库」）。",
          {"project": PROJECT, "repoName": S, "private": B, "description": S, "org": S,
           "remote": S, "branch": S, "existing": B}, ["project"], open_world=True),
]


def _guard_github_push(project: str) -> None:
    """Pushing a github.com remote through MCP needs the user's push grant."""
    p = core.find_project(project)
    urls = core.git(p["path"], "remote", "-v", check=False)
    if "github.com" in urls:
        github.require("push")


def _call(name: str, a: dict):
    if name == "hub_search":
        return search.search_projects(a.get("query", ""), a.get("group"), a.get("tags") or [], a.get("kind"),
                                      bool(a.get("missing")))
    if name == "hub_grep":
        return search.grep(a["pattern"], a.get("projects"), a.get("group"), a.get("tags"), bool(a.get("regex")),
                           a.get("ignoreCase", True), a.get("globs"), a.get("maxHits", 50))
    if name == "hub_overview":
        return manage.overview(a.get("group"))
    if name == "hub_info":
        p = core.find_project(a["project"])
        isgit = core.is_git(p["path"])
        remotes = core.git(p["path"], "remote").split() if isgit else []
        return {**p, "kinds": core.detect(p["path"])["kinds"], "remoteTargets": core.remote_targets(p["path"], remotes),
                "readme": core.readme_head(p["path"]),
                "recent": [c["subject"] for c in core.git_log(p["path"], n=5)] if isgit else []}
    if name == "hub_status":
        return core.git_info(core.find_project(a["project"])["path"])
    if name == "hub_commit":
        p = core.find_project(a["project"])
        return core.commit_to_branch(p["path"], a.get("branch", ""), a["message"], a.get("files") or None,
                                     a.get("base"), a.get("author"))
    if name == "hub_check":
        r = core.full_check(core.find_project(a["project"]), with_command=a.get("runCommand", True))
        r["ok"] = not any(x["severity"] == "error" for x in r["problems"])
        return r
    if name == "hub_run":
        r = core.run_once(core.find_project(a["project"]), a.get("command"), float(a.get("timeout", 30)))
        r["lines"] = r["lines"][-120:]
        return r
    if name == "hub_push":
        if not a.get("dryRun"):
            _guard_github_push(a["project"])
        return core.push_project(core.find_project(a["project"]), a.get("branch"), a.get("remote"), a.get("to"),
                                 bool(a.get("dryRun")), a.get("previewId"))
    if name == "github_status":
        return github.status()
    if name == "github_create_repo":
        return github.create_repo(a["name"], a.get("private"), a.get("description", ""), a.get("org"))
    if name == "github_publish_project":
        return github.publish(a["project"], a.get("repoName"), a.get("private"), a.get("description"), a.get("org"),
                              a.get("remote", "origin"), a.get("branch"), bool(a.get("existing")))
    raise KeyError(name)


def _result(payload, is_error=False) -> dict:
    text = json.dumps(payload, ensure_ascii=False, indent=1, default=str)
    res = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if isinstance(payload, dict):
        res["structuredContent"] = payload
    else:
        res["structuredContent"] = {"items": payload}
    return res


def handle(msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:  # notification (initialized, cancelled, …)
        return None
    try:
        if method == "initialize":
            wanted = (msg.get("params") or {}).get("protocolVersion")
            return _ok(mid, {"protocolVersion": wanted if wanted in PROTOCOLS else PROTOCOLS[0],
                             "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER,
                             "instructions": "管理本机已登记项目：查找、跨项目搜代码、提交到分支、检查/运行、推送；"
                                             "用户授权后可新建 GitHub 仓库并发布项目。项目引用优先用 hub_search 返回的 ref。"})
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            return _ok(mid, {"tools": TOOLS})
        if method == "tools/call":
            params = msg.get("params") or {}
            name, args = params.get("name"), params.get("arguments") or {}
            if name not in {t["name"] for t in TOOLS}:
                return _err(mid, -32602, f"Unknown tool: {name}")
            try:
                return _ok(mid, _result(_call(name, args)))
            except core.HubError as e:
                return _ok(mid, _result({"error": str(e), **e.details}, True))
            except (KeyError, TypeError, ValueError) as e:
                return _ok(mid, _result({"error": f"参数错误：{type(e).__name__}: {e}"}, True))
        return _err(mid, -32601, f"Method not found: {method}")
    except Exception as e:  # never let one request kill the server
        traceback.print_exc(file=sys.stderr)
        return _err(mid, -32603, f"{type(e).__name__}: {e}")


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve() -> None:
    """Newline-delimited JSON-RPC on stdin/stdout. Logs go to stderr only."""
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            reply = _err(None, -32700, "Parse error")
        else:
            if isinstance(msg, list):  # batch
                replies = [r for r in (handle(m) for m in msg if isinstance(m, dict)) if r]
                reply = replies or None
            else:
                reply = handle(msg) if isinstance(msg, dict) else _err(None, -32600, "Invalid Request")
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
            stdout.flush()


def config_snippets(python: str, script: str) -> dict:
    return {
        "claude": f'claude mcp add project-hub -- "{python}" "{script}" mcp',
        "codex": f'[mcp_servers.project-hub]\ncommand = "{python.replace(chr(92), "/")}"\n'
                 f'args = ["{script.replace(chr(92), "/")}", "mcp"]',
        "json": json.dumps({"mcpServers": {"project-hub": {"command": python, "args": [script, "mcp"]}}},
                           ensure_ascii=False, indent=2),
    }
