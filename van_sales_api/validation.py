"""Pure input checks; no framework dependency, also used by unit tests."""
import hashlib
import json
import math
import uuid


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("الكمية والمبلغ يجب أن يكونا أكبر من صفر")
    return number


def request_identity(user, request_id, payload):
    canonical = str(uuid.UUID(str(request_id)))
    key = hashlib.sha256(f"{user}:{canonical}".encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return key, fingerprint


def clean_lines(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 200:
        raise ValueError("أضف من صنف واحد إلى 200 صنف")
    result = []
    for row in rows:
        code, uom = str(row.get("item_code") or "").strip(), str(row.get("uom") or "").strip()
        if not code or not uom:
            raise ValueError("الصنف ووحدة البيع مطلوبان")
        result.append({"item_code": code, "uom": uom, "qty": positive(row.get("qty"))})
    return result
