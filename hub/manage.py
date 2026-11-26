"""Registry management: overview of all projects, pinning, relocating, pruning, batch edits."""
from __future__ import annotations

import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from . import core


def _quick_status(p: dict) -> dict:
    """Cheap per-project status for the overview (one git call per project)."""
    root = p["path"]
    row = {"id": p["id"], "ref": "id:" + p["id"], "name": p["name"], "group": p["group"],
           "tags": p.get("tags", []), "pinned": bool(p.get("pinned")), "path": root,
           "exists": os.path.isdir(root), "git": False}
    if not row["exists"]:
        return row
    r = subprocess.run(["git", "-c", "core.quotepath=false", "-C", root, "status", "--porcelain=v2",
                        "--branch", "-uno" if os.environ.get("HUB_FAST_STATUS") else "-unormal"],
                       capture_output=True, creationflags=core.NO_WINDOW, timeout=30)
    if r.returncode:
        return row
    row["git"] = True
    row.update(branch="", ahead=0, behind=0, changes=0, upstream=None)
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("# branch.head "):
            row["branch"] = line[14:]
        elif line.startswith("# branch.upstream "):
            row["upstream"] = line[18:]
        elif line.startswith("# branch.ab "):
            a, b = line[12:].split()
            row["ahead"], row["behind"] = int(a), -int(b)
        elif line and not line.startswith("#"):
            row["changes"] += 1
    last = subprocess.run(["git", "-C", root, "log", "-1", "--format=%ct\x1f%s"], capture_output=True,
                          creationflags=core.NO_WINDOW, timeout=30)
    if last.returncode == 0 and last.stdout.strip():
        ts, _, subject = last.stdout.decode("utf-8", "replace").strip().partition("\x1f")
        row["lastCommit"] = {"time": int(ts), "subject": subject}
    remotes = subprocess.run(["git", "-C", root, "remote", "get-url", "origin"], capture_output=True,
                             creationflags=core.NO_WINDOW, timeout=30)
    if remotes.returncode == 0:
        row["remote"] = core._display_url(remotes.stdout.decode("utf-8", "replace").strip())
    return row


def overview(group: str | None = None) -> dict:
    """Status of every registered project: branch, uncommitted changes, ahead/behind, last commit."""
    reg = core.load_registry()
    projects = reg["projects"]
    if group:
        g = core.find_group(reg, group)
        if not g:
            raise core.HubError(f"找不到分组：{group}")
        projects = [p for p in projects if p["group"] == g["id"]]
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(_quick_status, projects))
    rows.sort(key=lambda r: (not r["pinned"], -(r.get("lastCommit") or {}).get("time", 0), r["id"]))
    summary = {
        "projects": len(rows), "missing": sum(not r["exists"] for r in rows),
        "dirty": sum(bool(r.get("changes")) for r in rows), "ahead": sum(bool(r.get("ahead")) for r in rows),
        "behind": sum(bool(r.get("behind")) for r in rows), "noRemote": sum(r["git"] and not r.get("remote") for r in rows),
        "notGit": sum(r["exists"] and not r["git"] for r in rows),
    }
    return {"summary": summary, "projects": rows, "seconds": round(time.time() - t0, 2)}


def set_pinned(refs: list[str], pinned: bool) -> list[str]:
    ids = []
    with core.transaction() as reg:
        for ref in refs:
            p = core._resolve_project(reg["projects"], ref)
            p["pinned"] = bool(pinned)
            ids.append(p["id"])
    return ids


def relocate(ref: str, new_path: str) -> dict:
    """Point a registration at the folder's new location (project moved or renamed on disk)."""
    full = os.path.abspath(os.path.expanduser(new_path))
    if not os.path.isdir(full):
        raise core.HubError(f"目录不存在：{full}")
    key = core._path_key(full)
    with core.transaction() as reg:
        p = core._resolve_project(reg["projects"], ref)
        other = next((o for o in reg["projects"] if o is not p and core._path_key(o["path"]) == key), None)
        if other:
            raise core.HubError(f"该目录已登记为项目 {other['id']}", code="path_taken", project=other["id"])
        p["path"] = full
        # a pending push preview was reviewed against the old folder
        reg.get("pushPreviews", {}).pop(p["id"], None)
        return p


def prune(dry_run: bool = True) -> dict:
    """List (or remove) registrations whose folder no longer exists."""
    if dry_run:
        reg = core.load_registry()
        gone = [p for p in reg["projects"] if not os.path.isdir(p["path"])]
        return {"removed": [], "missing": [{"id": p["id"], "path": p["path"]} for p in gone], "dryRun": True}
    with core.transaction() as reg:
        gone = [p for p in reg["projects"] if not os.path.isdir(p["path"])]
        ids = {p["id"] for p in gone}
        reg["projects"] = [p for p in reg["projects"] if p["id"] not in ids]
        for key in ("pushPreviews", "pushPreviewHistory"):
            for pid in ids:
                reg.get(key, {}).pop(pid, None)
    return {"removed": sorted(ids), "missing": [], "dryRun": False}


def batch(refs: list[str], group: str | None = None, add_tags: list[str] | None = None,
          remove_tags: list[str] | None = None, pinned: bool | None = None) -> list[str]:
    """Apply group / tag / pin changes to many projects in one registry transaction."""
    if not refs:
        raise core.HubError("请选择项目")
    adds = [t.strip() for t in add_tags or [] if t.strip()]
    removes = {t.strip().casefold() for t in remove_tags or [] if t.strip()}
    ids = []
    with core.transaction() as reg:
        targets = [core._resolve_project(reg["projects"], r) for r in refs]
        gid = core.ensure_group(reg, group) if group else None
        for p in targets:
            if gid:
                p["group"] = gid
            if adds or removes:
                labels = {t.casefold(): t for t in p.get("tags", []) if t.casefold() not in removes}
                for t in adds:
                    labels.setdefault(t.casefold(), t)
                p["tags"] = list(labels.values())
            if pinned is not None:
                p["pinned"] = bool(pinned)
            ids.append(p["id"])
    return ids
