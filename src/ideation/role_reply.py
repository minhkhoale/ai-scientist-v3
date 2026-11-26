"""Parsing for the final reply emitted by the pinned headless CLI."""
from __future__ import annotations

import json
import re

_FINAL_MESSAGE = '--- final message ---'
_JSON_FENCE = re.compile(r'```json\s*\n(.*?)\n```', re.DOTALL)


def parse_final_reply(log: str) -> dict:
    """Parse one bare JSON object or one whole-reply fenced JSON object."""
    if _FINAL_MESSAGE not in log:
        raise ValueError('headless log has no final message')
    reply = log.rsplit(_FINAL_MESSAGE, 1)[1].strip()
    fence = _JSON_FENCE.fullmatch(reply)
    if fence:
        reply = fence.group(1).strip()
    value = json.loads(reply)
    if not isinstance(value, dict):
        raise ValueError('role reply must be a JSON object')
    return value
