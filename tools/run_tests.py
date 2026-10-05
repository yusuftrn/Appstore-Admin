#!/usr/bin/env python3
"""Run the regression suite with outbound socket connections forbidden."""
from pathlib import Path
import socket
import sys
import unittest
from unittest import mock


def blocked(*args, **kwargs):
    raise AssertionError('Network access is forbidden in offline tests')


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    suite = unittest.defaultTestLoader.discover(str(root / 'tests'))
    with mock.patch.object(socket.socket, 'connect', blocked), mock.patch.object(socket, 'create_connection', blocked):
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
