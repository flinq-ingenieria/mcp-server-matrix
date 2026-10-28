import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
from nio import RoomMessagesResponse
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_server_matrix import server  # noqa: E402

ROOM_A, ROOM_B, ROOM_C = "!a:hs", "!b:hs", "!c:hs"
_REAL_ASYNC_CLIENT = httpx.AsyncClient
T0 = 1_700_000_000_000  # 2023-11-14 22:13:20 UTC


def _notification(room, ts, body, read=False, highlight=False, etype="m.room.message"):
    actions = ["notify"] + ([{"set_tweak": "highlight"}] if highlight else [])
    event = {"type": etype, "sender": "@quico:hs", "origin_server_ts": ts, "event_id": f"$e{ts}"}
    if etype == "m.room.message":
        event["content"] = {"msgtype": "m.text", "body": body}
    return {"room_id": room, "read": read, "actions": actions, "event": event, "ts": ts}


# `read` is what Synapse's /notifications claims; it is deliberately wrong for
# ROOM_C and for the oldest ROOM_A item (stale threaded receipt): the counts
# from /sync are what decide.
PAGES = {
    None: {
        "notifications": [
            _notification(ROOM_B, T0 + 300_000, "¿Llegas a las 5?", highlight=True),
            _notification(ROOM_A, T0 + 200_000, "segundo"),
            _notification(ROOM_C, T0 + 150_000, "ya marcado como leído"),
        ],
        "next_token": "p2",
    },
    "p2": {
        "notifications": [
            _notification(ROOM_A, T0 + 100_000, "primero"),
            _notification(ROOM_B, T0 + 40_000, "", etype="m.room.encrypted"),
            _notification(ROOM_A, T0 + 10_000, "antiguo, ya leído"),
        ]
    },
}
SYNC = {
    "rooms": {
        "join": {
            ROOM_A: {"unread_notifications": {"notification_count": 2, "highlight_count": 0}},
            ROOM_B: {"unread_notifications": {"notification_count": 2, "highlight_count": 1}},
            ROOM_C: {"unread_notifications": {"notification_count": 0, "highlight_count": 0}},
        }
    }
}


def _event(ts, body, sender="@quico:hs", edit_of=None):
    content = {"msgtype": "m.text", "body": body}
    if edit_of:
        content = {
            "msgtype": "m.text",
            "body": f"* {body}",
            "m.new_content": {"msgtype": "m.text", "body": body},
            "m.relates_to": {"rel_type": "m.replace", "event_id": edit_of},
        }
    return SimpleNamespace(
        body=content["body"], sender=sender, server_timestamp=ts, event_id=f"$e{ts}",
        source={"type": "m.room.message", "content": content},
    )


# Readable history, newest first as /messages?dir=b returns it. ROOM_B is
# encrypted: nothing readable.
HISTORY = {
    ROOM_A: [
        _event(T0 + 250_000, "¿cómo regeneraba el EDI?"),
        _event(T0 + 220_000, "segundo, corregido", edit_of=f"$e{T0 + 200_000}"),
        _event(T0 + 200_000, "segundo"),
        _event(T0 + 100_000, "primero"),
        _event(T0 + 90_000, "anterior, ya leído"),
    ],
    ROOM_B: [],
}


def _handler(calls):
    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer tok"
        path = request.url.path
        if path.endswith("/sync"):
            return httpx.Response(200, json=SYNC)
        if path.endswith("/notifications"):
            return httpx.Response(200, json=PAGES[request.url.params.get("from")])
        if path.endswith("/messages"):
            return httpx.Response(200, json={"chunk": [{"event_id": "$latest"}]})
        if path.endswith("/read_markers"):
            return httpx.Response(200, json={})
        return httpx.Response(404)

    return handle


def _client():
    rooms = {
        ROOM_A: SimpleNamespace(display_name="Flinq IT", canonical_alias=None, topic=None),
        ROOM_B: SimpleNamespace(display_name="Quico", canonical_alias=None, topic=None),
        ROOM_C: SimpleNamespace(display_name="Facturación", canonical_alias=None, topic=None),
    }

    async def room_messages(room_id, start=None, limit=10, direction=None):
        return RoomMessagesResponse(room_id=room_id, chunk=HISTORY[room_id], start="s", end=None)

    return SimpleNamespace(access_token="tok", homeserver="https://hs", rooms=rooms, room_messages=room_messages)


def _run(monkeypatch, tool, args):
    calls = []
    transport = httpx.MockTransport(_handler(calls))
    monkeypatch.setattr(
        server.httpx, "AsyncClient", lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw)
    )
    result = asyncio.run(server._HANDLERS[tool](_client(), args))
    return result[0].text, calls


def test_list_unread_rooms_uses_sync_counts(monkeypatch):
    text, calls = _run(monkeypatch, "list_unread_rooms", {})
    assert text.splitlines() == [
        "4 unread in 2 rooms (most recent first):",
        f"- Quico ({ROOM_B}): 2 unread, 1 mentions — last 2023-11-14 22:18 UTC",
        f"- Flinq IT ({ROOM_A}): 2 unread — last 2023-11-14 22:16 UTC",
    ]
    sync = calls[0]
    assert sync.url.path.endswith("/sync") and sync.url.params["timeout"] == "0"
    assert json.loads(sync.url.params["filter"])["room"]["timeline"] == {"limit": 1}
    assert [c.url.params.get("from") for c in calls[1:]] == [None, "p2"]


def test_room_marked_read_is_not_unread_even_if_notifications_say_so(monkeypatch):
    text, _ = _run(monkeypatch, "list_unread_rooms", {})
    assert "Facturación" not in text
    text, _ = _run(monkeypatch, "read_unread_messages", {"room_id": ROOM_C})
    assert text == "No unread messages in Facturación."


def test_only_mentions_uses_highlight_filter_and_counts(monkeypatch):
    text, calls = _run(monkeypatch, "list_unread_rooms", {"only_mentions": True})
    assert calls[1].url.params["only"] == "highlight"
    assert text.splitlines()[0] == "1 unread in 1 rooms (most recent first):"


def test_read_unread_messages_include_what_followed(monkeypatch):
    text, _ = _run(monkeypatch, "read_unread_messages", {"room_id": ROOM_A})
    assert text.splitlines() == [
        "Room: Flinq IT (2 unread, 4 messages since the first one)",
        "[2023-11-14 22:15 UTC] quico: primero",
        "[2023-11-14 22:16 UTC] quico: segundo",
        "[2023-11-14 22:17 UTC] quico: (edit) segundo, corregido",
        "[2023-11-14 22:17 UTC] quico: ¿cómo regeneraba el EDI?",
    ]


def test_unreadable_notifications_still_listed(monkeypatch):
    text, _ = _run(monkeypatch, "read_unread_messages", {"room_id": ROOM_B})
    assert text.splitlines() == [
        "Room: Quico (2 unread)",
        "[2023-11-14 22:14 UTC] quico: [encrypted message — not readable by this connector]",
        "[2023-11-14 22:18 UTC] quico: ¿Llegas a las 5?",
    ]


def test_times_use_matrix_timezone(monkeypatch):
    monkeypatch.setattr(server, "DISPLAY_TZ", ZoneInfo("Europe/Madrid"))
    assert server._format_ts(T0) == "2023-11-14 23:13 CET"
    parsed, error = server._parse_datetime_input("2026-10-27T12:00", "from_date")
    assert error is None and parsed.isoformat() == "2026-10-27T11:00:00+00:00"


def test_mark_room_read_moves_both_markers(monkeypatch):
    text, calls = _run(monkeypatch, "mark_room_read", {"room_id": ROOM_A})
    assert "Marked Flinq IT as read" in text
    marker = calls[-1]
    assert marker.url.raw_path == b"/_matrix/client/v3/rooms/%21a%3Ahs/read_markers"
    assert json.loads(marker.content) == {"m.fully_read": "$latest", "m.read": "$latest"}
