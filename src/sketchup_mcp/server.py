from mcp.server.fastmcp import FastMCP, Context
import socket
import json
import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any, List

from . import startup

# Configure logging
logging.basicConfig(level=logging.INFO, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("SketchupMCPServer")

# Define version directly to avoid pkg_resources dependency
__version__ = "0.1.17"
logger.info(f"SketchupMCP Server version {__version__} starting up")

DEFAULT_SKETCHUP_HOST = "localhost"
DEFAULT_SKETCHUP_PORT = 9876
SKETCHUP_PORT_ENV = "SKETCHUP_MCP_PORT"
DEFAULT_REQUEST_TIMEOUT_MS = 15_000
REQUEST_TIMEOUT_ENV = "SKETCHUP_MCP_REQUEST_TIMEOUT_MS"
DEFAULT_IDLE_TIMEOUT_SEC = 3600.0
IDLE_TIMEOUT_ENV = "SKETCHUP_MCP_IDLE_TIMEOUT_SEC"
CONTENT_LENGTH_PREFIX = b"Content-Length:"

_sketchup_port_override = None
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
                try:
                    if _sketchup_connection is not None:
                        _sketchup_connection.disconnect()
                finally:
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

    def receive_full_response(self, sock, buffer_size=8192):
        """Receive the complete response, potentially in multiple chunks"""
        chunks = []
        sock.settimeout(get_request_timeout_ms() / 1000)
        
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
                    break
                except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                    logger.error(f"Socket connection error during receive: {str(e)}")
                    raise
        except socket.timeout:
            logger.warning("Socket timeout during chunked receive")
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

    def send_command(self, method: str, params: Dict[str, Any] = None, request_id: Any = None) -> Dict[str, Any]:
        """Send a JSON-RPC request to Sketchup and return the response"""
        mark_activity_started()
        try:
            return self._send_command(method, params, request_id)
        finally:
            mark_activity_finished()

    def _send_command(self, method: str, params: Dict[str, Any] = None, request_id: Any = None) -> Dict[str, Any]:
        """Send a JSON-RPC request to Sketchup and return the response"""
        # Try to connect if not connected
        if not self.connect():
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
        
        # Maximum number of retries
        max_retries = 2
        retry_count = 0
        
        while retry_count <= max_retries:
            try:
                request["_mcp"] = {
                    "sent_at_ms": int(time.time() * 1000),
                    "timeout_ms": get_request_timeout_ms(),
                }
                logger.info(f"Sending JSON-RPC request: {request}")
                
                # Log the exact bytes being sent
                request_body = json.dumps(request).encode('utf-8')
                request_bytes = _frame_json_payload(request_body)
                logger.info(f"Raw bytes being sent: {request_bytes}")
                
                self.sock.sendall(request_bytes)
                logger.info(f"Request sent, waiting for response...")
                
                self.sock.settimeout(get_request_timeout_ms() / 1000)
                
                response_data = self.receive_full_response(self.sock)
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
                retry_count += 1
                
                if retry_count <= max_retries:
                    logger.info(f"Retrying connection...")
                    self.disconnect()
                    if not self.connect():
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

# Global connection management
_sketchup_connection = None

def get_sketchup_host() -> str:
    return DEFAULT_SKETCHUP_HOST

def get_sketchup_port() -> int:
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
    global _sketchup_port_override, _sketchup_connection

    parsed_port = _parse_sketchup_port(port, "port")
    _sketchup_port_override = parsed_port

    if _sketchup_connection is not None and _sketchup_connection.port != parsed_port:
        try:
            _sketchup_connection.disconnect()
        finally:
            _sketchup_connection = None

    return parsed_port

def clear_sketchup_port_override():
    global _sketchup_port_override
    _sketchup_port_override = None

def get_sketchup_connection(allow_autostart: bool = True):
    """Get or create a persistent Sketchup connection"""
    global _sketchup_connection

    target_host = get_sketchup_host()
    target_port = get_sketchup_port()

    if (
        _sketchup_connection is not None
        and (
            _sketchup_connection.host != target_host
            or _sketchup_connection.port != target_port
        )
    ):
        # 端口可能通过环境变量切换，旧连接不能继续复用。
        logger.info(
            "Sketchup connection target changed from %s:%s to %s:%s; reconnecting",
            _sketchup_connection.host,
            _sketchup_connection.port,
            target_host,
            target_port,
        )
        try:
            _sketchup_connection.disconnect()
        finally:
            _sketchup_connection = None
    
    if _sketchup_connection is not None:
        return _sketchup_connection
    
    if _sketchup_connection is None:
        _sketchup_connection = SketchupConnection(host=target_host, port=target_port)
        if not _sketchup_connection.connect(allow_autostart=allow_autostart):
            logger.error("Failed to connect to Sketchup at %s:%s", target_host, target_port)
            _sketchup_connection = None
            permission_hint = (
                " Ask the user whether to start SketchUp. If the user agrees, call "
                "`allow_sketchup_autostart` and retry the SketchUp tool call."
            )
            if allow_autostart and startup.autostart_allowed():
                permission_hint = ""
            raise Exception(
                "Could not connect to Sketchup. Make sure the Sketchup extension is "
                f"running on {target_host}:{target_port}.{permission_hint}"
            )
        logger.info("Created new persistent connection to Sketchup")
    
    return _sketchup_connection

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    try:
        logger.info("SketchupMCP server starting up")
        try:
            sketchup = get_sketchup_connection(allow_autostart=False)
            logger.info("Successfully connected to Sketchup on startup")
        except Exception as e:
            logger.warning(f"Could not connect to Sketchup on startup: {str(e)}")
            logger.warning("Make sure the Sketchup extension is running")
        yield {}
    finally:
        global _sketchup_connection
        if _sketchup_connection:
            logger.info("Disconnecting from Sketchup")
            _sketchup_connection.disconnect()
            _sketchup_connection = None
        logger.info("SketchupMCP server shut down")

# Create MCP server with lifespan support
mcp = FastMCP(
    "SketchupMCP",
    instructions="Sketchup integration through the Model Context Protocol",
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
    """Set the Sketchup Ruby extension port for this MCP server process."""
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
def create_component(
    ctx: Context,
    type: str = "cube",
    position: List[float] = None,
    dimensions: List[float] = None
) -> str:
    """Create a new component in Sketchup"""
    try:
        logger.info(f"create_component called with type={type}, position={position}, dimensions={dimensions}, request_id={ctx.request_id}")
        
        sketchup = get_sketchup_connection()
        
        params = {
            "name": "create_component",
            "arguments": {
                "type": type,
                "position": position or [0,0,0],
                "dimensions": dimensions or [1,1,1]
            }
        }
        
        logger.info(f"Calling send_command with method='tools/call', params={params}, request_id={ctx.request_id}")
        
        result = sketchup.send_command(
            method="tools/call",
            params=params,
            request_id=ctx.request_id
        )
        
        logger.info(f"create_component result: {result}")
        return json.dumps(result)
    except Exception as e:
        logger.error(f"Error in create_component: {str(e)}")
        return f"Error creating component: {str(e)}"

@mcp.tool()
def delete_component(
    ctx: Context,
    id: str
) -> str:
    """Delete a component by ID"""
    try:
        sketchup = get_sketchup_connection()
        result = sketchup.send_command(
            method="tools/call",
            params={
                "name": "delete_component",
                "arguments": {"id": id}
            },
            request_id=ctx.request_id
        )
        return json.dumps(result)
    except Exception as e:
        return f"Error deleting component: {str(e)}"

@mcp.tool()
def transform_component(
    ctx: Context,
    id: str,
    position: List[float] = None,
    rotation: List[float] = None,
    scale: List[float] = None
) -> str:
    """Transform a component's position, rotation, or scale"""
    try:
        sketchup = get_sketchup_connection()
        arguments = {"id": id}
        if position is not None:
            arguments["position"] = position
        if rotation is not None:
            arguments["rotation"] = rotation
        if scale is not None:
            arguments["scale"] = scale
            
        result = sketchup.send_command(
            method="tools/call",
            params={
                "name": "transform_component",
                "arguments": arguments
            },
            request_id=ctx.request_id
        )
        return json.dumps(result)
    except Exception as e:
        return f"Error transforming component: {str(e)}"

@mcp.tool()
def get_selection(ctx: Context) -> str:
    """Get currently selected components"""
    try:
        sketchup = get_sketchup_connection()
        result = sketchup.send_command(
            method="tools/call",
            params={
                "name": "get_selection",
                "arguments": {}
            },
            request_id=ctx.request_id
        )
        return json.dumps(result)
    except Exception as e:
        return f"Error getting selection: {str(e)}"

@mcp.tool()
def set_material(
    ctx: Context,
    id: str,
    material: str
) -> str:
    """Set material for a component"""
    try:
        sketchup = get_sketchup_connection()
        result = sketchup.send_command(
            method="tools/call",
            params={
                "name": "set_material",
                "arguments": {
                    "id": id,
                    "material": material
                }
            },
            request_id=ctx.request_id
        )
        return json.dumps(result)
    except Exception as e:
        return f"Error setting material: {str(e)}"

@mcp.tool()
def export_scene(
    ctx: Context,
    format: str = "skp"
) -> str:
    """Export the current scene"""
    try:
        sketchup = get_sketchup_connection()
        result = sketchup.send_command(
            method="tools/call",
            params={
                "name": "export",
                "arguments": {
                    "format": format
                }
            },
            request_id=ctx.request_id
        )
        return json.dumps(result)
    except Exception as e:
        return f"Error exporting scene: {str(e)}"

@mcp.tool()
def capture_review_views(
    ctx: Context,
    persistent_id: int,
    output_dir: str = "",
    hide_others: bool = True,
    width: int = 1600,
    height: int = 1200
) -> str:
    """Capture isolated front/right/top review images for an entity by persistent_id."""
    try:
        logger.info(
            "capture_review_views called with persistent_id=%s, output_dir=%s, "
            "hide_others=%s, width=%s, height=%s",
            persistent_id,
            output_dir,
            hide_others,
            width,
            height,
        )

        sketchup = get_sketchup_connection()

        result = sketchup.send_command(
            method="tools/call",
            params={
                "name": "capture_review_views",
                "arguments": {
                    "persistent_id": persistent_id,
                    "output_dir": output_dir,
                    "hide_others": hide_others,
                    "width": width,
                    "height": height,
                },
            },
            request_id=ctx.request_id,
        )

        logger.info(f"capture_review_views result: {result}")

        response = {
            "success": True,
            "result": result.get("content", [{"text": "Success"}])[0].get("text", "Success")
            if isinstance(result.get("content"), list) and len(result.get("content", [])) > 0
            else "Success",
        }
        return json.dumps(response)
    except Exception as e:
        logger.error(f"Error in capture_review_views: {str(e)}")
        return json.dumps({
            "success": False,
            "error": str(e)
        })

@mcp.tool()
def create_mortise_tenon(
    ctx: Context,
    mortise_id: str,
    tenon_id: str,
    width: float = 1.0,
    height: float = 1.0,
    depth: float = 1.0,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    offset_z: float = 0.0
) -> str:
    """Create a mortise and tenon joint between two components"""
    try:
        logger.info(f"create_mortise_tenon called with mortise_id={mortise_id}, tenon_id={tenon_id}, width={width}, height={height}, depth={depth}, offsets=({offset_x}, {offset_y}, {offset_z})")
        
        sketchup = get_sketchup_connection()
        
        result = sketchup.send_command(
            method="tools/call",
            params={
                "name": "create_mortise_tenon",
                "arguments": {
                    "mortise_id": mortise_id,
                    "tenon_id": tenon_id,
                    "width": width,
                    "height": height,
                    "depth": depth,
                    "offset_x": offset_x,
                    "offset_y": offset_y,
                    "offset_z": offset_z
                }
            },
            request_id=ctx.request_id
        )
        
        logger.info(f"create_mortise_tenon result: {result}")
        return json.dumps(result)
    except Exception as e:
        logger.error(f"Error in create_mortise_tenon: {str(e)}")
        return f"Error creating mortise and tenon joint: {str(e)}"

@mcp.tool()
def create_dovetail(
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
    offset_z: float = 0.0
) -> str:
    """Create a dovetail joint between two components"""
    try:
        logger.info(f"create_dovetail called with tail_id={tail_id}, pin_id={pin_id}, width={width}, height={height}, depth={depth}, angle={angle}, num_tails={num_tails}")
        
        sketchup = get_sketchup_connection()
        
        result = sketchup.send_command(
            method="tools/call",
            params={
                "name": "create_dovetail",
                "arguments": {
                    "tail_id": tail_id,
                    "pin_id": pin_id,
                    "width": width,
                    "height": height,
                    "depth": depth,
                    "angle": angle,
                    "num_tails": num_tails,
                    "offset_x": offset_x,
                    "offset_y": offset_y,
                    "offset_z": offset_z
                }
            },
            request_id=ctx.request_id
        )
        
        logger.info(f"create_dovetail result: {result}")
        return json.dumps(result)
    except Exception as e:
        logger.error(f"Error in create_dovetail: {str(e)}")
        return f"Error creating dovetail joint: {str(e)}"

@mcp.tool()
def create_finger_joint(
    ctx: Context,
    board1_id: str,
    board2_id: str,
    width: float = 1.0,
    height: float = 1.0,
    depth: float = 1.0,
    num_fingers: int = 5,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    offset_z: float = 0.0
) -> str:
    """Create a finger joint (box joint) between two components"""
    try:
        logger.info(f"create_finger_joint called with board1_id={board1_id}, board2_id={board2_id}, width={width}, height={height}, depth={depth}, num_fingers={num_fingers}")
        
        sketchup = get_sketchup_connection()
        
        result = sketchup.send_command(
            method="tools/call",
            params={
                "name": "create_finger_joint",
                "arguments": {
                    "board1_id": board1_id,
                    "board2_id": board2_id,
                    "width": width,
                    "height": height,
                    "depth": depth,
                    "num_fingers": num_fingers,
                    "offset_x": offset_x,
                    "offset_y": offset_y,
                    "offset_z": offset_z
                }
            },
            request_id=ctx.request_id
        )
        
        logger.info(f"create_finger_joint result: {result}")
        return json.dumps(result)
    except Exception as e:
        logger.error(f"Error in create_finger_joint: {str(e)}")
        return f"Error creating finger joint: {str(e)}"

@mcp.tool()
def eval_ruby(
    ctx: Context,
    code: str
) -> str:
    """Evaluate arbitrary Ruby code in Sketchup"""
    try:
        logger.info(f"eval_ruby called with code length: {len(code)}")
        
        sketchup = get_sketchup_connection()
        
        result = sketchup.send_command(
            method="tools/call",
            params={
                "name": "eval_ruby",
                "arguments": {
                    "code": code
                }
            },
            request_id=ctx.request_id
        )
        
        logger.info(f"eval_ruby result: {result}")
        
        # Format the response to include the result
        response = {
            "success": True,
            "result": result.get("content", [{"text": "Success"}])[0].get("text", "Success") if isinstance(result.get("content"), list) and len(result.get("content", [])) > 0 else "Success"
        }
        
        return json.dumps(response)
    except Exception as e:
        logger.error(f"Error in eval_ruby: {str(e)}")
        return json.dumps({
            "success": False,
            "error": str(e)
        })

def main():
    start_idle_watchdog()
    mcp.run()

if __name__ == "__main__":
    main()
