"""保管负荷报表：基于只读事件快照重建持有人在时间窗内的持有负荷。

接口
----
GET /reports/custody-load?holder=<持有人>&start=<ISO-8601>&end=<ISO-8601>

时间语义
--------
- start / end **必须携带时区**（如 ``2026-09-01T00:00:00+08:00`` 或
  ``...Z``），服务端统一换算为 UTC；朴素时间、非法 ISO-8601、start >= end
  或 end 晚于查询时刻一律返回 400 invalid_time_range。
- 统计窗口为半开区间 **[start, end)**。

快照
----
在**同一个 SQLite 读事务**内先把「当时最大的事件数据库 id」固定为
snapshot_event_id（全库无事件时为 0），随后只读取 id <= 该上界的事件重建，
报表期间新追加的移交绝不进入本次结果。本模块只读，不写任何表、不推进链尾、
不改变交接流程。

重建规则（全库、逐链）
----------------------
对每条链，取其快照内全部事件，按 **(created_at, 事件数据库 id)** 排序后：

1. 创世事件（seq=0）令该链的持有人为 chains.first_holder；
2. 此后每条移交事件以 **receiver** 接续为新的持有人（自转交——receiver 与
   原持有人相同——持有人不变，不增加负荷）；
3. **同一时刻（同一 created_at）的多个事件先合并**：以该时刻最后一个（id
   最大的）事件后的持有人作为该时刻之后的状态，只比较「时刻前 → 时刻后」
   的净变化，时刻中间的瞬时状态不产生任何时长。同一秒内 A→B→C 的链，B 的
   持有区间为零时长，不计负荷；
4. 由此得到该人对每条链的一串半开持有区间 [t_i, t_j)（最后一段开放到
   无穷未来），先完整还原窗口之前的状态，再与 [start, end) 求交裁剪。

聚合
----
每条链在任一时刻至多处于一个持有区间（同刻合并保证不重复计数），把全部链
裁剪后的区间叠加成一条按时间分段的常数阶跃函数：

- peak_chain_count：窗口内同时持有链数的最大值（正峰值；从不持有则为 0）；
- peak_intervals：达到该正峰值的**全部合并连续区间**（相邻同峰值段合并，
  半开 [start, end)，UTC ISO-8601）；峰值为 0 时为空列表；
- total_chain_seconds：持有链数对时间（秒）的积分，即「链·秒」总量
  （整秒返回整数，窗口带亚秒精度时可为小数）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, Union

from .database import read_tx
from .service import ChainError


def _parse_report_time(value: str, field: str) -> datetime:
    """解析报表时间参数：必须是带时区的 ISO-8601，再统一换算为 UTC。"""
    text = value.strip()
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ChainError(
            400,
            "invalid_time_range",
            f"{field} 不是合法的 ISO-8601 时间（如 2026-09-01T00:00:00+08:00）: "
            f"{text}",
        ) from exc
    if dt.tzinfo is None:
        raise ChainError(
            400,
            "invalid_time_range",
            f"{field} 必须携带时区偏移量（如 +08:00 或 Z），朴素时间不被接受: {text}",
        )
    return dt.astimezone(timezone.utc)


def _format_dt(dt: datetime) -> str:
    """统一输出为显式 +00:00 偏移的 UTC ISO-8601 文本。"""
    return dt.astimezone(timezone.utc).isoformat()


def _delta_micros(earlier: datetime, later: datetime) -> int:
    """两个时刻之间的整微秒数（later >= earlier）。"""
    delta = later - earlier
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _micros_to_seconds(micros: int) -> Union[int, float]:
    """微秒整数转秒：整秒给 int（含 0），带亚秒给 float。"""
    if micros % 1_000_000 == 0:
        return micros // 1_000_000
    return micros / 1_000_000


def _chain_holder_segments(
    first_holder: str,
    events: list[tuple[datetime, int, str]],
    holder: str,
) -> list[tuple[datetime, Optional[datetime]]]:
    """把单条链的事件流重建为 holder 对该链的全部半开持有区间。

    events 已按 (created_at, id) 升序，每项为 (事件时刻, seq, receiver)；
    创世事件（seq=0）的持有人取自链条 first_holder 而非 receiver。同一时刻
    的事件先合并：只比较「该时刻前 / 该时刻后」的净持有人，时刻内部的瞬时
    交接不产生区间。末段若仍持有，右端为 None（开放）。
    """
    segments: list[tuple[datetime, Optional[datetime]]] = []
    current: Optional[str] = None  # 当前时刻之前的链上持有人；None = 链尚未创世
    seg_start: Optional[datetime] = None

    index = 0
    while index < len(events):
        when = events[index][0]
        # 折叠同一时刻（同一 created_at）的全部事件：id 序最后一个事件后的
        # receiver 即该时刻之后的持有人；若本刻包含创世，则从 first_holder 起。
        after: Optional[str] = None
        while index < len(events) and events[index][0] == when:
            _, seq, receiver = events[index]
            after = first_holder if seq == 0 else receiver
            index += 1

        if after == holder and current != holder:
            seg_start = when  # 本刻起持有
        elif after != holder and current == holder:
            # seg_start 必非空（current == holder 只可能由之前的开区间产生）
            segments.append((seg_start, when))  # type: ignore[arg-type]
            seg_start = None
        current = after

    if current == holder and seg_start is not None:
        segments.append((seg_start, None))  # 开放区间：持有至未来
    return segments


def _aggregate(
    window_start: datetime,
    window_end: datetime,
    intervals: list[tuple[datetime, datetime]],
) -> tuple[int, list[tuple[datetime, datetime]], int]:
    """把裁剪后的若干半开区间叠加为阶跃函数并统计。

    返回 (正峰值, 达到正峰值的全部合并连续区间, 持有链数对微秒的积分)。
    同一时刻的起止变化在同一分界点合并应用，绝不统计时刻内的瞬态。
    """
    deltas: dict[datetime, int] = {}
    for lo, hi in intervals:
        deltas[lo] = deltas.get(lo, 0) + 1
        deltas[hi] = deltas.get(hi, 0) - 1

    # 从窗口左边界（链数 0）起逐分界点展开为 (段起点, 段终点, 段内链数)：
    # count 描述 [prev, point) 段，到达 point 时合并应用该点的全部变化，
    # 同刻的起止变化相消，绝不统计时刻内的瞬态。
    count = 0
    segments: list[tuple[datetime, datetime, int]] = []
    prev = window_start
    for point in sorted(deltas):
        if point > prev:
            segments.append((prev, point, count))
        count += deltas[point]
        prev = point
    if prev < window_end:
        segments.append((prev, window_end, count))

    peak = max((c for _l, _r, c in segments), default=0)

    total_micros = 0
    for lo, hi, c in segments:
        if c:
            total_micros += c * _delta_micros(lo, hi)

    peak_runs: list[tuple[datetime, datetime]] = []
    if peak > 0:
        run_start: Optional[datetime] = None
        run_end: Optional[datetime] = None
        for lo, hi, c in segments:
            if c == peak:
                if run_start is None:
                    run_start, run_end = lo, hi
                elif lo == run_end:
                    run_end = hi  # 相邻同峰值段：合并为一个连续区间
                else:
                    peak_runs.append((run_start, run_end))  # type: ignore[arg-type]
                    run_start, run_end = lo, hi
            elif run_start is not None:
                peak_runs.append((run_start, run_end))  # type: ignore[arg-type]
                run_start = run_end = None
        if run_start is not None:
            peak_runs.append((run_start, run_end))  # type: ignore[arg-type]

    return peak, peak_runs, total_micros


def custody_load(holder: str, start_raw: str, end_raw: str) -> dict[str, Any]:
    """生成持有人 holder 在 [start, end) 内的保管负荷报表（只读）。

    时间参数必须带时区（内部换算 UTC）；start < end 且 end 不得晚于查询时刻，
    否则抛 400 invalid_time_range。在同一个 SQLite 读事务内固定
    snapshot_event_id（MAX(events.id)，空库为 0），仅用该快照内的事件重建：
    创世从 first_holder 起，移交以 receiver 接续，同刻变化合并后按
    (created_at, id) 逐链还原，先恢复窗口前状态再裁剪至 [start, end)。
    """
    start = _parse_report_time(start_raw, "start")
    end = _parse_report_time(end_raw, "end")
    if start >= end:
        raise ChainError(
            400,
            "invalid_time_range",
            f"start 必须严格早于 end（半开区间 [start, end)）：start={_format_dt(start)}，"
            f"end={_format_dt(end)}",
        )
    now = datetime.now(timezone.utc)
    if end > now:
        raise ChainError(
            400,
            "invalid_time_range",
            f"end 不能晚于查询时刻：end={_format_dt(end)}，"
            f"now={_format_dt(now)}",
        )

    with read_tx() as conn:
        # 先固定快照上界；随后的全部读取都只使用 id <= snapshot 的事件。
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS max_id FROM events"
        ).fetchone()
        snapshot_event_id = row["max_id"]

        chain_rows = conn.execute(
            "SELECT chain_id, first_holder FROM chains"
        ).fetchall()
        first_holders = {r["chain_id"]: r["first_holder"] for r in chain_rows}

        event_rows = conn.execute(
            """SELECT chain_id, seq, receiver, created_at
               FROM events
               WHERE id <= ?
               ORDER BY chain_id, created_at, id""",
            (snapshot_event_id,),
        ).fetchall()

    # 按链分组（SQL 已按 chain_id 有序），逐链重建持有人的持有区间并裁剪。
    clipped: list[tuple[datetime, datetime]] = []
    index = 0
    while index < len(event_rows):
        chain_id = event_rows[index]["chain_id"]
        chain_events: list[tuple[datetime, int, str]] = []
        while index < len(event_rows) and event_rows[index]["chain_id"] == chain_id:
            r = event_rows[index]
            chain_events.append(
                (
                    datetime.fromisoformat(r["created_at"]),
                    r["seq"],
                    r["receiver"],
                )
            )
            index += 1

        for seg_start, seg_end in _chain_holder_segments(
            first_holders.get(chain_id, ""), chain_events, holder
        ):
            lo = seg_start if seg_start > start else start
            hi = end if seg_end is None else (seg_end if seg_end < end else end)
            if lo < hi:  # 与窗口有正长度交集才计入
                clipped.append((lo, hi))

    peak, peak_runs, total_micros = _aggregate(start, end, clipped)

    return {
        "holder": holder,
        "start": _format_dt(start),
        "end": _format_dt(end),
        "snapshot_event_id": snapshot_event_id,
        "peak_chain_count": peak,
        "total_chain_seconds": _micros_to_seconds(total_micros),
        "peak_intervals": [
            {"start": _format_dt(lo), "end": _format_dt(hi)} for lo, hi in peak_runs
        ],
    }
