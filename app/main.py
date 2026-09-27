"""FastAPI 入口：多人文件交接留痕（哈希链）服务。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import audit, config, database, reports
from .schemas import (
    AuditEventsPage,
    BatchProofVerifyRequest,
    BatchProofVerifyResult,
    ChainOut,
    ChainSummary,
    CreateChainRequest,
    CustodyLoadReportOut,
    EventBatchProofOut,
    EventProofOut,
    HandoverBatchRequest,
    HandoverBatchResult,
    HandoverRequest,
    HandoverResult,
    HealthOut,
    InventoryReconcileRequest,
    InventoryReconcileResult,
    OfferCancelRequest,
    OfferCancelResult,
    OfferConfirmRequest,
    OfferConfirmResult,
    OfferCreateRequest,
    OfferOut,
    OfferReplaceRequest,
    OfferStatusOut,
    ProofVerifyRequest,
    ProofVerifyResult,
    SealCreateRequest,
    SealOut,
    SealReleaseRequest,
    VerifyResult,
    WitnessDecisionRequest,
    WitnessDecisionResult,
    WitnessStatusOut,
)
from .service import (
    ChainError,
    append_handover,
    append_handover_batch,
    cancel_offer,
    confirm_offer,
    create_chain,
    create_offer,
    create_seal,
    get_chain,
    get_event_proof,
    get_events_proof,
    get_latest_seal,
    get_offer,
    get_seal,
    get_witness_status,
    list_chains,
    record_witness_decision,
    reconcile_inventory,
    release_seal,
    replace_offer,
    verify_batch_proof,
    verify_chain,
    verify_event_proof,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动即完成数据库初始化（建表 / 校验目录）
    database.init_db()
    yield


app = FastAPI(
    title="文件交接留痕服务",
    description="基于 SHA-256 哈希链的多人文件交接记录、查询与完整性校验。",
    version="1.10.0",
    lifespan=lifespan,
)


@app.exception_handler(ChainError)
async def chain_error_handler(request: Request, exc: ChainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": exc.message},
    )


@app.get("/health", response_model=HealthOut, tags=["meta"], summary="健康检查")
def health() -> HealthOut:
    return HealthOut(status="ok")


@app.get(
    "/audit/events",
    response_model=AuditEventsPage,
    tags=["audit"],
    summary="交接事件审计检索（跨链快照分页）",
    description=(
        "跨全部交接链检索事件：支持 chain_id、holder、role（giver / receiver / "
        "either，默认 either）、created_from、created_to（ISO-8601 闭区间，"
        "朴素时间按 UTC 解释）与 limit（1～100，默认 50）。结果跨链按 "
        "(created_at, id) 升序返回。\n\n"
        "创世事件（seq=0）没有交出/接收方：**仅在 role=either 时**按链条首位"
        "持有人 first_holder 匹配 holder；role=giver / receiver 时创世事件不"
        "入选。\n\n"
        "**快照一致性翻页**：首次查询在同一读事务内把「当时最大的事件数据库 "
        "id」固定为快照上界，随后只在该不可变集合内翻页；翻页期间新追加的"
        "移交不会进入本次查询，因而不会重复或漏项。响应 next_cursor 携带过滤"
        "条件摘要、快照上界与末条排序键，并以 AUDIT_CURSOR_SECRET 做 "
        "HMAC-SHA256 签名。后续请求通过 next_cursor 查询参数原样回传游标，且"
        " chain_id/holder/role/created_from/created_to 必须与首次查询完全一致"
        "（仅 limit 可变）；游标损坏或被篡改返回 **400 invalid_cursor_signature**，"
        "格式非法返回 **400 invalid_cursor**，过滤条件不符返回 **400 "
        "cursor_filter_mismatch**——**一律不会回退为一次新的快照查询**。"
        "created_from 晚于 created_to 返回 400 invalid_time_range；提供 role "
        "却未提供 holder 返回 400 role_requires_holder。无更多项时 "
        "next_cursor 为 null。"
    ),
)
def api_audit_events(
    chain_id: Optional[str] = Query(
        None,
        min_length=1,
        max_length=200,
        description="仅检索指定交接链的事件；省略则跨全部链",
    ),
    holder: Optional[str] = Query(
        None,
        min_length=1,
        max_length=200,
        description=(
            "按持有人标识过滤；须配合 role 使用（缺省 role 视为 either）"
        ),
    ),
    role: Optional[Literal["giver", "receiver", "either"]] = Query(
        None,
        description=(
            "持有人参与角色：giver（交出方）/ receiver（接收方）/ either"
            "（任一，默认）。创世事件仅在 either 时按首位持有人匹配"
        ),
    ),
    created_from: Optional[str] = Query(
        None,
        description="创建时间下界（含），ISO-8601，如 2026-09-01T00:00:00+00:00",
    ),
    created_to: Optional[str] = Query(
        None,
        description="创建时间上界（含），ISO-8601，如 2026-09-30T23:59:59+00:00",
    ),
    limit: int = Query(
        50,
        ge=1,
        le=100,
        description="每页事件数，1～100；翻页时可在各页之间调整",
    ),
    next_cursor: Optional[str] = Query(
        None,
        description=(
            "上一页响应中的 next_cursor 原样回传；过滤条件须与首次查询一致，"
            "游标被篡改或条件不符返回 400 且不会回退为新查询"
        ),
    ),
) -> AuditEventsPage:
    return AuditEventsPage(
        **audit.search_audit_events(
            chain_id=chain_id,
            holder=holder,
            role=role,
            created_from=created_from,
            created_to=created_to,
            limit=limit,
            cursor=next_cursor,
        )
    )


@app.get(
    "/reports/custody-load",
    response_model=CustodyLoadReportOut,
    tags=["reports"],
    summary="保管负荷报表：持有人在时间窗内同时持有链数的峰值、区间与积分（只读）",
    description=(
        "查询参数 holder（持有人标识）、start / end（统计窗口，半开区间 "
        "**[start, end)**）。start / end **必须携带时区**（如 "
        "2026-09-01T00:00:00+08:00 或 ...Z），服务端统一换算为 UTC；时间非法、"
        "缺时区、start >= end 或 **end 晚于查询时刻**均返回 400 "
        "invalid_time_range。\n\n"
        "服务端在**同一个 SQLite 读事务**内先把「当时最大的事件数据库 id」"
        "固定为 snapshot_event_id（全库无事件时为 0），随后**仅用该快照**"
        "重建全库该人的持有区间：创世事件从链条 first_holder 起持有，移交事件"
        "以 receiver 接续为新持有人，事件按 (created_at, 事件数据库 id) 排序；"
        "**同一时刻（同一 created_at）的多个变化先合并**，只比较该时刻前后的"
        "净状态，时刻中间的瞬时交接不产生时长——每条链在任一时刻至多计一次，"
        "自转交（receiver 与原持有人相同）不增加负荷。重建先完整还原窗口之前"
        "的持有状态，再把各链的半开持有区间裁剪到 [start, end)。\n\n"
        "响应返回：peak_chain_count（窗口内最大同时持有链数）、"
        "peak_intervals（达到该**正峰值**的全部**合并连续区间**，半开 "
        "[start, end)，UTC ISO-8601，相邻同峰值段合并）、"
        "total_chain_seconds（持有链数对秒数的积分，即「链·秒」，整秒为整数、"
        "含亚秒时可为小数）。该人窗口内无任何持有记录（或全部为零时长瞬态）时"
        "peak_chain_count=0、total_chain_seconds=0、peak_intervals 为空列表。\n\n"
        "本报表**只读**：不追加事件、不推进链尾、不改变任何交接流程；报表期间"
        "新追加的事件 id 均大于 snapshot_event_id，不影响本次结果。"
    ),
)
def api_custody_load(
    holder: str = Query(
        ...,
        min_length=1,
        max_length=200,
        description="被统计的持有人标识（精确匹配 chains.first_holder / events.receiver）",
    ),
    start: str = Query(
        ...,
        description="统计窗口起点（含），带时区的 ISO-8601，如 2026-09-01T00:00:00+08:00",
    ),
    end: str = Query(
        ...,
        description="统计窗口终点（不含，半开 [start, end)），带时区的 ISO-8601；不得晚于查询时刻",
    ),
) -> CustodyLoadReportOut:
    return CustodyLoadReportOut(**reports.custody_load(holder, start, end))


@app.post(
    "/chains",
    response_model=ChainOut,
    status_code=201,
    tags=["chains"],
    summary="创建交接链",
    description="提交文件 SHA-256 摘要与首位持有人，生成序号 0 的创世记录并返回其哈希。",
)
def api_create_chain(req: CreateChainRequest) -> ChainOut:
    return ChainOut(**create_chain(req.file_hash, req.first_holder))


@app.post(
    "/handovers",
    response_model=HandoverResult,
    status_code=201,
    tags=["chains"],
    summary="执行一次移交（原子追加）",
    description=(
        "必须携带当前链尾哈希 tail_hash、接收方 receiver 与一次性随机数 nonce。"
        "交出方由服务端按当前持有人确定。链尾不匹配或随机数重放返回 409，不产生分叉。"
    ),
)
def api_handover(req: HandoverRequest) -> HandoverResult:
    return HandoverResult(
        **append_handover(req.chain_id, req.tail_hash, req.receiver, req.nonce)
    )


@app.post(
    "/handover-batches",
    response_model=HandoverBatchResult,
    status_code=201,
    tags=["chains"],
    summary="批量移交：同一批 2～20 条链一次移交给同一接收方（整批原子）",
    description=(
        "请求携带同一 receiver 与每条链的 chain_id、tail_hash、nonce"
        "（2～20 条，同批 chain_id 不得重复，重复或条数越界返回 422）。"
        "服务端在同一个 SQLite 立即事务内按请求顺序逐链完成与单链移交一致的"
        "校验（存在性 → 封存门禁 → 链尾匹配 → 随机数查重）并追加事件、推进"
        "各链尾：任一链不存在（404）、处于封存生效期（409 chain_on_hold）、"
        "链尾过期（409 tail_mismatch）或随机数重放（409 nonce_replay）时"
        "**整批失败、整体回滚，任何链都不新增事件**。全部成功才整体提交，"
        "成功响应 results 按请求 items 顺序返回每条链的新序号 seq 与新链尾 "
        "tail_hash（哈希规则与单链移交完全相同）。"
    ),
)
def api_handover_batch(req: HandoverBatchRequest) -> HandoverBatchResult:
    return HandoverBatchResult(
        **append_handover_batch(
            req.receiver, [item.model_dump() for item in req.items]
        )
    )


@app.post(
    "/inventories/reconcile",
    response_model=InventoryReconcileResult,
    tags=["inventories"],
    summary="交付清单核对：同一读事务快照比对 2～100 条链（只读）",
    description=(
        "收货方按交付清单批量核对 2～100 条链（同批 chain_id 不得重复，"
        "重复或条数越界返回 422）。服务端在**同一个 SQLite 读事务**内读取"
        "各链链头与当时最大的事件数据库 id（MAX(events.id)，全库无事件时为 0），"
        "返回 snapshot_event_id、matched_count 与逐项状态：链不存在为 missing，"
        "否则依 file_mismatch（文件摘要不符）→ holder_mismatch（当前持有人不符）"
        "→ tail_mismatch（链尾哈希不符）顺序取首个不符项，四项完全一致为 "
        "matched。结果严格保持输入顺序。本接口只读：不追加事件、不推进链尾。"
    ),
)
def api_reconcile_inventory(req: InventoryReconcileRequest) -> InventoryReconcileResult:
    return InventoryReconcileResult(
        **reconcile_inventory([item.model_dump() for item in req.items])
    )


@app.post(
    "/offers",
    response_model=OfferOut,
    status_code=201,
    tags=["offers"],
    summary="发起待接收移交邀约（可选 2～5 名见证人、可选撤销码）",
    description=(
        "以最新链尾 tail_hash 锁定邀约，指定 receiver、有效期（秒）与一次性确认码。"
        "服务端只保存确认码的 SHA-256 摘要，返回 offer_id 供接收方确认。"
        "可选提交 cancellation_code：服务端仅保存绑定 offer_id 做域分隔的 SHA-256 "
        "撤销码摘要，此后可凭码调用 POST /offers/{offer_id}/cancel 撤销或调用 "
        "POST /offers/{offer_id}/replace 原子替换；省略则创建不可撤销/不可替换"
        "的兼容旧行为邀约。"
        "可选提交 2～5 名见证人及通过阈值 witness_threshold：每位见证人获得一个"
        "独立一次性令牌（仅在本响应中明文返回一次，服务端只存其 SHA-256 摘要）。"
    ),
)
def api_create_offer(req: OfferCreateRequest) -> OfferOut:
    witnesses = (
        [{"name": w.name, "token": w.token} for w in req.witnesses]
        if req.witnesses
        else None
    )
    return OfferOut(
        **create_offer(
            req.chain_id,
            req.tail_hash,
            req.receiver,
            req.expires_in_seconds,
            req.confirmation_code,
            witnesses=witnesses,
            witness_threshold=req.witness_threshold,
            cancellation_code=req.cancellation_code,
        )
    )


@app.post(
    "/offers/{offer_id}/confirm",
    response_model=OfferConfirmResult,
    status_code=201,
    tags=["offers"],
    summary="确认邀约（原子追加移交事件）",
    description=(
        "接收方凭 offer_id 与一次性确认码确认。服务端在同一 SQLite 立即事务中校验"
        "邀约未过期、未使用、未撤销且链尾仍匹配；见证模式下还须无见证人拒绝且同意数达到"
        "阈值，然后才追加唯一移交事件并将邀约置为 accepted。过期、重复确认、"
        "见证拒绝、同意数不足、链尾已推进、邀约已撤销或已被替换均返回 409，不写事件。"
    ),
)
def api_confirm_offer(offer_id: str, req: OfferConfirmRequest) -> OfferConfirmResult:
    return OfferConfirmResult(**confirm_offer(offer_id, req.confirmation_code))


@app.post(
    "/offers/{offer_id}/cancel",
    response_model=OfferCancelResult,
    tags=["offers"],
    summary="凭一次性撤销码撤销邀约（不改动哈希链）",
    description=(
        "发起方凭 offer_id 与创建时可选提供的 cancellation_code 撤销邀约。服务端在"
        "同一 SQLite 立即事务中与接收方确认、见证人表态互斥地校验：邀约仍 pending、"
        "链尾未推进、未过期且无见证人拒绝，然后才将邀约置为 cancelled，绝不追加任何"
        "链上事件。撤销码错误返回 403 cancel_code_mismatch；邀约已 accepted、"
        "rejected、expired、terminated、cancelled、replaced，或创建时未提供撤销码"
        "（旧邀约），均返回 409 offer_not_cancellable 且不改写链。详情与见证查询会"
        "返回 cancelled 状态，但绝不返回撤销码摘要。"
    ),
)
def api_cancel_offer(offer_id: str, req: OfferCancelRequest) -> OfferCancelResult:
    return OfferCancelResult(**cancel_offer(offer_id, req.cancellation_code))


@app.post(
    "/offers/{offer_id}/replace",
    response_model=OfferOut,
    status_code=201,
    tags=["offers"],
    summary="凭原撤销码原子替换待处理邀约",
    description=(
        "发起方凭原邀约创建时提供的撤销码提交新邀约参数：新的 receiver、"
        "有效期 expires_in_seconds、一次性确认码 confirmation_code，以及可选的"
        "新撤销码 new_cancellation_code 与可选的全新见证配置（2～5 名见证人 + "
        "阈值）。服务端在同一个 SQLite 立即事务中依次确认原邀约仍 pending、"
        "未过期、锁定链尾未推进且无见证人拒绝、原撤销码摘要匹配，然后才将原"
        "邀约标记为 replaced 并创建锁定同一链尾的新邀约（replaces 指向原邀约）；"
        "任一校验失败整体回滚，绝不产生半替换。替换与确认、撤销、见证人表态"
        "互斥，竞争终态中至多一个成功。原撤销码错误返回 403 cancel_code_mismatch；"
        "非可替换状态（accepted / cancelled / replaced / expired / terminated / "
        "rejected，或原邀约创建时未提供撤销码）返回 409 offer_not_replaceable。"
        "新邀约的确认码/撤销码/见证令牌全部重新生成与旧凭证互不通用；响应与"
        "详情接口只暴露 replaces/replaced_by 关系，不泄露任何凭证摘要。"
    ),
)
def api_replace_offer(offer_id: str, req: OfferReplaceRequest) -> OfferOut:
    witnesses = (
        [{"name": w.name, "token": w.token} for w in req.witnesses]
        if req.witnesses
        else None
    )
    return OfferOut(
        **replace_offer(
            offer_id,
            req.cancellation_code,
            req.receiver,
            req.expires_in_seconds,
            req.confirmation_code,
            new_cancellation_code=req.new_cancellation_code,
            witnesses=witnesses,
            witness_threshold=req.witness_threshold,
        )
    )


@app.post(
    "/offers/{offer_id}/witness-decisions",
    response_model=WitnessDecisionResult,
    status_code=201,
    tags=["offers"],
    summary="见证人凭一次性令牌表态（同意 / 拒绝）",
    description=(
        "见证模式邀约的见证人凭创建时分发的独立一次性令牌表态 approved 或 rejected。"
        "服务端只保存令牌摘要，同一见证人至多表态一次（重复使用令牌返回 409，"
        "令牌错误返回 403）。任一拒绝使邀约终止；邀约过期、被撤销、被替换或链尾"
        "推进后表态返回 409。响应不含令牌摘要。"
    ),
)
def api_witness_decision(
    offer_id: str, req: WitnessDecisionRequest
) -> WitnessDecisionResult:
    return WitnessDecisionResult(
        **record_witness_decision(offer_id, req.token, req.decision)
    )


@app.get(
    "/offers/{offer_id}/witnesses",
    response_model=WitnessStatusOut,
    tags=["offers"],
    summary="查询见证邀约的见证人表态状态",
    description=(
        "返回见证邀约各见证人的表态结果与同意/拒绝/待表态计数、派生邀约状态，"
        "绝不返回令牌及其摘要。无见证邀约返回 404。"
    ),
)
def api_get_witness_status(offer_id: str) -> WitnessStatusOut:
    return WitnessStatusOut(**get_witness_status(offer_id))


@app.get(
    "/offers/{offer_id}",
    response_model=OfferStatusOut,
    tags=["offers"],
    summary="查询单个邀约状态",
    description=(
        "返回邀约状态（pending / accepted / expired / rejected / terminated / "
        "cancelled / replaced，后五者为派生或存储终态）、确认/见证结果与"
        "replaces / replaced_by 替换关系（各自只给 offer_id，可据此串联替换链），"
        "不含确认码、撤销码、令牌及其任何摘要；cancellable 仅表示该邀约创建时"
        "是否提供过撤销码。"
    ),
)
def api_get_offer(offer_id: str) -> OfferStatusOut:
    return OfferStatusOut(**get_offer(offer_id))


@app.get(
    "/chains",
    response_model=list[ChainSummary],
    tags=["chains"],
    summary="列出全部交接链",
)
def api_list_chains() -> list[ChainSummary]:
    return [ChainSummary(**c) for c in list_chains()]


@app.get(
    "/chains/{chain_id}",
    response_model=ChainOut,
    tags=["chains"],
    summary="查询链条（含全部事件）",
)
def api_get_chain(chain_id: str) -> ChainOut:
    return ChainOut(**get_chain(chain_id))


@app.get(
    "/chains/{chain_id}/verify",
    response_model=VerifyResult,
    tags=["chains"],
    summary="完整性校验",
    description="逐条重算哈希并校验前序指针，结果给出首个断裂序号 first_broken_seq。",
)
def api_verify_chain(chain_id: str) -> VerifyResult:
    return VerifyResult(**verify_chain(chain_id))


@app.get(
    "/chains/{chain_id}/events/{seq}/proof",
    response_model=EventProofOut,
    tags=["proofs"],
    summary="生成单个交接事件的 Merkle 包含证明（可选择披露）",
    description=(
        "根据该链**当前事件集合**为序号 seq 的事件生成 Merkle 包含证明。叶子为"
        ' SHA256("merkle-leaf:v1:" + 规范 JSON{chain_id,seq,event_hash})，'
        '绑定链、序号与事件哈希；内部节点为 SHA256("merkle-node:v1:" + 左 + 右)'
        "（左右均为定长 64 位十六进制），奇数层末节点与自身复制配对；单事件链"
        "根即叶子。响应返回事件、自叶子向根的左右方向兄弟路径 path、事件数"
        "event_count、同一读事务快照下的当前 tail_hash、merkle_root，以及"
        ' SHA256("merkle-snapshot:v1:" + 规范 JSON'
        "{chain_id,event_count,merkle_root,tail_hash}) 得到的 snapshot_digest。"
        "事件与链尾在同一读事务内取得；链条不存在返回 404，seq 越界返回 "
        "404 event_not_found。"
    ),
)
def api_get_event_proof(chain_id: str, seq: int) -> EventProofOut:
    return EventProofOut(**get_event_proof(chain_id, seq))


@app.get(
    "/chains/{chain_id}/events/proof",
    response_model=EventBatchProofOut,
    tags=["proofs"],
    summary="批量生成多个交接事件的 Merkle multiproof（可选择披露）",
    description=(
        "对去重且升序的 1～20 个序号（查询参数 seq 可重复传入）生成**一份**"
        " Merkle multiproof：事件集合与当前链尾在同一读事务内取得（同一快照），"
        "哈希规则与单事件证明一致（叶子绑定 chain_id/seq/event_hash，内部节点"
        '为 SHA256("merkle-node:v1:"+左+右)，奇数层末节点与自身复制配对）。'
        "响应返回被选事件（按 seq 升序，绝不包含未选择事件）、event_count、"
        "tail_hash、merkle_root、snapshot_digest，以及按 (level, index) 升序"
        "且不重复的最小 proof_nodes（level 0 为叶子层；奇数层自复制位置不产生"
        "证明节点）。去重后超过 20 个序号返回 400 too_many_seqs；链条不存在"
        "返回 404；任一序号越界（负数或不小于当前事件数）返回 404 "
        "event_not_found。"
    ),
)
def api_get_events_proof(
    chain_id: str,
    seq: list[int] = Query(
        ...,
        description=(
            "要证明的事件序号，可重复传入（如 ?seq=0&seq=3&seq=7）；"
            "服务端去重并升序排列，去重后 1～20 个"
        ),
    ),
) -> EventBatchProofOut:
    return EventBatchProofOut(**get_events_proof(chain_id, seq))


@app.post(
    "/proofs/verify",
    response_model=ProofVerifyResult,
    tags=["proofs"],
    summary="离线校验 Merkle 包含证明（不读取数据库）",
    description=(
        "仅依据请求体重算叶子（chain_id/seq/event_hash）、左右兄弟路径与"
        " snapshot_digest：按 event_count → seq → path → merkle_root → "
        "snapshot_digest 的顺序给出首个失败阶段 failed_stage，全部通过 valid "
        "才为 true。请求体与 GET 证明响应同构，可原样回填；event 中除 "
        "seq、event_hash 外的字段被忽略。本接口不读取数据库，可在任何离线"
        "环境复算。"
    ),
)
def api_verify_proof(req: ProofVerifyRequest) -> ProofVerifyResult:
    return ProofVerifyResult(
        **verify_event_proof(
            chain_id=req.chain_id,
            seq=req.event.seq,
            event_hash=req.event.event_hash,
            path=[step.model_dump() for step in req.path],
            event_count=req.event_count,
            tail_hash=req.tail_hash,
            merkle_root=req.merkle_root,
            snapshot_digest_claim=req.snapshot_digest,
        )
    )


@app.post(
    "/proofs/verify-batch",
    response_model=BatchProofVerifyResult,
    tags=["proofs"],
    summary="离线校验批量 Merkle multiproof（不读取数据库）",
    description=(
        "仅依据请求体按节点坐标重建 Merkle 根并重算 snapshot_digest：按 "
        "event_count → events → proof_nodes → merkle_root → snapshot_digest "
        "的顺序给出首个失败阶段 failed_stage，全部通过 valid 才为 true。"
        "重复或越界序号、缺失或多余节点、坐标冲突（重复坐标或与重算值不一致）"
        "以及不符合奇数层自复制规则的证明一律拒绝。请求体与批量证明响应同构，"
        "可原样回填；events 中除 seq、event_hash 外的字段被忽略。本接口不读取"
        "数据库，可在任何离线环境复算。"
    ),
)
def api_verify_batch_proof(req: BatchProofVerifyRequest) -> BatchProofVerifyResult:
    return BatchProofVerifyResult(
        **verify_batch_proof(
            chain_id=req.chain_id,
            events=[event.model_dump() for event in req.events],
            event_count=req.event_count,
            tail_hash=req.tail_hash,
            merkle_root=req.merkle_root,
            snapshot_digest_claim=req.snapshot_digest,
            proof_nodes=[node.model_dump() for node in req.proof_nodes],
        )
    )


@app.post(
    "/chains/{chain_id}/seals",
    response_model=SealOut,
    status_code=201,
    tags=["seals"],
    summary="对存在争议的交接链创建封存",
    description=(
        "持有人携带当前链尾 tail_hash、争议原因 reason、有效期 "
        "expires_in_seconds（秒，上限 30 天）与一次性解封码 release_code 创建封存。"
        "服务端只保存「绑定 seal_id 做域分隔」的 SHA-256 解封码摘要，明文绝不落库，"
        "且封存不追加任何哈希链事件。同一链同时只能有一个生效封存：重复封存返回 "
        "409 chain_seal_active；链尾不匹配返回 409 tail_mismatch；链条不存在返回 404。"
        "封存生效期间，直接移交（POST /handovers）以及邀约的创建、替换、确认都在"
        "各自现有 SQLite 立即事务内返回 409 chain_on_hold，且不写事件或新邀约；"
        "封存创建与这些操作并发时由立即事务与部分唯一索引保证只有一方成功。"
    ),
)
def api_create_seal(chain_id: str, req: SealCreateRequest) -> SealOut:
    return SealOut(
        **create_seal(
            chain_id,
            req.tail_hash,
            req.reason,
            req.expires_in_seconds,
            req.release_code,
        )
    )


@app.post(
    "/seals/{seal_id}/release",
    response_model=SealOut,
    tags=["seals"],
    summary="凭一次性解封码提前解封",
    description=(
        "持有人（或任何持有解封码的一方）凭 seal_id 与创建时提交的 "
        "release_code 提前解封。服务端在同一 SQLite 立即事务内校验封存仍 active、"
        "未过期且解封码摘要匹配，然后将封存置为 released 并记录 released_at，"
        "解封不追加任何链上事件。解封码错误返回 403 release_code_mismatch；封存不"
        "存在返回 404；已解封返回 409 seal_already_released；已过期返回 409 "
        "seal_expired（过期自动失效，无需再解封）。任何响应都不返回解封码摘要。"
    ),
)
def api_release_seal(seal_id: str, req: SealReleaseRequest) -> SealOut:
    return SealOut(**release_seal(seal_id, req.release_code))


@app.get(
    "/seals/{seal_id}",
    response_model=SealOut,
    tags=["seals"],
    summary="按封存 ID 查询封存状态",
    description=(
        "返回封存状态 active（生效中）/ released（已提前解封）/ expired（已过期"
        "自动失效，派生状态），以及原因 reason、锁定链尾 tail_hash 与创建时刻 "
        "created_at、到期时刻 expires_at、解封时刻 released_at；封存不存在返回 404。"
        "任何情况下都不返回解封码及其摘要。"
    ),
)
def api_get_seal(seal_id: str) -> SealOut:
    return SealOut(**get_seal(seal_id))


@app.get(
    "/chains/{chain_id}/seal",
    response_model=SealOut,
    tags=["seals"],
    summary="查询某条链最新的封存",
    description=(
        "返回该链最近一次封存的 active / released / expired 状态、原因与时间信息，"
        "绝不返回解封码摘要。链条不存在返回 404；该链从未有过封存返回 404 "
        "seal_not_found。"
    ),
)
def api_get_latest_seal(chain_id: str) -> SealOut:
    return SealOut(**get_latest_seal(chain_id))


def main() -> None:
    """本地直接运行入口（python -m app.main）。"""
    import uvicorn

    database.init_db()
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
