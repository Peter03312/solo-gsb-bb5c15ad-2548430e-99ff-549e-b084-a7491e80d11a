"""交接事件审计检索：快照一致性翻页与 HMAC 签名游标。

查询语义
--------
GET /audit/events 支持按 chain_id、holder、role（giver / receiver / either）、
created_from、created_to 过滤，跨链结果按 (created_at, 事件数据库 id) 升序返回。

- 创世事件（seq=0）没有 giver / receiver 字段：仅当 role=either 时按**首位
  持有人**（chains.first_holder）匹配；role=giver / receiver 时创世事件一律
  不入选（移交事件分别匹配 events.giver / events.receiver）。
- 首次查询（无游标）在**同一个读事务**内取「当时最大的事件数据库 id」作为
  快照上界 max_id，随后只在 id <= max_id 的不可变事件集合内翻页；翻页期间
  新追加的移交事件 id 均大于 max_id，永远不会插入到既有排序位置，因此不会
  造成翻页重复或漏项。
- 游标 next_cursor 携带「过滤条件摘要 + 快照上界 + 本页末条排序键」，并用
  AUDIT_CURSOR_SECRET 做 HMAC-SHA256 签名（域前缀 audit-cursor:v1:）。
  后续请求必须原样回传同一游标，且本次查询参数（除 limit 外）必须与游标内
  过滤条件摘要完全一致；游标被篡改、格式非法或过滤条件不符一律返回 400，
  **绝不回退为一次新的快照查询**。

时间过滤
--------
created_at 统一以「UTC、显式偏移、ISO-8601 文本」落库（无尾部分隔符差异），
故 created_from / created_to 解析为 datetime 后再格式化为同一规范 UTC 文本，
直接以字符串比较即可（字典序与时间序一致）；created_from 包含下界、
created_to 包含上界（闭区间）。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Optional

from . import config
from .database import read_tx
from .hashing import canonical_json
from .service import ChainError

# 游标载荷与签名的固定版本/域前缀，避免与其他签名用途相互混用。
_CURSOR_VERSION = 1
_CURSOR_DOMAIN = "audit-cursor:v1:"

# limit 在 API 层另有 1～100 约束；这里仅作为读取多一行所需的内部上限。
MAX_LIMIT = 100


def _parse_iso8601(value: str, field: str) -> datetime:
    """解析查询参数中的 ISO-8601 时间；朴素时间（无偏移）按 UTC 解释。"""
    text = value.strip()
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ChainError(
            400,
            "invalid_time_range",
            f"{field} 不是合法的 ISO-8601 时间（如 2026-09-01T00:00:00+00:00）: {text}",
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_ts(dt: datetime) -> str:
    """统一规范为显式 +00:00 偏移的 UTC 文本，便于字符串比较与游标一致性。"""
    return dt.isoformat()


def normalize_filters(
    chain_id: Optional[str],
    holder: Optional[str],
    role: Optional[str],
    created_from: Optional[str],
    created_to: Optional[str],
) -> dict[str, Optional[str]]:
    """规范化并交叉校验过滤条件，得到可签名/可比对的稳定字典。

    - chain_id / holder 去空白，空白视为未提供；
    - role 仅在 holder 非空时有意义（给了 role 却没给 holder -> 400）；
      holder 非空而 role 缺省时默认 either；
    - created_from / created_to 解析为规范 UTC 文本，from > to -> 400。
    """
    chain_id = chain_id.strip() if chain_id and chain_id.strip() else None
    holder = holder.strip() if holder and holder.strip() else None

    if role is not None and holder is None:
        raise ChainError(
            400,
            "role_requires_holder",
            "仅在按持有人 holder 过滤时 role 才有意义；请同时提供 holder，"
            "或省略 role",
        )
    if holder is not None and role is None:
        role = "either"

    ts_from = _parse_iso8601(created_from, "created_from") if created_from else None
    ts_to = _parse_iso8601(created_to, "created_to") if created_to else None
    if ts_from is not None and ts_to is not None and ts_from > ts_to:
        raise ChainError(
            400,
            "invalid_time_range",
            "created_from 不能晚于 created_to",
        )

    return {
        "chain_id": chain_id,
        "holder": holder,
        "role": role,
        "created_from": _format_ts(ts_from) if ts_from is not None else None,
        "created_to": _format_ts(ts_to) if ts_to is not None else None,
    }


def _filter_digest(filters: dict[str, Optional[str]]) -> str:
    """过滤条件摘要：规范化字典的键排序紧凑 JSON 的 SHA-256。

    游标只暴露摘要而非明文过滤条件；同一组过滤条件在任何语言/进程下得到
    相同摘要，不同过滤条件（含 None 与空串之别）摘要必然不同。
    """
    return hashlib.sha256(canonical_json(filters).encode("utf-8")).hexdigest()


def encode_cursor(
    filters: dict[str, Optional[str]],
    max_id: int,
    last_created_at: str,
    last_id: int,
) -> str:
    """生成下一页游标：过滤摘要 + 快照上界 + 末条排序键，HMAC 签名。

    载荷为 JSON {"v":1,"f":<过滤摘要>,"m":<上界id>,"t":<末条created_at>,
    "i":<末条id>}，UTF-8 后 URL 安全 base64（无填充）；签名为
    HMAC-SHA256(secret, "audit-cursor:v1:" + payload_b64) 的十六进制摘要。
    线上格式为 "<payload_b64>.<sig_hex>"。
    """
    payload = {
        "v": _CURSOR_VERSION,
        "f": _filter_digest(filters),
        "m": max_id,
        "t": last_created_at,
        "i": last_id,
    }
    raw = canonical_json(payload).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    sig = hmac.new(
        config.AUDIT_CURSOR_SECRET.encode("utf-8"),
        (_CURSOR_DOMAIN + payload_b64).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload_b64}.{sig}"


def decode_cursor(
    token: str,
    filters: dict[str, Optional[str]],
) -> tuple[int, str, int]:
    """校验并解码游标，返回 (快照上界 max_id, 末条 created_at, 末条 id)。

    任何篡改、格式错误、载荷缺字段/类型异常、版本不符、过滤摘要不一致都
    抛 400：签名不匹配 -> invalid_cursor_signature；过滤条件与本次请求不符
    -> cursor_filter_mismatch；其余 -> invalid_cursor。调用方绝不因此回退为
    新查询。
    """
    invalid = ChainError(400, "invalid_cursor", "分页游标格式非法")
    parts = token.split(".")
    if len(parts) != 2 or not parts[0] or len(parts[1]) != 64:
        raise invalid
    payload_b64, sig = parts

    expected = hmac.new(
        config.AUDIT_CURSOR_SECRET.encode("utf-8"),
        (_CURSOR_DOMAIN + payload_b64).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    # 先验签：未通过签名的载荷一律不解析，杜绝按篡改内容分支处理。
    if not hmac.compare_digest(expected, sig):
        raise ChainError(
            400,
            "invalid_cursor_signature",
            "分页游标签名无效（已损坏、被篡改或密钥已更换）",
        )

    try:
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise invalid from exc

    if not isinstance(payload, dict):
        raise invalid
    if payload.get("v") != _CURSOR_VERSION:
        raise ChainError(400, "invalid_cursor", "分页游标版本不受支持")

    max_id = payload.get("m")
    last_created_at = payload.get("t")
    last_id = payload.get("i")
    digest = payload.get("f")
    if (
        not isinstance(max_id, int)
        or isinstance(max_id, bool)
        or max_id < 0
        or not isinstance(last_id, int)
        or isinstance(last_id, bool)
        or last_id < 0
        or not isinstance(last_created_at, str)
        or not last_created_at
        or not isinstance(digest, str)
        or len(digest) != 64
    ):
        raise invalid

    if not hmac.compare_digest(digest, _filter_digest(filters)):
        raise ChainError(
            400,
            "cursor_filter_mismatch",
            "分页游标的过滤条件与本次请求不符：请使用首次查询的 chain_id/"
            "holder/role/created_from/created_to 原样翻页（仅 limit 可调整），"
            "或重新发起一次新的查询",
        )

    return max_id, last_created_at, last_id


def _build_query(
    filters: dict[str, Optional[str]],
    max_id: int,
    after: Optional[tuple[str, int]],
    limit: int,
) -> tuple[str, list[Any]]:
    """构造跨链审计检索 SQL 与参数。

    固定上界 id <= max_id（快照）；按 (created_at, id) 升序，after 给出严格
    大于的末条排序键（同 created_at 时以 id 去重，稳定且不重不漏）。
    role=either 且按持有人过滤时需要创世事件按 first_holder 匹配，故此时
    LEFT JOIN chains。
    """
    join_chains = filters["holder"] is not None and filters["role"] == "either"
    sql = ["SELECT e.* FROM events e"]
    if join_chains:
        sql.append("LEFT JOIN chains c ON c.chain_id = e.chain_id")
    sql.append("WHERE e.id <= ?")
    params: list[Any] = [max_id]

    if filters["chain_id"] is not None:
        sql.append("AND e.chain_id = ?")
        params.append(filters["chain_id"])

    holder = filters["holder"]
    role = filters["role"]
    if holder is not None:
        if role == "giver":
            # 创世事件 giver='' 且 seq=0，绝不会命中真实持有人。
            sql.append("AND e.giver = ?")
            params.append(holder)
        elif role == "receiver":
            sql.append("AND e.receiver = ?")
            params.append(holder)
        else:  # either：移交事件给出或接收，创世事件按首位持有人匹配。
            sql.append(
                "AND (e.giver = ? OR e.receiver = ?"
                " OR (e.seq = 0 AND c.first_holder = ?))"
            )
            params.extend([holder, holder, holder])

    if filters["created_from"] is not None:
        sql.append("AND e.created_at >= ?")
        params.append(filters["created_from"])
    if filters["created_to"] is not None:
        sql.append("AND e.created_at <= ?")
        params.append(filters["created_to"])

    if after is not None:
        # 键集翻页：严格位于上一页末条排序键之后。
        sql.append(
            "AND (e.created_at > ? OR (e.created_at = ? AND e.id > ?))"
        )
        params.extend([after[0], after[0], after[1]])

    sql.append("ORDER BY e.created_at ASC, e.id ASC")
    # 多取一行判断是否还有下一页。
    sql.append("LIMIT ?")
    params.append(limit + 1)
    return " ".join(sql), params


def _row_to_audit_event(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "chain_id": row["chain_id"],
        "seq": row["seq"],
        "giver": row["giver"],
        "receiver": row["receiver"],
        "nonce": row["nonce"],
        "prev_hash": row["prev_hash"],
        "event_hash": row["event_hash"],
        "created_at": row["created_at"],
    }


def search_audit_events(
    chain_id: Optional[str],
    holder: Optional[str],
    role: Optional[str],
    created_from: Optional[str],
    created_to: Optional[str],
    limit: int,
    cursor: Optional[str],
) -> dict[str, Any]:
    """跨链审计检索交接事件，返回一页结果与签名游标。

    无游标：同一读事务内确定快照上界（当前最大事件 id；空库取 0）后取首页。
    有游标：先规范化本次过滤条件，再验签并比对过滤摘要（任何不符 400，不回退
    为新查询），随后在游标锚定的同一快照、同一过滤条件下取下一页。
    无更多项时 next_cursor 为 null。
    """
    filters = normalize_filters(
        chain_id, holder, role, created_from, created_to
    )

    with read_tx() as conn:
        if cursor is None:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM events").fetchone()
            max_id = row["m"]
            after: Optional[tuple[str, int]] = None
        else:
            max_id, last_created_at, last_id = decode_cursor(cursor, filters)
            after = (last_created_at, last_id)

        query, params = _build_query(filters, max_id, after, limit)
        rows = conn.execute(query, params).fetchall()

    has_more = len(rows) > limit
    page = rows[:limit]
    events = [_row_to_audit_event(r) for r in page]

    next_cursor: Optional[str] = None
    if has_more and page:
        last = page[-1]
        next_cursor = encode_cursor(
            filters, max_id, last["created_at"], last["id"]
        )

    return {
        "events": events,
        "next_cursor": next_cursor,
    }
