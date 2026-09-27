"""哈希规则：紧凑 JSON（键名排序、无空白）后做 SHA-256。"""
import hashlib
import json
from typing import Any, Mapping


def canonical_json(obj: Mapping[str, Any]) -> str:
    """按键名排序的紧凑 JSON：无空格、不转义非 ASCII、键有序。

    同一组键值对在任何语言/机器上得到完全一致的字节串，
    服务端以此作为哈希计算的唯一规范表示。
    """
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def digest_hex(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
