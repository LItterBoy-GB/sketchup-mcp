"""Load this repository's venv using the base installation's GUI Python.

The scheduled task invokes this file with -I -S so environment/global/user packages cannot
override the virtual environment. No venv executable is launched, including after
uv recreates its console-subsystem pythonw.exe shim.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys


def run() -> None:
    root = Path(__file__).resolve().parents[1]
    venv = root / ".venv"
    config = venv / "pyvenv.cfg"
    if not config.is_file():
        raise RuntimeError("Missing .venv/pyvenv.cfg; recreate the venv and run manage-http-daemon.ps1 Install")
    homes = [line.split("=", 1)[1].strip() for line in config.read_text(encoding="utf-8").splitlines()
             if "=" in line and line.split("=", 1)[0].strip().lower() == "home"]
    if len(homes) != 1 or not Path(homes[0]).is_absolute() or Path(homes[0]).resolve() != Path(sys.base_prefix).resolve():
        raise RuntimeError("The venv's base Python changed; run manage-http-daemon.ps1 Start to refresh the task")
    packages = venv / "Lib" / "site-packages"
    if not packages.is_dir():
        raise RuntimeError("Missing venv site-packages; install the project before starting the HTTP daemon")

    # -S 禁用全局 site；只加载当前 venv 的依赖和 editable .pth。
    sys.prefix = sys.exec_prefix = str(venv)
    import site

    site.ENABLE_USER_SITE = False
    site.addsitedir(str(packages))
    from sketchup_mcp.http_daemon import main

    main()


if __name__ == "__main__":
    try:
        run()
    except Exception:
        # 依赖导入失败也留文件诊断，GUI Python 没有 stderr 可供用户查看。
        import logging
        from logging.handlers import RotatingFileHandler

        logs = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "SketchUpMCP" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(logs / "http-launcher.log", maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger = logging.getLogger("sketchup_mcp.bootstrap")
        logger.addHandler(handler)
        logger.propagate = False
        logger.exception("HTTP bootstrap failed")
        handler.close()
        raise SystemExit(1)
