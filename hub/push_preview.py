"""Observe the old object ID from Git's actual push advertisement, including on dry runs."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _shell_path(path: Path) -> str:
    return shlex.quote(path.absolute().as_posix())


@contextmanager
def observe(original_hooks: Path, snapshot: dict, expected_tips: list | None):
    """Install a per-command pre-push observer and forward the user's existing hooks."""
    with tempfile.TemporaryDirectory(prefix="hub-push-") as tmp:
        directory = Path(tmp)
        hooks = directory / "hooks"
        hooks.mkdir()
        settings = directory / "expected.json"
        settings.write_text(json.dumps({**snapshot, "tips": expected_tips}), "utf-8")
        updates = _shell_path(directory / "updates")
        check = (f"MSYS2_ARG_CONV_EXCL='*' {_shell_path(Path(sys.executable))} {_shell_path(Path(__file__))} "
                 f"{_shell_path(settings)} \"$2\" < {updates} || exit 1\n")
        originals = original_hooks.iterdir() if original_hooks.is_dir() else ()
        scripts = {p.name: f"exec {_shell_path(p)} \"$@\"\n" for p in originals
                   if p.is_file() and not p.name.endswith(".sample") and os.access(p, os.X_OK)}
        delegate = scripts.get("pre-push", "")
        scripts["pre-push"] = f"cat > {updates} || exit 1\n" + check + (
            f"exec < {updates}\n" + delegate if delegate else "")
        for name, script in scripts.items():
            hook = hooks / name
            hook.write_bytes(("#!/bin/sh\n" + script).encode("utf-8"))
            hook.chmod(0o700)
        yield ["-c", "core.hooksPath=" + hooks.as_posix()], directory / "observed.jsonl"


def _check(settings: Path, url: str) -> int:
    snapshot = json.loads(settings.read_text("utf-8"))
    log = settings.with_name("observed.jsonl")
    index = len(log.read_text("utf-8").splitlines()) if log.exists() else 0
    changes = []
    if index >= len(snapshot["urlHashes"]) or url_hash(url) != snapshot["urlHashes"][index]:
        changes.append("push_urls")
    sha = snapshot["sha"]
    tip = sha  # Git sends no update record for an up-to-date or already rejected ref.
    for line in sys.stdin.buffer:
        _, new, ref, old = line.decode("utf-8").rstrip("\n").split(" ")
        if new != sha or ref != "refs/heads/" + snapshot["target"]:
            changes.append("target")
        tip = old if old.strip("0") else None
    if snapshot["tips"] is not None:
        if index >= len(snapshot["tips"]) or tip != snapshot["tips"][index]:
            changes.append("remote_tip")
        head = subprocess.run(["git", "rev-parse", "--verify", "refs/heads/" + snapshot["branch"]],
                              capture_output=True)
        if head.stdout.strip() != sha.encode("ascii"):
            changes.append("local_sha")
    with log.open("a", encoding="utf-8") as out:
        out.write(json.dumps({"tip": tip, "changes": changes}) + "\n")
    return int(bool(changes))


if __name__ == "__main__":
    sys.exit(_check(Path(sys.argv[1]), sys.argv[2]))
