from mcp.server.fastmcp import FastMCP, Context
import socket
import json
import asyncio
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, Dict, Any, Iterator, List, Optional, Tuple

from . import instance_registry, modal_guard, startup

# Configure logging
logging.basicConfig(level=logging.INFO, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("SketchupMCPServer")

# Define version directly to avoid pkg_resources dependency
__version__ = "0.2.0"
logger.info(f"SketchupMCP Server version {__version__} starting up")

DEFAULT_SKETCHUP_HOST = "localhost"
DEFAULT_SKETCHUP_PORT = 9876
SKETCHUP_PORT_ENV = "SKETCHUP_MCP_PORT"
DEFAULT_REQUEST_TIMEOUT_MS = 15_000
DISCOVERY_REQUEST_TIMEOUT_MS = 2_000
DISCOVERY_MAX_WORKERS = 16
REQUEST_TIMEOUT_ENV = "SKETCHUP_MCP_REQUEST_TIMEOUT_MS"
DEFAULT_IDLE_TIMEOUT_SEC = 0.0
IDLE_TIMEOUT_ENV = "SKETCHUP_MCP_IDLE_TIMEOUT_SEC"
CONTENT_LENGTH_PREFIX = b"Content-Length:"

_sketchup_port_override: Optional[int] = None
_port_override_lock = threading.RLock()
_port_locks: Dict[Tuple[str, int], threading.RLock] = {}
_port_locks_lock = threading.Lock()
_last_activity_monotonic = time.monotonic()
_active_request_count = 0
_activity_lock = threading.Lock()
_idle_watchdog_started = False

def _frame_json_payload(payload: bytes) -> bytes:
    return f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload

def _looks_like_framed_response(data: bytes) -> bool:
    prefix = CONTENT_LENGTH_PREFIX.lower()
    lowered = data.lower()
    return lowered.startswith(prefix) or prefix.startswith(lowered)

def _extract_framed_body(data: bytes) -> bytes | None:
    header_end = data.find(b"\r\n\r\n")
    if header_end == -1:
        return None

    header = data[:header_end].decode("ascii", errors="replace")
    content_length = None
    for line in header.split("\r\n"):
        name, separator, value = line.partition(":")
        if separator and name.strip().lower() == "content-length":
            content_length = int(value.strip())
            break

    if content_length is None or content_length < 0:
        raise ValueError("Missing or invalid Content-Length header")

    body_start = header_end + len(b"\r\n\r\n")
    body_end = body_start + content_length
    if len(data) < body_end:
        return None

    return data[body_start:body_end]

def _request_prevents_modal_hang(request: Dict[str, Any]) -> bool:
    if request.get("method") != "tools/call":
        return False

    params = request.get("params")
    if not isinstance(params, dict) or params.get("name") != "eval_ruby":
        return False

    arguments = params.get("arguments")
    return isinstance(arguments, dict) and arguments.get("prevent_modal_hang") is True

def mark_activity_started() -> None:
    global _active_request_count, _last_activity_monotonic
    with _activity_lock:
        _active_request_count += 1
        _last_activity_monotonic = time.monotonic()

def mark_activity_finished() -> None:
    global _active_request_count, _last_activity_monotonic
    with _activity_lock:
        _active_request_count = max(0, _active_request_count - 1)
        _last_activity_monotonic = time.monotonic()

def _server_is_idle_too_long(now: float, timeout_sec: float) -> bool:
    if timeout_sec <= 0:
        return False

    with _activity_lock:
        if _active_request_count > 0:
            return False
        return now - _last_activity_monotonic > timeout_sec

def start_idle_watchdog() -> None:
    global _idle_watchdog_started
    if _idle_watchdog_started:
        return

    timeout_sec = get_idle_timeout_sec()
    if timeout_sec <= 0:
        logger.info("MCP idle watchdog disabled")
        return

    _idle_watchdog_started = True

    def watch_idle_timeout() -> None:
        interval = min(60.0, max(1.0, timeout_sec / 4.0))
        while True:
            time.sleep(interval)
            if _server_is_idle_too_long(time.monotonic(), timeout_sec):
                logger.info("MCP server idle for %.1f seconds; exiting", timeout_sec)
                os._exit(0)

    thread = threading.Thread(target=watch_idle_timeout, name="sketchup-mcp-idle-watchdog", daemon=True)
    thread.start()

@dataclass
class SketchupConnection:
    host: str
    port: int
    sock: socket.socket = None
    
    def connect(self, allow_autostart: bool = True) -> bool:
        """Connect to the Sketchup extension socket server"""
        if self.sock:
            try:
                # Test if connection is still alive
                self.sock.settimeout(0.1)
                self.sock.send(b'')
                return True
            except (socket.error, BrokenPipeError, ConnectionResetError):
                # Connection is dead, close it and reconnect
                logger.info("Connection test failed, reconnecting...")
                self.disconnect()

        if self._connect_once():
            return True

        if allow_autostart and startup.maybe_start_sketchup(self.port):
            logger.info("Sketchup autostart requested; waiting for port %s", self.port)
            if self._connect_once():
                return True

            startup_timeout = startup.get_startup_timeout()
            if startup_timeout <= 0:
                return False

            deadline = time.monotonic() + startup_timeout
            while time.monotonic() <= deadline:
                time.sleep(0.5)
                if self._connect_once():
                    return True

        return False

    def _connect_once(self) -> bool:
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.host, self.port))
            logger.info(f"Connected to Sketchup at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Sketchup: {str(e)}")
            self.sock = None
            return False
    
    def disconnect(self):
        """Disconnect from the Sketchup extension"""
        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                logger.error(f"Error disconnecting from Sketchup: {str(e)}")
            finally:
                self.sock = None

    def receive_full_response(self, sock, buffer_size=8192, timeout_ms: Optional[int] = None):
        """Receive the complete response, potentially in multiple chunks"""
        chunks = []
        effective_timeout_ms = get_request_timeout_ms() if timeout_ms is None else timeout_ms
        sock.settimeout(effective_timeout_ms / 1000)
        
        try:
            while True:
                try:
                    chunk = sock.recv(buffer_size)
                    if not chunk:
                        if not chunks:
                            raise Exception("Connection closed before receiving any data")
                        break
                    
                    chunks.append(chunk)
                    
                    try:
                        data = b''.join(chunks)
                        if _looks_like_framed_response(data):
                            body = _extract_framed_body(data)
                            if body is None:
                                continue
                            logger.info(f"Received complete framed response ({len(body)} bytes)")
                            return body

                        json.loads(data.decode('utf-8'))
                        logger.info(f"Received complete response ({len(data)} bytes)")
                        return data
                    except json.JSONDecodeError:
                        continue
                except socket.timeout:
                    logger.warning("Socket timeout during chunked receive")
                    if not chunks:
                        raise
                    break
                except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                    logger.error(f"Socket connection error during receive: {str(e)}")
                    raise
        except socket.timeout:
            logger.warning("Socket timeout during chunked receive")
            raise
        except Exception as e:
            logger.error(f"Error during receive: {str(e)}")
            raise
            
        if chunks:
            data = b''.join(chunks)
            logger.info(f"Returning data after receive completion ({len(data)} bytes)")
            if _looks_like_framed_response(data):
                body = _extract_framed_body(data)
                if body is None:
                    raise Exception("Incomplete framed response received")
                return body

            try:
                json.loads(data.decode('utf-8'))
                return data
            except json.JSONDecodeError:
                raise Exception("Incomplete JSON response received")
        else:
            raise Exception("No data received")

    def send_command(
        self,
        method: str,
        params: Dict[str, Any] = None,
        request_id: Any = None,
        *,
        allow_autostart: bool = True,
        request_timeout_ms: Optional[int] = None,
        max_retries: int = 2,
    ) -> Dict[str, Any]:
        """Send a JSON-RPC request to Sketchup and return the response"""
        mark_activity_started()
        try:
            return self._send_command(
                method,
                params,
                request_id,
                allow_autostart=allow_autostart,
                request_timeout_ms=request_timeout_ms,
                max_retries=max_retries,
            )
        finally:
            mark_activity_finished()

    def _send_command(
        self,
        method: str,
        params: Dict[str, Any] = None,
        request_id: Any = None,
        *,
        allow_autostart: bool = True,
        request_timeout_ms: Optional[int] = None,
        max_retries: int = 2,
    ) -> Dict[str, Any]:
        """Send a JSON-RPC request to Sketchup and return the response"""
        effective_timeout_ms = get_request_timeout_ms() if request_timeout_ms is None else int(request_timeout_ms)
        if effective_timeout_ms <= 0:
            raise ValueError("request_timeout_ms must be a positive integer")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")

        # Try to connect if not connected
        if not self.connect(allow_autostart=allow_autostart):
            raise ConnectionError("Not connected to Sketchup")
        
        # Ensure we're sending a proper JSON-RPC request
        if method == "tools/call" and params and "name" in params and "arguments" in params:
            # This is already in the correct format
            request = {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
                "id": request_id
            }
        else:
            # This is a direct command - convert to JSON-RPC
            command_name = method
            command_params = params or {}
            
            # Log the conversion
            logger.info(f"Converting direct command '{command_name}' to JSON-RPC format")
            
            request = {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": command_name,
                    "arguments": command_params
                },
                "id": request_id
            }
        
        retry_count = 0
        
        while retry_count <= max_retries:
            try:
                request["_mcp"] = {
                    "sent_at_ms": int(time.time() * 1000),
                    "timeout_ms": effective_timeout_ms,
                }
                logger.info(f"Sending JSON-RPC request: {request}")
                
                # Log the exact bytes being sent
                request_body = json.dumps(request).encode('utf-8')
                request_bytes = _frame_json_payload(request_body)
                logger.info(f"Raw bytes being sent: {request_bytes}")
                
                self.sock.sendall(request_bytes)
                logger.info(f"Request sent, waiting for response...")
                
                self.sock.settimeout(effective_timeout_ms / 1000)
                
                response_data = self.receive_full_response(self.sock, timeout_ms=effective_timeout_ms)
                logger.info(f"Received {len(response_data)} bytes of data")
                
                response = json.loads(response_data.decode('utf-8'))
                logger.info(f"Response parsed: {response}")

                if not isinstance(response, dict):
                    return response

                if "error" in response:
                    logger.error(f"Sketchup error: {response['error']}")
                    raise Exception(response["error"].get("message", "Unknown error from Sketchup"))

                return response.get("result", {})
                
            except (socket.timeout, ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                logger.warning(f"Connection error (attempt {retry_count+1}/{max_retries+1}): {str(e)}")
                if isinstance(e, socket.timeout) and _request_prevents_modal_hang(request):
                    modal = modal_guard.interrupt_modal_for_port(self.host, self.port, request_id=request_id)
                    if modal:
                        self.disconnect()
                        raise modal_guard.ModalGuardInterrupted(modal)

                retry_count += 1
                
                if retry_count <= max_retries:
                    logger.info(f"Retrying connection...")
                    self.disconnect()
                    if not self.connect(allow_autostart=allow_autostart):
                        logger.error("Failed to reconnect")
                        break
                else:
                    logger.error(f"Max retries reached, giving up")
                    self.sock = None
                    raise Exception(f"Connection to Sketchup lost after {max_retries+1} attempts: {str(e)}")
            
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON response from Sketchup: {str(e)}")
                if 'response_data' in locals() and response_data:
                    logger.error(f"Raw response (first 200 bytes): {response_data[:200]}")
                raise Exception(f"Invalid response from Sketchup: {str(e)}")
            
            except Exception as e:
                logger.error(f"Error communicating with Sketchup: {str(e)}")
                self.sock = None
                raise Exception(f"Communication error with Sketchup: {str(e)}")

# Request-scoped connection routing
def get_sketchup_host() -> str:
    return DEFAULT_SKETCHUP_HOST


def get_sketchup_port(port: Any = None) -> int:
    """Resolve an explicit request port before session and environment defaults."""
    if port is not None:
        return _parse_sketchup_port(port, "port")

    with _port_override_lock:
        if _sketchup_port_override is not None:
            return _sketchup_port_override

    raw_port = os.environ.get(SKETCHUP_PORT_ENV)
    if raw_port is None or raw_port.strip() == "":
        return DEFAULT_SKETCHUP_PORT

    return _parse_sketchup_port(raw_port, SKETCHUP_PORT_ENV)

def get_request_timeout_ms() -> int:
    raw_timeout = os.environ.get(REQUEST_TIMEOUT_ENV)
    if raw_timeout is None or raw_timeout.strip() == "":
        return DEFAULT_REQUEST_TIMEOUT_MS

    try:
        timeout_ms = int(raw_timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{REQUEST_TIMEOUT_ENV} must be a positive integer") from exc

    if timeout_ms <= 0:
        raise ValueError(f"{REQUEST_TIMEOUT_ENV} must be a positive integer")
    return timeout_ms

def get_idle_timeout_sec() -> float:
    raw_timeout = os.environ.get(IDLE_TIMEOUT_ENV)
    if raw_timeout is None or raw_timeout.strip() == "":
        return DEFAULT_IDLE_TIMEOUT_SEC

    try:
        timeout_sec = float(raw_timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{IDLE_TIMEOUT_ENV} must be a non-negative number") from exc

    if timeout_sec < 0:
        raise ValueError(f"{IDLE_TIMEOUT_ENV} must be a non-negative number")
    return timeout_sec

def _parse_sketchup_port(value: Any, source: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source} must be an integer from 1 to 65535") from exc

    if port < 1 or port > 65535:
        raise ValueError(f"{source} must be an integer from 1 to 65535")
    return port

def set_sketchup_port_override(port: Any) -> int:
    global _sketchup_port_override

    parsed_port = _parse_sketchup_port(port, "port")
    with _port_override_lock:
        _sketchup_port_override = parsed_port
    return parsed_port


def clear_sketchup_port_override() -> None:
    global _sketchup_port_override
    with _port_override_lock:
        _sketchup_port_override = None


def _connection_lock(host: str, port: int) -> threading.RLock:
    key = (host, port)
    with _port_locks_lock:
        lock = _port_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _port_locks[key] = lock
        return lock


def _connection_error(host: str, port: int, *, allow_autostart: bool) -> ConnectionError:
    permission_hint = ""
    if allow_autostart and not startup.autostart_allowed():
        permission_hint = (
            " Ask the user whether to start SketchUp. If the user agrees, call "
            "`allow_sketchup_autostart` and retry the SketchUp tool call."
        )
    return ConnectionError(
        "Could not connect to Sketchup at "
        f"{host}:{port}. Make sure the Sketchup extension is running.{permission_hint}"
    )


def get_sketchup_connection(allow_autostart: bool = True, port: Any = None) -> SketchupConnection:
    """Create a non-cached connection for compatibility with direct callers."""
    host = get_sketchup_host()
    target_port = get_sketchup_port(port)
    connection = SketchupConnection(host=host, port=target_port)
    if not connection.connect(allow_autostart=allow_autostart):
        connection.disconnect()
        raise _connection_error(host, target_port, allow_autostart=allow_autostart)
    return connection


@contextmanager
def _request_connection(
    port: Any = None,
    *,
    allow_autostart: bool = True,
) -> Iterator[Tuple[SketchupConnection, Dict[str, Any]]]:
    """Open exactly one serialized Ruby TCP connection for a resolved target."""
    host = get_sketchup_host()
    target_port = get_sketchup_port(port)
    target = {"host": host, "port": target_port}

    with _connection_lock(host, target_port):
        connection = SketchupConnection(host=host, port=target_port)
        if not connection.connect(allow_autostart=allow_autostart):
            connection.disconnect()
            raise _connection_error(host, target_port, allow_autostart=allow_autostart)
        try:
            yield connection, target
        finally:
            connection.disconnect()


def _send_ruby_tool_sync(
    name: str,
    arguments: Dict[str, Any],
    request_id: Any,
    *,
    port: Any = None,
    allow_autostart: bool = True,
    request_timeout_ms: Optional[int] = None,
    max_retries: int = 2,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    with _request_connection(port, allow_autostart=allow_autostart) as (connection, target):
        result = connection.send_command(
            method="tools/call",
            params={"name": name, "arguments": arguments},
            request_id=request_id,
            allow_autostart=allow_autostart,
            request_timeout_ms=request_timeout_ms,
            max_retries=max_retries,
        )
    return result, target


async def _send_ruby_tool(
    name: str,
    arguments: Dict[str, Any],
    request_id: Any,
    *,
    port: Any = None,
    allow_autostart: bool = True,
    request_timeout_ms: Optional[int] = None,
    max_retries: int = 2,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    return await asyncio.to_thread(
        _send_ruby_tool_sync,
        name,
        arguments,
        request_id,
        port=port,
        allow_autostart=allow_autostart,
        request_timeout_ms=request_timeout_ms,
        max_retries=max_retries,
    )


def _tool_error(exc: BaseException, port: Any = None) -> str:
    payload: Dict[str, Any] = {"success": False, "host": get_sketchup_host(), "error": str(exc)}
    try:
        payload["port"] = get_sketchup_port(port)
    except ValueError:
        payload["port"] = port
    return json.dumps(payload)


def _request_id(ctx: Context) -> Any:
    return getattr(ctx, "request_id", None)


def _result_text(result: Dict[str, Any]) -> Any:
    content = result.get("content")
    if not isinstance(content, list) or not content:
        raise ValueError("SketchUp response did not contain tool content")
    first = content[0]
    if not isinstance(first, dict) or "text" not in first:
        raise ValueError("SketchUp response did not contain text content")
    return first["text"]


def _get_instance_info_sync(port: Any = None) -> Dict[str, Any]:
    result, target = _send_ruby_tool_sync(
        "get_instance_info",
        {},
        request_id="instance-info",
        port=port,
        allow_autostart=False,
        request_timeout_ms=DISCOVERY_REQUEST_TIMEOUT_MS,
        max_retries=0,
    )
    raw_info = _result_text(result)
    info = json.loads(raw_info) if isinstance(raw_info, str) else raw_info
    if not isinstance(info, dict):
        raise ValueError("SketchUp instance info must be an object")
    return {**info, **target}


def _discovery_candidates(ports: Optional[List[int]]) -> Dict[int, Dict[str, Any]]:
    candidates: Dict[int, Dict[str, Any]] = {}
    for entry in instance_registry.load_registered_instances():
        candidates[entry["port"]] = entry

    for port in ports or []:
        parsed_port = _parse_sketchup_port(port, "ports")
        candidates.setdefault(parsed_port, {"port": parsed_port})
    return candidates


def _list_sketchup_instances_sync(ports: Optional[List[int]]) -> Dict[str, Any]:
    instances: List[Dict[str, Any]] = []
    unavailable: List[Dict[str, Any]] = []

    candidates = sorted(_discovery_candidates(ports).items())
    if not candidates:
        return {"success": True, "instances": [], "unavailable": []}

    def probe(candidate: Tuple[int, Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
        port, registry_entry = candidate
        try:
            info = _get_instance_info_sync(port)
            return "ready", {"status": "ready", "registered": "registry_path" in registry_entry, **info}
        except Exception as exc:
            return "unavailable", {
                "port": port,
                "pid": registry_entry.get("pid"),
                "registered": "registry_path" in registry_entry,
                "status": "unavailable",
                "error": str(exc),
            }

    with ThreadPoolExecutor(
        max_workers=min(len(candidates), DISCOVERY_MAX_WORKERS),
        thread_name_prefix="sketchup-discovery",
    ) as executor:
        for status, payload in executor.map(probe, candidates):
            (instances if status == "ready" else unavailable).append(payload)

    return {"success": True, "instances": instances, "unavailable": unavailable}

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage the MCP process without binding it to one SketchUp listener."""
    try:
        logger.info("SketchupMCP server starting up")
        yield {}
    finally:
        logger.info("SketchupMCP server shut down")

# Create MCP server with lifespan support
mcp = FastMCP(
    "SketchupMCP",
    instructions=(
        "SketchUp integration through the Model Context Protocol. If the user specifies "
        "a SketchUp listener port, pass that port directly to every SketchUp tool call. "
        "Explicit tool port takes precedence over the session default and environment. "
        "If the target instance is unclear, call list_sketchup_instances before any "
        "model-changing tool. Never fall back to another port after an explicit-port "
        "failure. Transport closed refers to the MCP stdio bridge, not a Ruby TCP-port response."
    ),
    lifespan=server_lifespan
)

# Tool endpoints
@mcp.tool()
def allow_sketchup_autostart(ctx: Context, allowed: bool = True) -> str:
    """Allow this MCP server process to start SketchUp when the Ruby port is unreachable."""
    startup.set_session_autostart_allowed(allowed)
    return json.dumps({
        "success": True,
        "autostart_allowed": startup.autostart_allowed(),
        "session_autostart_allowed": bool(allowed),
    })

@mcp.tool()
def set_connection_port(ctx: Context, port: int) -> str:
    """Set the default port used only when a SketchUp tool call omits port."""
    try:
        target_port = set_sketchup_port_override(port)
        return json.dumps({
            "success": True,
            "host": get_sketchup_host(),
            "port": target_port,
        })
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e),
        })

@mcp.tool()
async def get_instance_info(ctx: Context, port: int | None = None) -> str:
    """Read a target SketchUp listener's identity and active-model fingerprint."""
    try:
        info = await asyncio.to_thread(_get_instance_info_sync, port)
        return json.dumps({"success": True, "instance": info})
    except Exception as e:
        return _tool_error(e, port)

@mcp.tool()
async def list_sketchup_instances(ctx: Context, ports: List[int] | None = None) -> str:
    """List registered local SketchUp listeners and explicitly supplied ports only."""
    try:
        return json.dumps(await asyncio.to_thread(_list_sketchup_instances_sync, ports))
    except Exception as e:
        return _tool_error(e)


@mcp.tool()
async def get_modal_state(ctx: Context, port: int | None = None) -> str:
    """Inspect a target listener's SketchUp modal state without sending Ruby code."""
    try:
        target_port = get_sketchup_port(port)
        result = await asyncio.to_thread(
            modal_guard.modal_state_for_port,
            get_sketchup_host(),
            target_port,
            request_id=_request_id(ctx),
        )
        return json.dumps(result)
    except Exception as e:
        return _tool_error(e, port)


@mcp.tool()
async def close_modal(ctx: Context, port: int | None = None) -> str:
    """Close a target listener's detected modal window without waiting for Ruby TCP work."""
    try:
        target_port = get_sketchup_port(port)
        result = await asyncio.to_thread(
            modal_guard.close_modal_for_port,
            get_sketchup_host(),
            target_port,
            request_id=_request_id(ctx),
        )
        return json.dumps(result)
    except Exception as e:
        return _tool_error(e, port)

@mcp.tool()
async def create_component(
    ctx: Context,
    type: str = "cube",
    position: List[float] = None,
    dimensions: List[float] = None,
    port: int | None = None,
) -> str:
    """Create a component in the requested SketchUp listener."""
    try:
        result, _target = await _send_ruby_tool(
            "create_component",
            {"type": type, "position": position or [0, 0, 0], "dimensions": dimensions or [1, 1, 1]},
            _request_id(ctx),
            port=port,
        )
        return json.dumps(result)
    except Exception as e:
        return _tool_error(e, port)

@mcp.tool()
async def delete_component(
    ctx: Context,
    id: str,
    port: int | None = None,
) -> str:
    """Delete a component from the requested SketchUp listener."""
    try:
        result, _target = await _send_ruby_tool("delete_component", {"id": id}, _request_id(ctx), port=port)
        return json.dumps(result)
    except Exception as e:
        return _tool_error(e, port)

@mcp.tool()
async def transform_component(
    ctx: Context,
    id: str,
    position: List[float] = None,
    rotation: List[float] = None,
    scale: List[float] = None,
    port: int | None = None,
) -> str:
    """Transform a component in the requested SketchUp listener."""
    try:
        arguments = {"id": id}
        if position is not None:
            arguments["position"] = position
        if rotation is not None:
            arguments["rotation"] = rotation
        if scale is not None:
            arguments["scale"] = scale
        result, _target = await _send_ruby_tool("transform_component", arguments, _request_id(ctx), port=port)
        return json.dumps(result)
    except Exception as e:
        return _tool_error(e, port)

@mcp.tool()
async def get_selection(ctx: Context, port: int | None = None) -> str:
    """Get the selection from the requested SketchUp listener."""
    try:
        result, _target = await _send_ruby_tool("get_selection", {}, _request_id(ctx), port=port)
        return json.dumps(result)
    except Exception as e:
        return _tool_error(e, port)

@mcp.tool()
async def set_material(
    ctx: Context,
    id: str,
    material: str,
    port: int | None = None,
) -> str:
    """Set material in the requested SketchUp listener."""
    try:
        result, _target = await _send_ruby_tool(
            "set_material", {"id": id, "material": material}, _request_id(ctx), port=port
        )
        return json.dumps(result)
    except Exception as e:
        return _tool_error(e, port)

@mcp.tool()
async def export_scene(
    ctx: Context,
    format: str = "skp",
    port: int | None = None,
) -> str:
    """Export the scene from the requested SketchUp listener."""
    try:
        result, _target = await _send_ruby_tool("export", {"format": format}, _request_id(ctx), port=port)
        return json.dumps(result)
    except Exception as e:
        return _tool_error(e, port)

@mcp.tool()
async def capture_review_views(
    ctx: Context,
    persistent_id: int,
    output_dir: str = "",
    hide_others: bool = True,
    width: int = 1600,
    height: int = 1200,
    port: int | None = None,
) -> str:
    """Capture review views from the requested SketchUp listener."""
    try:
        result, target = await _send_ruby_tool(
            "capture_review_views",
            {
                "persistent_id": persistent_id,
                "output_dir": output_dir,
                "hide_others": hide_others,
                "width": width,
                "height": height,
            },
            _request_id(ctx),
            port=port,
        )
        response = {
            "success": True,
            "result": _result_text(result),
            "target": target,
        }
        return json.dumps(response)
    except Exception as e:
        return _tool_error(e, port)

@mcp.tool()
async def create_mortise_tenon(
    ctx: Context,
    mortise_id: str,
    tenon_id: str,
    width: float = 1.0,
    height: float = 1.0,
    depth: float = 1.0,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    offset_z: float = 0.0,
    port: int | None = None,
) -> str:
    """Create a mortise and tenon joint in the requested SketchUp listener."""
    try:
        result, _target = await _send_ruby_tool(
            "create_mortise_tenon",
            {
                "mortise_id": mortise_id,
                "tenon_id": tenon_id,
                "width": width,
                "height": height,
                "depth": depth,
                "offset_x": offset_x,
                "offset_y": offset_y,
                "offset_z": offset_z,
            },
            _request_id(ctx),
            port=port,
        )
        return json.dumps(result)
    except Exception as e:
        return _tool_error(e, port)

@mcp.tool()
async def create_dovetail(
    ctx: Context,
    tail_id: str,
    pin_id: str,
    width: float = 1.0,
    height: float = 1.0,
    depth: float = 1.0,
    angle: float = 15.0,
    num_tails: int = 3,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    offset_z: float = 0.0,
    port: int | None = None,
) -> str:
    """Create a dovetail joint in the requested SketchUp listener."""
    try:
        result, _target = await _send_ruby_tool(
            "create_dovetail",
            {
                "tail_id": tail_id,
                "pin_id": pin_id,
                "width": width,
                "height": height,
                "depth": depth,
                "angle": angle,
                "num_tails": num_tails,
                "offset_x": offset_x,
                "offset_y": offset_y,
                "offset_z": offset_z,
            },
            _request_id(ctx),
            port=port,
        )
        return json.dumps(result)
    except Exception as e:
        return _tool_error(e, port)

@mcp.tool()
async def create_finger_joint(
    ctx: Context,
    board1_id: str,
    board2_id: str,
    width: float = 1.0,
    height: float = 1.0,
    depth: float = 1.0,
    num_fingers: int = 5,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    offset_z: float = 0.0,
    port: int | None = None,
) -> str:
    """Create a finger joint in the requested SketchUp listener."""
    try:
        result, _target = await _send_ruby_tool(
            "create_finger_joint",
            {
                "board1_id": board1_id,
                "board2_id": board2_id,
                "width": width,
                "height": height,
                "depth": depth,
                "num_fingers": num_fingers,
                "offset_x": offset_x,
                "offset_y": offset_y,
                "offset_z": offset_z,
            },
            _request_id(ctx),
            port=port,
        )
        return json.dumps(result)
    except Exception as e:
        return _tool_error(e, port)

@mcp.tool()
async def eval_ruby(
    ctx: Context,
    code: str,
    prevent_modal_hang: bool = False,
    port: int | None = None,
) -> str:
    """Evaluate Ruby in the requested SketchUp listener."""
    try:
        arguments = {"code": code}
        if prevent_modal_hang:
            arguments["prevent_modal_hang"] = True
        result, target = await _send_ruby_tool("eval_ruby", arguments, _request_id(ctx), port=port)
        response = {
            "success": True,
            "result": _result_text(result),
            "target": target,
        }
        if "ui_events" in result:
            response["ui_events"] = result["ui_events"]
        return json.dumps(response)
    except modal_guard.ModalGuardInterrupted as e:
        payload = e.to_payload()
        payload["host"] = get_sketchup_host()
        payload["port"] = get_sketchup_port(port)
        return json.dumps(payload)
    except Exception as e:
        return _tool_error(e, port)

def main():
    start_idle_watchdog()
    mcp.run()

if __name__ == "__main__":
    main()
