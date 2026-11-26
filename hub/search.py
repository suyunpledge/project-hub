"""Project search (ranked, with a fuzzy fallback) and code search across projects."""
from __future__ import annotations

import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from . import core

# ---------------------------------------------------------------- project search


def _fields(p: dict, group_name: str) -> dict[str, str]:
    return {
        "id": p["id"].casefold(), "name": p["name"].casefold(),
        "tags": "\n".join(p.get("tags", [])).casefold(),
        "group": f"{p['group']}\n{group_name}".casefold(),
        "description": p.get("description", "").casefold(),
        "path": p["path"].casefold(),
    }


# weight of a keyword hit per field; an exact or prefix name hit outranks everything
_WEIGHTS = {"name": 30, "id": 30, "tags": 18, "group": 10, "description": 8, "path": 4}


def _score(words: list[str], f: dict[str, str]) -> float | None:
    """Strict score: every word must appear somewhere. None when a word is missing."""
    total = 0.0
    for w in words:
        best = 0.0
        for field, text in f.items():
            if w in text:
                weight = _WEIGHTS[field]
                if field in ("name", "id"):
                    if text == w:
                        weight += 70
                    elif text.startswith(w):
                        weight += 35
                elif field == "tags" and w in text.split("\n"):
                    weight += 12
                best = max(best, weight)
        if not best:
            return None
        total += best
    return total


def _subsequence(w: str, text: str) -> float:
    """Fuzzy score of ``w`` as an in-order subsequence of ``text`` (0 when it isn't one).
    Tighter matches and matches at word starts score higher, e.g. "pyt" ~ "py-tool"."""
    pos, last, score, gaps = 0, -1, 0.0, 0
    for ch in w:
        i = text.find(ch, pos)
        if i < 0:
            return 0.0
        if last >= 0:
            gaps += i - last - 1
        score += 2 if i == 0 or not text[i - 1].isalnum() else 1
        last, pos = i, i + 1
    return score / (1 + gaps * 0.15)


def _fuzzy(words: list[str], f: dict[str, str]) -> float | None:
    total = 0.0
    for w in words:
        best = max(_subsequence(w, f["name"]) * 3, _subsequence(w, f["id"]) * 3,
                   _subsequence(w, f["tags"]) * 2, _subsequence(w, f["description"]))
        if best < len(w) * 0.6:  # too scattered to be what was meant
            return None
        total += best
    return total


def search_projects(query: str, group: str | None = None, tags: list[str] | None = None,
                    kind: str | None = None, missing: bool = False, fuzzy: bool = True) -> list[dict]:
    """Ranked project search.

    Every keyword must match (substring, case-insensitive) one of: name, id, tags, group,
    description, path. Results are ordered by relevance. When nothing matches strictly and
    ``fuzzy`` is on, in-order subsequence matches are returned instead, marked ``fuzzy: true``.
    """
    reg = core.load_registry()
    selected = core.find_group(reg, group) if group else None
    if group and not selected:
        raise core.HubError(f"找不到分组：{group}")
    groups = {g["id"]: g["name"] for g in reg["groups"]}
    words = query.casefold().split()
    required = {t.strip().casefold() for t in tags or []}
    pool = []
    for p in reg["projects"]:
        if selected and selected["id"] != p["group"]:
            continue
        labels = p.get("tags", [])
        if not required.issubset(t.casefold() for t in labels):
            continue
        exists = os.path.isdir(p["path"])
        if missing and exists:
            continue
        if kind and (not exists or kind.casefold() not in core.detect(p["path"])["kinds"]):
            continue
        group_name = groups.get(p["group"], p["group"])
        pool.append((p, group_name, exists, _fields(p, group_name)))

    def build(scorer, is_fuzzy):
        out = []
        for p, group_name, exists, f in pool:
            score = scorer(words, f) if words else 0.0
            if score is None:
                continue
            item = {**p, "ref": "id:" + p["id"], "tags": p.get("tags", []), "groupName": group_name,
                    "exists": exists, "score": round(score + (5 if p.get("pinned") else 0), 1)}
            if is_fuzzy:
                item["fuzzy"] = True
            out.append(item)
        return sorted(out, key=lambda r: (-r["score"], r["id"]))

    results = build(_score, False)
    if not results and words and fuzzy:
        results = build(_fuzzy, True)
    return results


# ---------------------------------------------------------------- code search

MAX_HITS_PER_PROJECT = 200


def _grep_one(p: dict, pattern: str, regex: bool, ignore_case: bool, globs: list[str],
              max_hits: int) -> dict:
    root = p["path"]
    res = {"id": p["id"], "ref": "id:" + p["id"], "name": p["name"], "path": root, "hits": [], "truncated": False}
    if not os.path.isdir(root):
        res["error"] = "目录不存在"
        return res
    if core.is_git(root):
        args = ["git", "-c", "core.quotepath=false", "-C", root, "grep", "-n", "-I", "--untracked",
                "--no-color", "--column", "--max-count", str(max_hits)]
        args += ["-i"] if ignore_case else []
        args += ["-E"] if regex else ["-F"]
        args += ["-e", pattern, "--"] + (globs or [])
        r = subprocess.run(args, capture_output=True, creationflags=core.NO_WINDOW, timeout=60)
        if r.returncode not in (0, 1):
            res["error"] = r.stderr.decode("utf-8", "replace").strip()[:300]
            return res
        lines = r.stdout.decode("utf-8", "replace").splitlines()
    else:
        lines = _walk_grep(root, pattern, regex, ignore_case, globs, max_hits)
    for ln in lines:
        m = re.match(r"^(.*?):(\d+):(\d+):(.*)$", ln)
        if not m:
            continue
        res["hits"].append({"file": m[1], "line": int(m[2]), "col": int(m[3]), "text": m[4].strip()[:300]})
        if len(res["hits"]) >= max_hits:
            res["truncated"] = True
            break
    return res


def _walk_grep(root: str, pattern: str, regex: bool, ignore_case: bool, globs: list[str],
               max_hits: int) -> list[str]:
    import fnmatch
    flags = re.I if ignore_case else 0
    rx = re.compile(pattern if regex else re.escape(pattern), flags)
    out = []
    for rel in core.list_files(root):
        if globs and not any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(os.path.basename(rel), g) for g in globs):
            continue
        full = os.path.join(root, rel)
        try:
            if os.path.getsize(full) > core.MAX_READ:
                continue
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        if b"\0" in data[:8000]:
            continue
        for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            m = rx.search(line)
            if m:
                out.append(f"{rel}:{n}:{m.start() + 1}:{line}")
                if len(out) >= max_hits:
                    return out
    return out


def grep(pattern: str, projects: list[str] | None = None, group: str | None = None,
         tags: list[str] | None = None, regex: bool = False, ignore_case: bool = True,
         globs: list[str] | None = None, max_hits: int = 50) -> dict:
    """Search file contents of many projects at once (git grep, in parallel)."""
    if not pattern:
        raise core.HubError("请提供要搜索的内容")
    if regex:
        try:
            re.compile(pattern)
        except re.error as e:
            raise core.HubError(f"正则表达式无效：{e}") from e
    max_hits = max(1, min(int(max_hits), MAX_HITS_PER_PROJECT))
    if projects:
        targets = [core.find_project(r) for r in projects]
    else:
        targets = search_projects("", group, tags, fuzzy=False)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(targets)))) as pool:
        results = list(pool.map(lambda p: _grep_one(p, pattern, regex, ignore_case, globs or [], max_hits),
                                targets))
    hits = [r for r in results if r["hits"] or r.get("error")]
    hits.sort(key=lambda r: (-len(r["hits"]), r["id"]))
    return {"pattern": pattern, "projects": len(targets), "matched": sum(1 for r in hits if r["hits"]),
            "total": sum(len(r["hits"]) for r in hits), "seconds": round(time.time() - t0, 2), "results": hits}
