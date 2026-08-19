import json
import os
import subprocess
import tempfile
import threading
import weakref
from pathlib import Path
from typing import Any, Optional


AUTOSTART_ENV = "SKETCHUP_MCP_AUTOSTART"
SKETCHUP_EXE_ENV = "SKETCHUP_MCP_SKETCHUP_EXE"
STARTUP_TIMEOUT_ENV = "SKETCHUP_MCP_STARTUP_TIMEOUT"
DEFAULT_STARTUP_TIMEOUT = 45.0

# Direct library callers from older releases did not have an MCP session. Keep a
# narrow compatibility fallback for those callers only; MCP tool requests pass
# their actual ctx.session and never share this value.
_legacy_session_autostart_allowed = False
_session_autostart_allowed: "weakref.WeakKeyDictionary[Any, bool]" = weakref.WeakKeyDictionary()
_session_autostart_lock = threading.RLock()


def _truthy(value: Optional[str]) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def autostart_enabled() -> bool:
    return _truthy(os.environ.get(AUTOSTART_ENV))


def set_session_autostart_allowed(allowed: bool, session: Any = None) -> None:
    """Record temporary autostart consent for one MCP session.

    ``session=None`` preserves the old direct-call behavior. Real MCP requests
    always provide ``ctx.session``, which makes the consent independent for
    every stdio or Streamable HTTP session and lets weak references reclaim it.
    """
    global _legacy_session_autostart_allowed
    if session is None:
        _legacy_session_autostart_allowed = bool(allowed)
        return

    with _session_autostart_lock:
        try:
            _session_autostart_allowed[session] = bool(allowed)
        except TypeError:
            # A non-weak-referenceable session is not a valid MCP transport
            # session. Do not silently promote its consent to process scope.
            raise ValueError("MCP session does not support isolated autostart state")


def clear_session_autostart_allowed(session: Any = None) -> None:
    """Clear one session's temporary consent, or the legacy fallback."""
    global _legacy_session_autostart_allowed
    if session is None:
        _legacy_session_autostart_allowed = False
        return

    with _session_autostart_lock:
        _session_autostart_allowed.pop(session, None)


def autostart_allowed(session: Any = None, *, service_preapproved: bool = False) -> bool:
    """Return effective autostart permission without leaking session consent."""
    if autostart_enabled() or service_preapproved:
        return True

    if session is None:
        return _legacy_session_autostart_allowed

    with _session_autostart_lock:
        return bool(_session_autostart_allowed.get(session, False))


def get_startup_timeout() -> float:
    raw_timeout = os.environ.get(STARTUP_TIMEOUT_ENV)
    if raw_timeout is None or raw_timeout.strip() == "":
        return DEFAULT_STARTUP_TIMEOUT

    try:
        timeout = float(raw_timeout)
    except ValueError as exc:
        raise ValueError(f"{STARTUP_TIMEOUT_ENV} must be a positive number") from exc

    if timeout <= 0:
        raise ValueError(f"{STARTUP_TIMEOUT_ENV} must be a positive number")
    return timeout


def maybe_start_sketchup(
    port: int,
    *,
    session: Any = None,
    service_preapproved: bool = False,
    sketchup_exe: Optional[str] = None,
) -> bool:
    if not autostart_allowed(session, service_preapproved=service_preapproved):
        return False

    # Keep the legacy call shape when no launch override is in effect. Older
    # embedding code and tests patch ``launch_sketchup(port)`` directly.
    if sketchup_exe is None:
        launch_sketchup(port)
    else:
        launch_sketchup(port, sketchup_exe=sketchup_exe)
    return True


def launch_sketchup(port: int, sketchup_exe: Optional[str] = None) -> Path:
    exe_path = resolve_sketchup_exe(sketchup_exe)
    startup_script = write_rubystartup_script(port)

    subprocess.Popen(
        [str(exe_path), "-RubyStartup", str(startup_script)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    return startup_script


def resolve_sketchup_exe(sketchup_exe: Optional[str] = None) -> Path:
    raw_path = sketchup_exe or os.environ.get(SKETCHUP_EXE_ENV)
    if raw_path:
        path = Path(raw_path)
        if path.is_file():
            return path
        raise FileNotFoundError(f"SketchUp executable not found: {path}")

    discovered = discover_sketchup_exe()
    if discovered is not None:
        return discovered

    raise FileNotFoundError(
        f"SketchUp executable not found. Set {SKETCHUP_EXE_ENV} to SketchUp.exe."
    )


def discover_sketchup_exe() -> Optional[Path]:
    roots = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
    ]

    for root in [Path(value) for value in roots if value]:
        for year in range(2026, 2016, -1):
            candidate = root / "SketchUp" / f"SketchUp {year}" / "SketchUp.exe"
            if candidate.is_file():
                return candidate
    return None


def write_rubystartup_script(port: int) -> Path:
    script_path = Path(tempfile.gettempdir()) / f"sketchup_mcp_start_{port}.rb"
    script_path.write_text(rubystartup_script(port), encoding="utf-8")
    return script_path


def rubystartup_script(port: int) -> str:
    local_main = _local_ruby_main()
    local_main_literal = json.dumps(str(local_main).replace("\\", "/")) if local_main else "nil"

    return f"""# Auto-generated by sketchup-mcp.
begin
  require 'sketchup'

  loaded = false
  begin
    require 'su_mcp/main'
    loaded = !!defined?(SU_MCP)
  rescue LoadError
    loaded = false
  end

  local_main = {local_main_literal}
  if !loaded && local_main && File.exist?(local_main)
    load local_main
    loaded = !!defined?(SU_MCP)
  end

  raise 'Could not load Sketchup MCP Ruby extension' unless loaded && SU_MCP.respond_to?(:start_server)

  UI.start_timer(0.2, false) do
    SU_MCP.start_server({int(port)})
  end
rescue Exception => e
  message = "Failed to start Sketchup MCP: #{{e.message}}"
  begin
    UI.messagebox(message)
  rescue Exception
  end
  puts message
  puts e.backtrace.join("\\n") if e.backtrace
end
"""


def _local_ruby_main() -> Optional[Path]:
    repo_root = Path(__file__).resolve().parents[2]
    candidate = repo_root / "su_mcp" / "su_mcp" / "main.rb"
    return candidate if candidate.is_file() else None
