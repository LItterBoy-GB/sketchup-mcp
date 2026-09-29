"""Console-free Windows entry point for the per-user HTTP scheduled task."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable, Optional


HTTP_TOKEN_ENV = "SKETCHUP_MCP_HTTP_TOKEN"


def read_user_token() -> str:
    import winreg

    # 计划任务可能继承旧环境，必须读取当前用户注册表中的最新 Token。
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            token, value_type = winreg.QueryValueEx(key, HTTP_TOKEN_ENV)
    except FileNotFoundError:
        token, value_type = "", winreg.REG_SZ
    if value_type not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(token, str) or not token.strip():
        raise RuntimeError(f"User-scoped {HTTP_TOKEN_ENV} is missing; run manage-http-daemon.ps1 Install")
    return token


def main(argv: Optional[Iterable[str]] = None) -> None:
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    log_path = root / "SketchUpMCP" / "logs" / "http-launcher.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("sketchup_mcp.http_launcher")
    handler = RotatingFileHandler(log_path, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    # pythonw 没有标准流；为依赖库补齐空流，业务日志仍写入轮转文件。
    original_streams = (sys.stdin, sys.stdout, sys.stderr)
    opened_streams = []
    try:
        for name, mode in (("stdin", "r"), ("stdout", "w"), ("stderr", "w")):
            if getattr(sys, name) is None:
                stream = open(os.devnull, mode, encoding="utf-8")
                opened_streams.append(stream)
                setattr(sys, name, stream)

        os.environ[HTTP_TOKEN_ENV] = read_user_token()
        # 延迟导入，让缺失依赖等启动错误也能留在日志中。
        from .http_server import main as run_http

        run_http(argv)
    except SystemExit as exc:
        if exc.code:
            logger.error("HTTP daemon exited during startup or execution (code %s)", exc.code)
        raise
    except Exception:
        logger.exception("HTTP daemon failed")
        raise SystemExit(1)
    finally:
        sys.stdin, sys.stdout, sys.stderr = original_streams
        for stream in opened_streams:
            stream.close()
        logger.removeHandler(handler)
        handler.close()


if __name__ == "__main__":
    main()
