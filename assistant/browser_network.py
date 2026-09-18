"""Private HTTPS tunnels: connect only to resolved public IPs, keep TLS in Chromium."""

import base64
import hmac
import secrets
import select
import socket
import socketserver
import threading
import time

from .errors import AssistantError


class PublicHTTPSProxy:
    """Task-local transport, not an upstream/system proxy or a permission grant."""

    def __init__(self, resolve, normalize):
        self.resolve, self.normalize = resolve, normalize
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._addresses, self._failures = {}, []
        self.username, self.password = "browser", secrets.token_urlsafe(32)
        self._authorization = "Basic " + base64.b64encode(
            f"{self.username}:{self.password}".encode()).decode()
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                owner._handle(self.request)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            block_on_close = False

        self._server = Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.1}, daemon=True)
        self._thread.start()

    @property
    def settings(self):
        return {"server": f"http://127.0.0.1:{self._server.server_address[1]}",
                "username": self.username, "password": self.password,
                "bypass": "<-loopback>"}

    def failures(self):
        with self._lock:
            return [dict(item) for item in self._failures[-12:]]

    def clear_failures(self):
        with self._lock:
            self._failures.clear()

    def close(self):
        self._stop.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=1)

    def _connect(self, host):
        with self._lock:
            addresses = self._addresses.get(host)
        if addresses is None:
            addresses = self.resolve(host)
            with self._lock:
                addresses = self._addresses.setdefault(host, addresses)
        deadline = time.monotonic() + 5
        for address in addresses:
            if self._stop.is_set():
                raise OSError("浏览器连接层已关闭。")
            try:
                # Connect to the checked literal IP, never re-resolve the hostname.
                connection = socket.create_connection((address, 443), timeout=max(0.1, deadline - time.monotonic()))
                with self._lock:
                    self._failures = [item for item in self._failures if item["target"] != host]
                return connection
            except OSError:
                if time.monotonic() >= deadline:
                    break
        raise OSError("已验证公网地址连接失败或超时；根因未确定。")

    def _handle(self, client):
        upstream, host, established = None, "", False
        try:
            client.settimeout(5)
            header = b""
            while b"\r\n\r\n" not in header:
                chunk = client.recv(4096)
                if not chunk:
                    return
                header += chunk
                if len(header) > 16384:
                    return
            header, _, initial_data = header.partition(b"\r\n\r\n")
            lines = header.decode("ascii").split("\r\n")
            fields = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
            authorization = next((value.strip() for key, value in fields.items()
                                  if key.lower() == "proxy-authorization"), "")
            if not hmac.compare_digest(authorization, self._authorization):
                client.sendall(b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="browser"\r\nContent-Length: 0\r\n\r\n')
                return
            method, authority, version = lines[0].split()
            raw_host, port = authority.rsplit(":", 1)
            if method != "CONNECT" or port != "443" or version not in {"HTTP/1.0", "HTTP/1.1"}:
                raise AssistantError("连接层仅允许 HTTPS 443 隧道。", 403)
            host = self.normalize(raw_host)
            upstream = self._connect(host)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            established = True
            if initial_data:
                upstream.sendall(initial_data)
            # TLS and certificate validation remain end-to-end in Chromium.
            client.settimeout(5)
            upstream.settimeout(5)
            while not self._stop.is_set():
                readable, _, _ = select.select([client, upstream], [], [], 0.5)
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is client else client).sendall(data)
        except (AssistantError, OSError, ValueError, UnicodeError, TypeError, KeyError) as error:
            if not established and host:
                with self._lock:
                    self._failures.append({"target": host, "kind": "permission" if isinstance(error, AssistantError)
                                           and error.status == 403 else "network", "reason": str(error)[:300]})
                    self._failures = self._failures[-50:]
            if not established:
                try:
                    client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                except OSError:
                    pass
        finally:
            if upstream is not None:
                upstream.close()
