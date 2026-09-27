"""交接链核心业务逻辑。

哈希规则
--------
创世事件（seq=0）：
    canonical = sort_keys_json({"file_hash": ..., "holder": ..., "seq": 0})
    event_hash = SHA256(canonical)
移交事件（seq>=1）：
    canonical = sort_keys_json({
        "giver": 交出方, "nonce": 一次性随机数, "prev_hash": 前序哈希,
        "receiver": 接收方, "seq": 序号,
    })
    event_hash = SHA256(canonical)

交出方由服务端依据链尾持有人（current_holder）确定，客户端不能伪造。

批量移交（POST /handover-batches）
---------------------------------
把同一批文件对应的 2～20 条交接链一次移交给同一接收方：请求携带 receiver 与
每条链的 chain_id / tail_hash / nonce（同批 chain_id 不得重复，由请求模型
保证）。服务端在**同一个** SQLite 立即事务内按请求顺序逐链完成与单链移交
完全一致的「存在性 → 封存门禁 → 链尾匹配 → 随机数查重 → 追加事件 → 推进
链尾」，全部成功才整体提交；任一链不存在（404 not_found）、处于争议封存
生效期（409 chain_on_hold）、链尾不匹配（409 tail_mismatch）或随机数
重放（409 nonce_replay）时整批回滚——**任何链都不新增事件**。成功响应按
请求顺序返回每条链的新序号与新链尾。

待接收移交（邀约）流程
----------------------
1. 持有人以最新 tail_hash 发起邀约（指定 receiver、有效期、一次性确认码，
   可选一次性撤销码），服务端只保存确认码/撤销码摘要并返回 offer_id；
2. 接收方凭 offer_id + 确认码确认：在同一 SQLite 立即事务中校验邀约
   未过期、未使用且链尾仍匹配，然后追加唯一移交事件并将邀约置为 accepted；
3. 发起方也可凭 offer_id + 撤销码撤销（POST /offers/{offer_id}/cancel）：
   在同一 SQLite 立即事务中校验邀约仍 pending、未过期、无拒绝、链尾未推进，
   然后将邀约置为 cancelled，不追加任何链上事件。撤销与接收方确认、见证人
   表态互斥，竞争中最多只有一个终态成功；
4. 发起方也可凭 offer_id + 原撤销码原子替换邀约
   （POST /offers/{offer_id}/replace）：在同一 SQLite 立即事务中校验原邀约
   仍 pending、未过期、无拒绝、链尾未推进，然后把原邀约置为 replaced 并创建
   锁定同一链尾的新邀约（全新的接收方、有效期、确认码、可选撤销码、可选见证
   配置）。替换与确认、撤销、见证人表态互斥，竞争中至多一个终态成功，失败整体
   回滚，绝不产生半替换；
5. 过期、重复确认、链尾已推进、已撤销或已被替换均返回 409 且不写事件。

争议封存（chain seal）
----------------------
持有人可对存在争议的交接链申请封存（POST /chains/{chain_id}/seals）：
携带创建时的当前链尾 tail_hash、争议原因 reason、有效期 expires_in_seconds
与一次性解封码 release_code。
- 解封码只保存「seal:<seal_id>:release-code:<code>」的 SHA-256 摘要，
  摘要域绑定 seal_id 且与确认码/撤销码/见证令牌分属不同域，明文绝不落库；
  任何查询响应都不返回该摘要。
- 同一链同时只能有一个生效封存：部分唯一索引 uq_active_seal_per_chain 与
  BEGIN IMMEDIATE 立即事务共同保证，重复封存返回 409 chain_seal_active。
- 封存生效期间（存储态 active 且未过期），直接移交（POST /handovers）以及
  邀约的创建（POST /offers）、替换（POST /offers/{id}/replace）、确认
  （POST /offers/{id}/confirm）都在各自现有的 SQLite 立即事务内最前置校验
  封存并返回 409 chain_on_hold，且绝不写入事件或新邀约；封存创建与这些
  操作共用同一把写锁与同一立即事务语义，并发时只有一方成功。
- 封存行不写任何哈希链事件，不影响链条可验证性。
- 提前解封（POST /seals/{seal_id}/release）：凭一次性解封码在立即事务内把
  封存置为 released；解封码错误返回 403 release_code_mismatch；封存已解封
  返回 409 seal_already_released；已过期（派生）返回 409 seal_expired。
- 过期自动失效：封存有效期由创建时固定的 expires_at 决定（创建接口在取得
  写锁后取同一个带微秒精度的 UTC 时刻，用于已有封存判定、创建与到期计算，
  有效期自该时刻完整起算），过期不需任何写操作——读写事务按当前时间实时
  派生 expired；创建新封存时，同链已过期的 active 旧封存在同一事务内物化
  为 expired，随后再插入新封存。
- 查询（GET /seals/{seal_id} 与 GET /chains/{chain_id}/seal）返回
  active / released / expired 三种对外状态、原因与创建/到期/解封时间信息，
  绝不暴露解封码摘要。

撤销码规则
----------
- 创建邀约时 cancellation_code 可选：省略则邀约不可撤销（与旧邀约一致）；
- 服务端只保存「offer:<offer_id>:cancel-code:<code>」的 SHA-256 摘要，
  摘要域绑定 offer_id 且与确认码、见证令牌分属不同域，明文绝不落库；
- 凭码撤销：撤销码错误返回 403 cancel_code_mismatch；邀约已 accepted、
  rejected（派生）、expired（派生）、terminated（派生）、cancelled、
  replaced 或根本没有撤销码时返回 409 offer_not_cancellable，且不改写哈希链。

原子替换规则
------------
- 仅当邀约创建时提供过 cancellation_code 时才可替换（与可撤销条件一致）；
  发起方凭原撤销码调用 POST /offers/{offer_id}/replace，提交新的 receiver、
  expires_in_seconds、confirmation_code，以及可选的新 cancellation_code 与
  全新见证配置（新邀约不复用旧码、旧见证令牌）；
- 服务端在同一个 SQLite 立即事务内依次校验：邀约仍 pending → 未过期 →
  锁定链尾仍是当前链尾 → 无见证人拒绝 → 原撤销码摘要匹配，随后先插入
  锁定同一链尾的新邀约（replaces 指向旧邀约），再把旧邀约置为 replaced
  （replaced_by 指向新邀约），任一校验失败或影响行数异常均整体回滚；
- 替换与确认、撤销、见证人表态共用同一把写锁与立即事务语义，竞争终态
  （accepted / cancelled / replaced / rejected …）中至多一个成功；
- 原撤销码错误返回 403 cancel_code_mismatch；任何非可替换状态（accepted /
  cancelled / replaced / expired / terminated / rejected，或创建时未提供
  撤销码的旧邀约）返回 409 offer_not_replaceable。
- 详情接口对旧邀约返回 replaced_by、对新邀约返回 replaces，均不返回任何
  凭证（确认码/撤销码/令牌）摘要。

见证确认模式（可选）
--------------------
发起邀约时可提交 2～5 名见证人及通过阈值 threshold：
1. 每位见证人持有一个互相独立的一次性令牌；服务端只保存
   「offer:<offer_id>:witness-token:<token>」的 SHA-256 摘要，明文不落库；
2. 见证人凭令牌调用表态接口同意（approved）或拒绝（rejected），
   令牌命中即作废——同一见证人至多表态一次，重复使用令牌返回 409；
3. 任一拒绝都会使邀约进入 rejected 终止态，后续确认/表态一律 409，
   且不追加任何链上事件；
4. 接收方确认时，在同一个 SQLite 立即事务内依次校验：确认码摘要 →
   邀约未过期 → 邀约未使用 → 链尾仍匹配 → 无拒绝且同意数达到阈值，
   全部通过才追加移交事件。拒绝（offer_rejected）、见证数不足
   （witness_threshold_not_met）、过期、链尾推进均为 409 且不写事件；
5. 无见证邀约（不提交见证人）行为与上述普通邀约完全一致，向后兼容。

可选择披露证明（Merkle 包含证明）
--------------------------------
- GET /chains/{chain_id}/events/{seq}/proof：在**同一个读事务**内取齐该链
  当前全部事件与当前链尾（同一快照），以各事件 event_hash 为叶构造 Merkle 树。
  叶子绑定 chain_id / seq / event_hash（merkle.leaf_hash，域前缀
  merkle-leaf:v1:）；内部节点用固定域分隔前缀 merkle-node:v1: 拼接左右定长
  十六进制子摘要，奇数层末节点与自身复制配对；snapshot_digest 按 chain_id /
  event_count / merkle_root / tail_hash 的规范 JSON 计算（域前缀
  merkle-snapshot:v1:）。链条不存在 404 not_found；seq 越界（含负数）404
  event_not_found。
- POST /proofs/verify：**只依据请求体重算**叶子、兄弟路径与 snapshot_digest，
  不读取数据库；请求体与证明响应同构，可原样回填。首个失败阶段按
  event_count -> seq -> path -> merkle_root -> snapshot_digest 顺序返回。
- GET /chains/{chain_id}/events/proof?seq=...：批量版本。对去重且升序的
  1～20 个序号生成一份 Merkle multiproof：同一读事务内取齐事件与链尾，
  响应返回被选事件（按 seq 升序）、event_count、tail_hash、merkle_root、
  snapshot_digest，以及按 (level, index) 升序且不重复的最小 proof_nodes
  （level 0 为叶子层；奇数层末节点自复制配对，不产生证明节点）。去重后
  超过 20 个序号返回 400 too_many_seqs；任一序号越界返回 404
  event_not_found。响应只含被选事件，不泄露未选择事件。
- POST /proofs/verify-batch：批量证明的离线校验，**只依据请求体**按节点
  坐标重建根，不读取数据库；请求体与批量证明响应同构，可原样回填（events
  中仅 seq、event_hash 参与重算）。首个失败阶段按 event_count -> events ->
  proof_nodes -> merkle_root -> snapshot_digest 顺序返回；重复或越界序号、
  缺失或多余节点、坐标冲突、不符合奇数层自复制规则的证明一律拒绝。

交接事件审计检索（GET /audit/events）
-------------------------------------
跨链审计检索的实现位于 audit.py：过滤条件为 chain_id / holder / role
（giver / receiver / either）/ created_from / created_to，跨链按
(created_at, 事件数据库 id) 升序返回。创世事件（seq=0）无 giver /
receiver，仅在 role=either 时按 chains.first_holder 匹配持有人。首次查询在
同一读事务内把当时 MAX(events.id) 固定为快照上界，签名游标携带过滤条件摘要、
上界与末条排序键（HMAC-SHA256，密钥 AUDIT_CURSOR_SECRET），保证翻页期间新
追加的移交不进入旧快照、不重不漏；游标损坏或过滤条件不符一律 400 且不回退
为新查询。

交付清单核对（POST /inventories/reconcile）
-------------------------------------------
收货方按交付清单批量核对 2～100 条链（同批 chain_id 不得重复，由请求模型
保证，重复或条数越界返回 422）。服务端在**同一个 SQLite 读事务**内先把
「当时最大的事件数据库 id」固定为快照锚点 snapshot_event_id（全库无事件时
为 0），再按请求顺序逐链读取链头（chains 行）并与清单声明的四项比对：
链不存在 -> missing；文件摘要 file_hash 不符 -> file_mismatch；当前持有人
current_holder 不符 -> holder_mismatch；链尾 tail_hash 不符 ->
tail_mismatch；四项全部一致 -> matched。逐项状态按上述顺序取首个不符项，
结果严格保持输入顺序，matched_count 统计 matched 条数。本接口**只读**：
不追加任何事件、不推进任何链尾、不写任何表。
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .database import read_tx, write_tx
from .hashing import canonical_json, digest_hex
from . import merkle


class ChainError(Exception):
    """业务错误：携带 HTTP 状态码。"""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _code_digest(code: str) -> str:
    """一次性确认码的存储摘要：带域分隔的 SHA-256，绝不落库明文。"""
    return digest_hex(f"offer-code:{code}")


def _witness_token_digest(offer_id: str, token: str) -> str:
    """见证人一次性令牌的存储摘要。

    摘要域中混入 offer_id：同一令牌字符串在不同邀约间不可复用，
    且与确认码摘要分属不同域。绝不落库明文。
    """
    return digest_hex(f"offer:{offer_id}:witness-token:{token}")


def _cancel_code_digest(offer_id: str, code: str) -> str:
    """一次性撤销码的存储摘要。

    摘要域绑定 offer_id（同一撤销码字符串在不同邀约间不可复用），且与
    确认码摘要（offer-code:）、见证令牌摘要（:witness-token:）分属不同域。
    绝不落库明文。
    """
    return digest_hex(f"offer:{offer_id}:cancel-code:{code}")


def _release_code_digest(seal_id: str, code: str) -> str:
    """封存一次性解封码的存储摘要。

    摘要域绑定 seal_id（同一解封码字符串在不同封存间不可复用），且与确认码
    （offer-code:）、见证令牌（:witness-token:）、撤销码（:cancel-code:）
    分属不同域。绝不落库明文。
    """
    return digest_hex(f"seal:{seal_id}:release-code:{code}")


def _genesis_payload(file_hash: str, holder: str) -> dict[str, Any]:
    return {"file_hash": file_hash, "holder": holder, "seq": 0}


def _handover_payload(
    seq: int, prev_hash: str, giver: str, receiver: str, nonce: str
) -> dict[str, Any]:
    # 键名顺序无所谓 —— canonical_json 会 sort_keys；显式写出仅为可读性。
    return {
        "giver": giver,
        "nonce": nonce,
        "prev_hash": prev_hash,
        "receiver": receiver,
        "seq": seq,
    }


def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "seq": row["seq"],
        "prev_hash": row["prev_hash"],
        "giver": row["giver"],
        "receiver": row["receiver"],
        "nonce": row["nonce"],
        "event_hash": row["event_hash"],
        "canonical": row["canonical"],
        "created_at": row["created_at"],
    }


def create_chain(file_hash: str, first_holder: str) -> dict[str, Any]:
    """创建交接链：写入创世记录（序号 0）。原子完成。"""
    chain_id = uuid.uuid4().hex
    created_at = _now()

    payload = _genesis_payload(file_hash, first_holder)
    canonical = canonical_json(payload)
    genesis_hash = digest_hex(canonical)

    try:
        with write_tx() as conn:
            conn.execute(
                """INSERT INTO chains
                   (chain_id, file_hash, first_holder, tail_hash,
                    current_holder, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (chain_id, file_hash, first_holder, genesis_hash,
                 first_holder, created_at),
            )
            conn.execute(
                """INSERT INTO events
                   (chain_id, seq, prev_hash, giver, receiver, nonce,
                    event_hash, canonical, created_at)
                   VALUES (?, 0, '', '', '', '', ?, ?, ?)""",
                (chain_id, genesis_hash, canonical, created_at),
            )
    except sqlite3.IntegrityError as exc:  # 理论上 UUID 冲突才会走到这里
        raise ChainError(409, "conflict", f"链条创建冲突: {exc}") from exc

    return {
        "chain_id": chain_id,
        "file_hash": file_hash,
        "first_holder": first_holder,
        "tail_hash": genesis_hash,
        "current_holder": first_holder,
        "length": 1,
        "created_at": created_at,
        "events": [
            {
                "seq": 0,
                "prev_hash": "",
                "giver": "",
                "receiver": "",
                "nonce": "",
                "event_hash": genesis_hash,
                "canonical": canonical,
                "created_at": created_at,
            }
        ],
    }


def _append_handover_tx(
    conn: sqlite3.Connection, chain: sqlite3.Row, receiver: str, nonce: str
) -> dict[str, Any]:
    """在已开启的写事务内向链尾追加一次移交事件并推进链头。

    调用方必须已完成链尾匹配等前置校验；本函数负责算哈希、插事件、
    更新链头，与调用方的校验同处一个事务，整体原子提交或回滚。
    """
    chain_id = chain["chain_id"]
    giver = chain["current_holder"]
    last_event = conn.execute(
        "SELECT seq FROM events WHERE chain_id = ? ORDER BY seq DESC LIMIT 1",
        (chain_id,),
    ).fetchone()
    seq = last_event["seq"] + 1

    payload = _handover_payload(
        seq=seq,
        prev_hash=chain["tail_hash"],
        giver=giver,
        receiver=receiver,
        nonce=nonce,
    )
    canonical = canonical_json(payload)
    new_hash = digest_hex(canonical)
    created_at = _now()

    try:
        conn.execute(
            """INSERT INTO events
               (chain_id, seq, prev_hash, giver, receiver, nonce,
                event_hash, canonical, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (chain_id, seq, chain["tail_hash"], giver, receiver, nonce,
             new_hash, canonical, created_at),
        )
    except sqlite3.IntegrityError as exc:
        msg = str(exc)
        if "nonce" in msg:
            raise ChainError(
                409, "nonce_replay", "随机数重放被拒绝（并发请求）"
            ) from exc
        raise ChainError(409, "conflict", f"追加冲突: {msg}") from exc

    conn.execute(
        """UPDATE chains
           SET tail_hash = ?, current_holder = ?
           WHERE chain_id = ?""",
        (new_hash, receiver, chain_id),
    )

    return {
        "chain_id": chain_id,
        "seq": seq,
        "prev_hash": chain["tail_hash"],
        "giver": giver,
        "receiver": receiver,
        "nonce": nonce,
        "event_hash": new_hash,
        "canonical": canonical,
        "tail_hash": new_hash,
        "current_holder": receiver,
    }


def append_handover(
    chain_id: str, tail_hash: str, receiver: str, nonce: str
) -> dict[str, Any]:
    """追加一次移交。

    冲突（409）情形：链条不存在 -> 404；链尾哈希不匹配（过期/竞争请求）；
    随机数在该链内重放；链条处于争议封存生效期（chain_on_hold）。
    任何冲突都不会写入数据，链条不会分叉。
    """
    with write_tx() as conn:
        chain = conn.execute(
            "SELECT * FROM chains WHERE chain_id = ?", (chain_id,)
        ).fetchone()
        if chain is None:
            raise ChainError(404, "not_found", f"链条不存在: {chain_id}")

        # 封存门禁：最前置校验。封存生效期间直接移交一律 409 chain_on_hold，
        # 与封存创建共用立即事务，竞争时只有一方成功，绝不写入事件。
        _raise_if_chain_on_hold(conn, chain_id)

        if chain["tail_hash"] != tail_hash:
            raise ChainError(
                409,
                "tail_mismatch",
                "链尾哈希不匹配：链条已被其他移交推进，请获取最新链尾后重试",
            )

        # UNIQUE(chain_id, nonce) 是最终防线；这里先给出明确错误信息。
        dup = conn.execute(
            "SELECT seq FROM events WHERE chain_id = ? AND nonce = ? AND seq > 0",
            (chain_id, nonce),
        ).fetchone()
        if dup is not None:
            raise ChainError(
                409,
                "nonce_replay",
                f"随机数已被使用（重放被拒绝），首次出现于序号 {dup['seq']}",
            )

        result = _append_handover_tx(conn, chain, receiver, nonce)

    return result


def append_handover_batch(
    receiver: str, items: list[dict[str, str]]
) -> dict[str, Any]:
    """把同一批 2～20 条交接链一次性移交给同一接收方（整批原子）。

    与单链移交（append_handover）复用同一套校验与哈希规则：所有链的「存在性
    -> 封存门禁 -> 链尾匹配 -> 随机数查重 -> 追加事件 -> 推进链尾」都在
    **同一个** SQLite 立即事务内按请求顺序完成。任一链不存在（404）、处于
    争议封存生效期（409 chain_on_hold）、链尾不匹配（409 tail_mismatch）或
    随机数重放（409 nonce_replay）时抛出 ChainError，整个事务回滚——**任何
    链都不新增事件**，整批失败；全部链校验并追加成功才整体提交。同批
    chain_id 互不重复（由请求模型保证），各链在本事务内互不干扰，每条链都
    沿用 _append_handover_tx 的哈希计算与唯一约束兜底。
    """
    results: list[dict[str, Any]] = []
    with write_tx() as conn:
        for item in items:
            chain_id = item["chain_id"]
            chain = conn.execute(
                "SELECT * FROM chains WHERE chain_id = ?", (chain_id,)
            ).fetchone()
            if chain is None:
                raise ChainError(404, "not_found", f"链条不存在: {chain_id}")

            # 封存门禁：与单链移交一致，最前置校验；任一链处于封存生效期整批失败。
            _raise_if_chain_on_hold(conn, chain_id)

            if chain["tail_hash"] != item["tail_hash"]:
                raise ChainError(
                    409,
                    "tail_mismatch",
                    f"链 {chain_id} 的链尾哈希不匹配：链条已被其他移交推进，"
                    "整批未生效，请获取最新链尾后重试",
                )

            # UNIQUE(chain_id, nonce) 是最终防线；这里先给出明确错误信息。
            dup = conn.execute(
                "SELECT seq FROM events WHERE chain_id = ? AND nonce = ? AND seq > 0",
                (chain_id, item["nonce"]),
            ).fetchone()
            if dup is not None:
                raise ChainError(
                    409,
                    "nonce_replay",
                    f"链 {chain_id} 的随机数已被使用（重放被拒绝，首次出现于序号 "
                    f"{dup['seq']}），整批未生效",
                )

            result = _append_handover_tx(conn, chain, receiver, item["nonce"])
            results.append(
                {
                    "chain_id": chain_id,
                    "seq": result["seq"],
                    "tail_hash": result["tail_hash"],
                }
            )

    return {"receiver": receiver, "count": len(results), "results": results}


def _load_witnesses(
    conn: sqlite3.Connection, offer_id: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM offer_witnesses WHERE offer_id = ? ORDER BY witness_index",
        (offer_id,),
    ).fetchall()


def _witness_summary(
    witness_rows: list[sqlite3.Row],
) -> tuple[int, int, int, list[dict[str, Any]]]:
    """统计见证人表态，并构造对外可见的状态列表（不含令牌摘要）。"""
    approved = rejected = pending = 0
    states: list[dict[str, Any]] = []
    for row in witness_rows:
        decision = row["decision"]
        if decision == "approved":
            approved += 1
        elif decision == "rejected":
            rejected += 1
        else:
            pending += 1
        states.append(
            {
                "witness_index": row["witness_index"],
                "name": row["name"],
                "decision": decision,
                "decided_at": row["decided_at"],
            }
        )
    return approved, rejected, pending, states


def _derived_offer_status(
    offer: sqlite3.Row,
    witness_rows: list[sqlite3.Row],
    current_tail_hash: Optional[str],
    now: Optional[datetime] = None,
) -> str:
    """邀约对外状态。

    存储状态为 pending / accepted / cancelled / replaced；其余均为派生状态：
    - rejected：pending 且见证模式下已有见证人拒绝（一旦拒绝不可恢复）；
    - expired：pending 且当前时间已过有效期；
    - terminated：pending 且邀约锁定的链尾已被其他移交推进；
    派生顺序为 accepted > cancelled > replaced > rejected > expired >
    terminated > pending（各存储终态互斥，不会同时出现）。
    """
    if offer["status"] == "accepted":
        return "accepted"

    if offer["status"] == "cancelled":
        return "cancelled"

    if offer["status"] == "replaced":
        return "replaced"

    for row in witness_rows:
        if row["decision"] == "rejected":
            return "rejected"

    now = now or _now_dt()
    if now > _parse_ts(offer["expires_at"]):
        return "expired"

    if current_tail_hash is not None and current_tail_hash != offer["tail_hash"]:
        return "terminated"

    return "pending"


def _offer_to_dict(
    offer: sqlite3.Row,
    witness_rows: list[sqlite3.Row],
    current_tail_hash: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """邀约行 + 见证行 -> 对外字典（任何情况下都不返回确认码/令牌摘要）。"""
    approved, rejected, pending, states = _witness_summary(witness_rows)
    witness_mode = offer["witness_threshold"] is not None
    return {
        "offer_id": offer["offer_id"],
        "chain_id": offer["chain_id"],
        "receiver": offer["receiver"],
        "status": _derived_offer_status(
            offer, witness_rows, current_tail_hash, now
        ),
        "expires_at": offer["expires_at"],
        "created_at": offer["created_at"],
        "accepted_at": offer["accepted_at"],
        "accepted_seq": offer["accepted_seq"],
        "cancellable": offer["cancel_code_digest"] is not None,
        "cancelled_at": offer["cancelled_at"],
        "replaces": offer["replaces"],
        "replaced_by": offer["replaced_by"],
        "replaced_at": offer["replaced_at"],
        "witness_mode": witness_mode,
        "witness_threshold": offer["witness_threshold"],
        "witness_count": len(witness_rows),
        "approved_count": approved,
        "rejected_count": rejected,
        "pending_count": pending,
        "witnesses": states,
    }


def _insert_offer_tx(
    conn: sqlite3.Connection,
    chain_id: str,
    tail_hash: str,
    receiver: str,
    expires_in_seconds: int,
    confirmation_code: str,
    witnesses: Optional[list[dict[str, str]]] = None,
    witness_threshold: Optional[int] = None,
    cancellation_code: Optional[str] = None,
    replaces: Optional[str] = None,
    offer_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """在已开启的写事务内插入一条 pending 邀约（含见证人）并返回创建数据。

    调用方必须已完成链尾匹配、旧邀约状态等全部前置校验；本函数只负责生成
    offer_id、计算有效期与凭证摘要并写入，与调用方的校验同处一个事务，整体
    原子提交或回滚。replaces 非空表示该邀约由原子替换产生（replaces 指向
    被替换的旧邀约）；新邀约的确认码、撤销码、见证令牌均按新 offer_id 做
    域分隔，绝不复用旧邀约的任何凭证。
    """
    offer_id = offer_id or uuid.uuid4().hex
    now = now or _now_dt()
    created_at = now.isoformat(timespec="seconds")
    expires_at = (now + timedelta(seconds=expires_in_seconds)).isoformat(
        timespec="seconds"
    )

    cancel_digest = (
        _cancel_code_digest(offer_id, cancellation_code)
        if cancellation_code is not None
        else None
    )

    conn.execute(
        """INSERT INTO offers
           (offer_id, chain_id, tail_hash, receiver, code_digest,
            witness_threshold, status, expires_at, created_at,
            cancel_code_digest, cancelled_at, replaces)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, NULL, ?)""",
        (
            offer_id,
            chain_id,
            tail_hash,
            receiver,
            _code_digest(confirmation_code),
            witness_threshold if witnesses else None,
            expires_at,
            created_at,
            cancel_digest,
            replaces,
        ),
    )

    created_witnesses: list[dict[str, Any]] = []
    if witnesses:
        for index, witness in enumerate(witnesses):
            conn.execute(
                """INSERT INTO offer_witnesses
                   (offer_id, witness_index, name, token_digest,
                    decision, decided_at)
                   VALUES (?, ?, ?, ?, NULL, NULL)""",
                (
                    offer_id,
                    index,
                    witness["name"],
                    _witness_token_digest(offer_id, witness["token"]),
                ),
            )
            created_witnesses.append(
                {
                    "witness_index": index,
                    "name": witness["name"],
                    "token": witness["token"],
                }
            )

    return {
        "offer_id": offer_id,
        "chain_id": chain_id,
        "receiver": receiver,
        "status": "pending",
        "expires_at": expires_at,
        "created_at": created_at,
        "cancellable": cancellation_code is not None,
        "witness_mode": bool(witnesses),
        "witness_threshold": witness_threshold if witnesses else None,
        "witnesses": created_witnesses,
    }


def create_offer(
    chain_id: str,
    tail_hash: str,
    receiver: str,
    expires_in_seconds: int,
    confirmation_code: str,
    witnesses: Optional[list[dict[str, str]]] = None,
    witness_threshold: Optional[int] = None,
    cancellation_code: Optional[str] = None,
) -> dict[str, Any]:
    """发起待接收移交邀约。

    以当前链尾 tail_hash 锁定邀约：链尾不匹配（链条已推进）返回 409，
    邀约创建即无意义。确认码只保存 SHA-256 摘要，明文不落库。

    cancellation_code 非空时邀约「可撤销/可替换」：保存绑定 offer_id 做域
    分隔的 SHA-256 摘要（offer:<offer_id>:cancel-code:<code>），此后可凭码
    调用 cancel_offer 或 replace_offer；省略时 cancel_code_digest 为 NULL，
    邀约不可撤销也不可替换，行为与旧版本完全一致。

    witnesses 非空时创建见证确认模式邀约（2～5 名见证人 + 通过阈值），
    每位见证人的独立一次性令牌同样只保存摘要；创建响应中一次性返回
    令牌明文，由发起方线下分发，此后服务端不再展示。
    """
    with write_tx() as conn:
        chain = conn.execute(
            "SELECT * FROM chains WHERE chain_id = ?", (chain_id,)
        ).fetchone()
        if chain is None:
            raise ChainError(404, "not_found", f"链条不存在: {chain_id}")

        # 封存门禁：生效期间不得创建新邀约，与封存创建互斥。
        _raise_if_chain_on_hold(conn, chain_id)

        if chain["tail_hash"] != tail_hash:
            raise ChainError(
                409,
                "tail_mismatch",
                "链尾哈希不匹配：链条已被其他移交推进，请获取最新链尾后再发起邀约",
            )

        created = _insert_offer_tx(
            conn,
            chain_id=chain_id,
            tail_hash=tail_hash,
            receiver=receiver,
            expires_in_seconds=expires_in_seconds,
            confirmation_code=confirmation_code,
            witnesses=witnesses,
            witness_threshold=witness_threshold,
            cancellation_code=cancellation_code,
        )

    return created


def confirm_offer(offer_id: str, confirmation_code: str) -> dict[str, Any]:
    """接收方凭 offer_id + 确认码确认邀约，追加唯一移交事件。

    所有校验与写入在同一个 SQLite 立即事务（BEGIN IMMEDIATE）中完成：
    校验确认码摘要 -> 邀约未过期 -> 邀约未使用 -> 链尾仍匹配 ->
    （见证模式）无拒绝且同意数达到阈值，然后追加移交事件、推进链头
    并将邀约置为 accepted，整体原子提交。
    过期、重复确认、见证拒绝、同意数不足、链尾已推进、已撤销或已被替换均
    返回 409 且整体回滚，不写任何事件。
    """
    with write_tx() as conn:
        offer = conn.execute(
            "SELECT * FROM offers WHERE offer_id = ?", (offer_id,)
        ).fetchone()
        if offer is None:
            raise ChainError(404, "not_found", f"邀约不存在: {offer_id}")

        # 封存门禁（最前置，先于确认码比对）：封存生效期间确认一律 409
        # chain_on_hold 且不写事件，也不据此泄露确认码是否正确。
        _raise_if_chain_on_hold(conn, offer["chain_id"])

        if offer["status"] == "cancelled":
            raise ChainError(
                409, "offer_not_cancellable", "邀约已撤销，不能确认，也不再可撤销"
            )

        if offer["status"] == "replaced":
            raise ChainError(
                409, "offer_replaced", "邀约已被原子替换为新邀约，请凭新邀约确认"
            )

        if offer["code_digest"] != _code_digest(confirmation_code):
            raise ChainError(403, "code_mismatch", "确认码不正确")

        if _now_dt() > _parse_ts(offer["expires_at"]):
            raise ChainError(
                409, "offer_expired", "邀约已过期，请由持有人重新发起邀约"
            )

        if offer["status"] != "pending":
            raise ChainError(
                409, "offer_already_accepted", "邀约已被确认，不能重复使用"
            )

        chain = conn.execute(
            "SELECT * FROM chains WHERE chain_id = ?", (offer["chain_id"],)
        ).fetchone()
        if chain is None:  # 外键约束下理论上不可达
            raise ChainError(404, "not_found", f"链条不存在: {offer['chain_id']}")

        if chain["tail_hash"] != offer["tail_hash"]:
            raise ChainError(
                409,
                "tail_mismatch",
                "链尾已推进：邀约所锁定的链尾不再是最新，邀约终止",
            )

        if offer["witness_threshold"] is not None:
            witness_rows = _load_witnesses(conn, offer_id)
            approved, rejected, _pending, _states = _witness_summary(witness_rows)
            if rejected > 0:
                raise ChainError(
                    409,
                    "offer_rejected",
                    f"已有 {rejected} 名见证人拒绝，邀约终止，不能确认",
                )
            if approved < offer["witness_threshold"]:
                raise ChainError(
                    409,
                    "witness_threshold_not_met",
                    "见证同意数未达到通过阈值："
                    f"{approved}/{offer['witness_threshold']}",
                )

        # nonce 由邀约派生，天然一次性：同一邀约不可能确认出第二个事件，
        # 也不会与手工移交的 nonce 冲突；UNIQUE(chain_id, nonce) 兜底。
        result = _append_handover_tx(
            conn, chain, offer["receiver"], nonce=f"offer:{offer_id}"
        )

        conn.execute(
            """UPDATE offers
               SET status = 'accepted', accepted_at = ?, accepted_seq = ?
               WHERE offer_id = ?""",
            (_now(), result["seq"], offer_id),
        )

    result["offer_id"] = offer_id
    return result


def cancel_offer(offer_id: str, cancellation_code: str) -> dict[str, Any]:
    """发起方凭 offer_id + 撤销码撤销邀约，不追加任何链上事件。

    所有校验与写入在同一个 SQLite 立即事务（BEGIN IMMEDIATE）中完成，与
    接收方确认、见证人表态、原子替换互斥：进程内由写锁串行，进程间由 SQLite
    写锁串行，竞争终态（accepted / cancelled / replaced / rejected …）中至多
    一个成功。

    可撤销的充要条件：存储状态仍为 pending、未过期、无见证人拒绝、锁定的
    链尾仍是当前链尾。任一不满足（含邀约已 accepted / rejected / expired /
    terminated / cancelled / replaced，以及创建时未提供撤销码的旧邀约）返回
    409 offer_not_cancellable 且不改写任何数据、不动哈希链；撤销码错误返回
    403 cancel_code_mismatch。撤销码摘要本身从不出现在任何响应中。
    """
    with write_tx() as conn:
        offer = conn.execute(
            "SELECT * FROM offers WHERE offer_id = ?", (offer_id,)
        ).fetchone()
        if offer is None:
            raise ChainError(404, "not_found", f"邀约不存在: {offer_id}")

        # 终态 / 无撤销码：一律 409 offer_not_cancellable，不区分具体原因，
        # 且在验码之前判定 —— 不具备撤销条件时不泄露撤销码是否匹配。
        if offer["status"] != "pending":
            if offer["status"] == "accepted":
                message = "邀约已被接收方确认，不能撤销"
            elif offer["status"] == "replaced":
                message = "邀约已被原子替换为新邀约，不能撤销"
            else:  # cancelled
                message = "邀约已撤销，不能重复撤销"
            raise ChainError(409, "offer_not_cancellable", message)

        if _now_dt() > _parse_ts(offer["expires_at"]):
            raise ChainError(
                409,
                "offer_not_cancellable",
                "邀约已过期，不能撤销（过期为终止态）",
            )

        chain = conn.execute(
            "SELECT tail_hash FROM chains WHERE chain_id = ?",
            (offer["chain_id"],),
        ).fetchone()
        if chain is None:  # 外键约束下理论上不可达
            raise ChainError(404, "not_found", f"链条不存在: {offer['chain_id']}")
        if chain["tail_hash"] != offer["tail_hash"]:
            raise ChainError(
                409,
                "offer_not_cancellable",
                "链尾已推进：邀约所锁定的链尾不再是最新，不能撤销",
            )

        witness_rows = _load_witnesses(conn, offer_id)
        if any(r["decision"] == "rejected" for r in witness_rows):
            raise ChainError(
                409,
                "offer_not_cancellable",
                "已有见证人拒绝，邀约已终止，不能撤销",
            )

        if offer["cancel_code_digest"] is None:
            # 旧邀约 / 创建时省略 cancellation_code：兼容行为，不可撤销。
            raise ChainError(
                409,
                "offer_not_cancellable",
                "该邀约未设置撤销码，不能撤销",
            )

        if offer["cancel_code_digest"] != _cancel_code_digest(
            offer_id, cancellation_code
        ):
            raise ChainError(403, "cancel_code_mismatch", "撤销码不正确")

        cancelled_at = _now()
        # WHERE 条件再兜底一次：本事务内竞态被写锁排除，影响行数不为 1
        # 只可能意味着状态已被其他终态占据，此时绝不写入。
        cursor = conn.execute(
            """UPDATE offers
               SET status = 'cancelled', cancelled_at = ?
               WHERE offer_id = ? AND status = 'pending'""",
            (cancelled_at, offer_id),
        )
        if cursor.rowcount != 1:
            raise ChainError(
                409,
                "offer_not_cancellable",
                "邀约已离开 pending 状态（竞争终止），撤销未生效",
            )

    return {
        "offer_id": offer_id,
        "chain_id": offer["chain_id"],
        "status": "cancelled",
        "cancellable": True,
        "cancelled_at": cancelled_at,
        "tail_hash": offer["tail_hash"],
    }


def replace_offer(
    offer_id: str,
    cancellation_code: str,
    receiver: str,
    expires_in_seconds: int,
    confirmation_code: str,
    new_cancellation_code: Optional[str] = None,
    witnesses: Optional[list[dict[str, str]]] = None,
    witness_threshold: Optional[int] = None,
) -> dict[str, Any]:
    """发起方凭原撤销码原子替换待处理邀约。

    所有校验与写入在同一个 SQLite 立即事务（BEGIN IMMEDIATE）中完成，与
    接收方确认、撤销、见证人表态互斥：进程内由写锁串行，进程间由 SQLite
    写锁串行，竞争终态（accepted / cancelled / replaced / rejected …）中
    至多一个成功。

    可替换的充要条件与可撤销一致：存储状态仍为 pending、未过期、无见证人
    拒绝、锁定链尾仍是当前链尾，且创建时提供过撤销码。任一不满足（含邀约
    已 accepted / cancelled / replaced / rejected / expired / terminated，
    以及创建时未提供撤销码的旧邀约）返回 409 offer_not_replaceable，且不改
    写任何数据；原撤销码错误返回 403 cancel_code_mismatch。

    通过校验后，先在事务内创建锁定同一链尾的新邀约（新接收方、新有效期、
    新确认码摘要，以及可选的新撤销码摘要与全新见证配置；replaces 指向旧
    邀约），再把旧邀约置为 replaced（replaced_by 指向新邀约），并以
    UPDATE ... WHERE status='pending' 的影响行数兜底；任一步失败整体回滚，
    绝不产生半替换。原邀约的确认码/撤销码/见证令牌在替换后全部作废，
    任何凭证摘要都不出现在响应中（新见证令牌除外——与创建邀约一致，明文
    仅在本响应中返回一次）。
    """
    with write_tx() as conn:
        offer = conn.execute(
            "SELECT * FROM offers WHERE offer_id = ?", (offer_id,)
        ).fetchone()
        if offer is None:
            raise ChainError(404, "not_found", f"邀约不存在: {offer_id}")

        # 封存门禁（最前置，先于原撤销码比对）：封存生效期间既不能写入新邀约，
        # 也不能终结旧邀约，一律 409 chain_on_hold，与封存创建互斥。
        _raise_if_chain_on_hold(conn, offer["chain_id"])

        # 非 pending 一律 409 offer_not_replaceable，且在验码之前判定 ——
        # 不具备替换条件时不泄露原撤销码是否匹配。
        if offer["status"] != "pending":
            if offer["status"] == "accepted":
                message = "邀约已被接收方确认，不能替换"
            elif offer["status"] == "replaced":
                message = "邀约已被替换，不能重复替换（请替换最新邀约）"
            else:  # cancelled
                message = "邀约已撤销，不能替换"
            raise ChainError(409, "offer_not_replaceable", message)

        if _now_dt() > _parse_ts(offer["expires_at"]):
            raise ChainError(
                409,
                "offer_not_replaceable",
                "邀约已过期，不能替换（过期为终止态），请重新发起邀约",
            )

        chain = conn.execute(
            "SELECT * FROM chains WHERE chain_id = ?", (offer["chain_id"],)
        ).fetchone()
        if chain is None:  # 外键约束下理论上不可达
            raise ChainError(404, "not_found", f"链条不存在: {offer['chain_id']}")
        if chain["tail_hash"] != offer["tail_hash"]:
            raise ChainError(
                409,
                "offer_not_replaceable",
                "链尾已推进：邀约所锁定的链尾不再是最新，不能替换",
            )

        witness_rows = _load_witnesses(conn, offer_id)
        if any(r["decision"] == "rejected" for r in witness_rows):
            raise ChainError(
                409,
                "offer_not_replaceable",
                "已有见证人拒绝，邀约已终止，不能替换",
            )

        if offer["cancel_code_digest"] is None:
            # 旧邀约 / 创建时省略 cancellation_code：兼容行为，不可替换。
            raise ChainError(
                409,
                "offer_not_replaceable",
                "该邀约未设置撤销码，不能替换",
            )

        if offer["cancel_code_digest"] != _cancel_code_digest(
            offer_id, cancellation_code
        ):
            raise ChainError(403, "cancel_code_mismatch", "原撤销码不正确")

        # 先插入新邀约（replaces 自引用指向旧邀约），再置旧邀约为 replaced：
        # 顺序由外键约束决定 —— 新行先存在，旧行的 replaced_by 外键才可写入。
        # 新邀约锁定与原邀约完全相同的链尾；所有凭证按新 offer_id 重新做域
        # 分隔，不复用旧确认码/撤销码/见证令牌。
        created = _insert_offer_tx(
            conn,
            chain_id=offer["chain_id"],
            tail_hash=offer["tail_hash"],
            receiver=receiver,
            expires_in_seconds=expires_in_seconds,
            confirmation_code=confirmation_code,
            witnesses=witnesses,
            witness_threshold=witness_threshold,
            cancellation_code=new_cancellation_code,
            replaces=offer_id,
        )
        new_offer_id = created["offer_id"]

        replaced_at = created["created_at"]
        # WHERE 条件再兜底一次：本事务内竞态被写锁排除，影响行数不为 1
        # 只可能意味着旧邀约已被确认/撤销/替换等终态占据，此时整体回滚，
        # 绝不留下「旧邀约仍 pending、新邀约已建」的半替换。
        cursor = conn.execute(
            """UPDATE offers
               SET status = 'replaced', replaced_by = ?, replaced_at = ?
               WHERE offer_id = ? AND status = 'pending'""",
            (new_offer_id, replaced_at, offer_id),
        )
        if cursor.rowcount != 1:
            raise ChainError(
                409,
                "offer_not_replaceable",
                "邀约已离开 pending 状态（竞争终止），替换未生效",
            )

    created["replaces"] = offer_id
    return created


def record_witness_decision(
    offer_id: str, token: str, decision: str
) -> dict[str, Any]:
    """见证人凭独立一次性令牌表态同意 / 拒绝。

    令牌以摘要比对（摘要域绑定 offer_id），同一见证人至多表态一次：
    令牌一旦命中一行且该行已有 decision，即返回 409 witness_already_decided。
    所有校验与写入在同一个 SQLite 立即事务内完成。

    邀约本身终止（不存在 404；确认码错误不属于本流程；已接受、已过期、
    链尾已推进、已被其他见证人拒绝）时表态一律 409，且不会改动任何数据。
    返回的表态结果绝不包含令牌摘要。
    """
    with write_tx() as conn:
        offer = conn.execute(
            "SELECT * FROM offers WHERE offer_id = ?", (offer_id,)
        ).fetchone()
        if offer is None:
            raise ChainError(404, "not_found", f"邀约不存在: {offer_id}")

        if offer["witness_threshold"] is None:
            raise ChainError(
                409,
                "offer_not_witnessed",
                "该邀约不是见证确认模式，没有见证人",
            )

        witness = conn.execute(
            "SELECT * FROM offer_witnesses WHERE offer_id = ? AND token_digest = ?",
            (offer_id, _witness_token_digest(offer_id, token)),
        ).fetchone()
        if witness is None:
            # 与确认码错误一致：凭证无效返回 403，且不区分「不存在/已用」。
            raise ChainError(403, "token_mismatch", "见证人令牌不正确")

        if witness["decision"] is not None:
            raise ChainError(
                409,
                "witness_already_decided",
                f"见证人 {witness['name']} 已表态，令牌为一次性，不能重复使用",
            )

        if offer["status"] == "cancelled":
            raise ChainError(
                409,
                "offer_not_cancellable",
                "邀约已撤销，表态无效，也不再可撤销",
            )

        if offer["status"] == "replaced":
            raise ChainError(
                409,
                "offer_replaced",
                "邀约已被原子替换为新邀约，旧令牌作废，表态无效",
            )

        if offer["status"] != "pending":
            raise ChainError(
                409, "offer_already_accepted", "邀约已被确认，不能继续表态"
            )

        if _now_dt() > _parse_ts(offer["expires_at"]):
            raise ChainError(409, "offer_expired", "邀约已过期，表态无效")

        chain = conn.execute(
            "SELECT tail_hash FROM chains WHERE chain_id = ?",
            (offer["chain_id"],),
        ).fetchone()
        if chain is None:  # 外键约束下理论上不可达
            raise ChainError(404, "not_found", f"链条不存在: {offer['chain_id']}")
        if chain["tail_hash"] != offer["tail_hash"]:
            raise ChainError(
                409,
                "tail_mismatch",
                "链尾已推进：邀约所锁定的链尾不再是最新，邀约终止",
            )

        witness_rows = _load_witnesses(conn, offer_id)
        if any(r["decision"] == "rejected" for r in witness_rows):
            raise ChainError(
                409, "offer_rejected", "已有见证人拒绝，邀约终止，不能继续表态"
            )

        decided_at = _now()
        conn.execute(
            """UPDATE offer_witnesses
               SET decision = ?, decided_at = ?
               WHERE offer_id = ? AND witness_index = ?""",
            (decision, decided_at, offer_id, witness["witness_index"]),
        )

        witness_rows = _load_witnesses(conn, offer_id)
        approved, rejected, pending, _states = _witness_summary(witness_rows)

    return {
        "offer_id": offer_id,
        "witness_index": witness["witness_index"],
        "name": witness["name"],
        "decision": decision,
        "decided_at": decided_at,
        "status": "rejected" if rejected > 0 else "pending",
        "witness_threshold": offer["witness_threshold"],
        "approved_count": approved,
        "rejected_count": rejected,
        "pending_count": pending,
    }


def get_offer(offer_id: str) -> dict[str, Any]:
    """查询单个邀约状态（不返回确认码/令牌及其摘要）。"""
    with read_tx() as conn:
        offer = conn.execute(
            "SELECT * FROM offers WHERE offer_id = ?", (offer_id,)
        ).fetchone()
        if offer is None:
            raise ChainError(404, "not_found", f"邀约不存在: {offer_id}")
        witness_rows = _load_witnesses(conn, offer_id)
        chain = conn.execute(
            "SELECT tail_hash FROM chains WHERE chain_id = ?",
            (offer["chain_id"],),
        ).fetchone()

    current_tail = chain["tail_hash"] if chain is not None else None
    return _offer_to_dict(offer, witness_rows, current_tail)


def get_witness_status(offer_id: str) -> dict[str, Any]:
    """查询见证邀约的见证人表态状态；无见证邀约返回 404。

    响应只包含各见证人的名称、序号与表态结果，绝不包含令牌摘要。
    """
    with read_tx() as conn:
        offer = conn.execute(
            "SELECT * FROM offers WHERE offer_id = ?", (offer_id,)
        ).fetchone()
        if offer is None:
            raise ChainError(404, "not_found", f"邀约不存在: {offer_id}")
        if offer["witness_threshold"] is None:
            raise ChainError(
                404, "witness_mode_not_found", "该邀约不是见证确认模式"
            )
        witness_rows = _load_witnesses(conn, offer_id)
        chain = conn.execute(
            "SELECT tail_hash FROM chains WHERE chain_id = ?",
            (offer["chain_id"],),
        ).fetchone()

    current_tail = chain["tail_hash"] if chain is not None else None
    data = _offer_to_dict(offer, witness_rows, current_tail)
    return {
        "offer_id": offer_id,
        "witness_mode": True,
        "status": data["status"],
        "witness_threshold": offer["witness_threshold"],
        "witness_count": data["witness_count"],
        "approved_count": data["approved_count"],
        "rejected_count": data["rejected_count"],
        "pending_count": data["pending_count"],
        "witnesses": data["witnesses"],
    }


def get_chain(chain_id: str) -> dict[str, Any]:
    with read_tx() as conn:
        chain = conn.execute(
            "SELECT * FROM chains WHERE chain_id = ?", (chain_id,)
        ).fetchone()
        if chain is None:
            raise ChainError(404, "not_found", f"链条不存在: {chain_id}")

        rows = conn.execute(
            "SELECT * FROM events WHERE chain_id = ? ORDER BY seq", (chain_id,)
        ).fetchall()

    return {
        "chain_id": chain["chain_id"],
        "file_hash": chain["file_hash"],
        "first_holder": chain["first_holder"],
        "tail_hash": chain["tail_hash"],
        "current_holder": chain["current_holder"],
        "length": len(rows),
        "created_at": chain["created_at"],
        "events": [_row_to_event(r) for r in rows],
    }


def list_chains() -> list[dict[str, Any]]:
    with read_tx() as conn:
        rows = conn.execute(
            """SELECT c.*, COUNT(e.seq) AS length
               FROM chains c
               LEFT JOIN events e ON e.chain_id = c.chain_id
               GROUP BY c.chain_id
               ORDER BY c.created_at, c.chain_id"""
        ).fetchall()
    return [
        {
            "chain_id": r["chain_id"],
            "file_hash": r["file_hash"],
            "current_holder": r["current_holder"],
            "tail_hash": r["tail_hash"],
            "length": r["length"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def verify_chain(chain_id: str) -> dict[str, Any]:
    """重放全链并指出首个断裂序号。

    对每个事件重新计算哈希并与存储值比对，同时校验序号连续与 prev_hash 指向。
    另将重算出的链尾与 chains 头信息比对，以发现头部被直接篡改的情况。
    """
    with read_tx() as conn:
        chain = conn.execute(
            "SELECT * FROM chains WHERE chain_id = ?", (chain_id,)
        ).fetchone()
        if chain is None:
            raise ChainError(404, "not_found", f"链条不存在: {chain_id}")
        rows = conn.execute(
            "SELECT * FROM events WHERE chain_id = ? ORDER BY seq", (chain_id,)
        ).fetchall()

    length = len(rows)
    expected_next_seq = length
    prev_hash = ""
    broken_seq: Optional[int] = None
    reason: Optional[str] = None

    for i, row in enumerate(rows):
        if row["seq"] != i:
            broken_seq, reason = row["seq"], f"序号不连续：期望 {i}，实际 {row['seq']}"
            break

        if row["seq"] == 0:
            payload = _genesis_payload(chain["file_hash"], chain["first_holder"])
        else:
            payload = _handover_payload(
                seq=row["seq"],
                prev_hash=row["prev_hash"],
                giver=row["giver"],
                receiver=row["receiver"],
                nonce=row["nonce"],
            )
        recomputed = digest_hex(canonical_json(payload))

        if recomputed != row["event_hash"]:
            broken_seq, reason = (
                row["seq"],
                f"事件哈希不匹配：记录 {row['event_hash']}，重算 {recomputed}"
                + ("（文件摘要或首位持有人被篡改）" if row["seq"] == 0 else ""),
            )
            break

        if row["event_hash"] != row["prev_hash"] and row["prev_hash"] != prev_hash:
            broken_seq, reason = (
                row["seq"],
                f"前序指针断裂：期望指向 {prev_hash}，实际指向 {row['prev_hash']}",
            )
            break

        prev_hash = row["event_hash"]

    recomputed_tail = prev_hash if broken_seq is None else None

    if broken_seq is None and recomputed_tail != chain["tail_hash"]:
        # 事件本身全部可验证，但链条头的链尾缓存被篡改：没有具体事件序号可指
        reason = (
            "链条头部信息被篡改：链尾哈希为 "
            f"{chain['tail_hash']}，按事件重算应为 {recomputed_tail}"
        )
        # first_broken_seq 保持 null
    valid = broken_seq is None and recomputed_tail == chain["tail_hash"]
    if valid and length > 0:
        # 最后一个事件的接收方必须等于头信息中的当前持有人
        last_holder = chain["first_holder"] if length == 1 else rows[-1]["receiver"]
        if chain["current_holder"] != last_holder:
            valid = False
            reason = (
                "链条头部信息被篡改：当前持有人为 "
                f"{chain['current_holder']}，按事件推导应为 {last_holder}"
            )

    return {
        "chain_id": chain_id,
        "valid": valid,
        "length": length,
        "expected_next_seq": expected_next_seq,
        "first_broken_seq": broken_seq,
        "reason": reason,
        "recomputed_tail_hash": recomputed_tail,
        "stored_tail_hash": chain["tail_hash"],
    }


# ---------------------------------------------------------------------------
# 交付清单核对（只读批量比对）
# ---------------------------------------------------------------------------


def reconcile_inventory(items: list[dict[str, str]]) -> dict[str, Any]:
    """按交付清单批量核对 2～100 条链的头信息（只读，不推进任何状态）。

    在**同一个 SQLite 读事务**内完成全部读取：先把「当时最大的事件数据库 id」
    固定为快照锚点 snapshot_event_id（MAX(events.id)，全库无事件时为 0），
    再按请求顺序逐链读取链头（chains 行）并与清单声明比对。逐项状态按
    missing -> file_mismatch -> holder_mismatch -> tail_mismatch 的顺序取首个
    不符项，四项全部一致为 matched：

    - missing：链不存在（不再比对其余字段）；
    - file_mismatch：链存在但文件摘要 file_hash 与链头不符；
    - holder_mismatch：文件摘要一致但当前持有人 current_holder 不符；
    - tail_mismatch：前三项一致但链尾 tail_hash 不符（清单已过期）。

    结果严格保持输入顺序，matched_count 统计 matched 条数。同批 chain_id
    不得重复、条数 2～100（均由请求模型保证，违反返回 422）。本函数只读：
    不追加事件、不推进链尾、不写任何表。
    """
    results: list[dict[str, str]] = []
    matched_count = 0
    with read_tx() as conn:
        row = conn.execute("SELECT MAX(id) AS max_id FROM events").fetchone()
        snapshot_event_id = row["max_id"] if row["max_id"] is not None else 0

        for item in items:
            chain = conn.execute(
                """SELECT file_hash, current_holder, tail_hash
                   FROM chains WHERE chain_id = ?""",
                (item["chain_id"],),
            ).fetchone()
            if chain is None:
                status = "missing"
            elif chain["file_hash"] != item["file_hash"]:
                status = "file_mismatch"
            elif chain["current_holder"] != item["current_holder"]:
                status = "holder_mismatch"
            elif chain["tail_hash"] != item["tail_hash"]:
                status = "tail_mismatch"
            else:
                status = "matched"
                matched_count += 1
            results.append({"chain_id": item["chain_id"], "status": status})

    return {
        "snapshot_event_id": snapshot_event_id,
        "matched_count": matched_count,
        "count": len(items),
        "results": results,
    }


# ---------------------------------------------------------------------------
# 交接事件的可选择披露证明（Merkle 包含证明）
# ---------------------------------------------------------------------------


def get_event_proof(chain_id: str, seq: int) -> dict[str, Any]:
    """为指定事件生成 Merkle 包含证明。

    事件集合与当前链尾必须在**同一个读事务**内取得，二者构成同一快照：
    既避免事件数与 tail_hash 取自两个时刻，也使返回的 snapshot_digest 准确
    锚定本次快照。链条不存在返回 404 not_found；seq 越界（负数或不小于
    当前事件数）返回 404 event_not_found。

    叶子绑定 chain_id / seq / event_hash（merkle.leaf_hash）；内部节点使用
    固定域分隔前缀 merkle-node:v1: 拼接左右定长十六进制子哈希；奇数层末节点
    与自身复制配对。snapshot_digest 按 chain_id / event_count / merkle_root /
    tail_hash 的规范 JSON 计算。
    """
    with read_tx() as conn:
        chain = conn.execute(
            "SELECT * FROM chains WHERE chain_id = ?", (chain_id,)
        ).fetchone()
        if chain is None:
            raise ChainError(404, "not_found", f"链条不存在: {chain_id}")
        rows = conn.execute(
            "SELECT * FROM events WHERE chain_id = ? ORDER BY seq", (chain_id,)
        ).fetchall()

    if not isinstance(seq, int) or seq < 0 or seq >= len(rows):
        raise ChainError(
            404,
            "event_not_found",
            f"事件序号越界: {seq}（该链当前共 {len(rows)} 个事件）",
        )

    event_hashes = [row["event_hash"] for row in rows]
    proof = merkle.build_inclusion_proof(chain_id, event_hashes, seq)
    snapshot = merkle.snapshot_digest(
        chain_id=chain_id,
        event_count=proof["event_count"],
        tail_hash=chain["tail_hash"],
        merkle_root=proof["merkle_root"],
    )

    return {
        "chain_id": chain_id,
        "event": _row_to_event(rows[seq]),
        "leaf_hash": proof["leaf_hash"],
        "path": proof["path"],
        "event_count": proof["event_count"],
        "tail_hash": chain["tail_hash"],
        "merkle_root": proof["merkle_root"],
        "snapshot_digest": snapshot,
    }


def verify_event_proof(
    chain_id: str,
    seq: int,
    event_hash: str,
    path: list[dict[str, str]],
    event_count: int,
    tail_hash: str,
    merkle_root: str,
    snapshot_digest_claim: str,
) -> dict[str, Any]:
    """仅依据请求体重算并校验 Merkle 包含证明，**不读取数据库**。

    依次重算：叶子（绑定 chain_id/seq/event_hash）→ 按左右兄弟路径与「奇数层
    末节点复制配对」规则重算 Merkle 根 → snapshot_digest。首个失败阶段即停止，
    阶段顺序为 event_count → seq → path → merkle_root → snapshot_digest。
    """
    recomputed_leaf = merkle.leaf_hash(chain_id, seq, event_hash)
    recomputed_snapshot = merkle.snapshot_digest(
        chain_id=chain_id,
        event_count=event_count,
        tail_hash=tail_hash,
        merkle_root=merkle_root,
    )

    root, failed_stage = merkle.evaluate_inclusion(
        leaf=recomputed_leaf,
        seq=seq,
        path=[(step["direction"], step["sibling_hash"]) for step in path],
        event_count=event_count,
    )

    reason: Optional[str] = None
    if failed_stage is not None:
        if failed_stage == merkle.STAGE_EVENT_COUNT:
            reason = f"event_count 非法：{event_count}，必须为 >= 1 的整数"
        elif failed_stage == merkle.STAGE_SEQ:
            reason = f"seq 越界：{seq} 不在 [0, {event_count}) 范围内"
        else:  # path
            reason = (
                "兄弟路径不合法：层数、左右方向或奇数层末节点复制配对规则"
                "与 seq/event_count 不一致"
            )
    elif root != merkle_root:
        failed_stage = merkle.STAGE_ROOT
        reason = (
            "Merkle 根不匹配：按请求体重算为 "
            f"{root}，请求声明为 {merkle_root}"
        )
    elif recomputed_snapshot != snapshot_digest_claim:
        failed_stage = merkle.STAGE_SNAPSHOT
        reason = (
            "链尾快照摘要不匹配：按 chain_id/event_count/merkle_root/"
            f"tail_hash 重算为 {recomputed_snapshot}，"
            f"请求声明为 {snapshot_digest_claim}"
        )

    return {
        "valid": failed_stage is None,
        "failed_stage": failed_stage,
        "reason": reason,
        "chain_id": chain_id,
        "seq": seq,
        "recomputed_leaf_hash": recomputed_leaf,
        "recomputed_merkle_root": root,
        "recomputed_snapshot_digest": recomputed_snapshot,
    }


# ---------------------------------------------------------------------------
# 批量可选择披露证明（Merkle multiproof）
# ---------------------------------------------------------------------------


def get_events_proof(chain_id: str, seqs: list[int]) -> dict[str, Any]:
    """为去重且升序的 1～20 个序号生成一份 Merkle multiproof。

    事件集合与当前链尾必须在**同一个读事务**内取得（与单事件证明同一快照
    语义）。请求中的序号先去重、按升序排列；去重后超过
    merkle.MAX_MULTI_PROOF_LEAVES 个返回 400 too_many_seqs。链条不存在返回
    404 not_found；任一序号越界（负数或不小于当前事件数）返回 404
    event_not_found。响应只包含被选事件，绝不泄露未选择事件；proof_nodes
    为按 (level, index) 升序且不重复的最小兄弟节点集合。
    """
    unique_sorted = sorted(set(seqs))
    if not unique_sorted:
        raise ChainError(
            400, "too_few_seqs", "至少需要 1 个事件序号（查询参数 seq）"
        )
    if len(unique_sorted) > merkle.MAX_MULTI_PROOF_LEAVES:
        raise ChainError(
            400,
            "too_many_seqs",
            f"批量证明一次最多选择 {merkle.MAX_MULTI_PROOF_LEAVES} 个事件序号"
            f"（去重后为 {len(unique_sorted)} 个）",
        )

    with read_tx() as conn:
        chain = conn.execute(
            "SELECT * FROM chains WHERE chain_id = ?", (chain_id,)
        ).fetchone()
        if chain is None:
            raise ChainError(404, "not_found", f"链条不存在: {chain_id}")
        rows = conn.execute(
            "SELECT * FROM events WHERE chain_id = ? ORDER BY seq", (chain_id,)
        ).fetchall()

    event_count = len(rows)
    for seq in unique_sorted:
        if seq < 0 or seq >= event_count:
            raise ChainError(
                404,
                "event_not_found",
                f"事件序号越界: {seq}（该链当前共 {event_count} 个事件）",
            )

    event_hashes = [row["event_hash"] for row in rows]
    proof = merkle.build_multi_proof(chain_id, event_hashes, unique_sorted)
    snapshot = merkle.snapshot_digest(
        chain_id=chain_id,
        event_count=event_count,
        tail_hash=chain["tail_hash"],
        merkle_root=proof["merkle_root"],
    )

    return {
        "chain_id": chain_id,
        "events": [_row_to_event(rows[seq]) for seq in unique_sorted],
        "event_count": event_count,
        "tail_hash": chain["tail_hash"],
        "merkle_root": proof["merkle_root"],
        "snapshot_digest": snapshot,
        "proof_nodes": proof["proof_nodes"],
    }


def verify_batch_proof(
    chain_id: str,
    events: list[dict[str, Any]],
    event_count: int,
    tail_hash: str,
    merkle_root: str,
    snapshot_digest_claim: str,
    proof_nodes: list[dict[str, Any]],
) -> dict[str, Any]:
    """仅依据请求体按节点坐标重建 Merkle 根并校验批量证明，**不读取数据库**。

    依次重算：各被选事件的叶子（绑定 chain_id/seq/event_hash）→ 按证明节点
    坐标逐层重建 Merkle 根（奇数层末节点自复制）→ snapshot_digest。首个失败
    阶段即停止，阶段顺序为 event_count → events → proof_nodes → merkle_root
    → snapshot_digest。重复或越界序号、缺失或多余节点、坐标冲突以及不符合
    奇数层自复制规则的证明一律拒绝（valid=false）。
    """
    recomputed_leaves = [
        merkle.leaf_hash(chain_id, event["seq"], event["event_hash"])
        for event in events
    ]
    recomputed_snapshot = merkle.snapshot_digest(
        chain_id=chain_id,
        event_count=event_count,
        tail_hash=tail_hash,
        merkle_root=merkle_root,
    )

    root, failed_stage, context = merkle.evaluate_multi_inclusion(
        chain_id=chain_id,
        event_count=event_count,
        events=[(event["seq"], event["event_hash"]) for event in events],
        proof_nodes=[
            (node["level"], node["index"], node["node_hash"])
            for node in proof_nodes
        ],
    )

    reason: Optional[str] = None
    if failed_stage == merkle.STAGE_EVENT_COUNT:
        reason = f"event_count 非法：{event_count}，必须为 >= 1 的整数"
    elif failed_stage == merkle.STAGE_EVENTS:
        if context.get("duplicate_seq"):
            reason = "events 中存在重复序号：批量证明的序号必须去重"
        elif "seq" in context:
            reason = (
                f"事件序号越界：{context['seq']} 不在 [0, {event_count}) 范围内"
            )
        else:
            reason = (
                "events 数量非法：批量证明一次选择 1～"
                f"{merkle.MAX_MULTI_PROOF_LEAVES} 个事件"
            )
    elif failed_stage == merkle.STAGE_PROOF_NODES:
        coord = f"(level={context.get('level')}, index={context.get('index')})"
        if context.get("out_of_bounds"):
            reason = (
                f"证明节点坐标越界：{coord} 不在事件数为 {event_count} 的"
                "Merkle 树范围内（含奇数层末节点自复制位置之外不存在节点）"
            )
        elif context.get("duplicate_coordinate"):
            reason = f"证明节点坐标重复：{coord} 出现多次"
        elif context.get("missing"):
            reason = f"缺失必需的证明节点：{coord}"
        elif context.get("conflict"):
            reason = f"证明节点与重算值坐标冲突：{coord} 的哈希不一致"
        else:  # extra
            reason = f"多余的证明节点：{coord} 并非重建根所必需"
    elif root != merkle_root:
        failed_stage = merkle.STAGE_ROOT
        reason = (
            "Merkle 根不匹配：按请求体重算为 "
            f"{root}，请求声明为 {merkle_root}"
        )
    elif recomputed_snapshot != snapshot_digest_claim:
        failed_stage = merkle.STAGE_SNAPSHOT
        reason = (
            "链尾快照摘要不匹配：按 chain_id/event_count/merkle_root/"
            f"tail_hash 重算为 {recomputed_snapshot}，"
            f"请求声明为 {snapshot_digest_claim}"
        )

    return {
        "valid": failed_stage is None,
        "failed_stage": failed_stage,
        "reason": reason,
        "chain_id": chain_id,
        "seqs": [event["seq"] for event in events],
        "recomputed_leaf_hashes": recomputed_leaves,
        "recomputed_merkle_root": root,
        "recomputed_snapshot_digest": recomputed_snapshot,
    }


# ---------------------------------------------------------------------------
# 争议封存
# ---------------------------------------------------------------------------

_SEAL_TTL_MAX = 30 * 24 * 3600  # 封存有效期上限：30 天


def _seal_status(seal: sqlite3.Row, now: Optional[datetime] = None) -> str:
    """封存对外状态：released 为存储终态；active 行过期则派生 expired。

    封存行的 expires_at 在创建时固定且不再变化，因此「过期」无需任何写操作，
    读写时按当前时间实时派生（与邀约过期同样处理）。
    """
    if seal["status"] == "released":
        return "released"
    now = now or _now_dt()
    if now > _parse_ts(seal["expires_at"]):
        return "expired"
    return "active"


def _raise_if_chain_on_hold(
    conn: sqlite3.Connection,
    chain_id: str,
    now: Optional[datetime] = None,
) -> None:
    """在已开启的写事务内检查封存门禁：存在生效封存即抛 409 chain_on_hold。

    直接移交、邀约创建/替换/确认在各自的立即事务内、任何写入之前调用本函数，
    与封存创建共用同一把写锁与 BEGIN IMMEDIATE 语义：封存创建先提交则后续操作
    看到 active 行而失败；操作先提交则封存创建看到推进后的链尾（链尾不匹配）
    或直接在唯一索引上失败——竞争双方至多一个成功。
    """
    now = now or _now_dt()
    seal = conn.execute(
        """SELECT * FROM chain_seals
           WHERE chain_id = ? AND status = 'active'
           ORDER BY id DESC
           LIMIT 1""",
        (chain_id,),
    ).fetchone()
    if seal is not None and now <= _parse_ts(seal["expires_at"]):
        raise ChainError(
            409,
            "chain_on_hold",
            "链条处于争议封存生效期：直接移交与邀约的创建、替换、确认均被冻结，"
            "请由持有人凭解封码提前解封或等待封存到期",
        )


def _seal_to_dict(seal: sqlite3.Row, now: Optional[datetime] = None) -> dict[str, Any]:
    """封存行 -> 对外字典：只给原因与时间信息，绝不返回解封码摘要。"""
    return {
        "seal_id": seal["seal_id"],
        "chain_id": seal["chain_id"],
        "status": _seal_status(seal, now),
        "reason": seal["reason"],
        "tail_hash": seal["tail_hash"],
        "created_at": seal["created_at"],
        "expires_at": seal["expires_at"],
        "released_at": seal["released_at"],
    }


def create_seal(
    chain_id: str,
    tail_hash: str,
    reason: str,
    expires_in_seconds: int,
    release_code: str,
) -> dict[str, Any]:
    """持有人对交接链创建争议封存。

    在同一个 SQLite 立即事务（BEGIN IMMEDIATE）内完成：**取得写锁之后**取
    同一个带微秒精度的 UTC 时刻 now，统一用于已有封存判定、创建时刻与到期
    计算——等待写锁的耗时不会侵蚀有效期，整秒截断也不会让短有效期封存在
    创建瞬间就临近过期。随后：链条存在 -> 当前无生效封存（含已过期但尚未
    物化的 active 行）-> 携带的 tail_hash 必须仍是当前链尾（持有人身份由
    链尾持有语义隐式校验）-> 把同链已过期 active 旧封存物化为 expired ->
    插入新封存行（只存绑定 seal_id 做域分隔的解封码摘要）。expires_at =
    now + expires_in_seconds 以微秒精度持久化，创建响应、查询派生状态与
    移交/邀约门禁都依据这一截止时刻，完整有效期内封存保持生效。
    任一校验失败整体回滚，不写任何数据，也不触碰哈希链。

    链条不存在返回 404；已有生效封存返回 409 chain_seal_active；链尾不匹配
    返回 409 tail_mismatch；并发插入由部分唯一索引 uq_active_seal_per_chain
    兜底，落败方同样得到 409 chain_seal_active。
    """
    seal_id = uuid.uuid4().hex

    with write_tx() as conn:
        # 取得写锁后取同一个微秒精度 UTC 时刻：已有封存判定、created_at 与
        # expires_at 同源，有效期自这一刻完整起算 expires_in_seconds 秒。
        now = _now_dt()
        created_at = now.isoformat(timespec="microseconds")
        expires_at = (now + timedelta(seconds=expires_in_seconds)).isoformat(
            timespec="microseconds"
        )

        chain = conn.execute(
            "SELECT * FROM chains WHERE chain_id = ?", (chain_id,)
        ).fetchone()
        if chain is None:
            raise ChainError(404, "not_found", f"链条不存在: {chain_id}")

        active = conn.execute(
            """SELECT * FROM chain_seals
               WHERE chain_id = ? AND status = 'active'
               ORDER BY id DESC
               LIMIT 1""",
            (chain_id,),
        ).fetchone()
        if active is not None and now <= _parse_ts(active["expires_at"]):
            raise ChainError(
                409,
                "chain_seal_active",
                "该链已存在生效封存，同一链同时只能有一个生效封存；"
                "请先凭解封码提前解封或等待其到期",
            )

        if chain["tail_hash"] != tail_hash:
            raise ChainError(
                409,
                "tail_mismatch",
                "链尾哈希不匹配：仅当前持有人可凭最新链尾创建封存，"
                "请获取最新链尾后重试",
            )

        # 旧 active 封存均已过期（上面的生效封存检查已排除未过期情形）：在同一
        # 事务内物化为 expired（released_at 保持 NULL——过期时刻即固定不变的
        # expires_at），随后插入新行。expires_at 本身不再变化，这里仅落库派生
        # 终态，使部分唯一索引只约束当前生效封存。
        conn.execute(
            """UPDATE chain_seals
               SET status = 'expired'
               WHERE chain_id = ? AND status = 'active'""",
            (chain_id,),
        )

        try:
            conn.execute(
                """INSERT INTO chain_seals
                   (seal_id, chain_id, tail_hash, reason, status,
                    release_code_digest, expires_at, created_at, released_at)
                   VALUES (?, ?, ?, ?, 'active', ?, ?, ?, NULL)""",
                (
                    seal_id,
                    chain_id,
                    tail_hash,
                    reason,
                    _release_code_digest(seal_id, release_code),
                    expires_at,
                    created_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # 仅当并发事务抢先插入了生效封存（部分唯一索引）时发生。
            raise ChainError(
                409,
                "chain_seal_active",
                f"封存创建冲突：该链已有并发生效封存: {exc}",
            ) from exc

        seal = conn.execute(
            "SELECT * FROM chain_seals WHERE seal_id = ?", (seal_id,)
        ).fetchone()

    return _seal_to_dict(seal, now)


def release_seal(seal_id: str, release_code: str) -> dict[str, Any]:
    """凭一次性解封码提前解封。

    在同一个 SQLite 立即事务内完成：封存存在 -> 未被提前解封 -> 未过期 ->
    解封码摘要匹配 -> 置为 released 并记录 released_at，整体原子提交。解封不
    追加任何哈希链事件。解封码错误返回 403 release_code_mismatch（在确认封存
    仍 active 且未过期之后才验码，避免对终态/过期封存泄露码是否匹配）；封存
    不存在返回 404；已解封返回 409 seal_already_released；已过期返回
    409 seal_expired（过期自动失效，无需也不能再解封）。
    """
    with write_tx() as conn:
        seal = conn.execute(
            "SELECT * FROM chain_seals WHERE seal_id = ?", (seal_id,)
        ).fetchone()
        if seal is None:
            raise ChainError(404, "not_found", f"封存不存在: {seal_id}")

        if seal["status"] == "released":
            raise ChainError(
                409, "seal_already_released", "封存已提前解封，不能重复解封"
            )

        now = _now_dt()
        if now > _parse_ts(seal["expires_at"]):
            raise ChainError(
                409,
                "seal_expired",
                "封存已过有效期并自动失效，无需解封",
            )

        if seal["release_code_digest"] != _release_code_digest(
            seal_id, release_code
        ):
            raise ChainError(403, "release_code_mismatch", "解封码不正确")

        released_at = _now()
        cursor = conn.execute(
            """UPDATE chain_seals
               SET status = 'released', released_at = ?
               WHERE seal_id = ? AND status = 'active'""",
            (released_at, seal_id),
        )
        if cursor.rowcount != 1:
            # 立即事务内不会被插队；影响行数异常只可能是状态已被改写，绝不写入。
            raise ChainError(
                409,
                "seal_not_active",
                "封存已离开 active 状态（竞争终止），解封未生效",
            )

        seal = conn.execute(
            "SELECT * FROM chain_seals WHERE seal_id = ?", (seal_id,)
        ).fetchone()

    return _seal_to_dict(seal, now)


def get_seal(seal_id: str) -> dict[str, Any]:
    """按封存 ID 查询状态（不含解封码摘要）；封存不存在返回 404。"""
    with read_tx() as conn:
        seal = conn.execute(
            "SELECT * FROM chain_seals WHERE seal_id = ?", (seal_id,)
        ).fetchone()
        if seal is None:
            raise ChainError(404, "not_found", f"封存不存在: {seal_id}")
    return _seal_to_dict(seal)


def get_latest_seal(chain_id: str) -> dict[str, Any]:
    """查询某条链最新的一个封存（不含解封码摘要）。

    链条不存在返回 404；该链从未有过封存返回 404 seal_not_found。
    """
    with read_tx() as conn:
        chain = conn.execute(
            "SELECT chain_id FROM chains WHERE chain_id = ?", (chain_id,)
        ).fetchone()
        if chain is None:
            raise ChainError(404, "not_found", f"链条不存在: {chain_id}")
        seal = conn.execute(
            """SELECT * FROM chain_seals
               WHERE chain_id = ?
               ORDER BY id DESC
               LIMIT 1""",
            (chain_id,),
        ).fetchone()
        if seal is None:
            raise ChainError(404, "seal_not_found", "该链尚无封存记录")
    return _seal_to_dict(seal)
