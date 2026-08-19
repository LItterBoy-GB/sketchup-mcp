"""Stateful, loopback-only Streamable HTTP entry point for SketchUp MCP."""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import server


DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8765
DEFAULT_SKETCHUP_PORT = 9876
HTTP_TOKEN_ENV = "SKETCHUP_MCP_HTTP_TOKEN"
DEFAULT_SESSION_IDLE_TIMEOUT = 1800.0
HTTP_LOGGER_NAME = "sketchup_mcp.http_daemon"
HTTP_LOGGER_NAMES = ("SketchupMCPServer", "mcp", "uvicorn", HTTP_LOGGER_NAME)
HTTP_LOG_MAX_BYTES = 5 * 1024 * 1024
HTTP_LOG_BACKUP_COUNT = 5
_HTTP_STARTED_MONOTONIC = time.monotonic()


def _port(value: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from exc
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535")
    return port


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a positive number") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def validate_loopback_host(host: str) -> str:
    """Accept only a local loopback bind address for the authenticated service."""
    normalized = host.strip()
    if normalized.lower() == "localhost":
        return normalized
    try:
        if ipaddress.ip_address(normalized).is_loopback:
            return normalized
    except ValueError:
        pass
    raise ValueError("Streamable HTTP may listen only on a loopback host")


def required_http_token() -> str:
    token = os.environ.get(HTTP_TOKEN_ENV, "")
    if not token:
        raise RuntimeError(f"{HTTP_TOKEN_ENV} must be set before starting Streamable HTTP")
    return token


class StaticBearerTokenMiddleware:
    """Pure ASGI Bearer protection that preserves downstream SSE streaming."""

    def __init__(self, app: Callable[..., Awaitable[None]], *, token: str) -> None:
        self.app = app
        self._token = token

    async def __call__(self, scope: dict[str, Any], receive, send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/healthz":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        scheme, _, supplied = authorization.partition(" ")
        if scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(supplied, self._token):
            logging.getLogger(HTTP_LOGGER_NAME).warning(
                "Rejected unauthenticated HTTP MCP request on %s",
                scope.get("path", "/mcp"),
            )
            body = b'{"error":"unauthorized"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


class StreamableHTTPSessionCleanup:
    """Remove a terminated session from the MCP manager immediately after DELETE."""

    def __init__(self, app: Callable[..., Awaitable[None]], manager: Any) -> None:
        self.app = app
        self._manager = manager

    async def __call__(self, scope: dict[str, Any], receive, send) -> None:
        headers = dict(scope.get("headers", [])) if scope["type"] == "http" else {}
        session_id = headers.get(b"mcp-session-id", b"").decode("latin-1")
        is_delete = scope["type"] == "http" and scope.get("method") == "DELETE" and bool(session_id)
        response_status: Optional[int] = None

        async def tracked_send(message) -> None:
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message["status"]
            await send(message)

        await self.app(scope, receive, tracked_send)
        if is_delete and response_status is not None and 200 <= response_status < 300:
            # mcp 1.27.2 terminates DELETE transports but intentionally retains
            # the mapping. Remove both maps at this normal lifecycle boundary.
            self._manager._server_instances.pop(session_id, None)
            self._manager._session_owners.pop(session_id, None)


def default_http_log_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return root / "SketchUpMCP" / "logs" / "http-daemon.log"


def _http_log_targets() -> tuple[logging.Logger, ...]:
    return (logging.getLogger(), *(logging.getLogger(name) for name in HTTP_LOGGER_NAMES))


def configure_http_logging(log_path: Optional[Path] = None) -> logging.Logger:
    """Attach one HTTP-only rotating handler without touching stdio startup."""
    path = Path(log_path) if log_path is not None else default_http_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    targets = _http_log_targets()
    resolved_path = str(path.resolve())
    existing_handlers = {
        handler
        for target in targets
        for handler in target.handlers
        if getattr(handler, "_sketchup_mcp_http_daemon", False)
    }
    handler = next(
        (
            item
            for item in existing_handlers
            if isinstance(item, RotatingFileHandler) and item.baseFilename == resolved_path
        ),
        None,
    )

    stale_handlers = existing_handlers - ({handler} if handler is not None else set())
    if stale_handlers:
        for target in targets:
            for item in list(target.handlers):
                if item in stale_handlers:
                    target.removeHandler(item)
        for item in stale_handlers:
            item.close()

    if handler is None:
        handler = RotatingFileHandler(
            resolved_path,
            maxBytes=HTTP_LOG_MAX_BYTES,
            backupCount=HTTP_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        handler._sketchup_mcp_http_daemon = True

    for target in targets:
        if handler not in target.handlers:
            target.addHandler(handler)

    # These logger trees are captured directly, exactly once, while unrelated
    # loggers still flow to root. This change only occurs in HTTP main.
    for target in targets[1:]:
        target.setLevel(logging.INFO)
        target.propagate = False
    return logging.getLogger(HTTP_LOGGER_NAME)


async def healthz(_: Request) -> Response:
    return JSONResponse(
        {
            "status": "ok",
            "server_version": server.__version__,
            "uptime_seconds": max(0, int(time.monotonic() - _HTTP_STARTED_MONOTONIC)),
        }
    )


def create_http_app(
    *,
    sketchup_port: Optional[int] = None,
    allow_autostart: bool = False,
    sketchup_executable: Optional[str] = None,
    startup_timeout: Optional[float] = None,
    request_timeout_ms: Optional[int] = None,
    session_idle_timeout: float = DEFAULT_SESSION_IDLE_TIMEOUT,
):
    """Build the stateful `/mcp` app and configure its launch-time defaults.

    FastMCP's Streamable HTTP transport is intentionally left stateful. Its
    transport object becomes ``ctx.session``, which the shared tools use as the
    sole key for client-selected SketchUp defaults and temporary consent.
    """
    token = required_http_token()
    if session_idle_timeout <= 0:
        raise ValueError("session_idle_timeout must be a positive number")
    server.configure_service_defaults(
        sketchup_port=sketchup_port,
        allow_autostart=allow_autostart,
        sketchup_executable=sketchup_executable,
        startup_timeout=startup_timeout,
        request_timeout_ms=request_timeout_ms,
    )

    # HTTP and stdio share the one registered FastMCP tool server. Sessions are
    # still independently stateful at the Streamable HTTP transport layer.
    app = server.mcp.streamable_http_app()
    manager = server.mcp.session_manager
    manager.session_idle_timeout = session_idle_timeout

    for index, route in enumerate(app.router.routes):
        if getattr(route, "path", None) == "/mcp":
            app.router.routes[index] = Route(
                "/mcp",
                endpoint=StreamableHTTPSessionCleanup(route.endpoint, manager),
            )
            break
    app.router.routes.insert(0, Route("/healthz", endpoint=healthz, methods=["GET"]))
    app.add_middleware(StaticBearerTokenMiddleware, token=token)
    return app


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run SketchUp MCP over authenticated Streamable HTTP")
    parser.add_argument("--host", default=DEFAULT_HTTP_HOST, help="Loopback address to bind (default: 127.0.0.1)")
    parser.add_argument("--http-port", type=_port, default=DEFAULT_HTTP_PORT, help="HTTP listen port (default: 8765)")
    parser.add_argument(
        "--sketchup-port",
        type=_port,
        default=DEFAULT_SKETCHUP_PORT,
        help="Default SketchUp Ruby listener port for new MCP sessions (default: 9876)",
    )
    parser.add_argument("--allow-autostart", action="store_true", help="Preapprove SketchUp startup for this HTTP service")
    parser.add_argument("--sketchup-executable", help="Path to SketchUp.exe used by autostart")
    parser.add_argument("--startup-timeout", type=_positive_float, help="Seconds to wait after autostart")
    parser.add_argument("--request-timeout-ms", type=_positive_int, help="Ruby request timeout in milliseconds")
    parser.add_argument(
        "--session-idle-timeout",
        type=_positive_float,
        default=DEFAULT_SESSION_IDLE_TIMEOUT,
        help="Seconds before an inactive HTTP MCP session is reclaimed (default: 1800)",
    )
    args = parser.parse_args(argv)
    try:
        args.host = validate_loopback_host(args.host)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main(argv: Optional[Iterable[str]] = None) -> None:
    args = parse_args(argv)
    app = create_http_app(
        sketchup_port=args.sketchup_port,
        allow_autostart=args.allow_autostart,
        sketchup_executable=args.sketchup_executable,
        startup_timeout=args.startup_timeout,
        request_timeout_ms=args.request_timeout_ms,
        session_idle_timeout=args.session_idle_timeout,
    )

    import uvicorn

    # Deliberately do not start server.start_idle_watchdog(): HTTP sessions are
    # stateful and may remain idle between normal client requests.
    logger = configure_http_logging()
    logger.info("Starting loopback Streamable HTTP service on %s:%s", args.host, args.http_port)
    uvicorn.run(app, host=args.host, port=args.http_port, log_config=None)


if __name__ == "__main__":
    main()
