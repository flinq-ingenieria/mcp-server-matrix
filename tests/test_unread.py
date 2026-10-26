import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_server_matrix import server  # noqa: E402

ROOM_A, ROOM_B = "!a:hs", "!b:hs"
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _notification(room, ts, body, read=False, highlight=False, etype="m.room.message"):
    actions = ["notify"] + ([{"set_tweak": "highlight"}] if highlight else [])
    event = {"type": etype, "sender": "@quico:hs", "origin_server_ts": ts, "event_id": f"$e{ts}"}
    if etype == "m.room.message":
        event["content"] = {"msgtype": "m.text", "body": body}
    return {"room_id": room, "read": read, "actions": actions, "event": event, "ts": ts}


PAGES = {
    None: {
        "notifications": [
            _notification(ROOM_B, 1_700_000_300_000, "¿Llegas a las 5?", highlight=True),
            _notification(ROOM_A, 1_700_000_200_000, "segundo"),
            _notification(ROOM_A, 1_700_000_100_000, "ya leído", read=True),
        ],
        "next_token": "p2",
    },
    "p2": {
        "notifications": [
            _notification(ROOM_A, 1_700_000_050_000, "primero"),
            _notification(ROOM_B, 1_700_000_040_000, "", etype="m.room.encrypted"),
        ]
    },
}


def _handler(calls):
    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer tok"
        path = request.url.path
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
    }
    return SimpleNamespace(access_token="tok", homeserver="https://hs", rooms=rooms)


def _run(monkeypatch, tool, args):
    calls = []
    transport = httpx.MockTransport(_handler(calls))
    monkeypatch.setattr(
        server.httpx, "AsyncClient", lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw)
    )
    result = asyncio.run(server._HANDLERS[tool](_client(), args))
    return result[0].text, calls


def test_list_unread_rooms_counts_per_room(monkeypatch):
    text, calls = _run(monkeypatch, "list_unread_rooms", {})
    assert text.splitlines() == [
        "4 unread in 2 rooms (most recent first):",
        f"- Quico ({ROOM_B}): 2 unread, 1 mentions — last 2023-11-14 22:18",
        f"- Flinq IT ({ROOM_A}): 2 unread — last 2023-11-14 22:16",
    ]
    assert [c.url.params.get("from") for c in calls] == [None, "p2"]


def test_only_mentions_uses_highlight_filter(monkeypatch):
    _, calls = _run(monkeypatch, "list_unread_rooms", {"only_mentions": True})
    assert calls[0].url.params["only"] == "highlight"


def test_read_unread_messages_groups_oldest_first(monkeypatch):
    text, _ = _run(monkeypatch, "read_unread_messages", {"room_id": ROOM_A})
    assert text.splitlines() == [
        "Room: Flinq IT (2 unread)",
        "[2023-11-14 22:14] quico: primero",
        "[2023-11-14 22:16] quico: segundo",
    ]
    text, _ = _run(monkeypatch, "read_unread_messages", {"room_id": ROOM_B})
    assert "[encrypted message" in text


def test_mark_room_read_moves_both_markers(monkeypatch):
    text, calls = _run(monkeypatch, "mark_room_read", {"room_id": ROOM_A})
    assert "Marked Flinq IT as read" in text
    marker = calls[-1]
    assert marker.url.raw_path == b"/_matrix/client/v3/rooms/%21a%3Ahs/read_markers"
    assert json.loads(marker.content) == {"m.fully_read": "$latest", "m.read": "$latest"}
