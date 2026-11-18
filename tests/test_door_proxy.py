"""Door proxy request buffering.

The boxlite door relay replays one complete HTTP request per short-lived
exec, so the host listener must buffer headers + body before forwarding.
This helper was referenced but never defined, which turned every boxlite
proxy request into a 502.
"""
import socket

import pytest

from neuralosd._cmd.door import _read_http_request


def _pair():
    return socket.socketpair()


def test_reads_headers_and_content_length_body():
    client, server = _pair()
    try:
        client.sendall(b"POST /v1/ask HTTP/1.1\r\nHost: x\r\n"
                       b"Content-Length: 5\r\n\r\n")
        client.sendall(b"hello")
        req = _read_http_request(server, timeout=2)
    finally:
        client.close()
        server.close()
    assert req == (b"POST /v1/ask HTTP/1.1\r\nHost: x\r\n"
                   b"Content-Length: 5\r\n\r\nhello")


def test_reads_a_body_that_arrived_with_the_headers():
    client, server = _pair()
    try:
        client.sendall(b"POST / HTTP/1.1\r\nContent-Length: 3\r\n\r\nabc")
        req = _read_http_request(server, timeout=2)
    finally:
        client.close()
        server.close()
    assert req.endswith(b"\r\n\r\nabc")


def test_no_content_length_means_no_body():
    client, server = _pair()
    try:
        client.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
        req = _read_http_request(server, timeout=2)
    finally:
        client.close()
        server.close()
    assert req == b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n"


def test_chunked_body_is_read_to_the_terminator():
    client, server = _pair()
    try:
        client.sendall(b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n")
        client.sendall(b"5\r\nhello\r\n0\r\n\r\n")
        req = _read_http_request(server, timeout=2)
    finally:
        client.close()
        server.close()
    assert req.endswith(b"5\r\nhello\r\n0\r\n\r\n")


def test_peer_close_returns_empty():
    client, server = _pair()
    client.close()
    try:
        assert _read_http_request(server, timeout=2) == b""
    finally:
        server.close()


def test_oversized_headers_are_refused():
    client, server = _pair()
    try:
        client.sendall(b"GET / HTTP/1.1\r\nX: " + b"a" * 200)
        with pytest.raises(ValueError, match="headers"):
            _read_http_request(server, limit=100, timeout=2)
    finally:
        client.close()
        server.close()


def test_oversized_body_is_refused():
    client, server = _pair()
    try:
        client.sendall(b"POST / HTTP/1.1\r\nContent-Length: 999999\r\n\r\n")
        with pytest.raises(ValueError, match="body"):
            _read_http_request(server, limit=100, timeout=2)
    finally:
        client.close()
        server.close()


def test_invalid_content_length_is_refused():
    client, server = _pair()
    try:
        client.sendall(b"POST / HTTP/1.1\r\nContent-Length: nope\r\n\r\n")
        with pytest.raises(ValueError, match="Content-Length"):
            _read_http_request(server, timeout=2)
    finally:
        client.close()
        server.close()
