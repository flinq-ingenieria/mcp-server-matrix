"""
MODULE_CONTRACT:
  PURPOSE: MCP server for Matrix — send/read messages, manage rooms via matrix-nio
  SCOPE: stdio MCP server exposing Matrix Client-Server API as MCP tools
  DEPENDS: mcp (Python SDK), matrix-nio
  INPUTS: MCP tool calls via stdio (JSON-RPC)
  OUTPUTS: Tool results — messages, room lists, event confirmations
  LINKS: https://spec.matrix.org/latest/client-server-api/
  MODULE_MAP: main, serve, _send_message, _read_messages, _search_messages,
              _list_rooms, _search_rooms, _get_room_info, _get_room_members,
              _send_reaction, _list_unread_rooms, _read_unread_messages,
              _mark_room_read
  CHANGE_SUMMARY:
    - 0.1.0: Initial release — 11 tools for Matrix interaction
    - 0.2.0: Unread tools (list_unread_rooms, read_unread_messages,
             mark_room_read) backed by GET /notifications
"""

# START_BLOCK_IMPORTS
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from urllib.parse import quote

import httpx
import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from nio import (
    AsyncClient,
    JoinedMembersResponse,
    LoginResponse,
    MessageDirection,
    SyncResponse,
    RoomGetStateEventResponse,
    RoomMessagesResponse,
    RoomResolveAliasResponse,
    RoomSendResponse,
    WhoamiResponse,
)
# END_BLOCK_IMPORTS

logger = logging.getLogger("mcp-server-matrix")

# START_BLOCK_AUTH
_client: AsyncClient | None = None


async def _get_client() -> AsyncClient:
    """Return an authenticated AsyncClient, caching across calls.

    Reads configuration from environment variables:
        MATRIX_HOMESERVER   — e.g. https://matrix.example.com
        MATRIX_USER         — full user ID, e.g. @bot:example.com
        MATRIX_PASSWORD     — account password
        MATRIX_ACCESS_TOKEN — (optional) use token instead of password login
        MATRIX_DEVICE_ID    — (optional) device ID for an access token
        MATRIX_DEVICE_NAME  — (optional) device name for password logins
        MATRIX_STORE_PATH   — (optional) persistent nio store / session dir
    """
    global _client

    if _client is not None:
        return _client

    homeserver = os.environ.get("MATRIX_HOMESERVER", "").strip().rstrip("/")
    user = os.environ.get("MATRIX_USER", "")
    password = os.environ.get("MATRIX_PASSWORD", "")
    access_token = os.environ.get("MATRIX_ACCESS_TOKEN", "")
    device_id = os.environ.get("MATRIX_DEVICE_ID", "").strip()
    device_name = os.environ.get("MATRIX_DEVICE_NAME", "").strip() or "mcp-server-matrix"

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
        if device_id:
            client.device_id = device_id
    elif not await _restore_session(client, store_path, homeserver, user):
        resp = await client.login(password=password, device_name=device_name)
        if not isinstance(resp, LoginResponse):
            raise RuntimeError(f"Matrix login failed: {resp}")
        logger.info("Logged in as %s (device %s)", resp.user_id, resp.device_id)
        _save_session(store_path, homeserver, user, resp.device_id, resp.access_token)

    _client = client
    return _client


# Password logins create a new Matrix device each time. When the server is
# restarted often (e.g. one process per user behind a gateway), reuse the
# session from the previous login instead of piling up devices.
_SESSION_FILE = "session.json"


async def _restore_session(
    client: AsyncClient, store_path: str, homeserver: str, user: str
) -> bool:
    path = Path(store_path) / _SESSION_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if data.get("homeserver") != homeserver or data.get("user") != user:
        return False
    client.access_token = data.get("access_token", "")
    client.user_id = user
    client.device_id = data.get("device_id", "")
    resp = await client.whoami()
    if isinstance(resp, WhoamiResponse) and resp.user_id == user:
        logger.info("Reusing Matrix session for %s (device %s)", user, client.device_id)
        return True
    logger.info("Stored Matrix session is no longer valid; logging in again")
    client.access_token = ""
    return False


def _save_session(
    store_path: str, homeserver: str, user: str, device_id: str, access_token: str
) -> None:
    path = Path(store_path) / _SESSION_FILE
    payload = {
        "homeserver": homeserver,
        "user": user,
        "device_id": device_id,
        "access_token": access_token,
    }
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    except OSError as exc:
        logger.warning("Could not save Matrix session: %s", exc)


# Sync filter: only fetch the state events we need for room search and
# a single timeline event per room to seed the prev_batch token.
# This keeps the initial sync payload small even with hundreds of rooms.
_SYNC_FILTER: dict = {
    "room": {
        "timeline": {"limit": 1},
        "state": {
            "types": [
                "m.room.name",
                "m.room.canonical_alias",
                "m.room.topic",
            ],
            "lazy_load_members": True,
        },
        "ephemeral": {"limit": 0, "types": []},
        "account_data": {"limit": 0, "types": []},
        "include_leave": False,
    },
    "presence": {"limit": 0, "types": []},
    "account_data": {"limit": 0, "types": []},
}


ROOM_CACHE_TTL = 30 * 60  # seconds before an incremental sync refreshes the room list
_synced_at: float | None = None


async def _ensure_synced(client: AsyncClient) -> None:
    """Populate the room cache, refreshing it incrementally when it is stale.

    A long-running server would otherwise never see rooms joined after its
    first sync.
    """
    global _synced_at
    now = monotonic()
    if client.rooms:
        if _synced_at is None:
            _synced_at = now  # rooms came from a sync we did not time; start the clock
        if now - _synced_at < ROOM_CACHE_TTL:
            return
        since = getattr(client, "next_batch", None) or None
        resp = await client.sync(timeout=0, since=since, sync_filter=_SYNC_FILTER)
        if isinstance(resp, SyncResponse):
            _synced_at = now
            logger.debug("Room cache refreshed — %d rooms", len(client.rooms))
        else:
            logger.warning("Incremental sync failed: %s", resp)
        return
    resp = await client.sync(timeout=0, sync_filter=_SYNC_FILTER)
    if not isinstance(resp, SyncResponse) or not getattr(resp, "next_batch", ""):
        logger.warning("Initial sync did not yield next_batch; retrying (%s)", resp)
        resp = await client.sync(timeout=30000, sync_filter=_SYNC_FILTER)
    if not isinstance(resp, SyncResponse) or not getattr(resp, "next_batch", ""):
        raise RuntimeError("Matrix sync did not return a usable next_batch token")
    _synced_at = now
    logger.info("Room cache populated — %d rooms", len(client.rooms))


async def _close_client() -> None:
    """Close the cached client."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None
# END_BLOCK_AUTH


# START_BLOCK_HELPERS
def _response_status(response: object) -> int | None:
    return getattr(getattr(response, "transport_response", None), "status", None)


def _response_message(response: object) -> str | None:
    message = getattr(response, "message", None)
    return message if isinstance(message, str) else None


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


def _best_room_match(rooms: dict, query: str) -> str | None:
    """Return the room_id that best matches query using ranked LIKE logic.

    Priority: exact display-name > starts-with > contains in name/alias/topic.
    When multiple candidates share the same tier, pick the one with the shortest
    display name (most specific match). Always returns one result or None.
    """
    q = query.lower()

    exact = [rid for rid, r in rooms.items() if (r.display_name or "").lower() == q]
    if exact:
        return exact[0]

    starts = [rid for rid, r in rooms.items() if (r.display_name or "").lower().startswith(q)]
    if starts:
        return min(starts, key=lambda rid: len(rooms[rid].display_name or rid))

    contains = [
        rid for rid, r in rooms.items()
        if q in (r.display_name or "").lower()
        or q in (r.canonical_alias or "").lower()
        or q in (r.topic or "").lower()
    ]
    if contains:
        return min(contains, key=lambda rid: len(rooms[rid].display_name or rid))

    return None


async def _resolve_room_id_for_read(
    client: AsyncClient, room_ref: str
) -> tuple[str | None, str | None]:
    """Resolve a room identifier to a room ID.

    Accepts a room ID (!room:server), alias (#room:server), or any substring of
    the room display name / alias / topic. Always picks the best single match.
    """
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

    # Name lookup — populate room cache on demand, only if needed.
    await _ensure_synced(client)
    room_id = _best_room_match(client.rooms, value)
    if room_id:
        logger.debug("Room name %r resolved to %s", value, room_id)
        return room_id, None

    return None, (
        f"Room not found: {value!r}. "
        "Try search_rooms to see available rooms."
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


def _format_sender(matrix_id: str) -> str:
    """Extract a readable name from a Matrix user ID (@localpart:server)."""
    if matrix_id.startswith("@") and ":" in matrix_id:
        return matrix_id[1:matrix_id.index(":")]
    return matrix_id


def _format_message_line(sender: str, ts_ms: int, body: str) -> str:
    """Format a single message as a readable line."""
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    time_str = dt.strftime("%Y-%m-%d %H:%M")
    return f"[{time_str}] {_format_sender(sender)}: {body}"
# END_BLOCK_HELPERS


# START_BLOCK_TOOLS
# Annotations let clients tell reads from writes (e.g. skip confirmation for reads).
READ_ONLY_TOOL = types.ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)
WRITE_TOOL = types.ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
)
MARK_READ_TOOL = types.ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
)
TOOLS = [
    types.Tool(
        name="send_message",
        annotations=WRITE_TOOL,
        description="Send a text message to a Matrix room.",
        inputSchema={
            "type": "object",
            "properties": {
                "room_id": {
                    "type": "string",
                    "description": (
                        "Room ID (!abc:server), alias (#room:server), "
                        "or any substring of the room name"
                    ),
                },
                "text": {"type": "string", "description": "Message text to send"},
            },
            "required": ["room_id", "text"],
        },
    ),
    types.Tool(
        name="read_messages",
        annotations=READ_ONLY_TOOL,
        description=(
            "Read messages from a Matrix room in chronological order. "
            "The room can be identified by ID, alias, or any substring of its name. "
            "Returns plain readable text (sender, time, body) — no raw Matrix metadata. "
            "Use 'since' (next_token from a previous call) to page backwards through history. "
            "Call repeatedly with next_token until completed=true to read the full history."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "room_id": {
                    "type": "string",
                    "description": (
                        "Room ID (!abc:server), alias (#room:server), "
                        "or any substring of the room name (e.g. 'Flinq IT')"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Messages to return per call (default 50, max 500)",
                    "default": 50,
                },
                "since": {
                    "type": "string",
                    "description": "next_token from a previous read_messages call to continue pagination",
                },
                "from_date": {
                    "type": "string",
                    "description": "Lower bound timestamp (ISO 8601 or YYYY-MM-DD)",
                },
                "to_date": {
                    "type": "string",
                    "description": "Upper bound timestamp (ISO 8601 or YYYY-MM-DD)",
                },
            },
            "required": ["room_id"],
        },
    ),
    types.Tool(
        name="search_messages",
        annotations=READ_ONLY_TOOL,
        description=(
            "Search for messages containing a keyword or phrase in a room's history. "
            "Use this to check if something was mentioned, find a decision, or locate a specific conversation. "
            "Returns matching messages as plain readable text."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "room_id": {
                    "type": "string",
                    "description": "Room ID, alias, or any substring of the room name",
                },
                "query": {
                    "type": "string",
                    "description": "Text to search for (case-insensitive)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum matching messages to return (default 20, max 100)",
                    "default": 20,
                },
                "from_date": {
                    "type": "string",
                    "description": "Only search messages after this date (ISO 8601 or YYYY-MM-DD)",
                },
                "to_date": {
                    "type": "string",
                    "description": "Only search messages before this date (ISO 8601 or YYYY-MM-DD)",
                },
            },
            "required": ["room_id", "query"],
        },
    ),
    types.Tool(
        name="search_rooms",
        annotations=READ_ONLY_TOOL,
        description=(
            "Search joined Matrix rooms by name, alias, or topic. "
            "Returns matching rooms so the user can confirm which one to use. "
            "When only one room matches, you can proceed directly."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text to search in room name, alias, or topic (case-insensitive)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results (default 20, max 50)",
                    "default": 20,
                },
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="list_rooms",
        annotations=READ_ONLY_TOOL,
        description=(
            "List Matrix rooms the user has joined, alphabetically. "
            "Use search_rooms instead when looking for a specific room."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Rooms per page (default 30, max 100)",
                    "default": 30,
                },
                "offset": {
                    "type": "integer",
                    "description": "Rooms to skip for pagination (default 0)",
                    "default": 0,
                },
            },
        },
    ),
    types.Tool(
        name="get_room_info",
        annotations=READ_ONLY_TOOL,
        description="Get details about a Matrix room: name, topic, member count, alias.",
        inputSchema={
            "type": "object",
            "properties": {
                "room_id": {
                    "type": "string",
                    "description": "Room ID, alias, or substring of room name",
                },
            },
            "required": ["room_id"],
        },
    ),
    types.Tool(
        name="get_room_members",
        annotations=READ_ONLY_TOOL,
        description="List the members of a Matrix room.",
        inputSchema={
            "type": "object",
            "properties": {
                "room_id": {
                    "type": "string",
                    "description": "Room ID, alias, or substring of room name",
                },
            },
            "required": ["room_id"],
        },
    ),
    types.Tool(
        name="send_reaction",
        annotations=WRITE_TOOL,
        description="Send an emoji reaction to a specific message in a Matrix room.",
        inputSchema={
            "type": "object",
            "properties": {
                "room_id": {
                    "type": "string",
                    "description": "Room ID or name substring",
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
        name="list_unread_rooms",
        annotations=READ_ONLY_TOOL,
        description=(
            "List rooms with unread messages for the user, with unread and mention counts. "
            "Use this first when the user asks what is new or unread (e.g. while driving), "
            "then read_unread_messages to get the actual messages. Counts follow the user's "
            "Matrix notification settings: muted rooms are not counted."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "only_mentions": {
                    "type": "boolean",
                    "description": "Only count messages that mention the user (highlights)",
                    "default": False,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum rooms to return (default 20, max 100)",
                    "default": 20,
                },
            },
        },
    ),
    types.Tool(
        name="read_unread_messages",
        annotations=READ_ONLY_TOOL,
        description=(
            "Read the user's unread Matrix messages, grouped by room, oldest first, as "
            "plain text suitable for reading aloud. Optionally restrict to one room. "
            "Does not mark anything as read; call mark_room_read afterwards if the user "
            "wants the messages marked as read."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "room_id": {
                    "type": "string",
                    "description": "Optional room ID, alias, or substring of the room name",
                },
                "only_mentions": {
                    "type": "boolean",
                    "description": "Only messages that mention the user",
                    "default": False,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum messages to return (default 50, max 200)",
                    "default": 50,
                },
            },
        },
    ),
    types.Tool(
        name="mark_room_read",
        annotations=MARK_READ_TOOL,
        description=(
            "Mark everything in a Matrix room as read for the user (moves the read marker "
            "and read receipt to the latest message). Only call this when the user asks."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "room_id": {
                    "type": "string",
                    "description": "Room ID, alias, or substring of the room name",
                },
            },
            "required": ["room_id"],
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
    room_id, resolve_error = await _resolve_room_id_for_read(client, args["room_id"])
    if resolve_error:
        return _error(resolve_error)
    text = args["text"]

    resp = await client.room_send(
        room_id=room_id,
        message_type="m.room.message",
        content={"msgtype": "m.text", "body": text},
    )

    if isinstance(resp, RoomSendResponse):
        return _ok(f"Sent to {room_id}: {resp.event_id}")
    return _error(f"Failed to send: {resp}")


_PAGINATE_TIMEOUT = 25.0  # seconds max per _paginate_messages call


async def _paginate_messages(
    client: AsyncClient,
    room_id: str,
    start_token: str | None,
    limit: int,
    from_dt: datetime | None,
    to_dt: datetime | None,
    match_text: str | None = None,
    timeout: float = _PAGINATE_TIMEOUT,
) -> tuple[list[dict], str | None, bool]:
    """Core pagination engine shared by read_messages and search_messages.

    Fetches from the server in batches of 100 (respects server limits) and
    accumulates up to `limit` qualifying messages going backwards in time.
    Stops after `timeout` seconds to avoid blocking indefinitely on deep history.

    Returns (messages, next_token, completed).
    messages are in chronological order (oldest first).
    If match_text is set, only messages whose body contains it are included.
    """
    messages: list[dict] = []
    page_token = start_token
    next_token = start_token
    completed = False
    seen_tokens: set[str] = set()
    deadline = monotonic() + timeout

    while len(messages) < limit:
        if monotonic() > deadline:
            logger.warning("Pagination timeout after %.0fs for room %s", timeout, room_id)
            break

        page_key = page_token if page_token is not None else "__start__"
        if page_key in seen_tokens:
            logger.warning("Pagination stalled at %s for room %s", page_key, room_id)
            break
        seen_tokens.add(page_key)

        resp = await client.room_messages(
            room_id=room_id,
            start=page_token,
            limit=100,
            direction=MessageDirection.back,
        )

        if not isinstance(resp, RoomMessagesResponse):
            logger.warning(
                "room_messages failed room_id=%r start=%r type=%s status=%r body=%r",
                room_id, page_token, type(resp).__name__,
                _response_status(resp), _response_message(resp),
            )
            break

        page_messages: list[dict] = []
        hit_lower_bound = False
        for event in resp.chunk:
            if not hasattr(event, "body"):
                continue
            event_ts = getattr(event, "server_timestamp", None)
            if event_ts is None:
                continue
            event_dt = datetime.fromtimestamp(event_ts / 1000, tz=timezone.utc)

            if to_dt and event_dt > to_dt:
                continue
            if from_dt and event_dt < from_dt:
                hit_lower_bound = True
                completed = True
                break

            body = event.body or ""
            if match_text and match_text not in body.lower():
                continue

            page_messages.append({
                "sender": event.sender,
                "ts_ms": event_ts,
                "body": body,
                "event_id": event.event_id,
            })

        page_messages.reverse()
        messages = page_messages + messages
        next_token = resp.end

        if hit_lower_bound:
            break
        if not next_token or next_token == page_token:
            completed = True
            break

        page_token = next_token

    return messages[-limit:], next_token, completed


async def _read_messages(client: AsyncClient, args: dict) -> list[types.TextContent]:
    room_ref = args["room_id"]
    room_id, resolve_error = await _resolve_room_id_for_read(client, room_ref)
    if resolve_error:
        return _error(resolve_error)

    limit = args.get("limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return _error("Invalid limit: expected a positive integer")
    limit = min(limit, 500)

    since = (args.get("since") or "").strip()
    from_date = (args.get("from_date") or "").strip()
    to_date = (args.get("to_date") or "").strip()

    from_dt, to_dt = None, None
    if from_date:
        from_dt, err = _parse_datetime_input(from_date, "from_date")
        if err:
            return _error(err)
    if to_date:
        to_dt, err = _parse_datetime_input(to_date, "to_date")
        if err:
            return _error(err)
    if from_dt and to_dt and from_dt > to_dt:
        return _error("from_date must be before to_date")

    room = client.rooms.get(room_id)
    start_token = since or _room_prev_batch(room)

    messages, next_token, completed = await _paginate_messages(
        client, room_id, start_token, limit, from_dt, to_dt
    )

    room_name = (room.display_name if room else None) or room_id
    header = f"Room: {room_name}"
    if from_dt or to_dt:
        header += f"  [{from_dt.date() if from_dt else '…'} → {to_dt.date() if to_dt else '…'}]"
    header += f"  ({len(messages)} messages)"

    lines = [header, ""]
    lines.extend(_format_message_line(m["sender"], m["ts_ms"], m["body"]) for m in messages)

    if not completed and next_token:
        lines.append(f"\nnext_token: {next_token}  ← pass this as 'since' to read further back")
    else:
        lines.append("\n[completed: no more messages]")

    return _ok("\n".join(lines))


def _rooms_from_cache(
    client: AsyncClient, query: str = "", limit: int = 100, offset: int = 0
) -> tuple[list[dict], int]:
    """Build a paginated, filtered room list from the in-memory nio cache.

    No homeserver API calls — all data comes from client.rooms populated by sync.
    Returns (page, total_matching).
    """
    results = []
    for room_id, room in client.rooms.items():
        name = room.display_name or room_id
        alias = room.canonical_alias or ""
        topic = room.topic or ""
        if query and (
            query not in name.lower()
            and query not in alias.lower()
            and query not in topic.lower()
        ):
            continue
        results.append({
            "room_id": room_id,
            "display_name": name,
            "member_count": room.member_count,
            "canonical_alias": alias or None,
            "topic": topic or None,
        })
    results.sort(key=lambda r: r["display_name"].lower())
    return results[offset: offset + limit], len(results)


async def _list_rooms(client: AsyncClient, args: dict) -> list[types.TextContent]:
    await _ensure_synced(client)

    raw_limit = args.get("limit", 30)
    if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or raw_limit < 1:
        return _error("Invalid limit: expected a positive integer")
    limit = min(raw_limit, 100)

    raw_offset = args.get("offset", 0)
    if isinstance(raw_offset, bool) or not isinstance(raw_offset, int) or raw_offset < 0:
        return _error("Invalid offset: expected a non-negative integer")

    rooms, total = _rooms_from_cache(client, limit=limit, offset=raw_offset)

    end = raw_offset + len(rooms)
    header = f"Rooms {raw_offset + 1}–{end} of {total}"
    if end < total:
        header += f" — next page: offset={end}"
    header += ":"

    lines = [header]
    lines.extend(_format_room_line(room) for room in rooms)
    return _ok("\n".join(lines))


async def _search_rooms(client: AsyncClient, args: dict) -> list[types.TextContent]:
    await _ensure_synced(client)

    query = (args.get("query") or "").strip()
    if not query:
        return _error("query must be a non-empty string")

    raw_limit = args.get("limit", 20)
    if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or raw_limit < 1:
        return _error("Invalid limit: expected a positive integer")
    limit = min(raw_limit, 50)

    rooms, total = _rooms_from_cache(client, query=query.lower(), limit=limit)

    if not rooms:
        return _ok(f'No rooms found matching "{query}"')

    if len(rooms) == 1:
        r = rooms[0]
        return _ok(
            f"Found 1 room: {r['display_name']} ({r['room_id']})"
            + (f" | {r['member_count']} members" if r.get("member_count") else "")
        )

    lines = [
        f"{total} rooms match '{query}' (showing {len(rooms)}).",
        "Ask the user which room to use, then proceed with the chosen room ID or name:",
        "",
    ]
    lines.extend(_format_room_line(r) for r in rooms)
    return _ok("\n".join(lines))


async def _get_room_info(client: AsyncClient, args: dict) -> list[types.TextContent]:
    room_id, resolve_error = await _resolve_room_id_for_read(client, args["room_id"])
    if resolve_error:
        return _error(resolve_error)
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
    return _ok(_format_room_info_text(info))


async def _get_room_members(client: AsyncClient, args: dict) -> list[types.TextContent]:
    room_ref = args["room_id"]
    room_id, resolve_error = await _resolve_room_id_for_read(client, room_ref)
    if resolve_error:
        return _error(resolve_error)

    resp = await client.joined_members(room_id)
    if isinstance(resp, JoinedMembersResponse):
        lines = [f"Members of {room_id} ({len(resp.members)}):"]
        for member in sorted(resp.members, key=lambda m: (getattr(m, "display_name", None) or "").lower()):
            display = getattr(member, "display_name", None) or _format_sender(member.user_id)
            lines.append(f"  {display} ({member.user_id})")
        return _ok("\n".join(lines))

    logger.warning(
        "joined_members failed room_id=%r type=%s status=%r body=%r",
        room_id, type(resp).__name__, _response_status(resp), _response_message(resp),
    )
    return _error(f"Failed to get members: {resp}")


async def _search_messages(client: AsyncClient, args: dict) -> list[types.TextContent]:
    room_ref = args["room_id"]
    room_id, resolve_error = await _resolve_room_id_for_read(client, room_ref)
    if resolve_error:
        return _error(resolve_error)

    query = (args.get("query") or "").strip().lower()
    if not query:
        return _error("query must be a non-empty string")

    limit = args.get("limit", 20)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return _error("Invalid limit: expected a positive integer")
    limit = min(limit, 100)

    from_date = (args.get("from_date") or "").strip()
    to_date = (args.get("to_date") or "").strip()
    from_dt, to_dt = None, None
    if from_date:
        from_dt, err = _parse_datetime_input(from_date, "from_date")
        if err:
            return _error(err)
    if to_date:
        to_dt, err = _parse_datetime_input(to_date, "to_date")
        if err:
            return _error(err)

    room = client.rooms.get(room_id)
    start_token = _room_prev_batch(room)

    messages, _, completed = await _paginate_messages(
        client, room_id, start_token, limit, from_dt, to_dt, match_text=query
    )

    room_name = (room.display_name if room else None) or room_id
    if not messages:
        suffix = " (full history scanned)" if completed else " (partial scan)"
        return _ok(f'No messages containing "{query}" found in {room_name}{suffix}.')

    lines = [f'{len(messages)} messages containing "{query}" in {room_name}:', ""]
    lines.extend(_format_message_line(m["sender"], m["ts_ms"], m["body"]) for m in messages)
    if not completed:
        lines.append("\n[Note: only the most recent history was scanned]")
    return _ok("\n".join(lines))


async def _send_reaction(client: AsyncClient, args: dict) -> list[types.TextContent]:
    room_id, resolve_error = await _resolve_room_id_for_read(client, args["room_id"])
    if resolve_error:
        return _error(resolve_error)

    resp = await client.room_send(
        room_id=room_id,
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
        return _ok(f"Reaction sent: {resp.event_id}")
    return _error(f"Failed to react: {resp}")


# --- unread ---------------------------------------------------------------
# Unread state comes from GET /notifications: the homeserver evaluates the
# user's push rules and read receipts, so the `read` flag is authoritative
# at request time and independent of this client's (filtered) sync cache.
_NOTIFICATIONS_PAGE = 100
_NOTIFICATIONS_MAX_SCAN = 500


async def _matrix_request(
    client: AsyncClient,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json: dict | None = None,
) -> dict:
    if not client.access_token:
        raise RuntimeError("Matrix client is not logged in")
    homeserver = str(client.homeserver).rstrip("/")
    async with httpx.AsyncClient(timeout=30.0) as http:
        response = await http.request(
            method,
            f"{homeserver}/_matrix/client/v3{path}",
            params={key: value for key, value in (params or {}).items() if value is not None},
            json=json,
            headers={"Authorization": f"Bearer {client.access_token}"},
        )
    if response.status_code >= 400:
        raise RuntimeError(f"Matrix {method} {path} failed: HTTP {response.status_code} {response.text[:200]}")
    return response.json() if response.content else {}


def _is_highlight(notification: dict) -> bool:
    for action in notification.get("actions") or []:
        if isinstance(action, dict) and action.get("set_tweak") == "highlight":
            return action.get("value", True) is not False
    return False


async def _unread_notifications(
    client: AsyncClient, *, only_mentions: bool, room_id: str | None = None
) -> tuple[list[dict], bool]:
    """Return unread notifications (newest first) and whether the scan was cut short."""
    unread: list[dict] = []
    scanned = 0
    next_token: str | None = None
    while scanned < _NOTIFICATIONS_MAX_SCAN:
        page = await _matrix_request(
            client,
            "GET",
            "/notifications",
            params={
                "limit": _NOTIFICATIONS_PAGE,
                "from": next_token,
                "only": "highlight" if only_mentions else None,
            },
        )
        items = page.get("notifications") or []
        scanned += len(items)
        for item in items:
            if item.get("read"):
                continue
            if room_id and item.get("room_id") != room_id:
                continue
            unread.append(item)
        next_token = page.get("next_token")
        if not next_token or not items:
            return unread, False
    return unread, True


def _notification_text(event: dict) -> str:
    content = event.get("content") or {}
    event_type = event.get("type")
    if event_type == "m.room.encrypted":
        return "[encrypted message — not readable by this connector]"
    if event_type == "m.room.message":
        body = str(content.get("body") or "").strip()
        msgtype = content.get("msgtype")
        if msgtype in {"m.image", "m.file", "m.video", "m.audio"}:
            return f"[{msgtype[2:]}] {body}".strip()
        return body or "[empty message]"
    if event_type == "m.room.member" and content.get("membership") == "invite":
        return "[room invitation]"
    return f"[{event_type}]"


async def _room_label(client: AsyncClient, room_id: str) -> str:
    room = client.rooms.get(room_id)
    return (room.display_name if room else None) or room_id


def _bounded_int(args: dict, key: str, default: int, maximum: int) -> int | str:
    value = args.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return f"Invalid {key}: expected a positive integer"
    return min(value, maximum)


async def _list_unread_rooms(client: AsyncClient, args: dict) -> list[types.TextContent]:
    limit = _bounded_int(args, "limit", 20, 100)
    if isinstance(limit, str):
        return _error(limit)
    only_mentions = bool(args.get("only_mentions"))
    await _ensure_synced(client)
    notifications, truncated = await _unread_notifications(client, only_mentions=only_mentions)

    rooms: dict[str, dict] = {}
    for item in notifications:
        entry = rooms.setdefault(item["room_id"], {"unread": 0, "mentions": 0, "last_ts": 0})
        entry["unread"] += 1
        entry["mentions"] += 1 if _is_highlight(item) else 0
        entry["last_ts"] = max(entry["last_ts"], int((item.get("event") or {}).get("origin_server_ts") or item.get("ts") or 0))

    if not rooms:
        scope = "unread mentions" if only_mentions else "unread messages"
        return _ok(f"No {scope}.")

    ordered = sorted(rooms.items(), key=lambda pair: pair[1]["last_ts"], reverse=True)
    total = sum(entry["unread"] for entry in rooms.values())
    lines = [f"{total} unread in {len(rooms)} rooms (most recent first):"]
    for room_id, entry in ordered[:limit]:
        when = datetime.fromtimestamp(entry["last_ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        mentions = f", {entry['mentions']} mentions" if entry["mentions"] else ""
        lines.append(
            f"- {await _room_label(client, room_id)} ({room_id}): {entry['unread']} unread{mentions} — last {when}"
        )
    if len(ordered) > limit:
        lines.append(f"... and {len(ordered) - limit} more rooms")
    if truncated:
        lines.append("[Note: only the most recent notifications were scanned]")
    return _ok("\n".join(lines))


async def _read_unread_messages(client: AsyncClient, args: dict) -> list[types.TextContent]:
    limit = _bounded_int(args, "limit", 50, 200)
    if isinstance(limit, str):
        return _error(limit)
    room_id = None
    if args.get("room_id"):
        room_id, resolve_error = await _resolve_room_id_for_read(client, args["room_id"])
        if resolve_error:
            return _error(resolve_error)
    await _ensure_synced(client)
    notifications, truncated = await _unread_notifications(
        client, only_mentions=bool(args.get("only_mentions")), room_id=room_id
    )
    if not notifications:
        return _ok("No unread messages" + (f" in {await _room_label(client, room_id)}." if room_id else "."))

    # Newest `limit` messages, then oldest first within each room.
    selected = notifications[:limit]
    by_room: dict[str, list[dict]] = {}
    for item in selected:
        by_room.setdefault(item["room_id"], []).append(item["event"] if isinstance(item.get("event"), dict) else {})
    lines: list[str] = []
    for rid, events in by_room.items():
        events.sort(key=lambda event: event.get("origin_server_ts") or 0)
        lines.append(f"Room: {await _room_label(client, rid)} ({len(events)} unread)")
        for event in events:
            lines.append(
                _format_message_line(
                    str(event.get("sender") or "?"),
                    int(event.get("origin_server_ts") or 0),
                    _notification_text(event),
                )
            )
        lines.append("")
    if len(notifications) > limit:
        lines.append(f"[{len(notifications) - limit} older unread messages not shown; raise limit to read them]")
    if truncated:
        lines.append("[Note: only the most recent notifications were scanned]")
    return _ok("\n".join(lines).rstrip())


async def _mark_room_read(client: AsyncClient, args: dict) -> list[types.TextContent]:
    room_id, resolve_error = await _resolve_room_id_for_read(client, args.get("room_id", ""))
    if resolve_error:
        return _error(resolve_error)
    encoded = quote(room_id, safe="")
    latest = await _matrix_request(
        client, "GET", f"/rooms/{encoded}/messages", params={"dir": "b", "limit": 1}
    )
    chunk = latest.get("chunk") or []
    if not chunk or not chunk[0].get("event_id"):
        return _error(f"No events found in {room_id}")
    event_id = chunk[0]["event_id"]
    await _matrix_request(
        client,
        "POST",
        f"/rooms/{encoded}/read_markers",
        json={"m.fully_read": event_id, "m.read": event_id},
    )
    return _ok(f"Marked {await _room_label(client, room_id)} as read (up to {event_id}).")


_HANDLERS = {
    "send_message": _send_message,
    "read_messages": _read_messages,
    "search_messages": _search_messages,
    "list_rooms": _list_rooms,
    "search_rooms": _search_rooms,
    "get_room_info": _get_room_info,
    "get_room_members": _get_room_members,
    "send_reaction": _send_reaction,
    "list_unread_rooms": _list_unread_rooms,
    "read_unread_messages": _read_unread_messages,
    "mark_room_read": _mark_room_read,
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


if __name__ == "__main__":
    main()
# END_BLOCK_MAIN
