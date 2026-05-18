"""Wire-format constants shared by the Telegram in/out adapters."""

from __future__ import annotations

from typing import Final

ID_MARKER_PREFIX: Final[str] = "#id:"
"""Marker placed at the end of every outbound bot message so inbound
replies can be correlated back to the originating decision via
``reply_to_message``.
"""
