"""Private HTTPS tunnels: connect only to resolved public IPs, keep TLS in Chromium."""

import select
import socket
import socketserver
import threading
from urllib.parse import urlsplit

from .errors import AssistantError


class PublicHTTPSProxy:
    """Public-IP transport with an optional HTTP upstream proxy."""

    def __init__(self, resolve, normalize, upstream=""):
        self.resolve, self.normalize = resolve, normalize
        proxy = urlsplit(upstream) if upstream else None
        self._upstream = (proxy.hostname, proxy.port) if proxy else None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._addresses, self._failures = {}, []
        self._allowed_hosts = set()
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
                "bypass": "<-loopback>"}

    def failures(self):
        with self._lock:
            return [dict(item) for item in self._failures[-12:]]

    def allow_host(self, host):
        """Register an approved page request before it reaches the transport."""
        host = self.normalize(host)
        with self._lock:
            self._allowed_hosts.add(host)

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
        last_error = None
        for address in addresses:
            if self._stop.is_set():
                raise OSError("浏览器连接层已关闭。")
            try:
                # Connect to the checked literal IP, never re-resolve the hostname.
                connection = self._open_connection(address)
                with self._lock:
                    self._failures = [item for item in self._failures if item["target"] != host]
                return connection
            except OSError as error:
                last_error = error
                continue
        raise OSError("已验证公网地址连接失败：" + str(last_error)) from last_error

    def _open_connection(self, address):
        if not self._upstream:
            return socket.create_connection((address, 443), timeout=5)
        connection = socket.create_connection(self._upstream, timeout=5)
        # Tunnel to the checked IP; Chromium still validates the site's TLS name.
        authority = f"[{address}]:443" if ":" in address else f"{address}:443"
        try:
            connection.sendall(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode("ascii"))
            response = bytearray()
            while not response.endswith(b"\r\n\r\n") and len(response) < 16384:
                chunk = connection.recv(1)
                if not chunk:
                    raise OSError("上游 HTTP 代理提前关闭连接。")
                response.extend(chunk)
            if not response.endswith(b"\r\n\r\n"):
                raise OSError("上游 HTTP 代理响应头过长。")
            status = response.split(b"\r\n", 1)[0].split()
            if len(status) < 2 or status[1] != b"200":
                raise OSError("上游 HTTP 代理拒绝建立 HTTPS 隧道。")
            return connection
        except OSError:
            connection.close()
            raise

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
            # This task-local public-IP relay has no account authority. Avoid
            # an HTTP auth challenge and Playwright's origin-wide credentials;
            # no auth coordination with the response interception is needed.
            method, authority, version = lines[0].split()
            raw_host, port = authority.rsplit(":", 1)
            if method != "CONNECT" or port != "443" or version not in {"HTTP/1.0", "HTTP/1.1"}:
                raise AssistantError("连接层仅允许 HTTPS 443 隧道。", 403)
            host = self.normalize(raw_host)
            with self._lock:
                allowed = host in self._allowed_hosts
            if not allowed:
                # Chromium can initiate background/preconnect traffic unrelated
                # to a routed page request. Do not resolve it or call it a task
                # failure. Actual requests register before continue/redirect.
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                return
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
