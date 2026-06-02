# mcp-server-matrix

[![README на русском](https://img.shields.io/badge/README-на_русском-blue)](README.ru.md)

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server for [Matrix](https://matrix.org) — the open, decentralized communication protocol.

Built on [matrix-nio](https://github.com/matrix-nio/matrix-nio), this server lets any MCP-compatible AI assistant (Claude, OpenClaw, etc.) send and read messages, manage rooms, and interact with any Matrix homeserver (Synapse, Dendrite, Conduit).

## Features

| Tool | Description |
|------|-------------|
| `send_message` | Send a text message to a room |
| `send_html` | Send an HTML-formatted message |
| `read_messages` | Read recent messages or filter by date range (with pagination) |
| `list_rooms` | List all joined rooms |
| `get_room_info` | Room details — name, topic, members, encryption status |
| `join_room` | Join a room by ID or alias |
| `leave_room` | Leave a room |
| `create_room` | Create a new room (with optional invites) |
| `invite_user` | Invite a user to a room |
| `send_reaction` | React to a message with an emoji |
| `resolve_alias` | Resolve `#alias:server` to a room ID |

## Installation

```bash
pip install mcp-server-matrix
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uvx mcp-server-matrix
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

## Usage

### Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "matrix": {
      "command": "mcp-server-matrix",
      "env": {
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
      "command": "mcp-server-matrix",
      "env": {
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
        "command": "mcp-server-matrix",
        "env": {
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

mcp-server-matrix
```

## Examples

Once connected, your AI assistant can:

- **Read messages**: "What are the latest messages in #general?"
- **Send messages**: "Send 'Hello everyone!' to #announcements"
- **Manage rooms**: "Create a room called 'Project X' and invite @alice:matrix.org"
- **Get info**: "How many members are in #team-chat?"

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
