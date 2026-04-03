"""
MODULE_CONTRACT:
  PURPOSE: MCP server for Matrix — send/read messages, manage rooms via matrix-nio
  SCOPE: stdio MCP server exposing Matrix Client-Server API as MCP tools
  DEPENDS: mcp (Python SDK), matrix-nio
  INPUTS: MCP tool calls via stdio (JSON-RPC)
  OUTPUTS: Tool results — messages, room lists, event confirmations
  LINKS: https://spec.matrix.org/latest/client-server-api/
  MODULE_MAP: main, serve, _send_message, _read_messages, _list_rooms,
              _get_room_info, _join_room, _leave_room, _create_room,
              _invite_user, _send_reaction, _resolve_alias
  CHANGE_SUMMARY:
    - 0.1.0: Initial release — 11 tools for Matrix interaction
"""

# START_BLOCK_IMPORTS
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from nio import (
    AsyncClient,
    JoinedRoomsResponse,
    JoinError,
    LoginResponse,
    MessageDirection,
    RoomCreateResponse,
    RoomInviteResponse,
    RoomMessagesResponse,
    RoomResolveAliasResponse,
    RoomSendResponse,
)
# END_BLOCK_IMPORTS

logger = logging.getLogger("mcp-server-matrix")

# START_BLOCK_AUTH
_client: AsyncClient | None = None
_synced: bool = False


async def _get_client() -> AsyncClient:
    """Return an authenticated AsyncClient, caching across calls.

    Reads configuration from environment variables:
        MATRIX_HOMESERVER  — e.g. https://matrix.example.com
        MATRIX_USER        — full user ID, e.g. @bot:example.com
        MATRIX_PASSWORD    — account password
        MATRIX_ACCESS_TOKEN — (optional) use token instead of password login
    """
    global _client

    if _client is not None:
        return _client

    homeserver = os.environ.get("MATRIX_HOMESERVER", "")
    user = os.environ.get("MATRIX_USER", "")
    password = os.environ.get("MATRIX_PASSWORD", "")
    access_token = os.environ.get("MATRIX_ACCESS_TOKEN", "")

    if not homeserver:
        raise EnvironmentError("MATRIX_HOMESERVER is not set")
    if not user:
        raise EnvironmentError("MATRIX_USER is not set")
    if not password and not access_token:
        raise EnvironmentError("Set MATRIX_PASSWORD or MATRIX_ACCESS_TOKEN")

    store_path = os.environ.get(
        "MATRIX_STORE_PATH",
        str(Path.home() / ".mcp-server-matrix" / "nio_store"),
    )
    Path(store_path).mkdir(parents=True, exist_ok=True)

    client = AsyncClient(homeserver=homeserver, user=user, store_path=store_path)

    if access_token:
        client.access_token = access_token
        client.user_id = user
    else:
        resp = await client.login(password=password, device_name="mcp-server-matrix")
        if not isinstance(resp, LoginResponse):
            raise RuntimeError(f"Matrix login failed: {resp}")
        logger.info("Logged in as %s (device %s)", resp.user_id, resp.device_id)

    _client = client
    return _client


async def _ensure_synced(client: AsyncClient) -> None:
    """Run an initial /sync to populate room state and pagination tokens."""
    global _synced
    if _synced:
        return
    await client.sync(timeout=10000, full_state=True)
    _synced = True
    logger.info("Initial sync complete — %d rooms", len(client.rooms))


async def _close_client() -> None:
    """Close the cached client."""
    global _client, _synced
    if _client is not None:
        await _client.close()
        _client = None
        _synced = False
# END_BLOCK_AUTH


# START_BLOCK_HELPERS
def _ok(text: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=text)]


def _error(msg: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=f"ERROR: {msg}")]
# END_BLOCK_HELPERS


# START_BLOCK_TOOLS
TOOLS = [
    types.Tool(
        name="send_message",
        description="Send a text message to a Matrix room.",
        inputSchema={
            "type": "object",
            "properties": {
                "room_id": {
                    "type": "string",
                    "description": "Room ID (e.g. !abc123:matrix.org) or alias (#room:matrix.org)",
                },
                "text": {
                    "type": "string",
                    "description": "Message text to send",
                },
            },
            "required": ["room_id", "text"],
        },
    ),
    types.Tool(
        name="read_messages",
        description="Read recent messages from a Matrix room. Returns messages in reverse chronological order.",
        inputSchema={
            "type": "object",
            "properties": {
                "room_id": {
                    "type": "string",
                    "description": "Room ID (e.g. !abc123:matrix.org)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max messages to return (default 20, max 100)",
                    "default": 20,
                },
                "since": {
                    "type": "string",
                    "description": "Pagination token from a previous read_messages call",
                },
            },
            "required": ["room_id"],
        },
    ),
    types.Tool(
        name="list_rooms",
        description="List all Matrix rooms the user has joined.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    types.Tool(
        name="get_room_info",
        description="Get detailed information about a Matrix room — name, topic, members, aliases.",
        inputSchema={
            "type": "object",
            "properties": {
                "room_id": {
                    "type": "string",
                    "description": "Room ID (e.g. !abc123:matrix.org)",
                },
            },
            "required": ["room_id"],
        },
    ),
    types.Tool(
        name="join_room",
        description="Join a Matrix room by ID or alias.",
        inputSchema={
            "type": "object",
            "properties": {
                "room_id_or_alias": {
                    "type": "string",
                    "description": "Room ID (!abc123:matrix.org) or alias (#room:matrix.org)",
                },
            },
            "required": ["room_id_or_alias"],
        },
    ),
    types.Tool(
        name="leave_room",
        description="Leave a Matrix room.",
        inputSchema={
            "type": "object",
            "properties": {
                "room_id": {
                    "type": "string",
                    "description": "Room ID to leave",
                },
            },
            "required": ["room_id"],
        },
    ),
    types.Tool(
        name="create_room",
        description="Create a new Matrix room.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Display name for the room",
                },
                "topic": {
                    "type": "string",
                    "description": "Room topic / description",
                },
                "invite": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "User IDs to invite (e.g. ['@user:matrix.org'])",
                },
                "is_direct": {
                    "type": "boolean",
                    "description": "Create as a direct message room (default false)",
                    "default": False,
                },
            },
            "required": ["name"],
        },
    ),
    types.Tool(
        name="invite_user",
        description="Invite a user to a Matrix room.",
        inputSchema={
            "type": "object",
            "properties": {
                "room_id": {
                    "type": "string",
                    "description": "Room ID",
                },
                "user_id": {
                    "type": "string",
                    "description": "User ID to invite (e.g. @user:matrix.org)",
                },
            },
            "required": ["room_id", "user_id"],
        },
    ),
    types.Tool(
        name="send_reaction",
        description="Send a reaction (emoji) to a message in a Matrix room.",
        inputSchema={
            "type": "object",
            "properties": {
                "room_id": {
                    "type": "string",
                    "description": "Room ID containing the message",
                },
                "event_id": {
                    "type": "string",
                    "description": "Event ID of the message to react to",
                },
                "emoji": {
                    "type": "string",
                    "description": "Reaction emoji (e.g. '👍', '❤️')",
                },
            },
            "required": ["room_id", "event_id", "emoji"],
        },
    ),
    types.Tool(
        name="resolve_alias",
        description="Resolve a Matrix room alias (#room:server) to a room ID.",
        inputSchema={
            "type": "object",
            "properties": {
                "alias": {
                    "type": "string",
                    "description": "Room alias (e.g. #general:matrix.org)",
                },
            },
            "required": ["alias"],
        },
    ),
    types.Tool(
        name="send_html",
        description="Send a message with HTML formatting to a Matrix room.",
        inputSchema={
            "type": "object",
            "properties": {
                "room_id": {
                    "type": "string",
                    "description": "Room ID or alias",
                },
                "html": {
                    "type": "string",
                    "description": "HTML-formatted message body",
                },
                "text": {
                    "type": "string",
                    "description": "Plain-text fallback for clients that don't render HTML",
                },
            },
            "required": ["room_id", "html"],
        },
    ),
]
# END_BLOCK_TOOLS


# START_BLOCK_SERVER
app = Server("mcp-server-matrix")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        client = await _get_client()
    except Exception as exc:
        logger.error("Auth error: %s", exc)
        return _error(f"AuthError: {exc}")

    await _ensure_synced(client)

    try:
        handler = _HANDLERS.get(name)
        if handler is None:
            return _error(f"Unknown tool: {name}")
        return await handler(client, arguments)
    except Exception as exc:
        logger.exception("Tool '%s' failed", name)
        return _error(f"{type(exc).__name__}: {exc}")
# END_BLOCK_SERVER


# START_BLOCK_HANDLERS
async def _send_message(client: AsyncClient, args: dict) -> list[types.TextContent]:
    room_id = args["room_id"]
    text = args["text"]

    resp = await client.room_send(
        room_id=room_id,
        message_type="m.room.message",
        content={"msgtype": "m.text", "body": text},
    )

    if isinstance(resp, RoomSendResponse):
        return _ok(json.dumps({"event_id": resp.event_id, "room_id": room_id}))
    return _error(f"Failed to send: {resp}")


async def _read_messages(client: AsyncClient, args: dict) -> list[types.TextContent]:
    room_id = args["room_id"]
    limit = min(args.get("limit", 20), 100)
    since = args.get("since", "")

    start_token = since
    if not start_token:
        room = client.rooms.get(room_id)
        if room and hasattr(room, "prev_batch"):
            start_token = room.prev_batch
        if not start_token:
            await client.sync(timeout=5000)
            room = client.rooms.get(room_id)
            start_token = room.prev_batch if room else ""

    if not start_token:
        return _error(f"Cannot read room {room_id} — not joined or no sync token")

    resp = await client.room_messages(
        room_id=room_id,
        start=start_token,
        limit=limit,
        direction=MessageDirection.back,
    )

    if not isinstance(resp, RoomMessagesResponse):
        return _error(f"Failed to read messages: {resp}")

    messages = []
    for event in resp.chunk:
        if hasattr(event, "body"):
            ts = datetime.fromtimestamp(
                event.server_timestamp / 1000, tz=timezone.utc
            ).isoformat()
            messages.append({
                "sender": event.sender,
                "timestamp": ts,
                "body": event.body,
                "event_id": event.event_id,
            })

    return _ok(json.dumps({
        "messages": messages,
        "count": len(messages),
        "next_token": resp.end,
    }, ensure_ascii=False))


async def _list_rooms(client: AsyncClient, _args: dict) -> list[types.TextContent]:
    resp = await client.joined_rooms()

    if not isinstance(resp, JoinedRoomsResponse):
        return _error(f"Failed to list rooms: {resp}")

    rooms = []
    for room_id in resp.rooms:
        room = client.rooms.get(room_id)
        rooms.append({
            "room_id": room_id,
            "display_name": room.display_name if room else room_id,
            "member_count": room.member_count if room else 0,
            "topic": room.topic if room and hasattr(room, "topic") else None,
        })

    return _ok(json.dumps(rooms, ensure_ascii=False))


async def _get_room_info(client: AsyncClient, args: dict) -> list[types.TextContent]:
    room_id = args["room_id"]
    room = client.rooms.get(room_id)

    if not room:
        return _error(f"Room {room_id} not found — bot may not have joined it")

    info = {
        "room_id": room_id,
        "display_name": room.display_name,
        "topic": room.topic if hasattr(room, "topic") else None,
        "member_count": room.member_count,
        "canonical_alias": room.canonical_alias if hasattr(room, "canonical_alias") else None,
        "encrypted": room.encrypted if hasattr(room, "encrypted") else False,
        "members": [m for m in room.users] if hasattr(room, "users") else [],
    }
    return _ok(json.dumps(info, ensure_ascii=False))


async def _join_room(client: AsyncClient, args: dict) -> list[types.TextContent]:
    target = args["room_id_or_alias"]
    resp = await client.join(target)

    if isinstance(resp, JoinError):
        return _error(f"Cannot join {target}: {resp.message}")

    room_id = resp.room_id if hasattr(resp, "room_id") else target
    return _ok(json.dumps({"room_id": room_id}))


async def _leave_room(client: AsyncClient, args: dict) -> list[types.TextContent]:
    room_id = args["room_id"]
    resp = await client.room_leave(room_id)

    if hasattr(resp, "transport_response") and resp.transport_response.status >= 400:
        return _error(f"Cannot leave {room_id}: {resp}")

    return _ok(json.dumps({"status": "ok", "room_id": room_id}))


async def _create_room(client: AsyncClient, args: dict) -> list[types.TextContent]:
    resp = await client.room_create(
        name=args["name"],
        topic=args.get("topic", ""),
        invite=args.get("invite", []),
        is_direct=args.get("is_direct", False),
    )

    if isinstance(resp, RoomCreateResponse):
        return _ok(json.dumps({"room_id": resp.room_id}))
    return _error(f"Failed to create room: {resp}")


async def _invite_user(client: AsyncClient, args: dict) -> list[types.TextContent]:
    resp = await client.room_invite(room_id=args["room_id"], user_id=args["user_id"])

    if isinstance(resp, RoomInviteResponse):
        return _ok(json.dumps({"status": "ok"}))
    return _error(f"Failed to invite: {resp}")


async def _send_reaction(client: AsyncClient, args: dict) -> list[types.TextContent]:
    resp = await client.room_send(
        room_id=args["room_id"],
        message_type="m.reaction",
        content={
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": args["event_id"],
                "key": args["emoji"],
            }
        },
    )

    if isinstance(resp, RoomSendResponse):
        return _ok(json.dumps({"event_id": resp.event_id}))
    return _error(f"Failed to react: {resp}")


async def _resolve_alias(client: AsyncClient, args: dict) -> list[types.TextContent]:
    resp = await client.room_resolve_alias(args["alias"])

    if isinstance(resp, RoomResolveAliasResponse):
        return _ok(json.dumps({
            "room_id": resp.room_id,
            "servers": resp.servers,
        }))
    return _error(f"Cannot resolve alias: {resp}")


async def _send_html(client: AsyncClient, args: dict) -> list[types.TextContent]:
    html = args["html"]
    plain = args.get("text", html)

    resp = await client.room_send(
        room_id=args["room_id"],
        message_type="m.room.message",
        content={
            "msgtype": "m.text",
            "body": plain,
            "format": "org.matrix.custom.html",
            "formatted_body": html,
        },
    )

    if isinstance(resp, RoomSendResponse):
        return _ok(json.dumps({"event_id": resp.event_id}))
    return _error(f"Failed to send: {resp}")


_HANDLERS = {
    "send_message": _send_message,
    "read_messages": _read_messages,
    "list_rooms": _list_rooms,
    "get_room_info": _get_room_info,
    "join_room": _join_room,
    "leave_room": _leave_room,
    "create_room": _create_room,
    "invite_user": _invite_user,
    "send_reaction": _send_reaction,
    "resolve_alias": _resolve_alias,
    "send_html": _send_html,
}
# END_BLOCK_HANDLERS


# START_BLOCK_MAIN
async def serve() -> None:
    """Run the MCP server over stdio."""
    logger.info("Starting mcp-server-matrix (stdio)")
    async with stdio_server() as (read, write):
        try:
            await app.run(read, write, app.create_initialization_options())
        finally:
            await _close_client()


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(
        level=os.environ.get("MCP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(serve())
# END_BLOCK_MAIN
