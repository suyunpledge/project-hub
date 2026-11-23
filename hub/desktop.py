"""Project Hub as a desktop app.

Starts the local server in-process and shows the UI in its own window:

1. pywebview, if installed (``pip install pywebview``) — native WebView2 window;
2. otherwise Microsoft Edge / Chrome in ``--app`` mode with a dedicated profile — a standalone
   window without tabs or address bar, whose lifetime is tied to this process;
3. otherwise the default browser.

Closing the window stops the server and any processes started from the Run tab.
If an instance is already running, a second launch just opens another window onto it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

from . import core, server

ICON = Path(__file__).parent / "static" / "icon.ico"


def _existing(port: int) -> str | None:
    url = f"http://127.0.0.1:{port}/"
    try:
        with urllib.request.urlopen(url + "api/config", timeout=1) as r:
            if "previewPort" in json.loads(r.read()):
                return url
    except (OSError, ValueError):
        pass
    return None


def _find_browser() -> str | None:
    candidates = [
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    ]
    for c in candidates:
        p = os.path.expandvars(c)
        if os.path.isfile(p):
            return p
    return shutil.which("msedge") or shutil.which("chrome") or shutil.which("chromium")


def open_window(url: str) -> None:
    """Show ``url`` in an app window and block until it is closed."""
    try:
        import webview  # type: ignore
    except ImportError:
        webview = None
    if webview:
        webview.create_window("Project Hub", url, width=1440, height=920, min_size=(960, 600),
                              background_color="#050814")
        webview.start()
        return
    browser = _find_browser()
    if browser:
        profile = core.DATA_DIR / "desktop-profile"
        profile.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen([
            browser, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1440,920",
            "--no-first-run", "--no-default-browser-check", "--disable-features=Translate",
            "--disable-sync",
        ])
        proc.wait()
        return
    import webbrowser
    webbrowser.open(url)
    print("没有找到 Edge/Chrome，已用默认浏览器打开；关闭此窗口（Ctrl+C）退出。", flush=True)
    threading.Event().wait()


def _log_to_file() -> None:
    # Under pythonw there is no console: keep a log for troubleshooting.
    if sys.stdout is None or sys.stderr is None:
        core.DATA_DIR.mkdir(parents=True, exist_ok=True)
        fh = open(core.DATA_DIR / "desktop.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = fh


def main(port: int = 8790) -> None:
    _log_to_file()
    url = _existing(port)
    if url:  # already running: just open another window onto it
        open_window(url)
        return
    try:
        _, url = server.start(port)
    except OSError:  # port taken by something else
        _, url = server.start(0)
    print(f"Project Hub desktop: {url}", flush=True)
    try:
        open_window(url)
    finally:
        server.shutdown()


def install_shortcuts() -> list[str]:
    """Create Desktop and Start-menu shortcuts (Windows) that launch the app without a console."""
    if os.name != "nt":
        raise core.HubError("快捷方式目前只支持 Windows")
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pyw if pyw.exists() else sys.executable)
    script = str(core.ROOT / "hub.py")
    made = []
    ps_lines = []
    for folder in ("Desktop", "Programs"):
        ps_lines.append(
            f"$d=[Environment]::GetFolderPath('{folder}');"
            f"$p=Join-Path $d 'Project Hub.lnk';"
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($p);"
            f"$s.TargetPath='{exe}';$s.Arguments='\"{script}\" desktop';"
            f"$s.WorkingDirectory='{core.ROOT}';$s.IconLocation='{ICON},0';"
            "$s.Description='本机项目仓库';$s.Save();Write-Output $p"
        )
    for line in ps_lines:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", line], capture_output=True)
        if r.returncode:
            raise core.HubError(r.stderr.decode("utf-8", "replace") or r.stdout.decode("gbk", "replace"))
        made.append(r.stdout.decode("utf-8", "replace").strip() or r.stdout.decode("gbk", "replace").strip())
    return made
