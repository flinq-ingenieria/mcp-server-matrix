# mcp-server-matrix

[![README на русском](https://img.shields.io/badge/README-на_русском-blue)](README.ru.md)

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server for [Matrix](https://matrix.org) — the open, decentralized communication protocol.

Built on [matrix-nio](https://github.com/matrix-nio/matrix-nio), this server lets any MCP-compatible AI assistant (Claude, OpenClaw, etc.) send and read messages, manage rooms, and interact with any Matrix homeserver (Synapse, Dendrite, Conduit).

## Features

| Tool | Description |
|------|-------------|
| `send_message` | Send a text message to a room |
| `read_messages` | Read messages in chronological order, with date filters and pagination |
| `search_messages` | Find messages containing a keyword in a room's history |
| `list_rooms` | List joined rooms, alphabetically and paginated |
| `search_rooms` | Find joined rooms by name, alias or topic |
| `get_room_info` | Room details — name, topic, members, encryption status |
| `get_room_members` | List joined members with display names |
| `send_reaction` | React to a message with an emoji |
| `list_unread_rooms` | Rooms with unread messages, with unread and mention counts |
| `read_unread_messages` | Unread messages grouped by room, oldest first (optionally one room or mentions only) |
| `mark_room_read` | Move the read marker and receipt to the latest message of a room |

Rooms can be given as an ID (`!abc:server`), an alias (`#room:server`) or any
substring of the room name.

Unread state comes from the homeserver's `GET /_matrix/client/v3/notifications`,
so it follows the user's notification settings (muted rooms are not counted)
and is accurate at request time. Messages in end-to-end encrypted rooms are
listed but cannot be read by this connector.

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

Set environment variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `MATRIX_HOMESERVER` | Yes | Homeserver URL (e.g. `https://matrix.org`) |
| `MATRIX_USER` | Yes | Full user ID (e.g. `@bot:matrix.org`) |
| `MATRIX_PASSWORD` | Yes* | Account password |
| `MATRIX_ACCESS_TOKEN` | Yes* | Access token (alternative to password) |
| `MATRIX_STORE_PATH` | No | Path for nio session store (default: `~/.mcp-server-matrix/nio_store/`) |
| `MCP_LOG_LEVEL` | No | Log level: DEBUG, INFO, WARNING, ERROR (default: INFO) |

\* Provide either `MATRIX_PASSWORD` or `MATRIX_ACCESS_TOKEN`.

## Remote HTTP mode

The repository now also ships a dedicated Streamable HTTP entrypoint for
multiuser remote connectors:

```bash
PYTHONPATH=src python -m mcp_server_matrix.http_server --host 0.0.0.0 --port 8080 --path /mcp
```

Required runtime configuration for HTTP mode:

| Variable | Description |
|----------|-------------|
| `MATRIX_MCP_AUTH_ISSUER_URL` | OAuth issuer URL |
| `MATRIX_MCP_AUTH_INTROSPECTION_URL` | RFC 7662 introspection endpoint |
| `MATRIX_MCP_AUTH_RESOURCE_URL` | Canonical public MCP URL, e.g. `https://matrix-mcp.example.com/mcp` |
| `MATRIX_MCP_AUTH_REQUIRED_SCOPES` | Optional comma-separated scopes, e.g. `mcp:matrix` |
| `MATRIX_MCP_AUTH_CLIENT_ID` / `MATRIX_MCP_AUTH_CLIENT_SECRET` | Optional credentials for a dedicated backend introspection client |
| `MATRIX_MCP_CONTROL_PLANE_URL` | Base URL of the shared control-plane |
| `MATRIX_MCP_CONTROL_PLANE_TOKEN` | Internal service token used against the control-plane |
| `MATRIX_MCP_STORE_ROOT` | Optional persistent root for per-user nio stores |
| `MATRIX_MCP_AUDIT_LOG` | Optional JSONL audit log path |

In HTTP mode the server does not read `MATRIX_HOMESERVER`, `MATRIX_USER`, or
local credentials from the environment. Those are resolved per OAuth subject
from the control-plane instead.

## Usage

### Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "matrix": {
      "command": "python3",
      "args": ["-m", "mcp_server_matrix.server"],
      "env": {
        "PYTHONPATH": "/absolute/path/to/mcp-matrix/src",
        "MATRIX_HOMESERVER": "https://matrix.example.com",
        "MATRIX_USER": "@bot:example.com",
        "MATRIX_PASSWORD": "your-password"
      }
    }
  }
}
```

### Claude Code

```json
{
  "mcpServers": {
    "matrix": {
      "command": "python3",
      "args": ["-m", "mcp_server_matrix.server"],
      "env": {
        "PYTHONPATH": "/absolute/path/to/mcp-matrix/src",
        "MATRIX_HOMESERVER": "https://matrix.example.com",
        "MATRIX_USER": "@bot:example.com",
        "MATRIX_ACCESS_TOKEN": "syt_..."
      }
    }
  }
}
```

### OpenClaw

Add to `openclaw.json`:

```json
{
  "mcp": {
    "servers": {
      "matrix": {
        "command": "python3",
        "args": ["-m", "mcp_server_matrix.server"],
        "env": {
          "PYTHONPATH": "/absolute/path/to/mcp-matrix/src",
          "MATRIX_HOMESERVER": "https://matrix.example.com",
          "MATRIX_USER": "@bot:example.com",
          "MATRIX_PASSWORD": "your-password"
        }
      }
    }
  }
}
```

### Direct (stdio)

```bash
export MATRIX_HOMESERVER=https://matrix.example.com
export MATRIX_USER=@bot:example.com
export MATRIX_PASSWORD=your-password
export PYTHONPATH=$PWD/src

python -m mcp_server_matrix.server
```

## Examples

Once connected, your AI assistant can:

- **Read messages**: "What are the latest messages in #general:matrix.org?"
- **Resolve an alias before writing**: "Resolve #announcements:matrix.org to a room ID"
- **Send messages**: "Send 'Hello everyone!' to !abc123:matrix.org"
- **Manage rooms**: "Create a room called 'Project X' and invite @alice:matrix.org"
- **Get info**: "How many members are in !abc123:matrix.org?"

## Supported Homeservers

Tested with:

- [Synapse](https://github.com/element-hq/synapse) (reference implementation)
- Should work with any spec-compliant homeserver (Dendrite, Conduit, etc.)

## Requirements

- Python 3.10+
- A Matrix account on any homeserver
- Network access to the homeserver

## License

MIT

## Links

- [Model Context Protocol](https://modelcontextprotocol.io)
- [matrix-nio](https://github.com/matrix-nio/matrix-nio)
- [Matrix Spec](https://spec.matrix.org)
