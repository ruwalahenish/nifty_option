"""Shared test base: any attempt to reach the network during a test fails the test."""
import socket
import unittest
from unittest import mock


def _blocked(*args, **kwargs):
    raise AssertionError("network access attempted during a test (PLAN.md section 6, guard 13)")


class NoNetworkTestCase(unittest.TestCase):
    def setUp(self):
        super(NoNetworkTestCase, self).setUp()
        for target in ("socket.socket.connect", "socket.socket.connect_ex",
                       "socket.create_connection", "socket.getaddrinfo"):
            patcher = mock.patch(target, _blocked)
            patcher.start()
            self.addCleanup(patcher.stop)
