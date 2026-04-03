"""
MODULE_CONTRACT:
  PURPOSE: MCP server for Matrix — send/read messages, manage rooms via matrix-nio
  SCOPE: stdio MCP server exposing Matrix Client-Server API as MCP tools
  DEPENDS: mcp (Python SDK), matrix-nio
  INPUTS: MCP tool calls via stdio (JSON-RPC)
  OUTPUTS: Tool results — messages, room lists, event confirmations
  LINKS: https://spec.matrix.org/latest/client-server-api/
  MODULE_MAP: main, serve, _send_message, _read_messages, _list_rooms,
              _get_room_info, _get_room_members, _join_room, _leave_room,
              _create_room, _invite_user, _send_reaction, _resolve_alias,
              _get_messages_by_date, _get_notification_counts,
              _get_direct_messages, _send_direct_message, _get_user_profile
  CHANGE_SUMMARY:
    - 0.1.0: Initial release — 11 tools for Matrix interaction
    - 0.2.0: Merge send_message+send_html, add 6 new tools (16 total),
             better error mapping, reply_to support, markdown/html format
"""

# START_BLOCK_IMPORTS
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import markdown
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
    ProfileGetResponse,
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


def _friendly_error(resp: object) -> str:
    """Map common matrix-nio error responses to human-readable messages."""
    resp_type = type(resp).__name__
    message = getattr(resp, "message", "") or getattr(resp, "status_code", "") or str(resp)

    error_map = {
        "RoomSendError": "Failed to send message — check room permissions",
        "JoinError": "Cannot join room — room may not exist or access is denied",
        "RoomLeaveError": "Cannot leave room — you may not be a member",
        "RoomCreateError": "Failed to create room — check server limits",
        "RoomInviteError": "Failed to invite user — check permissions or user ID",
        "RoomResolveAliasError": "Cannot resolve alias — alias may not exist",
        "RoomMessagesError": "Failed to read messages — check room access",
        "JoinedMembersError": "Failed to get members — check room access",
        "ProfileGetError": "Failed to get user profile — user may not exist",
    }

    friendly = error_map.get(resp_type, f"Operation failed ({resp_type})")
    return f"{friendly}: {message}"
# END_BLOCK_HELPERS


# START_BLOCK_TOOLS
TOOLS = [
    types.Tool(
        name="send_message",
        description=(
            "Send a message to a Matrix room. Supports plain text, HTML, "
            "and Markdown formats. Can reply to a specific message."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "room_id": {
                    "type": "string",
                    "description": "Room ID (e.g. !abc123:matrix.org) or alias (#room:matrix.org)",
                },
                "text": {
                    "type": "string",
                    "description": "Message body",
                },
                "format": {
                    "type": "string",
                    "enum": ["text", "html", "markdown"],
                    "description": "Message format: 'text' (default), 'html', or 'markdown'",
                    "default": "text",
                },
                "reply_to": {
                    "type": "string",
                    "description": "Event ID to reply to (sets m.relates_to / m.in_reply_to)",
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
        name="get_room_members",
        description="List joined members of a Matrix room with display names and avatars.",
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
                    "description": "Reaction emoji (e.g. thumbs up, heart)",
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
        name="get_messages_by_date",
        description=(
            "Get messages from a Matrix room filtered by date range. "
            "Dates are ISO 8601 strings (e.g. '2026-04-01', '2026-04-01T12:00:00Z')."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "room_id": {
                    "type": "string",
                    "description": "Room ID (e.g. !abc123:matrix.org)",
                },
                "start_date": {
                    "type": "string",
                    "description": "Start of date range (ISO 8601, inclusive)",
                },
                "end_date": {
                    "type": "string",
                    "description": "End of date range (ISO 8601, inclusive)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max messages to return (default 50, max 500)",
                    "default": 50,
                },
            },
            "required": ["room_id", "start_date", "end_date"],
        },
    ),
    types.Tool(
        name="get_notification_counts",
        description="Get unread notification and mention counts for all rooms with pending notifications.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    types.Tool(
        name="get_direct_messages",
        description="List all direct message (DM) conversations — rooms with 2 or fewer members.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    types.Tool(
        name="send_direct_message",
        description=(
            "Send a direct message to a user. Finds existing DM room or creates one automatically."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "User ID to message (e.g. @user:matrix.org)",
                },
                "text": {
                    "type": "string",
                    "description": "Message body",
                },
                "format": {
                    "type": "string",
                    "enum": ["text", "html", "markdown"],
                    "description": "Message format: 'text' (default), 'html', or 'markdown'",
                    "default": "text",
                },
            },
            "required": ["user_id", "text"],
        },
    ),
    types.Tool(
        name="get_user_profile",
        description="Get a Matrix user's profile — display name and avatar URL.",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "User ID (e.g. @user:matrix.org)",
                },
            },
            "required": ["user_id"],
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
def _build_message_content(text: str, fmt: str, reply_to: str | None = None) -> dict:
    """Build m.room.message content dict with optional format and reply."""
    content: dict = {"msgtype": "m.text"}

    if fmt == "html":
        content["body"] = text
        content["format"] = "org.matrix.custom.html"
        content["formatted_body"] = text
    elif fmt == "markdown":
        html_body = markdown.markdown(text, extensions=["extra", "sane_lists"])
        content["body"] = text
        content["format"] = "org.matrix.custom.html"
        content["formatted_body"] = html_body
    else:
        content["body"] = text

    if reply_to:
        content["m.relates_to"] = {
            "m.in_reply_to": {"event_id": reply_to},
        }

    return content


async def _send_message(client: AsyncClient, args: dict) -> list[types.TextContent]:
    room_id = args["room_id"]
    text = args["text"]
    fmt = args.get("format", "text")
    reply_to = args.get("reply_to")

    content = _build_message_content(text, fmt, reply_to)

    resp = await client.room_send(
        room_id=room_id,
        message_type="m.room.message",
        content=content,
    )

    if isinstance(resp, RoomSendResponse):
        return _ok(json.dumps({"event_id": resp.event_id, "room_id": room_id}))
    return _error(_friendly_error(resp))


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
        return _error(_friendly_error(resp))

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
        return _error(_friendly_error(resp))

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


async def _get_room_members(client: AsyncClient, args: dict) -> list[types.TextContent]:
    room_id = args["room_id"]
    resp = await client.joined_members(room_id)

    if not isinstance(resp, JoinedMembersResponse):
        return _error(_friendly_error(resp))

    members = []
    for user_id, member in resp.members.items():
        # member is a RoomMember namedtuple or similar with display_name, avatar_url
        display_name = None
        avatar_url = None
        if isinstance(member, dict):
            display_name = member.get("display_name")
            avatar_url = member.get("avatar_url")
        elif hasattr(member, "display_name"):
            display_name = member.display_name
            avatar_url = getattr(member, "avatar_url", None)
        members.append({
            "user_id": user_id,
            "display_name": display_name,
            "avatar_url": avatar_url,
        })

    return _ok(json.dumps(members, ensure_ascii=False))


async def _join_room(client: AsyncClient, args: dict) -> list[types.TextContent]:
    target = args["room_id_or_alias"]
    resp = await client.join(target)

    if isinstance(resp, JoinError):
        return _error(_friendly_error(resp))

    room_id = resp.room_id if hasattr(resp, "room_id") else target
    return _ok(json.dumps({"room_id": room_id}))


async def _leave_room(client: AsyncClient, args: dict) -> list[types.TextContent]:
    room_id = args["room_id"]
    resp = await client.room_leave(room_id)

    if hasattr(resp, "transport_response") and resp.transport_response.status >= 400:
        return _error(_friendly_error(resp))

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
    return _error(_friendly_error(resp))


async def _invite_user(client: AsyncClient, args: dict) -> list[types.TextContent]:
    resp = await client.room_invite(room_id=args["room_id"], user_id=args["user_id"])

    if isinstance(resp, RoomInviteResponse):
        return _ok(json.dumps({"status": "ok"}))
    return _error(_friendly_error(resp))


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
    return _error(_friendly_error(resp))


async def _resolve_alias(client: AsyncClient, args: dict) -> list[types.TextContent]:
    resp = await client.room_resolve_alias(args["alias"])

    if isinstance(resp, RoomResolveAliasResponse):
        return _ok(json.dumps({
            "room_id": resp.room_id,
            "servers": resp.servers,
        }))
    return _error(_friendly_error(resp))


def _parse_iso_date(s: str) -> datetime:
    """Parse an ISO 8601 date/datetime string to a timezone-aware datetime."""
    s = s.strip()
    # Try full datetime first
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    # Fall back to date-only
    dt = datetime.strptime(s, "%Y-%m-%d")
    return dt.replace(tzinfo=timezone.utc)


async def _get_messages_by_date(client: AsyncClient, args: dict) -> list[types.TextContent]:
    room_id = args["room_id"]
    limit = min(args.get("limit", 50), 500)

    try:
        start_dt = _parse_iso_date(args["start_date"])
        end_dt = _parse_iso_date(args["end_date"])
    except (ValueError, KeyError) as exc:
        return _error(f"Invalid date format: {exc}")

    # If end_date is date-only (midnight), extend to end of day
    if end_dt.hour == 0 and end_dt.minute == 0 and end_dt.second == 0:
        end_dt = end_dt.replace(hour=23, minute=59, second=59)

    start_ts = int(start_dt.timestamp() * 1000)
    end_ts = int(end_dt.timestamp() * 1000)

    # Get sync token for room
    room = client.rooms.get(room_id)
    if not room or not hasattr(room, "prev_batch") or not room.prev_batch:
        return _error(f"Cannot read room {room_id} — not joined or no sync token")

    messages = []
    token = room.prev_batch
    max_iterations = 20  # safety limit

    for _ in range(max_iterations):
        resp = await client.room_messages(
            room_id=room_id,
            start=token,
            limit=100,
            direction=MessageDirection.back,
        )

        if not isinstance(resp, RoomMessagesResponse):
            return _error(_friendly_error(resp))

        done = False
        for event in resp.chunk:
            if not hasattr(event, "body"):
                continue
            ts = event.server_timestamp
            if ts < start_ts:
                done = True
                break
            if ts <= end_ts:
                iso = datetime.fromtimestamp(
                    ts / 1000, tz=timezone.utc
                ).isoformat()
                messages.append({
                    "sender": event.sender,
                    "timestamp": iso,
                    "body": event.body,
                    "event_id": event.event_id,
                })
                if len(messages) >= limit:
                    done = True
                    break

        if done or not resp.end or resp.end == token:
            break
        token = resp.end

    # Reverse to chronological order
    messages.reverse()

    return _ok(json.dumps({
        "messages": messages,
        "count": len(messages),
        "start_date": args["start_date"],
        "end_date": args["end_date"],
    }, ensure_ascii=False))


async def _get_notification_counts(client: AsyncClient, _args: dict) -> list[types.TextContent]:
    results = []
    for room_id, room in client.rooms.items():
        notif = getattr(room, "unread_notifications", None) or {}
        notification_count = notif.get("notification_count", 0) if isinstance(notif, dict) else 0
        highlight_count = notif.get("highlight_count", 0) if isinstance(notif, dict) else 0

        # Also check direct attributes
        if notification_count == 0:
            notification_count = getattr(room, "notification_count", 0) or 0
        if highlight_count == 0:
            highlight_count = getattr(room, "highlight_count", 0) or 0

        if notification_count > 0 or highlight_count > 0:
            results.append({
                "room_id": room_id,
                "display_name": room.display_name,
                "notification_count": notification_count,
                "highlight_count": highlight_count,
            })

    return _ok(json.dumps(results, ensure_ascii=False))


async def _get_direct_messages(client: AsyncClient, _args: dict) -> list[types.TextContent]:
    dms = []
    my_user_id = client.user_id

    for room_id, room in client.rooms.items():
        members = list(room.users.keys()) if hasattr(room, "users") else []
        if len(members) > 2:
            continue

        other_user = None
        for uid in members:
            if uid != my_user_id:
                other_user = uid
                break

        notif = getattr(room, "unread_notifications", None) or {}
        unread = notif.get("notification_count", 0) if isinstance(notif, dict) else 0
        if unread == 0:
            unread = getattr(room, "notification_count", 0) or 0

        dms.append({
            "room_id": room_id,
            "user_id": other_user,
            "display_name": room.display_name,
            "unread_count": unread,
        })

    return _ok(json.dumps(dms, ensure_ascii=False))


async def _send_direct_message(client: AsyncClient, args: dict) -> list[types.TextContent]:
    target_user = args["user_id"]
    text = args["text"]
    fmt = args.get("format", "text")
    my_user_id = client.user_id

    # Search for existing DM room
    dm_room_id = None
    for room_id, room in client.rooms.items():
        members = list(room.users.keys()) if hasattr(room, "users") else []
        if len(members) <= 2 and target_user in members:
            dm_room_id = room_id
            break

    # Create DM room if not found
    if dm_room_id is None:
        resp = await client.room_create(
            is_direct=True,
            invite=[target_user],
        )
        if not isinstance(resp, RoomCreateResponse):
            return _error(_friendly_error(resp))
        dm_room_id = resp.room_id

    content = _build_message_content(text, fmt)

    resp = await client.room_send(
        room_id=dm_room_id,
        message_type="m.room.message",
        content=content,
    )

    if isinstance(resp, RoomSendResponse):
        return _ok(json.dumps({
            "event_id": resp.event_id,
            "room_id": dm_room_id,
            "user_id": target_user,
        }))
    return _error(_friendly_error(resp))


async def _get_user_profile(client: AsyncClient, args: dict) -> list[types.TextContent]:
    user_id = args["user_id"]
    resp = await client.get_profile(user_id)

    if isinstance(resp, ProfileGetResponse):
        return _ok(json.dumps({
            "user_id": user_id,
            "display_name": resp.displayname,
            "avatar_url": resp.avatar_url,
        }, ensure_ascii=False))
    return _error(_friendly_error(resp))


_HANDLERS = {
    "send_message": _send_message,
    "read_messages": _read_messages,
    "list_rooms": _list_rooms,
    "get_room_info": _get_room_info,
    "get_room_members": _get_room_members,
    "join_room": _join_room,
    "leave_room": _leave_room,
    "create_room": _create_room,
    "invite_user": _invite_user,
    "send_reaction": _send_reaction,
    "resolve_alias": _resolve_alias,
    "get_messages_by_date": _get_messages_by_date,
    "get_notification_counts": _get_notification_counts,
    "get_direct_messages": _get_direct_messages,
    "send_direct_message": _send_direct_message,
    "get_user_profile": _get_user_profile,
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
