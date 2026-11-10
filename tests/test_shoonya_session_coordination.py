"""Cooperative Shoonya session protocol tests (harness v3.5 §1.1.1).

Verifies, without Chrome or network:
  1. A young, server-valid shared token is adopted (no login, no lock).
  2. Missing session -> lock acquired -> login runs -> lock released.
  3. A peer holding the lock publishes a fresh token -> waiter adopts it.
  4. A stale lock (old timestamp) is broken -> login proceeds.
  5. A server-valid but over-age token triggers the refresh path.
  6. Login failure falls back to the old (still-valid) token.
"""
import json
import os
import threading
import time

import pytest

import broker.shoonya_adapter as sa


class FakeNorenApi:
    def __init__(self):
        self.session_set = None
        self.limits_stat = "Ok"

    def set_session(self, userid, password, token, factor2):
        self.session_set = (userid, token)

    def injectOAuthHeader(self, token, userid, actid):
        pass

    def get_limits(self):
        return {"stat": self.limits_stat}

    def getAccessToken(self, auth_code, secret, client_id, userid):
        return ("fresh-token", userid, None, userid)


@pytest.fixture()
def session_env(tmp_path, monkeypatch):
    monkeypatch.setattr(sa, "SESSION_FILE", str(tmp_path / "shoonya_session.json"))
    monkeypatch.setattr(sa, "SESSION_LOCK_FILE", str(tmp_path / "shoonya_session.lock"))
    monkeypatch.setattr(sa, "PEER_POLL_SEC", 0.05)
    monkeypatch.setattr(sa, "PEER_WAIT_TIMEOUT_SEC", 3.0)
    monkeypatch.setattr(sa, "LOCK_STALE_SEC", 3.0)
    monkeypatch.setenv("SHOONYA_CLIENT_ID", "cid")
    monkeypatch.setenv("SHOONYA_USERNAME", "U1")
    monkeypatch.setenv("SHOONYA_PASSWORD", "pw")
    monkeypatch.setenv("SHOONYA_TOTP", "totp")
    monkeypatch.setenv("SHOONYA_CLIENT_SECRET", "sec")
    return tmp_path


def _write_session(tmp_path, token="tok-1", age_sec=600.0, userid="U1"):
    payload = {
        "userid": userid,
        "susertoken": token,
        "actid": userid,
        "updated_at": time.time() - age_sec,
    }
    with open(sa.SESSION_FILE, "w") as f:
        json.dump(payload, f)


def _make_provider(monkeypatch, login_stub=None):
    api = FakeNorenApi()
    if login_stub is not None:
        monkeypatch.setattr(sa.ShoonyaSessionProvider, "_login", login_stub)
    provider = sa.ShoonyaSessionProvider(api)
    return provider, api


def test_young_valid_token_adopted_without_login(session_env, monkeypatch):
    _write_session(session_env, token="tok-young", age_sec=600)
    calls = {"n": 0}

    def _fail_login(self):
        calls["n"] += 1
        return False

    provider, api = _make_provider(monkeypatch, _fail_login)
    assert provider.susertoken == "tok-young"
    assert api.session_set is not None
    assert calls["n"] == 0
    assert not os.path.exists(sa.SESSION_LOCK_FILE)


def test_missing_session_single_login(session_env, monkeypatch):
    def _ok_login(self):
        _write_session(session_env, token="tok-fresh", age_sec=0.0)
        self.susertoken = "tok-fresh"
        return True

    provider, _ = _make_provider(monkeypatch, _ok_login)
    assert provider.susertoken == "tok-fresh"
    assert not os.path.exists(sa.SESSION_LOCK_FILE)  # lock released


def test_peer_lock_wait_and_adopt(session_env, monkeypatch):
    # No session file yet; a "peer" holds the lock and publishes a token
    # shortly after we start waiting.
    lock = {
        "pid": os.getpid() + 1,  # alive-looking foreign pid (same range)
        "ts": time.time(),
        "host": "test",
    }
    monkeypatch.setattr(sa.ShoonyaSessionProvider, "_login_lock_is_stale", lambda self: False)
    with open(sa.SESSION_LOCK_FILE, "w") as f:
        json.dump(lock, f)

    def _peer_publishes():
        time.sleep(0.3)
        _write_session(session_env, token="tok-peer", age_sec=0.0)

    thread = threading.Thread(target=_peer_publishes)
    thread.start()

    def _fail_login(self):
        raise AssertionError("waiter must adopt the peer token, not log in")

    provider, _ = _make_provider(monkeypatch, _fail_login)
    thread.join(timeout=2)
    assert provider.susertoken == "tok-peer"
    # The peer's lock is not ours to release; login path never acquired it.
    assert os.path.exists(sa.SESSION_LOCK_FILE)


def test_stale_lock_broken_then_login(session_env, monkeypatch):
    with open(sa.SESSION_LOCK_FILE, "w") as f:
        json.dump({"pid": 999999, "ts": time.time() - 3600, "host": "old"}, f)

    def _ok_login(self):
        _write_session(session_env, token="tok-after-stale", age_sec=0.0)
        self.susertoken = "tok-after-stale"
        return True

    provider, _ = _make_provider(monkeypatch, _ok_login)
    assert provider.susertoken == "tok-after-stale"
    assert not os.path.exists(sa.SESSION_LOCK_FILE)


def test_old_but_valid_token_triggers_refresh(session_env, monkeypatch):
    # 22h old: server-valid but beyond the 20h refresh threshold.
    _write_session(session_env, token="tok-old", age_sec=22 * 3600)
    got_lock = []

    def _ok_login(self):
        got_lock.append(os.path.exists(sa.SESSION_LOCK_FILE))
        _write_session(session_env, token="tok-new", age_sec=0.0)
        self.susertoken = "tok-new"
        return True

    provider, _ = _make_provider(monkeypatch, _ok_login)
    assert provider.susertoken == "tok-new"
    assert got_lock == [True]  # login only ran while holding the lock


def test_login_failure_falls_back_to_old_token(session_env, monkeypatch):
    _write_session(session_env, token="tok-rescue", age_sec=23 * 3600)

    def _bad_login(self):
        return False

    provider, api = _make_provider(monkeypatch, _bad_login)
    assert provider.susertoken == "tok-rescue"
    assert api.session_set is not None


def test_oauth_websocket_monkeypatch():
    """Verify that _patch_noren_api_for_oauth correctly replaces legacy connect frame with OAuth 't: a'."""
    class DummyNoren:
        def __init__(self):
            self._NorenApi__username = "TEST_USER"
            self._NorenApi__accountid = "TEST_USER"
            self._NorenApi__susertoken = "TEST_TOKEN"
            self._NorenApi__on_open = None
            self._NorenApi__on_error = None
            self.sent_payloads = []

        def _NorenApi__on_open_callback(self, ws=None):
            pass

        def _NorenApi__on_data_callback(self, ws=None, message=None, data_type=None, continue_flag=None):
            pass

        def _NorenApi__ws_send(self, payload):
            self.sent_payloads.append(payload)

    sa._patch_noren_api_for_oauth(DummyNoren)
    assert getattr(DummyNoren, "_oauth_ws_patched", False) is True

    instance = DummyNoren()
    instance._NorenApi__on_open_callback()
    assert len(instance.sent_payloads) == 1
    sent = json.loads(instance.sent_payloads[0])
    assert sent["t"] == "a"
    assert sent["uid"] == "TEST_USER"
    assert sent["accesstoken"] == "TEST_TOKEN"
    assert sent["source"] == "API"

    opened = []
    instance._NorenApi__on_open = lambda: opened.append(True)
    instance._NorenApi__on_data_callback(message=json.dumps({"t": "ak", "s": "OK", "uid": "TEST_USER"}))
    assert opened == [True]

