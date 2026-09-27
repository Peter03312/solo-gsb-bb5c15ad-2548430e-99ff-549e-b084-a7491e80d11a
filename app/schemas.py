"""请求 / 响应模型与输入校验。"""
from __future__ import annotations

import re
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_NAME_MAX = 200
_NONCE_MAX = 256
_CODE_MAX = 128
_TOKEN_MAX = 256
_OFFER_TTL_MAX = 7 * 24 * 3600  # 邀约有效期上限：7 天
_SEAL_TTL_MAX = 30 * 24 * 3600  # 争议封存有效期上限：30 天
_REASON_MAX = 1000
_WITNESS_MIN = 2
_WITNESS_MAX = 5
_HANDOVER_BATCH_MIN = 2
_HANDOVER_BATCH_MAX = 20
_INVENTORY_RECONCILE_MIN = 2
_INVENTORY_RECONCILE_MAX = 100

def _validate_name(value: str, field: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field} 不能为空")
    if len(value) > _NAME_MAX:
        raise ValueError(f"{field} 长度不能超过 {_NAME_MAX}")
    return value


def _validate_hex64(value: str, field: str) -> str:
    value = value.strip().lower()
    if not _HEX64.match(value):
        raise ValueError(f"{field} 必须是 64 位十六进制哈希")
    return value


def _validate_code(value: str, field: str = "confirmation_code") -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field} 不能为空")
    if len(value) > _CODE_MAX:
        raise ValueError(f"{field} 长度不能超过 {_CODE_MAX}")
    return value


def _validate_token(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("token 不能为空（见证人一次性令牌）")
    if len(value) > _TOKEN_MAX:
        raise ValueError(f"token 长度不能超过 {_TOKEN_MAX}")
    return value


def _validate_witness_config(
    witnesses: Optional[list["WitnessInput"]],
    witness_threshold: Optional[int],
) -> None:
    """校验见证配置：无见证人则阈值必须缺省；否则 2～5 名、阈值 1～人数、

    名称与令牌均不重复。发起邀约与原子替换共用同一套规则。
    """
    witnesses = witnesses or []
    if not witnesses:
        # 无见证邀约：阈值必须缺省
        if witness_threshold is not None:
            raise ValueError("无见证人时不能设置 witness_threshold")
        return

    if not (_WITNESS_MIN <= len(witnesses) <= _WITNESS_MAX):
        raise ValueError(
            f"见证人数量必须在 {_WITNESS_MIN}～{_WITNESS_MAX} 名之间"
        )
    if witness_threshold is None:
        raise ValueError("见证邀约必须提供 witness_threshold")
    if not (1 <= witness_threshold <= len(witnesses)):
        raise ValueError("witness_threshold 必须在 1～见证人数量之间")

    names = [w.name for w in witnesses]
    if len(set(names)) != len(names):
        raise ValueError("见证人名称不能重复")
    tokens = [w.token for w in witnesses]
    if len(set(tokens)) != len(tokens):
        raise ValueError("见证人令牌不能重复")


class CreateChainRequest(BaseModel):
    file_hash: str = Field(..., description="交接文件的 SHA-256 摘要（64 位十六进制）")
    first_holder: str = Field(..., description="首位持有人标识")

    @field_validator("file_hash")
    @classmethod
    def _check_file_hash(cls, v: str) -> str:
        return _validate_hex64(v, "file_hash")

    @field_validator("first_holder")
    @classmethod
    def _check_first_holder(cls, v: str) -> str:
        return _validate_name(v, "first_holder")


class HandoverRequest(BaseModel):
    chain_id: str = Field(..., description="交接链 ID")
    tail_hash: str = Field(..., description="客户端认定的当前链尾哈希")
    receiver: str = Field(..., description="接收方标识")
    nonce: str = Field(..., description="一次性随机数，同一链内不可重复（防重放）")

    @field_validator("chain_id")
    @classmethod
    def _check_chain_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("chain_id 不能为空")
        return v

    @field_validator("tail_hash")
    @classmethod
    def _check_tail_hash(cls, v: str) -> str:
        return _validate_hex64(v, "tail_hash")

    @field_validator("receiver")
    @classmethod
    def _check_receiver(cls, v: str) -> str:
        return _validate_name(v, "receiver")

    @field_validator("nonce")
    @classmethod
    def _check_nonce(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("nonce 不能为空（一次性随机数）")
        if len(v) > _NONCE_MAX:
            raise ValueError(f"nonce 长度不能超过 {_NONCE_MAX}")
        return v


class HandoverBatchItem(BaseModel):
    """批量移交请求中的单条链：链 ID、客户端认定的当前链尾与一次性随机数。"""

    chain_id: str = Field(..., description="交接链 ID（同批内不得重复）")
    tail_hash: str = Field(..., description="客户端认定的该链当前链尾哈希")
    nonce: str = Field(
        ..., description="一次性随机数，该链内不可重复（防重放）；不同链相互独立"
    )

    @field_validator("chain_id")
    @classmethod
    def _check_chain_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("chain_id 不能为空")
        return v

    @field_validator("tail_hash")
    @classmethod
    def _check_tail_hash(cls, v: str) -> str:
        return _validate_hex64(v, "tail_hash")

    @field_validator("nonce")
    @classmethod
    def _check_nonce(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("nonce 不能为空（一次性随机数）")
        if len(v) > _NONCE_MAX:
            raise ValueError(f"nonce 长度不能超过 {_NONCE_MAX}")
        return v


class HandoverBatchRequest(BaseModel):
    """POST /handover-batches 请求体：同一接收方 + 2～20 条不重复的链。"""

    receiver: str = Field(..., description="同一接收方标识（整批共用）")
    items: list[HandoverBatchItem] = Field(
        ...,
        min_length=_HANDOVER_BATCH_MIN,
        max_length=_HANDOVER_BATCH_MAX,
        description=(
            f"同批移交的链（{_HANDOVER_BATCH_MIN}～{_HANDOVER_BATCH_MAX} 条，"
            "chain_id 不得重复）"
        ),
    )

    @field_validator("receiver")
    @classmethod
    def _check_receiver(cls, v: str) -> str:
        return _validate_name(v, "receiver")

    @model_validator(mode="after")
    def _check_unique_chain_ids(self) -> "HandoverBatchRequest":
        ids = [item.chain_id for item in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("同批 chain_id 不得重复")
        return self


class InventoryReconcileItem(BaseModel):
    """交付清单核对请求中的单条链声明：链 ID、文件摘要、当前持有人与链尾哈希。"""

    chain_id: str = Field(..., description="交接链 ID（同批内不得重复）")
    file_hash: str = Field(
        ..., description="清单认定的该链文件 SHA-256 摘要（64 位十六进制）"
    )
    current_holder: str = Field(..., description="清单认定的该链当前持有人标识")
    tail_hash: str = Field(
        ..., description="清单认定的该链当前链尾哈希（64 位十六进制）"
    )

    @field_validator("chain_id")
    @classmethod
    def _check_chain_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("chain_id 不能为空")
        return v

    @field_validator("file_hash")
    @classmethod
    def _check_file_hash(cls, v: str) -> str:
        return _validate_hex64(v, "file_hash")

    @field_validator("current_holder")
    @classmethod
    def _check_current_holder(cls, v: str) -> str:
        return _validate_name(v, "current_holder")

    @field_validator("tail_hash")
    @classmethod
    def _check_tail_hash(cls, v: str) -> str:
        return _validate_hex64(v, "tail_hash")


class InventoryReconcileRequest(BaseModel):
    """POST /inventories/reconcile 请求体：2～100 条不重复的链声明。"""

    items: list[InventoryReconcileItem] = Field(
        ...,
        min_length=_INVENTORY_RECONCILE_MIN,
        max_length=_INVENTORY_RECONCILE_MAX,
        description=(
            f"待核对的交付清单（{_INVENTORY_RECONCILE_MIN}～"
            f"{_INVENTORY_RECONCILE_MAX} 条，chain_id 不得重复）"
        ),
    )

    @model_validator(mode="after")
    def _check_unique_chain_ids(self) -> "InventoryReconcileRequest":
        ids = [item.chain_id for item in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("同批 chain_id 不得重复")
        return self


class InventoryReconcileItemResult(BaseModel):
    """交付清单核对结果中的单条链：逐项状态，严格按请求 items 顺序。"""

    chain_id: str
    status: Literal[
        "matched", "missing", "file_mismatch", "holder_mismatch", "tail_mismatch"
    ] = Field(
        ...,
        description=(
            "逐项核对状态：missing（链不存在）→ file_mismatch（文件摘要不符）→ "
            "holder_mismatch（当前持有人不符）→ tail_mismatch（链尾哈希不符），"
            "按此顺序判定首个不符项；全部一致为 matched"
        ),
    )


class InventoryReconcileResult(BaseModel):
    snapshot_event_id: int = Field(
        ...,
        description=(
            "本次核对锚定的事件快照：同一读事务内当时最大的事件数据库 id"
            "（MAX(events.id)，全库无事件时为 0）；核对期间新追加的事件 id 均"
            "大于该值，不影响本次结果"
        ),
    )
    matched_count: int = Field(
        ..., description="状态为 matched（四项完全一致）的条数"
    )
    count: int = Field(..., description="本次核对的清单条数（等于请求 items 条数）")
    results: list[InventoryReconcileItemResult] = Field(
        ..., description="各链逐项核对状态，严格按请求 items 顺序"
    )


class EventOut(BaseModel):
    seq: int
    prev_hash: str
    giver: str
    receiver: str
    nonce: str
    event_hash: str
    canonical: str = Field(..., description="计算该哈希所用的紧凑 JSON 原文，便于审计复核")
    created_at: str


class ChainOut(BaseModel):
    chain_id: str
    file_hash: str
    first_holder: str
    tail_hash: str
    current_holder: str
    length: int = Field(..., description="事件总数（含创世记录，因此首次创建后为 1）")
    created_at: str
    events: list[EventOut]


class ChainSummary(BaseModel):
    chain_id: str
    file_hash: str
    current_holder: str
    tail_hash: str
    length: int
    created_at: str


class AuditEventOut(BaseModel):
    """审计检索命中的交接事件（跨链统一视图，含事件数据库 id 与链 id）。"""

    id: int = Field(
        ...,
        description=(
            "事件数据库自增 id：与 created_at 共同构成跨链稳定排序键，"
            "同秒内多个事件按 id 定先后；仅追加、不回收"
        ),
    )
    chain_id: str = Field(..., description="事件所属交接链 ID")
    seq: int = Field(..., description="链内序号：0 为创世事件，>=1 为移交事件")
    giver: str = Field(
        ...,
        description=(
            "交出方标识；创世事件为空串（其持有人以链条 first_holder 表达，"
            "仅在 role=either 时参与持有人匹配）"
        ),
    )
    receiver: str = Field(..., description="接收方标识；创世事件为空串")
    nonce: str = Field(..., description="一次性随机数；创世事件为空串")
    prev_hash: str = Field(..., description="前序事件哈希；创世事件为空串")
    event_hash: str
    created_at: str = Field(..., description="事件创建时刻（UTC ISO-8601）")


class AuditEventsPage(BaseModel):
    events: list[AuditEventOut] = Field(
        ...,
        description=(
            "本页事件：跨链按 (created_at, id) 升序；数量不超过 limit。"
            "结果锚定首次查询时刻的快照（当时最大事件 id），翻页期间新追加的"
            "移交不会插入本查询，不会重复也不会漏项"
        ),
    )
    next_cursor: Optional[str] = Field(
        ...,
        description=(
            "下一页不透明游标：内含过滤条件摘要、快照上界与末条排序键，并用"
            " AUDIT_CURSOR_SECRET 做 HMAC 签名；回传时须保持 chain_id/holder/"
            "role/created_from/created_to 与首次查询完全一致（limit 可调整）。"
            "游标损坏/被篡改返回 400 invalid_cursor_signature，与本次过滤条件"
            "不符返回 400 cursor_filter_mismatch，均不会回退为新查询；"
            "无更多项时为 null"
        ),
    )


class HandoverResult(BaseModel):
    chain_id: str
    seq: int
    prev_hash: str
    giver: str
    receiver: str
    nonce: str
    event_hash: str
    canonical: str
    tail_hash: str
    current_holder: str


class HandoverBatchResultItem(BaseModel):
    """批量移交结果中的单条链：新序号与推进后的新链尾哈希。"""

    chain_id: str
    seq: int = Field(..., description="该链新追加移交事件的序号")
    tail_hash: str = Field(
        ..., description="该链推进后的新链尾哈希（即新事件哈希）"
    )


class HandoverBatchResult(BaseModel):
    receiver: str = Field(..., description="整批共用的接收方标识")
    count: int = Field(
        ..., description="本批成功移交的链数（等于请求 items 条数，2～20）"
    )
    results: list[HandoverBatchResultItem] = Field(
        ..., description="各链移交结果，严格按请求 items 顺序"
    )


class VerifyResult(BaseModel):
    chain_id: str
    valid: bool
    length: int
    expected_next_seq: int
    first_broken_seq: Optional[int] = Field(
        None, description="首个断裂事件的序号；链条头部信息被篡改时为 null"
    )
    reason: Optional[str] = None
    recomputed_tail_hash: Optional[str] = None
    stored_tail_hash: Optional[str] = None


class ProofPathStep(BaseModel):
    direction: Literal["left", "right"] = Field(
        ...,
        description=(
            "兄弟节点相对当前节点的方向：left 表示先拼兄弟哈希再拼当前哈希"
            "（当前节点为右孩子），right 反之。奇数层末节点与自身复制配对时"
            "为 right，且 sibling_hash 等于当前节点哈希。"
        ),
    )
    sibling_hash: str = Field(
        ...,
        description=(
            "该层兄弟节点哈希（64 位十六进制 Merkle 节点摘要）；路径自叶子向根"
        ),
    )

    @field_validator("sibling_hash")
    @classmethod
    def _check_sibling_hash(cls, v: str) -> str:
        return _validate_hex64(v, "sibling_hash")


class EventProofOut(BaseModel):
    chain_id: str
    event: EventOut = Field(..., description="被证明包含的交接事件（seq / event_hash 等）")
    leaf_hash: str = Field(
        ...,
        description=(
            "Merkle 叶子摘要：SHA256(\"merkle-leaf:v1:\" + 规范 JSON"
            "{chain_id,event_hash,seq})，叶子绑定链、序号与事件哈希"
        ),
    )
    path: list[ProofPathStep] = Field(
        ...,
        description=(
            "自叶子向根的兄弟路径，每项给出兄弟哈希与左右方向；"
            "单事件链为空列表。内部节点为 SHA256(\"merkle-node:v1:\"+左+右)，"
            "奇数层末节点与自身复制配对"
        ),
    )
    event_count: int = Field(
        ..., description="生成证明时该链的事件总数（含创世记录）"
    )
    tail_hash: str = Field(
        ..., description="同一读事务下该链的当前链尾哈希（事件集合与链尾为同一快照）"
    )
    merkle_root: str = Field(
        ...,
        description=(
            "按当前事件集合计算的 Merkle 根；单事件链时即 leaf_hash"
        ),
    )
    snapshot_digest: str = Field(
        ...,
        description=(
            "链尾快照摘要：SHA256(\"merkle-snapshot:v1:\" + 规范 JSON"
            "{chain_id,event_count,merkle_root,tail_hash})，把证明锚定到本次快照"
        ),
    )


class ProofEventClaim(BaseModel):
    """校验请求中的事件声明：校验端只信链 ID、序号与事件哈希并重算叶子。"""

    seq: int = Field(..., description="被证明事件的序号（>= 0）")
    event_hash: str = Field(..., description="被证明事件的 SHA-256 摘要（64 位十六进制）")

    @field_validator("event_hash")
    @classmethod
    def _check_event_hash(cls, v: str) -> str:
        return _validate_hex64(v, "event_hash")

    model_config = {"extra": "ignore"}


class ProofVerifyRequest(BaseModel):
    """POST /proofs/verify 请求体。

    与 GET 证明响应同构，可将证明响应原样回填；服务端只依据本体重算叶子、
    路径与 snapshot_digest，**不读取数据库**。event 中仅 seq、event_hash
    参与重算，其余字段被忽略（服务端不据此信任事件内容）。
    """

    chain_id: str = Field(..., description="证明所属交接链 ID")
    event: ProofEventClaim
    leaf_hash: Optional[str] = Field(
        None,
        description=(
            "仅为便于把证明响应原样回填而保留：服务端始终自行重算叶子，"
            "该字段被忽略，不参与任何可信判定"
        ),
    )
    path: list[ProofPathStep] = Field(..., description="待校验的兄弟路径（自叶子向根）")
    event_count: int = Field(..., description="生成证明时声明的事件总数")
    tail_hash: str = Field(..., description="生成证明时快照中的当前链尾哈希")
    merkle_root: str = Field(..., description="生成证明时声明的 Merkle 根")
    snapshot_digest: str = Field(
        ..., description="生成证明时返回的链尾快照摘要，服务端自行重算后比对"
    )

    @field_validator("chain_id")
    @classmethod
    def _check_chain_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("chain_id 不能为空")
        return v

    @field_validator("tail_hash")
    @classmethod
    def _check_tail_hash(cls, v: str) -> str:
        return _validate_hex64(v, "tail_hash")

    @field_validator("merkle_root")
    @classmethod
    def _check_merkle_root(cls, v: str) -> str:
        return _validate_hex64(v, "merkle_root")

    @field_validator("snapshot_digest")
    @classmethod
    def _check_snapshot_digest(cls, v: str) -> str:
        return _validate_hex64(v, "snapshot_digest")

    @field_validator("leaf_hash")
    @classmethod
    def _check_leaf_hash(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _validate_hex64(v, "leaf_hash")


class ProofVerifyResult(BaseModel):
    valid: bool = Field(
        ...,
        description=(
            "仅当叶子/路径重算根 == merkle_root 且 snapshot_digest 重算一致、"
            "路径结构合法时为 true"
        ),
    )
    failed_stage: Optional[Literal[
        "event_count", "seq", "path", "merkle_root", "snapshot_digest"
    ]] = Field(
        None,
        description=(
            "首个失败阶段（按 event_count → seq → path → merkle_root → "
            "snapshot_digest 顺序）；valid=true 时为 null"
        ),
    )
    reason: Optional[str] = Field(None, description="失败原因说明；成功为 null")
    chain_id: str
    seq: int
    recomputed_leaf_hash: str = Field(..., description="按 chain_id/seq/event_hash 重算的叶子摘要")
    recomputed_merkle_root: Optional[str] = Field(
        None, description="按路径重算的 Merkle 根；路径结构非法时为 null"
    )
    recomputed_snapshot_digest: str = Field(
        ..., description="按声明根/链尾/事件数重算的快照摘要（始终计算）"
    )


class ProofNodeOut(BaseModel):
    """Merkle multiproof 的证明节点：以 (level, index) 坐标定位。"""

    level: int = Field(..., description="节点所在层：0 为叶子层，逐层向根递增")
    index: int = Field(..., description="节点在该层内的序号（0 起）")
    node_hash: str = Field(
        ..., description="该坐标处的 Merkle 节点摘要（64 位十六进制）"
    )


class EventBatchProofOut(BaseModel):
    chain_id: str
    events: list[EventOut] = Field(
        ...,
        description=(
            "被证明包含的交接事件（按 seq 升序、去重）；只包含被选事件，"
            "不泄露未选择事件"
        ),
    )
    event_count: int = Field(
        ..., description="生成证明时该链的事件总数（含创世记录）"
    )
    tail_hash: str = Field(
        ..., description="同一读事务下该链的当前链尾哈希（事件集合与链尾为同一快照）"
    )
    merkle_root: str = Field(
        ..., description="按当前事件集合计算的 Merkle 根；单事件链时即叶子哈希"
    )
    snapshot_digest: str = Field(
        ...,
        description=(
            "链尾快照摘要：SHA256(\"merkle-snapshot:v1:\" + 规范 JSON"
            "{chain_id,event_count,merkle_root,tail_hash})，把证明锚定到本次快照"
        ),
    )
    proof_nodes: list[ProofNodeOut] = Field(
        ...,
        description=(
            "重建根所需的最小兄弟节点集合：按 (level, index) 升序且不重复；"
            "奇数层末节点与自身复制配对，自复制位置不产生证明节点"
        ),
    )


class BatchProofEventClaim(BaseModel):
    """批量校验请求中的事件声明：校验端只信链 ID、序号与事件哈希并重算叶子。"""

    seq: int = Field(..., description="被证明事件的序号（>= 0）")
    event_hash: str = Field(..., description="被证明事件的 SHA-256 摘要（64 位十六进制）")

    @field_validator("event_hash")
    @classmethod
    def _check_event_hash(cls, v: str) -> str:
        return _validate_hex64(v, "event_hash")

    model_config = {"extra": "ignore"}


class ProofNodeInput(BaseModel):
    """批量校验请求中的证明节点：坐标与摘要，语义与 ProofNodeOut 一致。"""

    level: int = Field(..., description="节点所在层：0 为叶子层")
    index: int = Field(..., description="节点在该层内的序号（0 起）")
    node_hash: str = Field(..., description="该坐标处的 Merkle 节点摘要（64 位十六进制）")

    @field_validator("node_hash")
    @classmethod
    def _check_node_hash(cls, v: str) -> str:
        return _validate_hex64(v, "node_hash")

    model_config = {"extra": "ignore"}


class BatchProofVerifyRequest(BaseModel):
    """POST /proofs/verify-batch 请求体。

    与批量证明响应同构，可将证明响应原样回填；服务端只依据本体重算叶子、
    按节点坐标重建 Merkle 根与 snapshot_digest，**不读取数据库**。events
    中仅 seq、event_hash 参与重算，其余字段被忽略。
    """

    chain_id: str = Field(..., description="证明所属交接链 ID")
    events: list[BatchProofEventClaim] = Field(
        ..., description="被证明事件声明（1～20 个，序号不得重复）"
    )
    event_count: int = Field(..., description="生成证明时声明的事件总数")
    tail_hash: str = Field(..., description="生成证明时快照中的当前链尾哈希")
    merkle_root: str = Field(..., description="生成证明时声明的 Merkle 根")
    snapshot_digest: str = Field(
        ..., description="生成证明时返回的链尾快照摘要，服务端自行重算后比对"
    )
    proof_nodes: list[ProofNodeInput] = Field(
        ..., description="待校验的证明节点集合（按坐标参与重建，可为空列表）"
    )

    @field_validator("chain_id")
    @classmethod
    def _check_chain_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("chain_id 不能为空")
        return v

    @field_validator("tail_hash")
    @classmethod
    def _check_tail_hash(cls, v: str) -> str:
        return _validate_hex64(v, "tail_hash")

    @field_validator("merkle_root")
    @classmethod
    def _check_merkle_root(cls, v: str) -> str:
        return _validate_hex64(v, "merkle_root")

    @field_validator("snapshot_digest")
    @classmethod
    def _check_snapshot_digest(cls, v: str) -> str:
        return _validate_hex64(v, "snapshot_digest")


class BatchProofVerifyResult(BaseModel):
    valid: bool = Field(
        ...,
        description=(
            "仅当按坐标重建的根 == merkle_root 且 snapshot_digest 重算一致、"
            "证明节点最小且无冲突时为 true"
        ),
    )
    failed_stage: Optional[Literal[
        "event_count", "events", "proof_nodes", "merkle_root", "snapshot_digest"
    ]] = Field(
        None,
        description=(
            "首个失败阶段（按 event_count → events → proof_nodes → "
            "merkle_root → snapshot_digest 顺序）；valid=true 时为 null"
        ),
    )
    reason: Optional[str] = Field(None, description="失败原因说明；成功为 null")
    chain_id: str
    seqs: list[int] = Field(..., description="请求中声明的被证明事件序号（按请求顺序）")
    recomputed_leaf_hashes: list[str] = Field(
        ..., description="按 chain_id/seq/event_hash 重算的叶子摘要（与 events 同序）"
    )
    recomputed_merkle_root: Optional[str] = Field(
        None, description="按节点坐标重建的 Merkle 根；证明节点非法时为 null"
    )
    recomputed_snapshot_digest: str = Field(
        ..., description="按声明根/链尾/事件数重算的快照摘要（始终计算）"
    )


class WitnessInput(BaseModel):
    name: str = Field(..., description="见证人标识")
    token: str = Field(
        ...,
        description=(
            "该见证人的独立一次性令牌（表态凭证，明文仅在创建响应中返回一次，"
            "服务端只保存其 SHA-256 摘要）"
        ),
    )

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return _validate_name(v, "witness name")

    @field_validator("token")
    @classmethod
    def _check_token(cls, v: str) -> str:
        return _validate_token(v)


class OfferCreateRequest(BaseModel):
    chain_id: str = Field(..., description="交接链 ID")
    tail_hash: str = Field(..., description="发起邀约时锁定的当前链尾哈希")
    receiver: str = Field(..., description="指定接收方标识（仅其应获知确认码）")
    expires_in_seconds: int = Field(
        ..., description="邀约有效期（秒），自创建时刻起算", ge=1, le=_OFFER_TTL_MAX
    )
    confirmation_code: str = Field(
        ..., description="一次性确认码，服务端只保存其 SHA-256 摘要"
    )
    cancellation_code: Optional[str] = Field(
        default=None,
        description=(
            "可选一次性撤销码：提供后邀约可凭此码调用取消接口撤销，"
            "服务端只保存「绑定 offer_id 做域分隔」的 SHA-256 摘要；"
            "省略则创建不可撤销的兼容旧行为邀约"
        ),
    )
    witnesses: Optional[list[WitnessInput]] = Field(
        default=None,
        description=(
            "见证人列表（2～5 名，各持独立一次性令牌）；"
            "省略或为空则创建兼容旧行为的无见证邀约"
        ),
    )
    witness_threshold: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "见证通过阈值：接收方确认时同意数须达到该值且无拒绝；"
            "提供见证人时必填，合法范围 1～见证人数量（上限 5）"
        ),
    )

    @field_validator("chain_id")
    @classmethod
    def _check_chain_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("chain_id 不能为空")
        return v

    @field_validator("tail_hash")
    @classmethod
    def _check_tail_hash(cls, v: str) -> str:
        return _validate_hex64(v, "tail_hash")

    @field_validator("receiver")
    @classmethod
    def _check_receiver(cls, v: str) -> str:
        return _validate_name(v, "receiver")

    @field_validator("confirmation_code")
    @classmethod
    def _check_confirmation_code(cls, v: str) -> str:
        return _validate_code(v)

    @field_validator("cancellation_code")
    @classmethod
    def _check_cancellation_code(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        code = _validate_code(v, "cancellation_code")
        return code or None

    @model_validator(mode="after")
    def _check_witnesses(self) -> "OfferCreateRequest":
        _validate_witness_config(self.witnesses, self.witness_threshold)
        return self


class WitnessCreatedOut(BaseModel):
    witness_index: int = Field(..., description="见证人次序号（0 起，按提交顺序）")
    name: str
    token: str = Field(
        ...,
        description="该见证人的一次性令牌明文（仅此一次返回，服务端不存明文）",
    )


class OfferOut(BaseModel):
    offer_id: str
    chain_id: str
    receiver: str
    status: str = Field(
        ...,
        description=(
            "邀约状态：pending / accepted / expired / rejected / "
            "terminated / cancelled / replaced（后六者为终止或派生状态）"
        ),
    )
    expires_at: str
    created_at: str
    cancellable: bool = Field(
        default=False,
        description=(
            "是否可凭撤销码取消/替换：仅创建时提供了 cancellation_code 的邀约为 true，"
            "旧邀约恒为 false"
        ),
    )
    witness_mode: bool = Field(
        default=False,
        description="是否见证确认模式（无见证邀约恒为 false）",
    )
    witness_threshold: Optional[int] = Field(
        default=None, description="见证通过阈值；无见证邀约为 null"
    )
    witnesses: list[WitnessCreatedOut] = Field(
        default_factory=list,
        description="见证人及其一次性令牌明文（仅创建/替换响应返回）",
    )
    replaces: Optional[str] = Field(
        default=None,
        description=(
            "若本邀约由原子替换产生，为被替换的旧邀约 offer_id；"
            "普通创建为 null"
        ),
    )


class OfferConfirmRequest(BaseModel):
    confirmation_code: str = Field(..., description="发起邀约时约定的一次性确认码")

    @field_validator("confirmation_code")
    @classmethod
    def _check_confirmation_code(cls, v: str) -> str:
        return _validate_code(v)


class OfferCancelRequest(BaseModel):
    cancellation_code: str = Field(
        ..., description="发起邀约时可选提供的一次性撤销码（服务端只存其摘要）"
    )

    @field_validator("cancellation_code")
    @classmethod
    def _check_cancellation_code(cls, v: str) -> str:
        return _validate_code(v, "cancellation_code")


class OfferCancelResult(BaseModel):
    offer_id: str
    chain_id: str
    status: Literal["cancelled"] = Field(
        "cancelled", description="撤销成功后邀约状态固定为 cancelled"
    )
    cancellable: bool = Field(
        ..., description="固定为 true；不可撤销的邀约返回 409 而非本响应"
    )
    cancelled_at: str = Field(..., description="撤销时刻（UTC ISO-8601）")
    tail_hash: str = Field(
        ...,
        description=(
            "邀约锁定的链尾哈希；撤销不追加任何事件，它仍是当前链尾"
        ),
    )


class OfferReplaceRequest(BaseModel):
    cancellation_code: str = Field(
        ...,
        description=(
            "原邀约创建时提供的一次性撤销码：替换的授权凭证，服务端以绑定"
            "原 offer_id 的 SHA-256 摘要比对，错误返回 403 cancel_code_mismatch"
        ),
    )
    receiver: str = Field(
        ..., description="新邀约的指定接收方标识（仅其应获知新确认码）"
    )
    expires_in_seconds: int = Field(
        ...,
        description="新邀约有效期（秒），自替换时刻起算",
        ge=1,
        le=_OFFER_TTL_MAX,
    )
    confirmation_code: str = Field(
        ..., description="新邀约的一次性确认码，服务端只保存其 SHA-256 摘要"
    )
    new_cancellation_code: Optional[str] = Field(
        default=None,
        description=(
            "可选的新邀约一次性撤销码：提供后新邀约可继续凭此码撤销/再次替换，"
            "服务端只保存「绑定新 offer_id 做域分隔」的 SHA-256 摘要；"
            "省略则新邀约不可撤销/不可再替换（原撤销码随旧邀约一并作废）"
        ),
    )
    witnesses: Optional[list[WitnessInput]] = Field(
        default=None,
        description=(
            "新邀约的见证人列表（2～5 名，各持独立全新令牌）；省略或为空则"
            "新邀约为无见证邀约。旧邀约的见证人表态与令牌不会迁移、不可复用"
        ),
    )
    witness_threshold: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "新邀约的见证通过阈值：新接收方确认时同意数须达到该值且无拒绝；"
            "提供见证人时必填，合法范围 1～见证人数量（上限 5）"
        ),
    )

    @field_validator("cancellation_code")
    @classmethod
    def _check_cancellation_code(cls, v: str) -> str:
        return _validate_code(v, "cancellation_code")

    @field_validator("receiver")
    @classmethod
    def _check_receiver(cls, v: str) -> str:
        return _validate_name(v, "receiver")

    @field_validator("confirmation_code")
    @classmethod
    def _check_confirmation_code(cls, v: str) -> str:
        return _validate_code(v, "confirmation_code")

    @field_validator("new_cancellation_code")
    @classmethod
    def _check_new_cancellation_code(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        code = _validate_code(v, "new_cancellation_code")
        return code or None

    @model_validator(mode="after")
    def _check_witnesses(self) -> "OfferReplaceRequest":
        _validate_witness_config(self.witnesses, self.witness_threshold)
        return self


class WitnessStateOut(BaseModel):
    witness_index: int
    name: str
    decision: Optional[Literal["approved", "rejected"]] = Field(
        None, description="表态结果：approved（同意）/ rejected（拒绝）/ null（未表态）"
    )
    decided_at: Optional[str] = None


class OfferStatusOut(BaseModel):
    offer_id: str
    chain_id: str
    receiver: str
    status: str = Field(
        ...,
        description=(
            "pending（待确认）/ accepted（已确认）/ expired（已过期，派生）/ "
            "rejected（见证模式下有见证人拒绝，派生）/ "
            "terminated（链尾已推进，派生）/ cancelled（已撤销，存储状态）/ "
            "replaced（已被原子替换，存储状态）"
        ),
    )
    expires_at: str
    created_at: str
    accepted_at: Optional[str] = None
    accepted_seq: Optional[int] = Field(
        None, description="确认后追加的移交事件序号；未确认时为 null"
    )
    cancellable: bool = Field(
        default=False,
        description=(
            "是否可凭撤销码取消/替换；旧邀约（无撤销码）恒为 false，"
            "响应不含撤销码及其摘要"
        ),
    )
    cancelled_at: Optional[str] = Field(None, description="撤销时刻；未撤销为 null")
    replaces: Optional[str] = Field(
        default=None,
        description=(
            "替换关系：本邀约若由 POST /offers/{id}/replace 产生，为被替换的"
            "旧邀约 offer_id；否则为 null"
        ),
    )
    replaced_by: Optional[str] = Field(
        default=None,
        description=(
            "替换关系：本邀约若已被原子替换，为替换它而产生的新邀约 offer_id；"
            "否则为 null。仅返回 ID 关系，不含任何凭证摘要"
        ),
    )
    replaced_at: Optional[str] = Field(
        default=None, description="被原子替换的时刻；未被替换为 null"
    )
    witness_mode: bool = False
    witness_threshold: Optional[int] = Field(
        None, description="见证通过阈值；无见证邀约为 null"
    )
    witness_count: int = Field(
        0, description="见证人总数（无见证邀约为 0）"
    )
    approved_count: int = Field(0, description="已同意的见证人数")
    rejected_count: int = Field(0, description="已拒绝的见证人数")
    pending_count: int = Field(0, description="尚未表态的见证人数")
    witnesses: list[WitnessStateOut] = Field(
        default_factory=list,
        description="各见证人的表态状态，不含令牌及其摘要",
    )


class WitnessStatusOut(BaseModel):
    offer_id: str
    witness_mode: bool = Field(
        ..., description="固定为 true；对无见证邀约查询返回 404"
    )
    status: str = Field(
        ...,
        description=(
            "邀约派生状态：pending / accepted / expired / rejected / "
            "terminated / cancelled / replaced"
        ),
    )
    witness_threshold: int = Field(..., description="通过所需同意数")
    witness_count: int
    approved_count: int
    rejected_count: int
    pending_count: int
    witnesses: list[WitnessStateOut] = Field(
        ..., description="各见证人表态状态，绝不含令牌摘要"
    )


class WitnessDecisionRequest(BaseModel):
    token: str = Field(
        ..., description="创建邀约时由服务端分发的该见证人独立一次性令牌"
    )
    decision: Literal["approved", "rejected"] = Field(
        ..., description="表态：approved（同意）或 rejected（拒绝）"
    )

    @field_validator("token")
    @classmethod
    def _check_token(cls, v: str) -> str:
        return _validate_token(v)


class WitnessDecisionResult(BaseModel):
    offer_id: str
    witness_index: int
    name: str
    decision: Literal["approved", "rejected"]
    decided_at: str
    status: str = Field(
        ...,
        description=(
            "表态后邀约派生状态：pending（仍可继续表态/确认）/ rejected（已有拒绝）"
        ),
    )
    witness_threshold: int
    approved_count: int
    rejected_count: int
    pending_count: int


class OfferConfirmResult(BaseModel):
    offer_id: str
    chain_id: str
    seq: int
    prev_hash: str
    giver: str
    receiver: str
    nonce: str = Field(..., description="由邀约派生的一次性随机数（offer:<offer_id>）")
    event_hash: str
    canonical: str
    tail_hash: str
    current_holder: str


class SealCreateRequest(BaseModel):
    tail_hash: str = Field(
        ...,
        description=(
            "创建封存时锁定的当前链尾哈希：必须仍是最新链尾，"
            "链尾不匹配返回 409 tail_mismatch"
        ),
    )
    reason: str = Field(
        ...,
        description=(
            "争议/封存原因（必填，最多 1000 字符）；查询时原样返回，便于审计"
        ),
    )
    expires_in_seconds: int = Field(
        ...,
        description=(
            "封存有效期（秒），自创建时刻起算；到期自动失效，无需写操作，"
            f"上限 {_SEAL_TTL_MAX} 秒（30 天）"
        ),
        ge=1,
        le=_SEAL_TTL_MAX,
    )
    release_code: str = Field(
        ...,
        description=(
            "一次性解封码：服务端只保存「绑定 seal_id 做域分隔」的 SHA-256 摘要，"
            "明文绝不落库，凭此码可调用 POST /seals/{seal_id}/release 提前解封"
        ),
    )

    @field_validator("tail_hash")
    @classmethod
    def _check_tail_hash(cls, v: str) -> str:
        return _validate_hex64(v, "tail_hash")

    @field_validator("reason")
    @classmethod
    def _check_reason(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("reason 不能为空（须说明争议/封存原因）")
        if len(v) > _REASON_MAX:
            raise ValueError(f"reason 长度不能超过 {_REASON_MAX}")
        return v

    @field_validator("release_code")
    @classmethod
    def _check_release_code(cls, v: str) -> str:
        return _validate_code(v, "release_code")


class SealReleaseRequest(BaseModel):
    release_code: str = Field(
        ...,
        description=(
            "创建封存时提交的一次性解封码（服务端只存绑定 seal_id 的摘要）；"
            "错误返回 403 release_code_mismatch"
        ),
    )

    @field_validator("release_code")
    @classmethod
    def _check_release_code(cls, v: str) -> str:
        return _validate_code(v, "release_code")


class SealOut(BaseModel):
    seal_id: str
    chain_id: str
    status: Literal["active", "released", "expired"] = Field(
        ...,
        description=(
            "封存状态：active（生效中）/ released（已凭解封码提前解封，存储终态）/ "
            "expired（已过 expires_at 自动失效，派生状态）"
        ),
    )
    reason: str = Field(..., description="创建封存时提交的争议/封存原因")
    tail_hash: str = Field(
        ...,
        description="创建封存时锁定的链尾哈希；封存不改动哈希链，它通常仍是当前链尾",
    )
    created_at: str = Field(..., description="封存创建时刻（UTC ISO-8601）")
    expires_at: str = Field(
        ..., description="封存有效期截止时刻（UTC ISO-8601）；过后状态派生为 expired"
    )
    released_at: Optional[str] = Field(
        default=None,
        description="凭解封码提前解封的时刻（UTC ISO-8601）；未解封为 null",
    )


class HealthOut(BaseModel):
    status: str


class CustodyPeakInterval(BaseModel):
    """保管负荷达到正峰值的一个连续时间区间（半开 [start, end)，UTC ISO-8601）。"""

    start: str = Field(
        ...,
        description="区间起点（含），UTC ISO-8601，显式 +00:00 偏移",
    )
    end: str = Field(
        ...,
        description="区间终点（不含，半开区间），UTC ISO-8601，显式 +00:00 偏移",
    )


class CustodyLoadReportOut(BaseModel):
    """GET /reports/custody-load 保管负荷报表。"""

    holder: str = Field(..., description="被统计的持有人标识")
    start: str = Field(
        ...,
        description="统计窗口起点（含）：请求参数换算为 UTC 后的 ISO-8601",
    )
    end: str = Field(
        ...,
        description="统计窗口终点（不含，半开 [start, end)）：换算为 UTC 后的 ISO-8601",
    )
    snapshot_event_id: int = Field(
        ...,
        description=(
            "本次报表锚定的事件快照：同一读事务内当时最大的事件数据库 id"
            "（MAX(events.id)，全库无事件时为 0）；仅用 id <= 该值的事件重建，"
            "报表期间新追加的移交不进入结果"
        ),
    )
    peak_chain_count: int = Field(
        ...,
        description=(
            "窗口 [start, end) 内该人同时持有链数的最大值（正峰值）；"
            "窗口内从不持有时为 0"
        ),
    )
    total_chain_seconds: Union[int, float] = Field(
        ...,
        description=(
            "持有链数对时间（秒）的积分，即「链·秒」总量；整秒时为整数，"
            "窗口含亚秒精度时可为小数；无任何持有时为 0"
        ),
    )
    peak_intervals: list[CustodyPeakInterval] = Field(
        ...,
        description=(
            "达到正峰值 peak_chain_count 的全部合并连续区间（半开 [start, end)，"
            "相邻同峰值段已合并，按开始时刻升序，UTC ISO-8601）；"
            "峰值为 0（无持有记录）时为空列表"
        ),
    )
