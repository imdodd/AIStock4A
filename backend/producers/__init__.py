"""三档 producer + tool-use loop。默认由 APP_PROFILE / PRODUCER 选档，
无 Key、断网、契约不过一律退到 rule，绝不中断批处理。"""
from __future__ import annotations

from producers.base import (DEFAULT_INBOX, Producer, atomic_write_json, fallback,
                            get_producer, inbox_path, read_json, register, scan_inbox)
from producers.discover import TOOLS, discover

__all__ = ["get_producer", "register", "Producer", "fallback",
           "atomic_write_json", "scan_inbox", "read_json", "inbox_path",
           "DEFAULT_INBOX", "discover", "TOOLS"]
