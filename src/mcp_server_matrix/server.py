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
    JoinedMembersResponse,
    JoinedRoomsResponse,
    JoinError,
    LoginResponse,
    MessageDirection,
    SyncResponse,
    RoomCreateResponse,
    RoomGetStateEventResponse,
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
    resp = await client.sync(timeout=10000, full_state=True)
    if not isinstance(resp, SyncResponse) or not getattr(resp, "next_batch", ""):
        raise RuntimeError("Matrix sync did not return a usable next_batch token")
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


def _parse_datetime_input(value: str, label: str) -> tuple[datetime | None, str | None]:
    """Parse an ISO 8601 or YYYY-MM-DD datetime string as UTC."""
    if not isinstance(value, str) or not value.strip():
        return None, f"Invalid {label}: expected an ISO 8601 datetime or YYYY-MM-DD"

    raw_value = value.strip()
    parsed: datetime | None = None

    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(raw_value, "%Y-%m-%d")
        except ValueError:
            return None, (
                f"Invalid {label}: expected an ISO 8601 datetime or YYYY-MM-DD"
            )

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc), None


async def _resolve_room_id_for_read(
    client: AsyncClient, room_ref: str
) -> tuple[str | None, str | None]:
    """Resolve a room identifier to a room ID for read operations."""
    if not isinstance(room_ref, str) or not room_ref.strip():
        return None, "Invalid room_id: expected a non-empty string"

    value = room_ref.strip()
    if value.startswith("!"):
        return value, None
    if value.startswith("#"):
        resp = await client.room_resolve_alias(value)
        if isinstance(resp, RoomResolveAliasResponse):
            return resp.room_id, None
        return None, f"Cannot resolve alias {value}: {resp}"
    return None, (
        "Invalid room_id format: expected a room ID (!room:server) "
        "or alias (#room:server)"
    )


def _room_prev_batch(room: object | None) -> str | None:
    """Return the best available pagination token cached for a room."""
    if room is None:
        return None

    prev_batch = getattr(room, "prev_batch", None)
    if prev_batch:
        return prev_batch

    timeline = getattr(room, "timeline", None)
    if timeline is not None:
        prev_batch = getattr(timeline, "prev_batch", None)
        if prev_batch:
            return prev_batch

    return None


async def _room_state_content(
    client: AsyncClient, room_id: str, event_type: str
) -> dict | None:
    """Fetch a state event content dict directly from the homeserver."""
    resp = await client.room_get_state_event(room_id, event_type)
    if isinstance(resp, RoomGetStateEventResponse):
        return resp.content
    return None


async def _room_display_name(client: AsyncClient, room_id: str, room: object | None) -> str:
    """Resolve a human-friendly room name with server-backed fallbacks."""
    if room is not None:
        display_name = getattr(room, "display_name", None)
        if display_name and display_name != room_id:
            return display_name

    name_content = await _room_state_content(client, room_id, "m.room.name")
    if name_content:
        name = name_content.get("name")
        if name:
            return name

    alias = await _room_canonical_alias(client, room_id, room)
    if alias:
        return alias

    if room is not None:
        display_name = getattr(room, "display_name", None)
        if display_name:
            return display_name

    return room_id


async def _room_topic(client: AsyncClient, room_id: str, room: object | None) -> str | None:
    """Resolve the room topic from state, with cache fallback."""
    topic_content = await _room_state_content(client, room_id, "m.room.topic")
    if topic_content:
        topic = topic_content.get("topic")
        if topic:
            return topic

    if room is not None:
        return getattr(room, "topic", None)

    return None


async def _room_canonical_alias(
    client: AsyncClient, room_id: str, room: object | None
) -> str | None:
    """Resolve the canonical alias from state, with cache fallback."""
    alias_content = await _room_state_content(client, room_id, "m.room.canonical_alias")
    if alias_content:
        alias = alias_content.get("alias")
        if alias:
            return alias

    if room is not None:
        return getattr(room, "canonical_alias", None)

    return None


async def _room_member_ids(client: AsyncClient, room_id: str, room: object | None) -> list[str]:
    """Get joined member ids directly from the homeserver, with cache fallback."""
    resp = await client.joined_members(room_id)
    if isinstance(resp, JoinedMembersResponse):
        return [member.user_id for member in resp.members]

    if room is not None and hasattr(room, "users"):
        return [member_id for member_id in room.users]

    return []


async def _room_member_count(client: AsyncClient, room_id: str, room: object | None) -> int:
    """Get the joined member count directly from the homeserver."""
    members = await _room_member_ids(client, room_id, room)
    if members:
        return len(members)

    if room is not None:
        joined_count = getattr(room, "joined_count", None)
        if isinstance(joined_count, int):
            return joined_count
        member_count = getattr(room, "member_count", None)
        if isinstance(member_count, int):
            return member_count

    return 0


def _format_room_line(room: dict) -> str:
    """Format a room summary line for human-readable tool output."""
    parts = [f"- {room['display_name']} ({room['room_id']})"]
    parts.append(f"members: {room['member_count']}")
    if room.get("canonical_alias"):
        parts.append(f"alias: {room['canonical_alias']}")
    if room.get("topic"):
        parts.append(f"topic: {room['topic']}")
    return " | ".join(parts)


def _format_room_info_text(info: dict) -> str:
    """Format room details for human-readable tool output."""
    lines = [
        f"Room: {info['display_name']}",
        f"ID: {info['room_id']}",
        f"Members: {info['member_count']}",
        f"Alias: {info['canonical_alias'] or '-'}",
        f"Topic: {info['topic'] or '-'}",
        f"Encrypted: {'yes' if info['encrypted'] else 'no'}",
    ]
    if info.get("members"):
        lines.append(f"Members list: {', '.join(info['members'])}")
    return "\n".join(lines)
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
        description=(
            "Read the last N visible messages from a Matrix room using room ID "
            "or alias. Returns messages in chronological order. Optional date "
            "bounds filter the paginated history by event timestamp."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "room_id": {
                    "type": "string",
                    "description": (
                        "Room ID (e.g. !abc123:matrix.org) or alias "
                        "(e.g. #room:matrix.org)"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of messages to return (1-100, default 20)",
                    "default": 20,
                },
                "since": {
                    "type": "string",
                    "description": "Pagination token from a previous read_messages call",
                },
                "from_date": {
                    "type": "string",
                    "description": (
                        "Optional lower bound for message timestamps "
                        "(ISO 8601 or YYYY-MM-DD)"
                    ),
                },
                "to_date": {
                    "type": "string",
                    "description": (
                        "Optional upper bound for message timestamps "
                        "(ISO 8601 or YYYY-MM-DD)"
                    ),
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
    room_ref = args["room_id"]
    room_id, resolve_error = await _resolve_room_id_for_read(client, room_ref)
    if resolve_error:
        return _error(resolve_error)

    limit = args.get("limit", 20)
    if isinstance(limit, bool) or not isinstance(limit, int):
        return _error("Invalid limit: expected an integer between 1 and 100")
    if limit < 1 or limit > 100:
        return _error("Invalid limit: must be between 1 and 100")

    since = args.get("since", "")
    if since is None:
        since = ""
    if not isinstance(since, str):
        return _error("Invalid since: expected a pagination token string")
    since = since.strip()

    from_date = args.get("from_date", "")
    to_date = args.get("to_date", "")
    if from_date is None:
        from_date = ""
    if to_date is None:
        to_date = ""
    if not isinstance(from_date, str) or not isinstance(to_date, str):
        return _error("Invalid from_date/to_date: expected ISO 8601 strings")

    from_dt = None
    to_dt = None
    if from_date.strip():
        from_dt, parse_error = _parse_datetime_input(from_date, "from_date")
        if parse_error:
            return _error(parse_error)
    if to_date.strip():
        to_dt, parse_error = _parse_datetime_input(to_date, "to_date")
        if parse_error:
            return _error(parse_error)

    if from_dt and to_dt and from_dt > to_dt:
        return _error("Invalid date range: from_date must be before to_date")

    room = client.rooms.get(room_id)
    start_token = since
    if not start_token:
        start_token = _room_prev_batch(room)

    messages = []
    page_token = start_token
    next_token = start_token
    completed = False
    page_limit = min(100, max(20, limit * 2))
    seen_tokens: set[str] = set()

    while len(messages) < limit:
        page_key = page_token if page_token is not None else "__start__"
        if page_key in seen_tokens:
            return _error(
                f"Failed to read messages from {room_id}: pagination stalled at {page_key}"
            )
        seen_tokens.add(page_key)

        resp = await client.room_messages(
            room_id=room_id,
            start=page_token,
            limit=page_limit,
            direction=MessageDirection.back,
        )

        if not isinstance(resp, RoomMessagesResponse):
            return _error(f"Failed to read messages from {room_id}: {resp}")

        page_messages = []
        hit_lower_bound = False
        for event in resp.chunk:
            if hasattr(event, "body"):
                event_ts = getattr(event, "server_timestamp", None)
                if event_ts is None:
                    continue

                event_dt = datetime.fromtimestamp(event_ts / 1000, tz=timezone.utc)

                if to_dt and event_dt > to_dt:
                    continue
                if from_dt and event_dt < from_dt:
                    completed = True
                    hit_lower_bound = True
                    break

                page_messages.append({
                    "sender": event.sender,
                    "timestamp": event_dt.isoformat(),
                    "timestamp_ms": event_ts,
                    "body": event.body,
                    "event_id": event.event_id,
                })

        # /messages with dir=back returns reverse chronological order.
        page_messages.reverse()
        messages = page_messages + messages
        next_token = resp.end

        if hit_lower_bound:
            break
        if not next_token or next_token == page_token:
            completed = True
            break

        page_token = next_token

    return _ok(json.dumps({
        "messages": [
            {
                "sender": message["sender"],
                "timestamp": message["timestamp"],
                "body": message["body"],
                "event_id": message["event_id"],
            }
            for message in messages[-limit:]
        ],
        "count": min(len(messages), limit),
        "next_token": next_token,
        "completed": completed,
        "room_id_resolved": room_id,
        "from_date": from_dt.isoformat() if from_dt else None,
        "to_date": to_dt.isoformat() if to_dt else None,
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
            "display_name": await _room_display_name(client, room_id, room),
            "member_count": await _room_member_count(client, room_id, room),
            "canonical_alias": await _room_canonical_alias(client, room_id, room),
            "topic": await _room_topic(client, room_id, room),
        })

    rooms.sort(key=lambda room: room["display_name"].lower())
    lines = ["Rooms:"]
    lines.extend(_format_room_line(room) for room in rooms)
    lines.append("")
    lines.append(json.dumps(rooms, ensure_ascii=False))
    return _ok("\n".join(lines))


async def _get_room_info(client: AsyncClient, args: dict) -> list[types.TextContent]:
    room_id = args["room_id"]
    room = client.rooms.get(room_id)

    info = {
        "room_id": room_id,
        "display_name": await _room_display_name(client, room_id, room),
        "topic": await _room_topic(client, room_id, room),
        "member_count": await _room_member_count(client, room_id, room),
        "canonical_alias": await _room_canonical_alias(client, room_id, room),
        "encrypted": room.encrypted if room and hasattr(room, "encrypted") else False,
        "members": await _room_member_ids(client, room_id, room),
    }
    return _ok(_format_room_info_text(info) + "\n\n" + json.dumps(info, ensure_ascii=False))


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
