"""Dashboard access token: optional, off by default, never logged, protects the API as well as the page."""
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "s3cr3t-token-for-the-test-0123456789"
API = ["/api/state", "/api/events", "/api/metrics", "/api/export", "/api/agent/A"]


def load(monkeypatch, token=None, token_file=None):
    for k in ("DASHBOARD_TOKEN", "DASHBOARD_TOKEN_FILE"):
        monkeypatch.delenv(k, raising=False)
    if token:
        monkeypatch.setenv("DASHBOARD_TOKEN", token)
    if token_file:
        monkeypatch.setenv("DASHBOARD_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("STATUS_URLS", ",".join(f"http://127.0.0.1:1/status" for _ in range(4)))
    spec = importlib.util.spec_from_file_location("dash_auth_mod", os.path.join(ROOT, "dashboard", "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def serve():
    servers = []

    def start(mod):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"
    yield start
    for s in servers:
        s.shutdown()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def call(url, method="GET", headers=None, data=None):
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        r = opener.open(req, timeout=5)
        return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def test_off_by_default_so_local_demos_stay_open(monkeypatch, serve):
    mod = load(monkeypatch)
    assert not mod.AUTH.enabled
    base = serve(mod)
    assert call(base + "/")[0] == 200 and b"Distributed Cyber-Resilience Platform" in call(base + "/")[1]
    assert call(base + "/api/events")[0] == 200
    assert call(base + "/login", "POST", data=b"token=x")[0] == 404      # no login endpoint when off


def test_every_api_endpoint_and_the_page_need_the_token(monkeypatch, serve):
    mod = load(monkeypatch, TOKEN)
    base = serve(mod)
    for path in API:
        code, body, hdr = call(base + path)
        assert code == 401, path
        assert hdr.get("WWW-Authenticate") == "Bearer" and TOKEN.encode() not in body
        assert call(base + path, headers={"Authorization": "Bearer wrong"})[0] == 401, path
        assert call(base + path, headers={"Authorization": f"Basic {TOKEN}"})[0] == 401, path
    code, body, _ = call(base + "/")
    assert code == 200 and b'name="token"' in body and b"Distributed Cyber-Resilience Platform</h1>" in body
    assert b'id="nodes"' not in body, "the real dashboard page must not be served without a token"
    assert call(base + "/healthz")[0] == 200                                  # liveness only, no data
    assert json.loads(call(base + "/healthz")[1]) == {"ok": True}


def test_bearer_token_gives_access_to_the_api_for_scripts(monkeypatch, serve):
    mod = load(monkeypatch, TOKEN)
    base = serve(mod)
    h = {"Authorization": f"Bearer {TOKEN}"}
    assert call(base + "/api/events", headers=h)[0] == 200
    assert call(base + "/api/metrics", headers=h)[0] == 200
    assert b'id="nodes"' in call(base + "/", headers=h)[1]


def test_browser_login_sets_an_httponly_session_cookie_that_is_not_the_token(monkeypatch, serve):
    mod = load(monkeypatch, TOKEN)
    base = serve(mod)
    code, body, _ = call(base + "/login", "POST", data=b"token=wrong")
    assert code == 401 and b"Wrong token" in body and TOKEN.encode() not in body
    code, _, hdr = call(base + "/login", "POST", data=f"token={TOKEN}".encode())
    assert code == 303
    cookie = hdr["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and TOKEN not in cookie
    sid = cookie.split(";")[0]
    assert call(base + "/api/events", headers={"Cookie": sid})[0] == 200
    assert b'id="nodes"' in call(base + "/", headers={"Cookie": sid})[1]
    assert call(base + "/api/events", headers={"Cookie": "cr_session=forged"})[0] == 401
    call(base + "/logout", headers={"Cookie": sid})
    assert call(base + "/api/events", headers={"Cookie": sid})[0] == 401, "logout ends the session"


def test_repeated_wrong_tokens_are_rate_limited(monkeypatch, serve):
    mod = load(monkeypatch, TOKEN)
    base = serve(mod)
    for _ in range(5):
        assert call(base + "/login", "POST", data=b"token=nope")[0] == 401
    assert call(base + "/login", "POST", data=b"token=nope")[0] == 429
    assert call(base + "/login", "POST", data=f"token={TOKEN}".encode())[0] == 429, "even the right token waits"
    assert call(base + "/api/events", headers={"Authorization": f"Bearer {TOKEN}"})[0] == 200   # scripts unaffected


def test_token_can_come_from_a_mounted_secret_file(monkeypatch, serve, tmp_path):
    f = tmp_path / "token"
    f.write_text(TOKEN + "\n")
    mod = load(monkeypatch, token_file=f)
    assert mod.AUTH.enabled
    base = serve(mod)
    assert call(base + "/api/events")[0] == 401
    assert call(base + "/api/events", headers={"Authorization": f"Bearer {TOKEN}"})[0] == 200
    # a misconfigured secret mount must FAIL CLOSED, never silently open the dashboard
    with pytest.raises(RuntimeError, match="refusing to start"):
        load(monkeypatch, token_file=tmp_path / "missing")
    empty = tmp_path / "empty"
    empty.write_text("\n")
    with pytest.raises(RuntimeError, match="empty"):
        load(monkeypatch, token_file=empty)


def test_the_token_is_never_hardcoded_or_logged():
    src = open(os.path.join(ROOT, "dashboard", "server.py")).read()
    assert "DASHBOARD_TOKEN" in src and TOKEN not in src
    for line in src.splitlines():
        if "print(" in line or "log." in line:
            assert "_token" not in line and "TOKEN" not in line.replace("access token:", "")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = dict(os.environ, PORT=str(port), DASHBOARD_TOKEN=TOKEN,
               STATUS_URLS=",".join(["http://127.0.0.1:1/status"] * 4))
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "dashboard", "server.py")], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        call(f"http://127.0.0.1:{port}/api/events")
        call(f"http://127.0.0.1:{port}/login", "POST", data=b"token=wrong")
        call(f"http://127.0.0.1:{port}/login", "POST", data=f"token={TOKEN}".encode())
    finally:
        p.terminate()
        out, _ = p.communicate(timeout=5)
    assert TOKEN not in out and "REQUIRED" in out
