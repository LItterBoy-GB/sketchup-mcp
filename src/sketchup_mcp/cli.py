import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Sequence, TextIO

from . import startup

DEFAULT_SKETCHUP_HOST = "localhost"
DEFAULT_SKETCHUP_PORT = 9876
SKETCHUP_PORT_ENV = "SKETCHUP_MCP_PORT"

SketchupConnection = None


def get_sketchup_host() -> str:
    return DEFAULT_SKETCHUP_HOST


def get_sketchup_port() -> int:
    raw_port = os.environ.get(SKETCHUP_PORT_ENV)
    if raw_port is None or raw_port.strip() == "":
        return DEFAULT_SKETCHUP_PORT
    return _port(raw_port)


def _connection_class():
    global SketchupConnection
    if SketchupConnection is None:
        from .server import SketchupConnection as ServerSketchupConnection

        SketchupConnection = ServerSketchupConnection
    return SketchupConnection


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from exc

    if port < 1 or port > 65_535:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sketchup-mcp-cli",
        description="Call the Sketchup Ruby MCP extension directly over TCP.",
    )
    parser.add_argument("--host", default=None, help="Sketchup Ruby extension host")
    parser.add_argument(
        "--port",
        type=_port,
        default=None,
        help=f"Sketchup Ruby extension port, defaults to SKETCHUP_MCP_PORT or {DEFAULT_SKETCHUP_PORT}",
    )
    parser.add_argument(
        "--start-sketchup-if-needed",
        action="store_true",
        help="Start SketchUp with -RubyStartup if the Ruby MCP port is not reachable",
    )
    parser.add_argument(
        "--sketchup-exe",
        default=None,
        help=f"Path to SketchUp.exe, defaults to auto-discovery or {startup.SKETCHUP_EXE_ENV}",
    )
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=None,
        help=f"Seconds to wait for SketchUp autostart, defaults to {startup.STARTUP_TIMEOUT_ENV} or {startup.DEFAULT_STARTUP_TIMEOUT:g}",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("ping", help="Check that the Sketchup Ruby extension TCP port is reachable")

    eval_parser = subparsers.add_parser("eval", help="Evaluate Ruby code in Sketchup")
    eval_parser.add_argument("code", nargs="?", help="Ruby code to evaluate")
    eval_parser.add_argument("-f", "--file", dest="code_file", help="Path to a Ruby file to evaluate")

    call_parser = subparsers.add_parser("call", help="Call a Sketchup Ruby extension tool")
    call_parser.add_argument("tool", help="Tool name, for example get_selection")
    call_parser.add_argument(
        "-a",
        "--arguments",
        default="{}",
        help="Tool arguments as a JSON object",
    )
    call_parser.add_argument("--arguments-file", help="Path to a JSON file containing tool arguments")

    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "eval":
        has_code = args.code is not None
        has_file = args.code_file is not None
        if has_code == has_file:
            parser.error("eval requires exactly one of inline code or --file")

    if args.command == "call" and args.arguments_file and args.arguments != "{}":
        parser.error("call accepts --arguments or --arguments-file, not both")

    return args


def load_eval_code(args: argparse.Namespace) -> str:
    if args.code_file:
        return Path(args.code_file).read_text(encoding="utf-8")
    return args.code


def load_call_arguments(args: argparse.Namespace) -> Dict[str, Any]:
    raw_arguments = (
        Path(args.arguments_file).read_text(encoding="utf-8")
        if args.arguments_file
        else args.arguments
    )
    parsed = json.loads(raw_arguments)
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments must be a JSON object")
    return parsed


def _connection(args: argparse.Namespace) -> Any:
    host = args.host or get_sketchup_host()
    port = args.port if args.port is not None else get_sketchup_port()
    return _connection_class()(host=host, port=port)


def apply_startup_options(args: argparse.Namespace) -> None:
    if args.start_sketchup_if_needed:
        os.environ[startup.AUTOSTART_ENV] = "1"
    if args.sketchup_exe:
        os.environ[startup.SKETCHUP_EXE_ENV] = args.sketchup_exe
    if args.startup_timeout is not None:
        if args.startup_timeout <= 0:
            raise ValueError("startup timeout must be a positive number")
        os.environ[startup.STARTUP_TIMEOUT_ENV] = str(args.startup_timeout)


def _write_json(stdout: TextIO, payload: Any) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")


def main(
    argv: Sequence[str] | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args = parse_args(argv)
    connection = None

    try:
        apply_startup_options(args)
        connection = _connection(args)
        if args.command == "ping":
            connected = connection.connect()
            _write_json(
                stdout,
                {
                    "success": connected,
                    "host": connection.host,
                    "port": connection.port,
                },
            )
            return 0 if connected else 1

        if args.command == "eval":
            result = connection.send_command(
                "eval_ruby",
                {"code": load_eval_code(args)},
                request_id=1,
            )
            _write_json(stdout, result)
            return 0

        if args.command == "call":
            result = connection.send_command(
                args.tool,
                load_call_arguments(args),
                request_id=1,
            )
            _write_json(stdout, result)
            return 0

        raise ValueError(f"Unsupported command: {args.command}")
    except Exception as exc:
        _write_json(stderr, {"success": False, "error": str(exc)})
        return 1
    finally:
        if connection is not None:
            connection.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
