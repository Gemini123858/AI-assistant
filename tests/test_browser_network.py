"""Synthetic transport regression checks; no sockets or real DNS are opened."""

import threading
import unittest
from unittest.mock import Mock, patch

from assistant.browser import normalize_domain, public_addresses
from assistant.browser_network import PublicHTTPSProxy
from assistant.config import Config
from assistant.errors import AssistantError


class BrowserNetworkTests(unittest.TestCase):
    def test_http_upstream_configuration(self):
        Config(browser_proxy="http://127.0.0.1:7890").validate()
        for value in ("socks5://127.0.0.1:7890", "http://127.0.0.1", "http://user:code@proxy.example:7890"):
            with self.subTest(value=value), self.assertRaises(AssistantError):
                Config(browser_proxy=value).validate()

    def proxy(self, resolve):
        proxy = object.__new__(PublicHTTPSProxy)
        proxy.resolve, proxy.normalize = resolve, normalize_domain
        proxy._lock, proxy._stop = threading.Lock(), threading.Event()
        proxy._addresses, proxy._failures = {}, []
        proxy._allowed_hosts = set()
        proxy._upstream = None
        return proxy

    def test_tunnel_connects_to_checked_literal_ip_and_reuses_pinned_addresses(self):
        resolve = Mock(return_value=["8.8.8.8", "8.8.4.4"])
        proxy = self.proxy(resolve)
        with patch("assistant.browser_network.socket.create_connection") as connect:
            proxy._connect("cdn.example")
            proxy._connect("cdn.example")
        resolve.assert_called_once_with("cdn.example")
        self.assertEqual([call.args[0] for call in connect.call_args_list], [("8.8.8.8", 443)] * 2)

    def test_private_only_dns_answers_are_rejected_before_any_upstream_connection(self):
        proxy = self.proxy(public_addresses)
        proxy.allow_host("cdn.example")
        client = Mock()
        client.recv.return_value = b"CONNECT cdn.example:443 HTTP/1.1\r\n\r\n"
        answers = [(2, 1, 6, "", (ip, 443)) for ip in ("10.0.0.1", "127.0.0.1")]
        with patch("assistant.browser.socket.getaddrinfo", return_value=answers), \
                patch("assistant.browser_network.socket.create_connection") as connect:
            proxy._handle(client)
        connect.assert_not_called()
        self.assertEqual(proxy.failures()[0]["kind"], "permission")
        self.assertIn(b"502", client.sendall.call_args.args[0])

    def test_connect_has_no_auth_challenge_or_origin_credentials(self):
        resolve = Mock()
        proxy = self.proxy(resolve)
        proxy.allow_host("cdn.example")
        client = Mock()
        client.recv.side_effect = [b"CONNECT cdn.example:443 HTTP/1.1\r\n\r\n", b""]
        upstream = Mock()
        with patch.object(proxy, "_connect", return_value=upstream) as connect, \
                patch("assistant.browser_network.select.select", return_value=([client], [], [])):
            proxy._handle(client)
        connect.assert_called_once_with("cdn.example")
        self.assertIn(b"200 Connection Established", client.sendall.call_args.args[0])
        upstream.close.assert_called_once()
        proxy._server = Mock(server_address=("127.0.0.1", 12345))
        self.assertEqual(set(proxy.settings), {"server", "bypass"})

    def test_unregistered_background_connect_does_not_resolve_or_pollute_task_errors(self):
        resolve = Mock()
        proxy = self.proxy(resolve)
        client = Mock()
        client.recv.return_value = b"CONNECT www.google.com:443 HTTP/1.1\r\n\r\n"
        with patch.object(proxy, "_connect") as connect:
            proxy._handle(client)
        resolve.assert_not_called()
        connect.assert_not_called()
        self.assertEqual(proxy.failures(), [])
        self.assertIn(b"502", client.sendall.call_args.args[0])

    def test_connection_failure_can_try_another_checked_ip(self):
        proxy = self.proxy(Mock(return_value=["8.8.8.8", "8.8.4.4"]))
        with patch("assistant.browser_network.socket.create_connection", side_effect=[OSError("synthetic"), Mock()]) as connect:
            proxy._connect("cdn.example")
        self.assertEqual([call.args[0] for call in connect.call_args_list], [("8.8.8.8", 443), ("8.8.4.4", 443)])
        self.assertEqual([call.kwargs["timeout"] for call in connect.call_args_list], [5, 5])

    def test_upstream_http_proxy_tunnels_to_checked_ip(self):
        proxy = self.proxy(Mock(return_value=["8.8.8.8"]))
        proxy._upstream = ("127.0.0.1", 7890)
        connection = Mock()
        connection.recv.side_effect = [bytes([b]) for b in b"HTTP/1.1 200 Connection Established\r\n\r\n"]
        with patch("assistant.browser_network.socket.create_connection", return_value=connection) as connect:
            self.assertIs(proxy._connect("cdn.example"), connection)
        connect.assert_called_once_with(("127.0.0.1", 7890), timeout=5)
        self.assertIn(b"CONNECT 8.8.8.8:443", connection.sendall.call_args.args[0])

    def test_upstream_rejection_closes_connection(self):
        proxy = self.proxy(Mock(return_value=["8.8.8.8"]))
        proxy._upstream = ("127.0.0.1", 7890)
        connection = Mock()
        connection.recv.side_effect = [bytes([b]) for b in b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n"]
        with patch("assistant.browser_network.socket.create_connection", return_value=connection):
            with self.assertRaises(OSError):
                proxy._connect("cdn.example")
        connection.close.assert_called_once()
