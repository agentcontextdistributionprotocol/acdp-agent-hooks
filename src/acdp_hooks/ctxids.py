"""Finding ACDP ``ctx_id``s in tool arguments and results.

Two rules, deliberately different:

- **Arguments** follow RFC-ACDP-0019 §4.4: walk the value in JCS order
  (object members by UTF-16 key order, array elements by index), take each
  string *value* that matches the whole ``ctx_id`` grammar, keep the first
  occurrence of each.
- **Results and free text** are scanned for embedded ``ctx_id``s, because a
  tool that fetched ACDP contexts usually returns them inside prose or a JSON
  blob. Those ids are inputs to everything the run does afterwards.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterable, Iterator
from typing import Any

MAX_DERIVED_FROM = 1000

_HOST = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"
_UUID4 = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_WHOLE = re.compile(rf"^acdp://{_HOST}/{_UUID4}$")
# An embedded match must not be the prefix of a longer host or id.
_EMBEDDED = re.compile(rf"(?<![A-Za-z0-9])acdp://{_HOST}/{_UUID4}(?![0-9A-Za-z-])")


def _utf16_key(key: str) -> list[int]:
    data = key.encode("utf-16-be")
    return [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 2)]


def is_ctx_id(value: object) -> bool:
    """True when ``value`` is a string matching the whole ``ctx_id`` grammar."""
    return isinstance(value, str) and len(value) <= 300 and _WHOLE.match(value) is not None


def _walk(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        if is_ctx_id(value):
            yield value
    elif isinstance(value, dict):
        for key in sorted(value, key=lambda k: _utf16_key(str(k))):
            yield from _walk(value[key])
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk(item)


def ctx_ids_in_arguments(arguments: Any) -> list[str]:
    """RFC-ACDP-0019 §4.4 extraction over a tool's arguments."""
    if isinstance(arguments, str):
        with contextlib.suppress(ValueError):
            arguments = json.loads(arguments)
    return dedupe(_walk(arguments))


def ctx_ids_in_text(value: Any) -> list[str]:
    """Every embedded ``ctx_id`` in ``value`` (any JSON-able value or text)."""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            value = str(value)
    return dedupe(m.group(0) for m in _EMBEDDED.finditer(value))


def dedupe(ids: Iterable[str], limit: int = MAX_DERIVED_FROM) -> list[str]:
    """First occurrence of each id, capped at ``limit``."""
    seen: dict[str, None] = {}
    for i in ids:
        if i not in seen:
            seen[i] = None
            if len(seen) >= limit:
                break
    return list(seen)
