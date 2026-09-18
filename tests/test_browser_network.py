"""Synthetic transport regression checks; no sockets or real DNS are opened."""

import threading
import unittest
from unittest.mock import Mock, patch

from assistant.browser import normalize_domain, public_addresses
from assistant.browser_network import PublicHTTPSProxy


class BrowserNetworkTests(unittest.TestCase):
    def proxy(self, resolve):
        proxy = object.__new__(PublicHTTPSProxy)
        proxy.resolve, proxy.normalize = resolve, normalize_domain
        proxy._lock, proxy._stop = threading.Lock(), threading.Event()
        proxy._addresses, proxy._failures = {}, []
        proxy._authorization = "Basic synthetic"
        return proxy

    def test_tunnel_connects_to_checked_literal_ip_and_reuses_pinned_addresses(self):
        resolve = Mock(return_value=["8.8.8.8", "8.8.4.4"])
        proxy = self.proxy(resolve)
        with patch("assistant.browser_network.socket.create_connection") as connect:
            proxy._connect("cdn.example")
            proxy._connect("cdn.example")
        resolve.assert_called_once_with("cdn.example")
        self.assertEqual([call.args[0] for call in connect.call_args_list], [("8.8.8.8", 443)] * 2)

    def test_mixed_dns_answers_are_rejected_before_any_upstream_connection(self):
        proxy = self.proxy(public_addresses)
        client = Mock()
        client.recv.return_value = b"CONNECT cdn.example:443 HTTP/1.1\r\nProxy-Authorization: Basic synthetic\r\n\r\n"
        answers = [(2, 1, 6, "", (ip, 443)) for ip in ("8.8.8.8", "127.0.0.1")]
        with patch("assistant.browser.socket.getaddrinfo", return_value=answers), \
                patch("assistant.browser_network.socket.create_connection") as connect:
            proxy._handle(client)
        connect.assert_not_called()
        self.assertEqual(proxy.failures()[0]["kind"], "permission")
        self.assertIn(b"502", client.sendall.call_args.args[0])

    def test_unauthenticated_local_clients_cannot_use_the_tunnel(self):
        resolve = Mock()
        proxy = self.proxy(resolve)
        client = Mock()
        client.recv.return_value = b"CONNECT cdn.example:443 HTTP/1.1\r\n\r\n"
        proxy._handle(client)
        resolve.assert_not_called()
        self.assertIn(b"407", client.sendall.call_args.args[0])

    def test_connection_failure_can_try_another_checked_ip(self):
        proxy = self.proxy(Mock(return_value=["8.8.8.8", "8.8.4.4"]))
        with patch("assistant.browser_network.socket.create_connection", side_effect=[OSError("synthetic"), Mock()]) as connect:
            proxy._connect("cdn.example")
        self.assertEqual([call.args[0] for call in connect.call_args_list], [("8.8.8.8", 443), ("8.8.4.4", 443)])
