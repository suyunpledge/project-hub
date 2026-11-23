#!/usr/bin/env python3
"""Project Hub command line — for people and agents.

    python hub.py desktop                              # 桌面应用窗口
    python hub.py install-shortcuts                    # 桌面 + 开始菜单快捷方式
    python hub.py serve [--port 8790] [--open]         # 只启动服务，用浏览器访问
    python hub.py list [--group G] [--ungrouped]
    python hub.py find [关键词 ...] [--group G] [--tag 标签] [--kind python] [--missing]
    python hub.py tag <project> [--add 标签] [--remove 标签]
    python hub.py push <project> [-b 分支] [--remote 远端] [--to 目标分支] [--dry-run]
    python hub.py groups
    python hub.py info <project>                       # 类型 / README 开头 / 最近提交（分类参考）
    python hub.py add <path> [--group 名称] [--name 名称]
    python hub.py scan <root> [--depth 2] [--add] [--group 名称]
    python hub.py group <name> <project>... [--color #hex] [--desc "说明"]
    python hub.py group-edit <group> [--rename N] [--color C] [--desc D] [--delete]
    python hub.py ungroup <project>...
    python hub.py status <project>
    python hub.py commit <project> -m "msg" [-b branch] [--base ref] [--author "N <e>"] [files...]
    python hub.py check <project> [--no-command]      # exit 1 on errors
    python hub.py run <project> [--cmd "..."] [--timeout 30]   # exit 1 on errors
    python hub.py set <project> run|check|preview|description|name "value"

<project> may be the project id, its name, or its path. Add --json for machine output.
"""
from __future__ import annotations

import argparse
import json
import sys

from hub import core

if sys.stdout:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def out(obj, as_json: bool, text: str | None = None) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2) if as_json or text is None else text)


def fmt_problems(probs: list[dict]) -> str:
    if not probs:
        return "✓ 没有发现问题"
    lines = [f"{'✗' if p['severity'] == 'error' else '!'} {p['file']}:{p['line']}:{p['col']}  "
             f"{p['message']}  [{p['source']}]" for p in probs]
    n_err = sum(p["severity"] == "error" for p in probs)
    return "\n".join(lines) + f"\n\n{n_err} 个错误，{len(probs) - n_err} 个警告"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="hub", description="本机项目仓库")
    ap.add_argument("--json", action="store_true", help="JSON 输出（给 Agent 用）")
    ap.add_argument("--data", help="登记数据目录（默认 project-hub/data）")
    sp = ap.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("desktop", help="以桌面应用窗口打开")
    s.add_argument("--port", type=int, default=8790)
    sp.add_parser("install-shortcuts", help="创建桌面和开始菜单快捷方式")
    s = sp.add_parser("serve", help="只启动服务")
    s.add_argument("--port", type=int, default=8790)
    s.add_argument("--open", action="store_true")
    s = sp.add_parser("list", help="列出项目")
    s.add_argument("--group", help="只列出该分组（id 或名称）")
    s.add_argument("--ungrouped", action="store_true", help="只列出未分组项目")
    s = sp.add_parser("find", help="按关键词、分组、标签或类型搜索已登记项目")
    s.add_argument("keywords", nargs="*", help="同时匹配所有关键词")
    s.add_argument("--group", help="分组 ID 或名称")
    s.add_argument("--tag", action="append", default=[], help="必需的标签，可重复")
    s.add_argument("--kind", help="项目类型，如 python、node、web、git")
    s.add_argument("--missing", action="store_true", help="仅显示目录已失效的登记")
    s = sp.add_parser("tag", help="查看或增减项目标签")
    s.add_argument("project")
    s.add_argument("--add", action="append", default=[], help="添加一个标签，可重复")
    s.add_argument("--remove", action="append", default=[], help="移除一个标签，可重复")
    sp.add_parser("groups", help="列出分组")
    s = sp.add_parser("info", help="项目概况")
    s.add_argument("project")
    s = sp.add_parser("add", help="登记项目")
    s.add_argument("path")
    s.add_argument("--group", default="default")
    s.add_argument("--name")
    s = sp.add_parser("scan", help="扫描目录找项目")
    s.add_argument("root")
    s.add_argument("--depth", type=int, default=2)
    s.add_argument("--add", action="store_true")
    s.add_argument("--group", default="default")
    s = sp.add_parser("group", help="把项目放进分组（不存在则创建）")
    s.add_argument("name")
    s.add_argument("projects", nargs="*")
    s.add_argument("--color")
    s.add_argument("--desc", dest="description")
    s = sp.add_parser("group-edit", help="改名 / 改色 / 写说明 / 删除分组")
    s.add_argument("group")
    s.add_argument("--rename")
    s.add_argument("--color")
    s.add_argument("--desc", dest="description")
    s.add_argument("--delete", action="store_true")
    s = sp.add_parser("ungroup", help="移回「未分组」")
    s.add_argument("projects", nargs="+")
    s = sp.add_parser("status", help="分支与未提交改动")
    s.add_argument("project")
    s = sp.add_parser("commit", help="提交到指定分支")
    s.add_argument("project")
    s.add_argument("-m", "--message", required=True)
    s.add_argument("-b", "--branch", default="")
    s.add_argument("--base")
    s.add_argument("--author")
    s.add_argument("--allow-empty", action="store_true")
    s.add_argument("files", nargs="*")
    s = sp.add_parser("push", help="将本地分支的已提交内容推送到已配置的远端")
    s.add_argument("project")
    s.add_argument("-b", "--branch", help="本地分支，默认当前分支")
    s.add_argument("--remote", help="Git 远端名称")
    s.add_argument("--to", help="目标分支，默认与本地分支同名")
    mode = s.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="连接远端预演推送并记录目标状态")
    mode.add_argument("--preview-id", help="按指定的成功预演核对并推送")
    s = sp.add_parser("check", help="静态/语法检查")
    s.add_argument("project")
    s.add_argument("--no-command", action="store_true")
    s = sp.add_parser("run", help="运行并捕获报错")
    s.add_argument("project")
    s.add_argument("--cmd", dest="command")
    s.add_argument("--timeout", type=float, default=30)
    s = sp.add_parser("set", help="修改项目配置")
    s.add_argument("project")
    s.add_argument("field", choices=["run", "check", "preview", "description", "name"])
    s.add_argument("value")
    # Keep the documented trailing flag and preserve a global --json before the command.
    for command in sp.choices.values():
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                             help="JSON 输出（给 Agent 用）")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    if a.data:
        core.DATA_DIR = core.Path(a.data).resolve()
        core.REGISTRY_FILE = core.DATA_DIR / "hub.json"
    j = a.json
    try:
        if a.cmd == "desktop":
            from hub.desktop import main as desktop
            desktop(a.port)
        elif a.cmd == "install-shortcuts":
            from hub.desktop import install_shortcuts
            made = install_shortcuts()
            out(made, j, "已创建快捷方式：\n" + "\n".join(made))
        elif a.cmd == "serve":
            from hub.server import serve
            serve(a.port, a.open)
        elif a.cmd == "list":
            reg = core.load_registry()
            names = {g["id"]: g["name"] for g in reg["groups"]}
            ps = reg["projects"]
            if a.group:
                group = core.find_group(reg, a.group)
                if group is None:
                    raise core.HubError(f"找不到分组：{a.group}")
                ps = [p for p in ps if p["group"] == group["id"]]
            if a.ungrouped:
                ps = [p for p in ps if p["group"] == "default"]
            rows = [f"[{names.get(p['group'], p['group'])}] {p['id']:<24} {p['path']}"
                    for p in sorted(ps, key=lambda p: (p["group"], p["id"]))]
            out(ps, j, "\n".join(rows) or "（没有项目）")
        elif a.cmd == "find":
            matches = core.search_projects(" ".join(a.keywords), a.group, a.tag, a.kind, a.missing)
            rows = [f"[{p['groupName']}] {p['ref']}  {p['name']}  {p['path']}"
                    + ("  [目录缺失]" if not p["exists"] else "")
                    + ("  #" + " #".join(p["tags"]) if p["tags"] else "") for p in matches]
            out(matches, j, "\n".join(rows) or "没有匹配的项目")
        elif a.cmd == "tag":
            p = core.edit_tags(a.project, a.add, a.remove) if a.add or a.remove else core.find_project(a.project)
            tags = p.get("tags", [])
            out({"id": p["id"], "tags": tags}, j, f"{p['id']}  " + ("、".join(tags) or "（无标签）"))
        elif a.cmd == "groups":
            reg = core.load_registry()
            res = [{**g, "count": sum(p["group"] == g["id"] for p in reg["projects"])}
                   for g in reg["groups"]]
            out(res, j, "\n".join(f"{g['id']:<20} {g['name']}  ({g['count']} 个)"
                                  + (f"  — {g['description']}" if g.get("description") else "")
                                  for g in res))
        elif a.cmd == "info":
            p = core.find_project(a.project)
            git = core.is_git(p["path"])
            remotes = core.git(p["path"], "remote").splitlines() if git else []
            targets = core.remote_targets(p["path"], remotes)
            destinations = "；".join(f"{name} → {', '.join(urls)}" for name, urls in targets.items())
            res = {**p, "tags": p.get("tags", []), "remotes": remotes, "remoteTargets": targets,
                   "kinds": core.detect(p["path"])["kinds"], "readme": core.readme_head(p["path"]),
                   "recent": [c["subject"] for c in core.git_log(p["path"], n=5)] if git else []}
            out(res, j, f"{p['id']}  [{', '.join(res['kinds'])}]  {p['path']}\n"
                        f"分组：{p['group']}   运行：{p['run'] or '-'}   检查：{p['check'] or '-'}\n"
                        f"标签：{'、'.join(res['tags']) or '-'}   推送地址：{destinations or '-'}\n"
                        f"最近提交：{'; '.join(res['recent']) or '-'}\n---\n{res['readme'] or '（无 README）'}")
        elif a.cmd == "add":
            p = core.add_project(a.path, a.group, a.name)
            out(p, j, f"已添加 {p['id']} → {p['path']}")
        elif a.cmd == "scan":
            found = core.scan(a.root, a.depth)
            added = []
            if a.add:
                _, added = core.register_projects([f["path"] for f in found if not f["registered"]], a.group)
            out({"found": found, "added": added}, j,
                "\n".join(f"{'*' if f['registered'] else ' '} {f['path']}  {','.join(f['kind'])}"
                          for f in found) + (f"\n已添加 {len(added)} 个" if a.add else ""))
        elif a.cmd == "group":
            r = core.set_group(a.projects, a.name, a.color, a.description)
            out(r, j, f"分组 {a.name}（{r['group']}）：放入 {len(r['moved'])} 个项目")
        elif a.cmd == "group-edit":
            core.edit_group(a.group, a.rename, a.color, a.description, a.delete)
            out({"ok": True}, j, "已删除分组" if a.delete else "已更新分组")
        elif a.cmd == "ungroup":
            r = core.set_group(a.projects, "default")
            out(r, j, f"已移回未分组：{', '.join(r['moved'])}")
        elif a.cmd == "status":
            p = core.find_project(a.project)
            info = core.git_info(p["path"])
            if not info["git"]:
                out(info, j, "不是 git 仓库")
            else:
                ch = "\n".join(f"  {c['label']:<4} {c['path']}" for c in info["changes"]) or "  （工作区干净）"
                out(info, j, f"分支 {info['branch']} @ {info['head']}\n{ch}")
        elif a.cmd == "commit":
            p = core.find_project(a.project)
            r = core.commit_to_branch(p["path"], a.branch, a.message, a.files or None, a.base,
                                      a.author, a.allow_empty)
            out(r, j, f"已提交到 {r['branch']}{'（新建分支）' if r['created'] else ''}：{r['sha'][:10]}")
        elif a.cmd == "push":
            r = core.push_project(core.find_project(a.project), a.branch, a.remote, a.to, a.dry_run, a.preview_id)
            action = "预演成功" if r["dryRun"] else "推送成功"
            status = ""
            if r["upToDate"]:
                status = "（远端已是最新）" if r["dryRun"] else "（远端已是最新，本次未更新分支）"
            if r.get("recovered"):
                action = "已核实远端并补记成功回执"
                status = "（本次没有重新推送）"
            addresses = ("\n" + core.format_destinations(r["destinations"]) if r.get("destinations")
                         else "、".join(r["pushUrls"]))
            out(r, j, f"{action}：{r['branch']} → {r['remote']}/{r['target']}  {r['sha'][:12]}"
                + status + "\n推送地址：" + addresses
                + (f"\n预演编号：{r['previewId']}" if r["dryRun"] else ""))
        elif a.cmd == "check":
            p = core.find_project(a.project)
            r = core.full_check(p, with_command=not a.no_command)
            out(r, j, f"检查了 {r['files']} 个文件，用时 {r['seconds']}s\n" + fmt_problems(r["problems"]))
            return 1 if any(x["severity"] == "error" for x in r["problems"]) else 0
        elif a.cmd == "run":
            p = core.find_project(a.project)
            r = core.run_once(p, a.command, a.timeout)
            tail = "\n".join(ln["t"] for ln in r["lines"][-60:])
            state = "仍在运行（已在超时后停止）" if r["alive"] else f"退出码 {r['code']}"
            out(r, j, f"$ {r['command']}\n{tail}\n\n{state}\n" + fmt_problems(r["problems"]))
            return 0 if r["ok"] else 1
        elif a.cmd == "set":
            p = core.update_project(core.find_project(a.project)["id"], {a.field: a.value})
            out(p, j, f"{p['id']}.{a.field} = {a.value}")
    except core.HubError as e:
        out({"error": str(e), **e.details}, j, f"错误：{e}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
