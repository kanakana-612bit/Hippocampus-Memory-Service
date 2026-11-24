from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone


class Conflict(ValueError):
    pass


class Denied(PermissionError):
    pass


class Invalid(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value).encode()).hexdigest()


def text_digest(value: str):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def identifier(kind, *parts):
    return kind + "_" + (digest(parts)[:32] if parts else uuid.uuid4().hex)


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Invalid("Duplicate JSON key")
            result[key] = value
        return result

    def constant(value):
        raise Invalid("Nonfinite JSON number")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise Invalid("Invalid UTF-8 JSON; input was not repaired") from exc
