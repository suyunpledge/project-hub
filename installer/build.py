"""Build dist/ProjectHub-Setup-<version>.exe — a self-contained Windows installer.

    python installer/build.py            # uses installer/cache/python-*-embed-amd64.zip
    python installer/build.py --download # fetch the Python runtime from python.org first

The installer bundles: the app (hub/, hub.py, docs), the official Python embeddable runtime,
ProjectHub.exe (launcher) and hub.cmd (CLI shim for agents). It is compiled with the C#
compiler that ships with Windows (.NET Framework 4.x), so no installer toolkit is required.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

# CI consoles (GitHub Actions on Windows) default to cp1252, which cannot print Chinese.
for _stream in (sys.stdout, sys.stderr):
    if _stream and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CACHE = HERE / "cache"
BUILD = HERE / "build"
DIST = ROOT / "dist"

PY_VERSION = "3.12.10"
PY_ZIP = f"python-{PY_VERSION}-embed-amd64.zip"
PY_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/{PY_ZIP}"
PY_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"

APP_FILES = ["hub.py", "README.md", "AGENTS.md", "LICENSE"]


def version() -> str:
    text = (ROOT / "hub" / "__init__.py").read_text("utf-8")
    return re.search(r'__version__\s*=\s*"([^"]+)"', text)[1]


def csc() -> str:
    windir = os.environ.get("WINDIR", r"C:\Windows")
    for fw in ("Framework64", "Framework"):
        p = Path(windir) / "Microsoft.NET" / fw / "v4.0.30319" / "csc.exe"
        if p.exists():
            return str(p)
    sys.exit("找不到 .NET Framework 4 的 csc.exe（Windows 10/11 自带）")


def fetch_runtime(download: bool) -> Path:
    z = CACHE / PY_ZIP
    if not z.exists():
        if not download:
            sys.exit(f"缺少 {z}。运行 python installer/build.py --download 从 python.org 下载。")
        CACHE.mkdir(parents=True, exist_ok=True)
        print(f"下载 {PY_URL}")
        urllib.request.urlretrieve(PY_URL, z)
    digest = hashlib.sha256(z.read_bytes()).hexdigest()
    if digest != PY_SHA256:
        z.unlink()
        sys.exit(f"{PY_ZIP} 的 SHA-256 不匹配（{digest}），已删除。请重新下载。")
    return z


def verify_signature(exe: Path) -> None:
    """The runtime's python.exe must carry a valid Python Software Foundation signature."""
    ps = (f"$s=Get-AuthenticodeSignature -LiteralPath '{exe}';"
          "Write-Output ($s.Status.ToString()+'|'+$s.SignerCertificate.Subject)")
    # Prefer PowerShell 7. Windows PowerShell started from pwsh (GitHub Actions) inherits pwsh's
    # PSModulePath and then cannot load its own Microsoft.PowerShell.Security module — clear it.
    shell = shutil.which("pwsh") or "powershell"
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    r = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=env)
    out = r.stdout.strip()
    status, _, subject = out.partition("|")
    if not status:
        # The check itself could not run. The archive already matched the pinned SHA-256 above,
        # so the runtime is the genuine python.org build; report why instead of failing blindly.
        sys.exit(f"{exe.name} 签名检查无法执行（{shell}）：{r.stderr.strip()[-800:] or '无输出'}")
    if status != "Valid" or "Python Software Foundation" not in subject:
        sys.exit(f"{exe.name} 签名无效：{out}")
    print(f"签名有效：{subject.split(',')[0]}")


def stage_runtime(zip_path: Path, dest: Path) -> None:
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest)
    verify_signature(dest / "python.exe")
    pth = next(dest.glob("python*._pth"))
    lines = pth.read_text("utf-8").splitlines()
    # make the app folder importable (`import hub`) — paths are relative to the ._pth file
    if "..\\app" not in lines:
        lines.insert(1, "..\\app")
    pth.write_text("\n".join(lines) + "\n", "utf-8")


def stage_app(dest: Path) -> None:
    shutil.copytree(ROOT / "hub", dest / "hub", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in APP_FILES:
        if (ROOT / name).exists():
            shutil.copy2(ROOT / name, dest / name)


def compile_cs(source: Path, out: Path, *, target: str, icon: Path, resources: list[tuple[Path, str]] = ()) -> None:
    args = [csc(), "/nologo", "/optimize+", f"/target:{target}", f"/out:{out}", f"/win32icon:{icon}",
            "/r:System.dll", "/r:System.Drawing.dll", "/r:System.Windows.Forms.dll",
            "/r:System.IO.Compression.dll", "/r:System.IO.Compression.FileSystem.dll"]
    args += [f"/resource:{path},{name}" for path, name in resources]
    args.append(str(source))
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        sys.exit(f"编译 {source.name} 失败：\n{r.stdout}\n{r.stderr}")


def zip_dir(src: Path, out: Path) -> None:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(src).as_posix())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--download", action="store_true", help="从 python.org 下载 Python 运行时")
    a = ap.parse_args()
    ver = version()
    icon = ROOT / "hub" / "static" / "icon.ico"
    runtime_zip = fetch_runtime(a.download)

    shutil.rmtree(BUILD, ignore_errors=True)
    payload = BUILD / "payload"
    stage_runtime(runtime_zip, payload / "runtime")
    stage_app(payload / "app")
    (payload / "hub.cmd").write_text('@echo off\r\n"%~dp0runtime\\python.exe" "%~dp0app\\hub.py" %*\r\n', "utf-8")
    compile_cs(HERE / "Launcher.cs", payload / "ProjectHub.exe", target="winexe", icon=icon)

    # smoke test the staged app with the bundled runtime before packaging
    r = subprocess.run([str(payload / "runtime" / "python.exe"), str(payload / "app" / "hub.py"), "--help"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode or "mcp" not in r.stdout:
        sys.exit(f"打包后的程序无法用内置 Python 运行：\n{r.stdout}\n{r.stderr}")

    zip_dir(payload, BUILD / "payload.zip")
    setup_src = BUILD / "Setup.cs"
    setup_src.write_text(re.sub(r'Version = "[^"]+"', f'Version = "{ver}"',
                                (HERE / "Setup.cs").read_text("utf-8")), "utf-8")
    DIST.mkdir(exist_ok=True)
    out = DIST / f"ProjectHub-Setup-{ver}.exe"
    compile_cs(setup_src, out, target="winexe", icon=icon,
               resources=[(BUILD / "payload.zip", "payload.zip"), (ROOT / "hub" / "static" / "icon.png", "logo.png")])
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    (DIST / f"{out.name}.sha256").write_text(f"{digest}  {out.name}\n", "utf-8")
    print(f"完成：{out}  ({out.stat().st_size / 1048576:.1f} MB)\nSHA-256 {digest}")


def _fail(message: str) -> None:
    """Report a build failure; on GitHub Actions also as an annotation (readable without the logs)."""
    print(message, file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS"):
        text = message.replace("%", "%25").replace(chr(13), "%0D").replace(chr(10), "%0A")
        print(f"::error title=installer build::{text}", flush=True)
    raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0):
            _fail(str(e.code))
        raise
    except Exception:
        import traceback
        _fail(traceback.format_exc()[-3000:])
