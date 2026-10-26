"""Password logins must not create a new Matrix device on every restart."""

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest
from nio import LoginResponse, WhoamiError, WhoamiResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_server_matrix import server  # noqa: E402

HS, USER = "https://hs.example", "@ana:hs.example"


class FakeClient:
    logins = 0

    def __init__(self, homeserver, user, store_path):
        self.homeserver, self.user, self.store_path = homeserver, user, store_path
        self.access_token = ""
        self.user_id = ""
        self.device_id = ""

    async def login(self, password, device_name):
        FakeClient.logins += 1
        n = FakeClient.logins
        self.access_token, self.user_id, self.device_id = f"tok{n}", USER, f"DEV{n}"
        return LoginResponse(USER, f"DEV{n}", f"tok{n}")

    async def whoami(self):
        if self.access_token == "revoked":
            return WhoamiError("M_UNKNOWN_TOKEN")
        return WhoamiResponse(USER, self.device_id, False)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AsyncClient", FakeClient)
    monkeypatch.setattr(server, "_client", None)
    FakeClient.logins = 0
    for key, value in {
        "MATRIX_HOMESERVER": HS,
        "MATRIX_USER": USER,
        "MATRIX_PASSWORD": "pw",
        "MATRIX_STORE_PATH": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("MATRIX_ACCESS_TOKEN", raising=False)
    return tmp_path


def _start(monkeypatch):
    monkeypatch.setattr(server, "_client", None)
    return asyncio.run(server._get_client())


def test_second_start_reuses_the_device(env, monkeypatch):
    first = _start(monkeypatch)
    second = _start(monkeypatch)
    assert FakeClient.logins == 1
    assert (second.device_id, second.access_token) == (first.device_id, first.access_token) == ("DEV1", "tok1")
    session = env / "session.json"
    assert stat.S_IMODE(os.stat(session).st_mode) == 0o600


def test_invalid_stored_token_logs_in_again(env, monkeypatch):
    _start(monkeypatch)
    data = json.loads((env / "session.json").read_text())
    (env / "session.json").write_text(json.dumps({**data, "access_token": "revoked"}))
    client = _start(monkeypatch)
    assert FakeClient.logins == 2 and client.device_id == "DEV2"


def test_session_of_another_account_is_ignored(env, monkeypatch):
    _start(monkeypatch)
    monkeypatch.setenv("MATRIX_USER", "@bob:hs.example")
    _start(monkeypatch)
    assert FakeClient.logins == 2


def test_access_token_mode_uses_configured_device(env, monkeypatch):
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "given")
    monkeypatch.setenv("MATRIX_DEVICE_ID", "PHONE")
    client = _start(monkeypatch)
    assert (client.access_token, client.device_id, FakeClient.logins) == ("given", "PHONE", 0)
