"""Shared runtime state, wired up once in main and used by web + DNS."""

import threading

from .zone import SnapshotHolder, ZONE


class Runtime:
    def __init__(self):
        self.store = None
        self.holder = SnapshotHolder()
        # serializes publish+swap so snapshot order matches commit order
        self.publish_lock = threading.Lock()
        self.zone = ZONE
        self.dns_query_host = "127.0.0.1"  # where the web UI sends real queries
        self.dns_query_port = 5353
        self.refresh_interval = 2.0  # seconds; DB re-sync safety net


runtime = Runtime()
