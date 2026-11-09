"""Auth checks also run without the sockets used by the full server selftest."""
import argparse
from email.message import Message
from http.client import parse_headers
import importlib.util
import io
from pathlib import Path
import tempfile

import pytest


@pytest.fixture
def local_temp(monkeypatch):
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
        monkeypatch.setattr(tempfile, "tempdir", directory)
        yield Path(directory)


@pytest.fixture
def server():
    spec = importlib.util.spec_from_file_location("oko_tail_auth", Path(__file__).with_name("oko-tail.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_auth_matrix(server, local_temp):
    server.selftest_auth()
    server.selftest_origin()


@pytest.mark.parametrize("supplied,expected", [(None, 401), ("wrong", 401), ("gate-test-only", 200)])
def test_proxy_secret_http_gate(server, local_temp, supplied, expected):
    secret = local_temp / "proxy-secret"
    secret.write_bytes(b"gate-test-only\n")
    auth = server.auth_config({"OKO_TAIL_AUTH_HEADER": "Remote-User", "OKO_TAIL_PROXY_SECRET_FILE": str(secret)})
    cls = server.handler(argparse.Namespace(), auth)
    request = cls.__new__(cls)  # drive the real HTTP gate without opening a listening socket
    request.headers = Message()
    request.headers["Host"] = "localhost"
    request.headers["Remote-User"] = "me"
    if supplied is not None:
        request.headers["x-oko-tail-proxy-secret"] = supplied
    request.client_address = ("127.0.0.1", 1234)
    codes = []
    request.send_response = codes.append
    request.send_header = lambda *args: None
    request.end_headers = lambda: None
    request.wfile = io.BytesIO()
    allowed = request.gate()
    assert (200 if allowed else codes[0]) == expected


@pytest.mark.parametrize("auth_env,inert", [
    ({"OKO_TAIL_AUTH_HEADER": "Remote-User"}, False),
    ({}, True),
    ({"OKO_TAIL_AUTH_HEADER": " \t"}, True),
    ({"OKO_TAIL_USER": "me", "OKO_TAIL_PASSWORD": "basic-test-only"}, True),
], ids=["forward", "no-auth", "blank-header", "basic-only"])
def test_check_proxy_secret_redacted(server, local_temp, monkeypatch, capsys, auth_env, inert):
    # Isolate --check from real credentials, databases and network calls.
    for name in list(server.os.environ):
        if name.startswith("OKO_TAIL_") or name == "API_SERVER_KEY":
            monkeypatch.delenv(name)
    monkeypatch.setattr(server, "db_check", lambda path: (True, "test database"))
    monkeypatch.setattr(server, "_api_key", lambda path: "")
    monkeypatch.setattr(server, "gateway_up", lambda *args, **kwargs: False)
    monkeypatch.setattr(server, "sock_paths", lambda home: [str(local_temp / "absent.sock")])
    monkeypatch.setattr(server, "progress_paths", lambda home: [str(local_temp / "absent-progress.sock")])
    monkeypatch.setattr(server, "HOST", "127.0.0.1")
    secret = local_temp / "proxy-secret"
    for name, value in auth_env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("OKO_TAIL_PROXY_SECRET_FILE", str(secret))
    args = argparse.Namespace(db="unused", env="unused", gateway="unused", session="s")
    for contents, expected, code in ((b"check-test-only\n", "set", 0), (b"", "missing", 1), (None, "missing", 1)):
        if contents is None:
            secret.unlink()
        else:
            secret.write_bytes(contents)
        assert server.check(args) == code
        output = capsys.readouterr().out
        row = next(line for line in output.splitlines() if "proxy secret" in line)
        assert expected in row
        assert row.startswith("FAIL" if code else "warn" if inert else "ok")
        assert ("inert" in row) == inert
        if inert:
            assert "OKO_TAIL_AUTH_HEADER" in row
        assert "check-test-only" not in output and str(secret) not in output
        assert "basic-test-only" not in output
    monkeypatch.delenv("OKO_TAIL_PROXY_SECRET_FILE")
    assert server.check(args) == 0
    row = next(line for line in capsys.readouterr().out.splitlines() if "proxy secret" in line)
    assert row.startswith("ok") and "off" in row
    assert "missing" not in row and "inert" not in row


@pytest.mark.parametrize("leading", [b" ", b"\t", b" \t"])
def test_proxy_secret_leading_whitespace_unsupported(server, local_temp, leading):
    # Documented constraint: the real HTTP parser strips leading SP/HTAB, not the file reader.
    secret = local_temp / "proxy-secret"
    secret.write_bytes(leading + b"whitespace-test-only\r\n")
    auth = server.auth_config({"OKO_TAIL_AUTH_HEADER": "Remote-User", "OKO_TAIL_PROXY_SECRET_FILE": str(secret)})
    headers = parse_headers(io.BytesIO(
        b"Remote-User: me\r\nX-Oko-Tail-Proxy-Secret: " + leading + b"whitespace-test-only\r\n\r\n"))
    assert headers["X-Oko-Tail-Proxy-Secret"] == "whitespace-test-only"
    assert server.proxy_secret(auth) == leading + b"whitespace-test-only"
    assert not server.authorized(auth, headers, "127.0.0.1")
    secret.write_bytes(b"whitespace-test-only\r\n")
    assert server.authorized(auth, headers, "127.0.0.1")
