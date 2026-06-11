# SketchupMCP - Sketchup Model Context Protocol Integration

SketchupMCP connects Sketchup to MCP clients such as Codex and opencode, allowing agents to directly interact with and control Sketchup. This integration enables prompt-assisted 3D modeling, scene creation, and manipulation in Sketchup.

Big Shoutout to [Blender MCP](https://github.com/ahujasid/blender-mcp) for the inspiration and structure.

## Features

* **Two-way communication**: Connect MCP clients to Sketchup through a TCP socket connection
* **Component manipulation**: Create, modify, delete, and transform components in Sketchup
* **Material control**: Apply and modify materials and colors
* **Scene inspection**: Get detailed information about the current Sketchup scene
* **Selection handling**: Get and manipulate selected components
* **Ruby code evaluation**: Execute arbitrary Ruby code directly in SketchUp for advanced operations

## Components

The system consists of two main components:

1. **Sketchup Extension**: A Sketchup extension that creates a TCP server within Sketchup to receive and execute commands
2. **MCP Server (`sketchup_mcp/server.py`)**: A Python server that implements the Model Context Protocol and connects to the Sketchup extension

## Installation

### Python MCP Server

Install `uv` first, then either run the MCP server directly with `uvx`:

```powershell
uvx sketchup-mcp
```

Or install this checkout for local development:

```powershell
cd H:\sketchup-mcp
uv pip install -e .
```

After local installation, activate the virtual environment before running the
entry-point commands directly:

```powershell
.\.venv\Scripts\Activate.ps1
sketchup-mcp
sketchup-mcp-cli --help
```

Without activating the virtual environment, run the local checkout through
`uvx --from .`:

```powershell
uvx --from . sketchup-mcp-cli --help
```

### Sketchup Extension

1. Download or build the latest `.rbz` file
2. In Sketchup, go to Window > Extension Manager
3. Click "Install Extension" and select the downloaded `.rbz` file
4. Restart Sketchup

## MCP Client Configuration

SketchupMCP is a stdio MCP server. The Python process is started by your MCP
client, then it connects to the Sketchup Ruby extension over local TCP.

### Codex

Codex reads MCP servers from `~/.codex/config.toml`, or from a project-scoped
`.codex/config.toml` when the project is trusted.

```toml
[mcp_servers.sketchup]
command = "uvx"
args = ["sketchup-mcp"]
startup_timeout_sec = 20
tool_timeout_sec = 300

[mcp_servers.sketchup.env]
SKETCHUP_MCP_PORT = "9876"
```

By default, the MCP server will not open Sketchup on its own. If the Ruby port
is not reachable, the tool response tells the agent to ask the user whether it
may start Sketchup. After the user agrees, call `allow_sketchup_autostart`, then
retry the Sketchup tool call.

To pre-approve Sketchup startup for this MCP server, add the autostart
environment variables:

```toml
[mcp_servers.sketchup.env]
SKETCHUP_MCP_PORT = "9876"
SKETCHUP_MCP_AUTOSTART = "1"
SKETCHUP_MCP_SKETCHUP_EXE = "C:\\Program Files\\SketchUp\\SketchUp 2026\\SketchUp.exe"
SKETCHUP_MCP_STARTUP_TIMEOUT = "45"
```

Autostart runs only after the user has approved it, either through
`allow_sketchup_autostart` in the current MCP process or through the
`SKETCHUP_MCP_AUTOSTART=1` pre-approval setting. The MCP server startup probe
does not launch Sketchup, so a conversation can call `set_connection_port` first
when it needs a non-default port.

For a second Sketchup instance, add another server name and a different port:

```toml
[mcp_servers.sketchup_9877]
command = "uvx"
args = ["sketchup-mcp"]
startup_timeout_sec = 20
tool_timeout_sec = 300

[mcp_servers.sketchup_9877.env]
SKETCHUP_MCP_PORT = "9877"
```

Restart Codex after changing the config.

### opencode

opencode can use either `opencode mcp add` or an `opencode.json` config file.
For a project-local setup, create `opencode.json` in the project root:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "sketchup": {
      "type": "local",
      "command": ["uvx", "sketchup-mcp"],
      "enabled": true,
      "environment": {
        "SKETCHUP_MCP_PORT": "9876",
        "SKETCHUP_MCP_AUTOSTART": "1",
        "SKETCHUP_MCP_SKETCHUP_EXE": "C:\\Program Files\\SketchUp\\SketchUp 2026\\SketchUp.exe"
      }
    }
  }
}
```

For multiple Sketchup instances, add more local MCP entries and use one port
per Sketchup window:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "sketchup_9876": {
      "type": "local",
      "command": ["uvx", "sketchup-mcp"],
      "enabled": true,
      "environment": {
        "SKETCHUP_MCP_PORT": "9876"
      }
    },
    "sketchup_9877": {
      "type": "local",
      "command": ["uvx", "sketchup-mcp"],
      "enabled": true,
      "environment": {
        "SKETCHUP_MCP_PORT": "9877"
      }
    }
  }
}
```

## Usage

### Starting the Connection

1. In Sketchup, go to Extensions > MCP Server > Start Server
2. The server will start on the default port (9876)
3. Start Codex, opencode, or another configured MCP client

### Using Multiple Sketchup Instances

Each Sketchup instance runs its own local TCP server. To connect multiple
instances at the same time, give each Sketchup instance a different port:

1. In Sketchup, go to Extensions > MCP Server > Set Port...
2. Enter a port such as `9876`, `9877`, or `9878`
3. Use the Current Port menu item to confirm the active port
4. Start the server from Extensions > MCP Server > Start Server
5. Start the matching MCP server with the same port

From an MCP conversation, switch the current MCP process to another Sketchup
port by calling `set_connection_port` before other Sketchup tools. For example,
call `set_connection_port` with `9877`, then call `get_selection` or
`eval_ruby`.

PowerShell example:

```powershell
$env:SKETCHUP_MCP_PORT = "9877"
uvx sketchup-mcp
```

### Direct CLI

For debugging or scripting, use `sketchup-mcp-cli` to call the Sketchup Ruby
extension directly without going through an MCP host:

```powershell
sketchup-mcp-cli --port 9877 ping
sketchup-mcp-cli --port 9877 eval "Sketchup.active_model.title"
sketchup-mcp-cli --port 9877 eval --file .\probe.rb
sketchup-mcp-cli --port 9877 call get_selection
sketchup-mcp-cli --port 9877 --start-sketchup-if-needed --sketchup-exe "C:\Program Files\SketchUp\SketchUp 2026\SketchUp.exe" ping
```

Use `eval --file` for larger Ruby snippets or when shell quoting would make an
inline string fragile.

Once connected, Codex, opencode, or another MCP client can interact with Sketchup using the following capabilities:

#### Tools

* `get_selection` - Gets information about currently selected components
* `allow_sketchup_autostart` - Allow this MCP process to start Sketchup after the user approves
* `set_connection_port` - Switch this MCP server process to another Sketchup Ruby TCP port
* `create_component` - Create a new component with specified parameters
* `delete_component` - Remove a component from the scene
* `transform_component` - Move, rotate, or scale a component
* `set_material` - Apply materials to components
* `export_scene` - Export the current scene to various formats
* `capture_review_views` - Capture front, right, and top review images for a top-level entity
* `create_mortise_tenon` - Create a mortise and tenon joint between two boards
* `create_dovetail` - Create a dovetail joint between two boards
* `create_finger_joint` - Create a finger joint between two boards
* `eval_ruby` - Execute arbitrary Ruby code in SketchUp for advanced operations

### Example Commands

Here are some examples of what you can ask your MCP client to do:

* "Create a simple house model with a roof and windows"
* "Select all components and get their information"
* "Make the selected component red"
* "Move the selected component 10 units up"
* "Export the current scene as a 3D model"
* "Create a complex arts and crafts cabinet using Ruby code"

## Troubleshooting

* **Connection issues**: Make sure both the Sketchup extension server and the MCP server are running
* **Command failures**: Check the Ruby Console in Sketchup for error messages
* **Timeout errors**: Try simplifying your requests or breaking them into smaller steps

## Technical Details

### Communication Protocol

The system uses a simple JSON-based protocol over TCP sockets:

* **Commands** are sent as JSON objects with a `type` and optional `params`
* **Responses** are JSON objects with a `status` and `result` or `message`

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

MIT 
