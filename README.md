# 文件交接留痕服务（Handoff Chain）

基于 **FastAPI + SQLite** 的多人文件交接留痕服务。每次交接形成一条只可追加的
**SHA-256 哈希链**，可查询、可审计、可完整性校验；并发移交与随机数重放由服务端
原子拒绝，**链条不会分叉**。

## 核心规则

- **创建链条**：提交文件 `SHA-256` 摘要与首位持有人，服务端写入序号 `0` 的创世事件：
  ```json
  {"file_hash": "<文件摘要>", "holder": "<首位持有人>", "seq": 0}
  ```
  对其键名排序的**紧凑 JSON**（无空白）计算 SHA-256，即创世哈希（初始链尾）。
- **执行移交**：每次请求须携带
  - `tail_hash`：客户端认定的**当前链尾哈希**；
  - `receiver`：接收方；
  - `nonce`：一次性随机数（同一链内用过即作废，防重放）。

  交出方（`giver`）由服务端依据当前链尾持有人确定，客户端无法伪造。
  新事件哈希按以下紧凑 JSON（`sort_keys` 后）计算：
  ```json
  {"giver":"...","nonce":"...","prev_hash":"<前序哈希>","receiver":"...","seq":<序号>}
  ```
  服务端在**同一个 SQLite 立即事务**中完成「校验链尾 → 查重随机数 → 追加事件 →
  推进链尾」，全程串行。链尾不匹配（过期/竞争请求）或随机数重放一律返回
  **`409 Conflict`**，不写入任何数据，因而不可能产生分叉。
- **批量移交** `POST /handover-batches`：把同一批文件对应的 **2～20 条**交接链
  **一次性移交给同一接收方**。请求携带 `receiver` 与每条链的 `chain_id`、
  `tail_hash`、`nonce`（**同批 `chain_id` 不得重复**，重复或条数不在 2～20
  范围内返回 422；各链随机数相互独立）。服务端在**同一个 SQLite 立即事务**内
  按请求顺序逐链完成与单链移交**完全一致**的「存在性 → 封存门禁 → 链尾匹配 →
  随机数查重 → 追加事件 → 推进链尾」（哈希计算、随机数查重与生效封存限制全部
  复用单链移交）；**任一链**不存在（404）、处于封存生效期（409
  `chain_on_hold`）、链尾过期（409 `tail_mismatch`）或随机数重放（409
  `nonce_replay`）时**整批失败、整体回滚——包括本来可以成功的链在内，任何链都
  不新增事件**。全部成功才整体提交，响应 `results` **按请求顺序**返回每条链的新
  序号 `seq` 与新链尾 `tail_hash`。
- **交付清单核对** `POST /inventories/reconcile`：收货方按交付清单批量核对
  **2～100 条**链（请求体 `items` 各携带 `chain_id`、`file_hash`、
  `current_holder`、`tail_hash`；**同批 `chain_id` 不得重复**，重复或条数不在
  2～100 范围内返回 422）。服务端在**同一个 SQLite 读事务**内读取各链链头与
  当时最大的事件数据库 id（`MAX(events.id)`，全库无事件时为 0），返回
  `snapshot_event_id`（核对锚定的事件快照）、`matched_count` 与逐项状态：
  链不存在为 `missing`，否则依 `file_mismatch`（文件摘要不符）→
  `holder_mismatch`（当前持有人不符）→ `tail_mismatch`（链尾哈希不符）顺序
  取**首个不符项**，四项完全一致为 `matched`。结果严格**保持输入顺序**。
  本接口**只读**：不追加事件、不推进链尾、不写任何表。
- **待接收移交（邀约确认）**：除直接移交外，持有人也可先发起邀约、由接收方确认：
  1. **发起邀约** `POST /offers`：携带当前 `tail_hash`、指定的 `receiver`、
     有效期 `expires_in_seconds`（秒，上限 7 天）与一次性确认码
     `confirmation_code`。服务端**只保存确认码的 SHA-256 摘要**（明文不落库），
     返回 `offer_id`；链尾不匹配返回 **409**。还可**可选**提交一次性撤销码
     `cancellation_code`：服务端仅保存「绑定 `offer_id` 做域分隔」的
     **SHA-256 撤销码摘要**；省略撤销码则创建**不可撤销**的邀约
     （与历史版本完全兼容）。
  2. **确认邀约** `POST /offers/{offer_id}/confirm`：接收方凭 `offer_id` 与
     确认码确认。服务端在**同一个 SQLite 立即事务**中依次校验「确认码摘要匹配 →
     邀约未过期 → 邀约未使用 → 邀约未撤销 → 链尾仍匹配」，全部通过后追加唯一
     移交事件（`nonce` 取 `offer:<offer_id>`，天然一次性）并将邀约置为
     `accepted`，整体原子提交。**过期（`offer_expired`）、重复确认
     （`offer_already_accepted`）、链尾已推进（`tail_mismatch`）、邀约已撤销
     （`offer_not_cancellable`）均返回 409 且不写任何事件**；
     确认码错误返回 **403**（`code_mismatch`）。
  3. **撤销邀约** `POST /offers/{offer_id}/cancel`（仅创建时提供了
     `cancellation_code` 的邀约可用）：发起方凭撤销码撤销。撤销、接收方确认、
     见证人表态与原子替换在**同一个 SQLite 立即事务层面互斥**，竞争终态中至多
     一个成功。仅当邀约**仍 `pending` 且链尾未推进、未过期、无见证人拒绝**时可
     撤销，撤销只把邀约置为 `cancelled`，**绝不追加或修改任何哈希链事件**；
     撤销码错误返回 **403**（`cancel_code_mismatch`）。
  4. **原子替换邀约** `POST /offers/{offer_id}/replace`（同样仅创建时提供了
     `cancellation_code` 的邀约可用）：发起方凭**原撤销码**提交新邀约参数——
     新接收方 `receiver`、新有效期 `expires_in_seconds`、新一次性确认码
     `confirmation_code`，以及**可选**的新撤销码 `new_cancellation_code` 与
     **可选的全新见证配置**（`witnesses` + `witness_threshold`）。服务端在
     **同一个 SQLite 立即事务**中依次确认原邀约**仍 `pending`、未过期、链尾未
     推进、无见证人拒绝、原撤销码摘要匹配**，然后先创建**锁定同一链尾**的新邀约
     （其 `replaces` 指向原邀约），再把原邀约标记为 `replaced`（其
     `replaced_by` 指向新邀约），两步与全部校验同事务原子提交，**失败整体回滚、
     绝不产生半替换**。原撤销码错误返回 **403**（`cancel_code_mismatch`）；任何
     非可替换状态（`accepted` / `cancelled` / `replaced` / `expired` /
     `terminated` / `rejected`，或创建时未提供撤销码的旧邀约）返回 **409**
     （`offer_not_replaceable`）。替换后原邀约的确认码、撤销码、见证令牌**全部
     作废**：对原邀约确认/撤销/见证表态一律 409；新邀约的所有凭证按新
     `offer_id` 重新做域分隔，可凭新确认码正常确认，省略 `new_cancellation_code`
     时新邀约不可撤销、也不可再次替换。
  5. **不可撤销/不可替换的情形**：邀约已 `accepted`、`rejected`、`expired`、
     `terminated`、`cancelled`、`replaced`，或创建时未提供撤销码（含全部旧
     邀约），调用撤销一律返回 **409**（`offer_not_cancellable`）、调用替换一律
     返回 **409**（`offer_not_replaceable`），且不改写链。
  6. **查询邀约** `GET /offers/{offer_id}`：返回邀约状态
     （`pending` / `accepted` / `expired` / `terminated` / `rejected` /
     `cancelled` / `replaced`，其中过期、终止、拒绝为派生状态）、`cancellable`
     标记、`cancelled_at`、确认结果，以及替换关系 **`replaces`**（本邀约替换了
     哪个旧邀约）与 **`replaced_by` / `replaced_at`**（本邀约被哪个新邀约在何时
     替换），二者只给 `offer_id`，可据此串联整条替换链；**不含确认码、撤销码、
     令牌及其任何摘要**。
- **见证确认（可选模式）**：发起邀约时可附带 2～5 名见证人及通过阈值
  `witness_threshold`（1～见证人数量），其余流程与普通邀约一致：
  1. 发起邀约时每个见证人提交 `name` 与一个**互相独立的一次性令牌** `token`。
     服务端只保存「绑定 offer_id 做域分隔」的 **SHA-256 令牌摘要**，明文仅在
     创建响应中返回一次，由发起方线下分发。
  2. **见证人表态** `POST /offers/{offer_id}/witness-decisions`：凭令牌表态
     `approved`（同意）或 `rejected`（拒绝）。令牌错误返回 **403**
     （`token_mismatch`）；令牌命中即作废，同一见证人重复表态返回 **409**
     （`witness_already_decided`）。
  3. 接收方确认时，服务端在**同一个 SQLite 立即事务**内于原有校验之后再校验
     「**无见证人拒绝** 且 **同意数达到阈值**」：有拒绝返回 **409**
     （`offer_rejected`），同意数不足返回 **409**
     （`witness_threshold_not_met`），均**不写任何事件**。
  4. **任一拒绝**即令邀约进入 `rejected` 终止态；**过期**、**链尾被其他移交
     推进**（状态派生为 `terminated`）、**被撤销码撤销**（`cancelled`）、
     **被原子替换**（`replaced`）同样终止，终止后表态、确认、撤销与再次替换均
     返回 409 且不写事件。撤销、替换与表态/确认在 SQLite 立即事务中互斥，
     竞争中只有一个终态能成功。
  5. **见证状态查询** `GET /offers/{offer_id}/witnesses`：返回各见证人的名称、
     序号、表态结果与同意/拒绝/待表态计数及派生邀约状态，**绝不返回令牌及其
     摘要**（`GET /offers/{offer_id}` 的邀约详情同样只给统计与表态结果）。
- **争议封存（chain seal）**：交接链出现争议时，持有人可对整条链申请封存，
  冻结期间所有「会推进哈希链或产生新邀约」的入口：
  1. **创建封存** `POST /chains/{chain_id}/seals`：持有人携带**当前链尾**
     `tail_hash`、争议原因 `reason`、有效期 `expires_in_seconds`（秒，上限
     30 天）与一次性解封码 `release_code`。服务端**只保存「绑定 `seal_id`
     做域分隔」的 SHA-256 解封码摘要**（`seal:<seal_id>:release-code:<code>`），
     明文不落库；封存本身**不追加任何哈希链事件**，链条仍可完整校验。链尾不
     匹配返回 **409**（`tail_mismatch`）；**同一链同时只能有一个生效封存**，
     重复封存返回 **409**（`chain_seal_active`，由部分唯一索引
     `uq_active_seal_per_chain` 与立即事务共同兜底）。
  2. **封存生效期间的冻结语义**：直接移交 `POST /handovers`，以及邀约的
     创建 `POST /offers`、替换 `POST /offers/{id}/replace`、确认
     `POST /offers/{id}/confirm`，都在各自**现有 SQLite 立即事务内的任何写入
     之前**检查封存，命中即返回 **409 `chain_on_hold`**，**不写入事件、不写入
     或替换邀约**。封存创建与这些操作共用同一把写锁与 `BEGIN IMMEDIATE`
     语义，并发时只有一方成功（封存先提交则操作被拒；操作先提交则链尾推进、
     封存因 `tail_mismatch` 失败）。
     （注：按需求范围，仅上述四类操作被冻结；邀约撤销与见证人表态不推进链、
     不产生新邀约，不在冻结之列。）
  3. **提前解封** `POST /seals/{seal_id}/release`：凭一次性解封码在同一立即
     事务内把封存置为 `released` 并记录 `released_at`，解封同样**不动哈希链**，
     解封后四类操作立即恢复。解封码错误返回 **403**
     （`release_code_mismatch`）；封存不存在 **404**；重复解封 **409**
     （`seal_already_released`）。
  4. **过期自动失效**：封存有效期由创建时固定的 `expires_at` 决定——创建接口
     在**取得写锁后**取同一个带微秒精度的 UTC 时刻，用于已有封存判定、创建
     时刻与到期计算，有效期自该时刻完整起算 `expires_in_seconds` 秒（等待
     写锁的耗时不会侵蚀有效期），到期**无需任何写操作**即自动失效——读写
     事务按当前时间实时派生 `expired` 状态，四类操作随之自然恢复；之后可
     重新创建封存（创建时同链已过期的旧封存会在同一事务内物化为 `expired`
     终态）。对已过期封存调用解封返回 **409**（`seal_expired`）。
  5. **查询封存** `GET /seals/{seal_id}`（按封存 ID）与
     `GET /chains/{chain_id}/seal`（某链最新封存）：返回
     `active` / `released` / `expired` 状态、`reason`、锁定链尾 `tail_hash`
     以及 `created_at` / `expires_at` / `released_at` 时间信息；**任何情况下
     都不返回解封码及其摘要**。链条不存在返回 404；该链从未有过封存时链级
     查询返回 **404**（`seal_not_found`）。
- **完整性校验**：服务端从创世记录起逐条重算哈希、校验序号连续性与 `prev_hash`
  指针，并比对链尾缓存；校验结果给出 **首个断裂序号 `first_broken_seq`**。
- **交接事件的可选择披露证明（Merkle 包含证明）**：无需公开整条链，即可向第三方
  证明「某事件确实包含在该链当前事件集合中、且当时链尾为某值」：
  1. **生成证明** `GET /chains/{chain_id}/events/{seq}/proof`：服务端在**同一个
     读事务**内取齐该链当前全部事件与当前链尾（二者构成同一快照），以事件的
     `event_hash` 为叶构造 Merkle 树。叶子**绑定 `chain_id`、`seq` 与
     `event_hash`**：
     ```
     leaf = SHA256("merkle-leaf:v1:" + 规范JSON{"chain_id","event_hash","seq"})
     ```
     内部节点采用**固定域分隔前缀**，左右为定长 64 位十六进制子摘要直接拼接，
     与叶子/快照分域，杜绝第二原像：
     ```
     node = SHA256("merkle-node:v1:" + left_hex + right_hex)
     ```
     **奇数层末节点与自身复制配对**（右兄弟即其自身，不引入其他填充值）；单
     事件链的根即叶子，兄弟路径为空。响应返回：被证明事件 `event`、自叶子向根
     的左右方向兄弟路径 `path`（每项 `{"direction","sibling_hash"}`，
     `left` 表示先拼兄弟再拼当前节点）、事件数 `event_count`、同一快照下的
     当前 `tail_hash`、`merkle_root`，以及按规范 JSON 计算的链尾快照摘要：
     ```
     snapshot_digest = SHA256("merkle-snapshot:v1:" + 规范JSON{
         "chain_id","event_count","merkle_root","tail_hash"})
     ```
     链条不存在返回 **404**；`seq` 越界（负数或不小于当前事件数）返回 **404**
     （`event_not_found`）。
  2. **离线校验** `POST /proofs/verify`：**只依据请求体重算**叶子、兄弟路径与
     `snapshot_digest`，**完全不读取数据库**，因此可在任何离线环境复算。请求体
     与生成响应同构，可把证明响应**原样回填**；`event` 中仅 `seq`、`event_hash`
     参与重算，其余字段被忽略。校验按 **`event_count` → `seq` → `path` →
     `merkle_root` → `snapshot_digest`** 的顺序进行，响应给出 `valid` 与
     **首个失败阶段 `failed_stage`**（成功为 `null`）及重算出的叶子/根/快照
     摘要。篡改 `event_hash`、链 ID 或序号会在 `merkle_root` 阶段失败；篡改
     `tail_hash`、`event_count` 或 `merkle_root` 会在 `snapshot_digest`（或
     `merkle_root`）阶段失败；路径层数、左右方向或奇数层复制配对规则不一致在
     `path` 阶段失败。注意：证明锚定的是「生成时刻的事件集合与链尾快照」，链继续
     追加后旧证明仍然有效，只需重新生成即可获得新快照的证明。
  3. **批量生成** `GET /chains/{chain_id}/events/proof?seq=...`：一次选择披露
     多个事件。查询参数 `seq` 可重复传入，服务端**去重并按升序**取 1～20 个
     序号，在**同一个读事务**内读取事件集合与链尾（与单事件证明同一快照语义、
     同一套哈希规则），生成**一份 Merkle multiproof**：响应返回被选事件
     `events`（按 `seq` 升序，**绝不包含未选择事件**）、`event_count`、
     `tail_hash`、`merkle_root`、`snapshot_digest`，以及重建根所需的**最小**
     证明节点集合 `proof_nodes`——每项以 `(level, index)` 坐标定位
     （`level` 0 为叶子层），按 `(level, index)` 升序且不重复；**奇数层末
     节点与自身复制配对的位置不产生证明节点**（校验方按规则自行复制）。去重
     后超过 20 个序号返回 **400**（`too_many_seqs`）；链条不存在返回 **404**；
     任一序号越界（负数或不小于当前事件数）返回 **404**（`event_not_found`）。
  4. **批量离线校验** `POST /proofs/verify-batch`：**只依据请求体按节点坐标
     重建根**，不读取数据库；请求体与批量证明响应同构，可**原样回填**
     （`events` 中仅 `seq`、`event_hash` 参与重算，其余字段被忽略）。校验按
     **`event_count` → `events` → `proof_nodes` → `merkle_root` →
     `snapshot_digest`** 的顺序给出 `valid`、首个失败阶段 `failed_stage`
     与重算值（叶子摘要列表、Merkle 根、快照摘要）。**重复或越界序号**
     （`events` 阶段）、**缺失或多余节点、坐标重复或与重算值冲突、坐标越界
     （含为奇数层末节点虚构配对节点等不符合自复制规则的情形）**
     （`proof_nodes` 阶段）一律拒绝；篡改事件哈希、链 ID 或序号在
     `merkle_root` 阶段失败，篡改 `tail_hash` 等在 `snapshot_digest` 阶段
     失败。
- **交接事件审计检索** `GET /audit/events`：跨**全部交接链**审计检索事件，
  适合离线合规导出与第三方审计：
  1. **过滤参数**（全部可选）：`chain_id`（限定单链）、`holder`（持有人标识）、
     `role`（`giver` / `receiver` / `either`，缺省 `either`）、`created_from`
     与 `created_to`（ISO-8601 时间，**闭区间**，无时区后缀的朴素时间按 UTC
     解释）、`limit`（每页条数，**1～100**，默认 50）。结果跨链按
     **`(created_at, 事件数据库 id)` 升序**返回——同秒内多个事件以单调自增的
     数据库 `id` 定先后，排序稳定。
  2. **创世事件的持有人语义**：创世事件（`seq=0`）没有 `giver` / `receiver`
     字段，**仅当 `role=either` 时按链条首位持有人 `first_holder` 参与 `holder`
     匹配**；`role=giver` / `receiver` 时创世事件一律不入选（移交事件分别匹配
     `giver` / `receiver`）。`role` 必须与 `holder` 同时使用（提供 `role` 却没
     有 `holder` 返回 **400** `role_requires_holder`；有 `holder` 而省略
     `role` 等价于 `either`）。
  3. **快照一致性翻页**：首次查询在**同一个读事务**内把「**当时最大的事件数据
     库 id**」固定为快照上界 `max_id`，此后整次检索只在 `id <= max_id` 的不可变
     事件集合内翻页。翻页期间即使有新的移交被追加（其 `id` 必然大于
     `max_id`），也**绝不会插入到既有排序位置**——跨页不会重复、不会漏项；
     需要最新数据时重新发起一次不带游标的查询即可获得新快照。
  4. **签名游标**：响应 `next_cursor` 是不透明字符串，载荷为「**过滤条件摘要**
     （规范化过滤条件的 SHA-256）+ **快照上界** + **本页末条排序键**
     （`created_at`、`id`）」，URL 安全 base64 后用 **`AUDIT_CURSOR_SECRET`**
     做 **HMAC-SHA256** 签名（域前缀 `audit-cursor:v1:`）。翻页时通过
     `next_cursor` 查询参数原样回传，且 `chain_id` / `holder` / `role` /
     `created_from` / `created_to` **必须与首次查询完全一致**（仅 `limit` 可在
     各页之间调整；时间串等价写法如 `Z` 与 `+00:00` 视为相同）。**游标损坏或
     被篡改返回 400 `invalid_cursor_signature`、格式非法返回 400
     `invalid_cursor`、与本次过滤条件不符返回 400 `cursor_filter_mismatch`，
     三者都不会回退为一次新的快照查询**；密钥轮换后旧游标同样返回 400。
  5. **页尾语义**：某页不足 `limit` 条（含首次查询结果为空）时 `next_cursor`
     为 **`null`**，表示快照内已无更多项；响应事件含数据库 `id`、`chain_id`、
     `seq`、`giver`、`receiver`、`nonce`、`prev_hash`、`event_hash`、
     `created_at`，创世事件的 `giver` / `receiver` / `nonce` / `prev_hash`
     为空串。`created_from` 晚于 `created_to` 返回 **400**
     `invalid_time_range`。
- **保管负荷报表** `GET /reports/custody-load`（**只读**，独立于交接流程，不
  写任何表）：统计某位持有人在指定时间窗内「同时持有多少条链」的负荷。
  1. **查询参数**：`holder`（持有人标识，必填）、`start` 与 `end`（统计窗口，
     **半开区间 `[start, end)`**，必填）。`start` / `end` **必须携带时区**
     （如 `2026-09-01T00:00:00+08:00` 或 `2026-09-01T00:00:00Z`），服务端
     **统一换算为 UTC**；时间非法、缺时区、`start >= end` 或 **`end` 晚于查询
     时刻**均返回 **400** `invalid_time_range`。
  2. **同一读事务内固定事件快照**：先把「**当时最大的事件数据库 id**」固定为
     `snapshot_event_id`（`MAX(events.id)`，全库无事件时为 0），随后**仅用
     `id <= snapshot_event_id` 的事件**重建全库持有区间；报表期间新追加的移交
     不进入本次结果。
  3. **重建规则**：对每条链按 **`(created_at, 事件数据库 id)`** 排序事件——
     创世事件（`seq=0`）从链条 `first_holder` 起持有，此后每条移交事件以
     **`receiver` 接续**为新持有人；**同一时刻（同一 `created_at`）的多个变化
     先合并**，只比较该时刻前 / 后的净状态，时刻中间的瞬时交接（如同一秒内
     A→B→C）对 B 只产生零时长区间、**不产生任何负荷**。由此**每条链在任一时刻
     至多计一次**；**自转交**（`receiver` 与原持有人相同）持有人不变、**不增加
     负荷**。重建先完整还原**窗口之前**的持有状态，再把各链的半开持有区间
     **裁剪到 `[start, end)`**。
  4. **响应字段**：`snapshot_event_id`（本次快照）、`peak_chain_count`（窗口内
     **最大同时持有链数**）、`peak_intervals`（达到该**正峰值**的**全部合并
     连续区间**，半开 `[start, end)`，相邻同峰值段合并，按开始时刻升序，UTC
     ISO-8601）、`total_chain_seconds`（**持有链数对秒数的积分**，即
     「链·秒」；整秒为整数，窗口含亚秒精度时可为小数）。该人窗口内**无任何
     持有记录**（或全部为零时长瞬态）时 `peak_chain_count=0`、
     `total_chain_seconds=0`、`peak_intervals=[]`。

## 一条命令启动（依赖安装 + 数据库初始化 + 启动）

需要已安装 Docker（含 Compose 插件）。在项目根目录执行：

```bash
docker compose up --build
```

该命令会：构建镜像（其中 `pip install -r requirements.txt` 完成依赖安装）→
容器启动时自动初始化 SQLite 数据库（自动建表，数据位于命名卷 `handoff-data`，
库文件 `/data/handoff.db`）→ 启动 API 服务。归档（备份）默认存放于命名卷
`handoff-backups`（容器内 `/backups`），见下文「数据库归档」。

### 访问地址与端口

| 用途 | 地址 |
| --- | --- |
| API 服务根地址 | <http://localhost:8000> |
| 健康检查 | <http://localhost:8000/health> |
| 交互式文档（Swagger UI） | <http://localhost:8000/docs> |
| OpenAPI 描述 | <http://localhost:8000/openapi.json> |

后台运行：`docker compose up --build -d`；停止：`docker compose down`
（数据保留在卷中；如需连数据一并清除：`docker compose down -v`）。

> 审计检索的翻页游标需要 HMAC 签名密钥 `AUDIT_CURSOR_SECRET`：Compose 已内置
> **可覆盖的开发默认值**（`dev-only-audit-cursor-secret-change-me`），因此
> 一键启动即可用；生产环境请通过环境变量或同目录 `.env` 覆盖，例如：
> `AUDIT_CURSOR_SECRET=$(openssl rand -hex 32) docker compose up --build -d`。

### 不用 Docker 的本地启动方式

```bash
pip install -r requirements.txt
DATA_DIR=./data python -m app.main        # 自动建库建表后启动
# 或显式初始化：
python -c "from app.database import init_db; init_db()"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 数据库归档（在线备份与只读核验）

容器内命令 `python -m app.backup` 提供 `create` 与 `verify` 两个子命令，源库固定取
**`DB_PATH`**（默认 `/data/handoff.db`，见「配置（环境变量）」），无需停服：

- **`create <目录>`**：用 **SQLite 在线备份机制**（backup API）生成一致快照——
  API 持续写入、WAL 未合并时快照依然完整一致，且源库以只读方式打开、绝不被改动。
  归档目录包含**独立可读的数据库** `handoff.db`（快照已转回 delete 日志模式，
  脱离源库与 WAL 即可只读打开）与**清单** `manifest.json`（记录格式版本
  `format` / `format_version`、创建时间、各文件 **SHA-256** 与大小、已执行的
  校验项）。整个归档**先在目标父目录下暂存**，快照通过 `integrity_check`、
  `foreign_key_check` 与**五张业务表**（chains / events / offers /
  offer_witnesses / chain_seals）**结构校验**后，才以**原子不覆盖重命名**
  （`renameat2` 的 `RENAME_NOREPLACE`）**发布**整个归档目录——“目标不存在”
  是发布成功的原子前提，竞争方先占到同名路径（哪怕是空目录）时发布失败、
  对方目录及其内容原样保留；**目标已存在、或与源库路径（含 `-wal` / `-shm`
  伴生文件）冲突时拒绝**；任一步失败即清理暂存目录，不改动源库与任何既有归档。
- **`verify <目录>`**：**只读**核验既有归档——解析并校验清单（格式、版本、
  必备条目），逐一重算清单内文件的 SHA-256 并比对大小，拒绝清单之外的文件，
  再对归档内数据库只读执行 `integrity_check`、`foreign_key_check` 与五张业务表
  结构校验。全部通过退出码 **0**；任何损坏/缺项以**非零退出码**退出并在
  stderr 逐条列出具体原因。核验不触碰源库，可在任何挂载了归档的离线环境运行。

```bash
# 服务运行中创建归档（/backups 为 Compose 命名卷 handoff-backups）
docker compose exec handoff-chain \
  python -m app.backup create /backups/handoff-$(date +%Y%m%d-%H%M%S)

# 只读核验归档
docker compose exec handoff-chain \
  python -m app.backup verify /backups/handoff-20260927-120000

# 服务未运行时也可用一次性容器执行（同样挂载两个卷）
docker compose run --rm handoff-chain \
  python -m app.backup create /backups/handoff-snapshot-1

# 不用 Docker 的本地用法（源库同样取 DB_PATH；目标父目录须已存在）
mkdir -p ./backups
DATA_DIR=./data python -m app.backup create ./backups/snap-1
DATA_DIR=./data python -m app.backup verify ./backups/snap-1
```

归档目录结构示例：

```
/backups/handoff-20260927-120000/
├── handoff.db      # SQLite 一致快照，可独立只读打开
└── manifest.json   # 格式版本 + 文件 SHA-256/大小 + 校验项清单
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/chains` | 创建交接链（文件摘要 + 首位持有人） |
| `POST` | `/handovers` | 执行一次移交（链尾哈希 + 接收方 + 一次性随机数） |
| `POST` | `/handover-batches` | 批量移交：同一批 2～20 条链（同批 chain_id 不得重复）在一个立即事务内整批校验并追加事件、推进各链尾给同一接收方；任一链不存在/封存生效/链尾过期/随机数重放则整批回滚，任何链不新增事件；响应按请求顺序返回各链新 seq 与 tail_hash |
| `POST` | `/inventories/reconcile` | 交付清单核对：同一读事务内读取 2～100 条链的链头与 MAX(events.id) 快照（无事件为 0），逐项按 missing → file_mismatch → holder_mismatch → tail_mismatch 判定，全一致为 matched；返回 snapshot_event_id、matched_count 与按输入顺序的逐项状态；只读，不写库 |
| `POST` | `/offers` | 发起邀约（链尾 + 接收方 + 有效期 + 一次性确认码；可选撤销码，凭码可撤销/替换；可选 2～5 名见证人与阈值） |
| `POST` | `/offers/{offer_id}/confirm` | 接收方凭确认码确认，原子追加移交事件（见证模式须无拒绝且同意数达阈值） |
| `POST` | `/offers/{offer_id}/cancel` | 凭一次性撤销码撤销邀约（仅 pending 且链尾未推进/未过期/无拒绝；错码 403，终态或无撤销码 409；不改链） |
| `POST` | `/offers/{offer_id}/replace` | 凭原撤销码原子替换邀约：同事务校验 pending/未过期/链尾未推进/无拒绝/撤销码，旧邀约置 replaced 并创建锁定同一链尾的新邀约（新接收方/有效期/确认码，可选新撤销码与见证配置；错码 403，非可替换状态 409，无半替换） |
| `POST` | `/offers/{offer_id}/witness-decisions` | 见证人凭独立一次性令牌表态同意 / 拒绝（每人至多一次） |
| `GET`  | `/offers/{offer_id}/witnesses` | 查询见证表态状态与计数（不含令牌摘要；无见证邀约 404） |
| `GET`  | `/offers/{offer_id}` | 查询单个邀约状态与 replaces / replaced_by 替换关系（pending / accepted / expired / rejected / terminated / cancelled / replaced） |
| `GET`  | `/chains` | 列出全部链条 |
| `GET`  | `/audit/events` | 跨链审计检索事件（chain_id/holder/role(giver/receiver/either)/created_from/created_to/limit 1～100；按 created_at,id 升序；创世事件仅 either 时按首位持有人匹配；首次查询固定 max_id 快照，HMAC 签名游标翻页，篡改或过滤条件不符 400；无更多项 next_cursor=null） |
| `GET`  | `/chains/{chain_id}` | 查询链条与全部事件 |
| `POST` | `/chains/{chain_id}/seals` | 创建争议封存（当前链尾 + 原因 + 有效期 + 一次性解封码；同链仅一个生效封存，重复 409） |
| `POST` | `/seals/{seal_id}/release` | 凭一次性解封码提前解封（错码 403；重复解封/已过期 409；不动哈希链） |
| `GET`  | `/seals/{seal_id}` | 按封存 ID 查询状态（active / released / expired，不含解封码摘要） |
| `GET`  | `/chains/{chain_id}/seal` | 查询某条链最新的封存（无封存 404 seal_not_found） |
| `GET`  | `/reports/custody-load` | 保管负荷报表（holder/start/end；时间须带时区并转 UTC，start<end 且 end 不得晚于查询时刻，否则 400 invalid_time_range；同一读事务固定 snapshot_event_id，仅用该快照重建全库该人持有区间：创世从 first_holder 起、移交以 receiver 接续，同刻变化合并后计算，每条链任一时刻至多计一次、自转交不增负荷；先还原窗口前状态再裁剪 [start,end)；返回 peak_chain_count、全部正峰值合并连续区间 peak_intervals 与积分 total_chain_seconds；无持有记录为 0 与空区间；只读） |
| `GET`  | `/chains/{chain_id}/verify` | 完整性校验，指出首个断裂序号 |
| `GET`  | `/chains/{chain_id}/events/{seq}/proof` | 生成单个事件的 Merkle 包含证明（叶子绑定 chain_id/seq/event_hash；奇数层末节点复制配对；返回事件/左右兄弟路径/事件数/链尾/根/snapshot_digest；seq 越界 404 event_not_found） |
| `POST` | `/proofs/verify` | 仅依据请求体重算叶子、路径与 snapshot_digest 离线校验，不读数据库；返回 valid 与首个失败阶段 |
| `GET`  | `/chains/{chain_id}/events/proof` | 批量生成 Merkle multiproof：`?seq=` 可重复传入，去重升序取 1～20 个序号，同一读事务快照；返回被选事件/event_count/tail_hash/merkle_root/snapshot_digest 与按 (level,index) 升序不重复的最小 proof_nodes（去重后超 20 个 400 too_many_seqs；序号越界 404 event_not_found） |
| `POST` | `/proofs/verify-batch` | 仅依据请求体按节点坐标重建根离线校验批量证明，不读数据库；拒绝重复/越界序号、缺失/多余节点、坐标冲突及违反奇数层自复制规则的证明；返回 valid、failed_stage 与重算值 |
| `GET`  | `/health` | 健康检查 |

错误响应统一为：`{"error": "<code>", "detail": "<说明>"}`。状态码约定：
**409** —— `tail_mismatch`（链尾不匹配/已推进，邀约派生状态为 `terminated`；
创建封存时链尾不匹配同样返回此码）、
`nonce_replay`（随机数重放）、
`chain_on_hold`（链条处于争议封存生效期：直接移交与邀约的创建、替换、确认
一律被冻结，不写事件或新邀约）、
`chain_seal_active`（该链已有生效封存，同一链同时只能有一个；并发封存落败
也返回此码）、
`seal_already_released`（封存已提前解封，不能重复解封）、
`seal_expired`（封存已过有效期自动失效，无需再解封）、
`seal_not_active`（解封在竞争中落败时的兜底错误码）、
`offer_expired`（邀约已过期）、
`offer_already_accepted`（邀约重复确认 / 已接受后继续表态）、
`offer_replaced`（邀约已被原子替换：旧确认/旧令牌对原邀约不再有效）、
`offer_rejected`（已有见证人拒绝，邀约终止）、
`witness_threshold_not_met`（同意数未达阈值）、
`witness_already_decided`（见证人已表态，令牌不可重复使用）、
`offer_not_witnessed`（对无见证邀约调用表态接口）、
`offer_not_cancellable`（邀约不可撤销：已 accepted / rejected / expired /
terminated / cancelled / replaced，或创建时未提供撤销码的旧邀约；撤销与
确认/表态/替换竞争落败也返回此码）、
`offer_not_replaceable`（邀约不可替换：已离开 pending —— accepted /
cancelled / replaced / rejected / expired / terminated，或创建时未提供撤销码
的旧邀约；替换与确认/撤销/表态竞争落败也返回此码，整体回滚不产生半替换）；
**403** —— `code_mismatch`（确认码错误）、`token_mismatch`（见证人令牌错误）、
`cancel_code_mismatch`（撤销码错误：撤销或原子替换时原撤销码不匹配）、
`release_code_mismatch`（封存解封码错误）；
**400** —— `too_many_seqs`（批量证明去重后超过 20 个事件序号）、
`too_few_seqs`（批量证明未提供任何有效序号）、
`invalid_time_range`（审计检索的 `created_from`/`created_to` 非合法 ISO-8601
时间，或 `created_from` 晚于 `created_to`；保管负荷报表的 `start`/`end` 非
合法 ISO-8601、**缺少时区**、`start >= end` 或 **`end` 晚于查询时刻**）、
`role_requires_holder`（审计检索提供了 `role` 却未提供 `holder`）、
`invalid_cursor`（审计翻页游标格式非法/版本不受支持/字段缺失）、
`invalid_cursor_signature`（审计翻页游标签名无效：损坏、被篡改或
`AUDIT_CURSOR_SECRET` 已轮换；**不会回退为新查询**）、
`cursor_filter_mismatch`（审计翻页游标携带的过滤条件摘要与本次请求的
chain_id/holder/role/created_from/created_to 不符；**不会回退为新查询**）；
**404** —— 链条、邀约、封存不存在，或对无见证邀约查询 `witness_mode_not_found`、
对从未有过封存的链查询最新封存 `seal_not_found`、查询 Merkle 证明时事件序号
越界（含负数）`event_not_found`（单事件与批量证明一致）。

批量移交 `POST /handover-batches` **不引入新错误码**：复用单链移交的 404
`not_found`、409 `chain_on_hold` / `tail_mismatch` / `nonce_replay`，任一链
命中即整批回滚（包括本来可以成功的链），错误 `detail` 中附带首个失败链的
`chain_id`；同批 `chain_id` 重复、条数不在 2～20 或字段格式非法为请求级
**422**。

交付清单核对 `POST /inventories/reconcile` 同样**不引入新错误码**：核对结果
（含 `missing` / `file_mismatch` / `holder_mismatch` / `tail_mismatch`）一律
以 **200** 逐项返回，不作错误处理；仅请求级校验失败为 **422**——同批
`chain_id` 重复、条数不在 2～100 或字段格式非法（如哈希非 64 位十六进制）。

## 使用示例

```bash
# 1) 计算文件摘要并创建链条
FILE_HASH=$(sha256sum some-file.bin | awk '{print $1}')
curl -s -X POST http://localhost:8000/chains \
  -H 'Content-Type: application/json' \
  -d "{\"file_hash\": \"$FILE_HASH\", \"first_holder\": \"alice\"}"

# 响应中含 chain_id 与 tail_hash（创世哈希），例如：
# {"chain_id":"ab12...","tail_hash":"9f0c...","current_holder":"alice", ...}

# 2) alice 移交给 bob（必须带上当前 tail_hash 和全新 nonce）
curl -s -X POST http://localhost:8000/handovers \
  -H 'Content-Type: application/json' \
  -d '{"chain_id":"ab12...","tail_hash":"9f0c...","receiver":"bob","nonce":"n-0001"}'
# 返回新的 event_hash（即新链尾），下一次移交必须携带它

# 3) 用旧链尾再交一次 → 409 tail_mismatch（拒绝，不分叉）
curl -i -X POST http://localhost:8000/handovers \
  -H 'Content-Type: application/json' \
  -d '{"chain_id":"ab12...","tail_hash":"9f0c...","receiver":"carol","nonce":"n-0002"}'

# 4) 随机数重放 → 409 nonce_replay
curl -i -X POST http://localhost:8000/handovers \
  -H 'Content-Type: application/json' \
  -d '{"chain_id":"ab12...","tail_hash":"<新链尾>","receiver":"carol","nonce":"n-0001"}'

# 5) 查询链条
curl -s http://localhost:8000/chains/ab12...

# 6) 完整性校验（正常：valid=true）
curl -s http://localhost:8000/chains/ab12.../verify
```

### 批量移交示例

适用于「同一批文件的多条交接链，一次性移交给同一接收方」的场景：2～20 条链的
校验与追加在**同一个 SQLite 立即事务**内完成，任一链失败则整批回滚。

```bash
# 1) 假设 chain A（aaaa...，当前链尾 tailA）与 chain B（bbbb...，当前链尾 tailB）
#    都由当前持有人持有，现把两条链一次性移交给同一接收方 carol
curl -s -X POST http://localhost:8000/handover-batches \
  -H 'Content-Type: application/json' \
  -d '{"receiver":"carol",
       "items":[
         {"chain_id":"aaaa...","tail_hash":"<A 当前链尾>","nonce":"batch-001-a"},
         {"chain_id":"bbbb...","tail_hash":"<B 当前链尾>","nonce":"batch-001-b"}]}'
# 201 响应：results 严格按请求顺序，给出每条链的新序号与新链尾
# {"receiver":"carol","count":2,
#  "results":[{"chain_id":"aaaa...","seq":4,"tail_hash":"<A 新链尾>"},
#             {"chain_id":"bbbb...","seq":2,"tail_hash":"<B 新链尾>"}]}
# 两条链的当前持有人都变为 carol；下一次各自移交必须携带返回的新链尾

# 2) 整批原子：第二条链的链尾过期 -> 409 tail_mismatch，
#    第一条链本来可以成功也不会写入任何事件，两条链的链尾都保持不变
curl -i -X POST http://localhost:8000/handover-batches \
  -H 'Content-Type: application/json' \
  -d '{"receiver":"carol",
       "items":[
         {"chain_id":"aaaa...","tail_hash":"<A 当前链尾>","nonce":"batch-002-a"},
         {"chain_id":"bbbb...","tail_hash":"<过期的旧链尾>","nonce":"batch-002-b"}]}'
# 409 {"error":"tail_mismatch",
#      "detail":"链 bbbb... 的链尾哈希不匹配：链条已被其他移交推进，整批未生效，请获取最新链尾后重试"}
# 随即查询 /chains/aaaa... 可确认其事件数与链尾与调用前完全一致

# 3) 任一链随机数重放（nonce 在该链内用过）-> 409 nonce_replay，同样整批回滚；
#    任一链处于封存生效期 -> 409 chain_on_hold；任一 chain_id 不存在 -> 404 not_found

# 4) 请求级校验（均为 422，不进入事务）：
#    同批 chain_id 重复 / 条数不在 2～20 / tail_hash 非 64 位十六进制 / nonce 为空
curl -i -X POST http://localhost:8000/handover-batches \
  -H 'Content-Type: application/json' \
  -d '{"receiver":"carol",
       "items":[
         {"chain_id":"aaaa...","tail_hash":"<A 当前链尾>","nonce":"x1"},
         {"chain_id":"aaaa...","tail_hash":"<A 当前链尾>","nonce":"x2"}]}'
# 422（同批 chain_id 不得重复）；注意各链 nonce 相互独立，不同链允许相同 nonce
```

### 交付清单核对示例

适用于「收货方拿到交付清单后，批量核对多条链是否与服务端一致」的场景：2～100 条
链的链头读取与快照锚点（`MAX(events.id)`）在**同一个 SQLite 读事务**内完成，
接口只读，不追加事件、不推进链尾。

```bash
# 1) 按交付清单核对：每条携带 chain_id / file_hash / current_holder / tail_hash
curl -s -X POST http://localhost:8000/inventories/reconcile \
  -H 'Content-Type: application/json' \
  -d '{"items":[
        {"chain_id":"aaaa...","file_hash":"<A 文件摘要>",
         "current_holder":"carol","tail_hash":"<A 当前链尾>"},
        {"chain_id":"bbbb...","file_hash":"<B 文件摘要>",
         "current_holder":"carol","tail_hash":"<B 当前链尾>"}]}'
# 200 响应：snapshot_event_id 为核对锚定的事件快照（MAX(events.id)，无事件为 0），
# results 严格按请求 items 顺序给出逐项状态
# {"snapshot_event_id":17,"matched_count":2,"count":2,
#  "results":[{"chain_id":"aaaa...","status":"matched"},
#             {"chain_id":"bbbb...","status":"matched"}]}

# 2) 逐项状态：链不存在 missing；否则按 文件摘要 -> 当前持有人 -> 链尾 顺序
#    取首个不符项（file_mismatch / holder_mismatch / tail_mismatch），
#    四项全一致才是 matched；matched_count 只统计 matched 条数
# {"snapshot_event_id":17,"matched_count":1,"count":4,
#  "results":[{"chain_id":"aaaa...","status":"matched"},
#             {"chain_id":"cccc...","status":"missing"},
#             {"chain_id":"bbbb...","status":"holder_mismatch"},
#             {"chain_id":"dddd...","status":"tail_mismatch"}]}

# 3) 请求级校验（均为 422，不进入读事务）：
#    同批 chain_id 重复 / 条数不在 2～100 / file_hash、tail_hash 非 64 位十六进制
curl -i -X POST http://localhost:8000/inventories/reconcile \
  -H 'Content-Type: application/json' \
  -d '{"items":[
        {"chain_id":"aaaa...","file_hash":"<A 文件摘要>",
         "current_holder":"carol","tail_hash":"<A 当前链尾>"},
        {"chain_id":"aaaa...","file_hash":"<A 文件摘要>",
         "current_holder":"carol","tail_hash":"<A 当前链尾>"}]}'
# 422（同批 chain_id 不得重复）
```

### 待接收移交（邀约确认）示例

```bash
# 1) alice 以当前链尾发起邀约：指定 bob、1 小时有效、确认码 "s3cret"
curl -s -X POST http://localhost:8000/offers \
  -H 'Content-Type: application/json' \
  -d '{"chain_id":"ab12...","tail_hash":"<当前链尾>","receiver":"bob",
       "expires_in_seconds":3600,"confirmation_code":"s3cret"}'
# 响应：{"offer_id":"7c4e...","status":"pending","expires_at":"...", ...}
# 服务端只保存确认码摘要；alice 通过线下渠道把 offer_id 与确认码告知 bob

# 2) bob 凭 offer_id + 确认码确认 → 原子追加移交事件，邀约置为 accepted
curl -s -X POST http://localhost:8000/offers/7c4e.../confirm \
  -H 'Content-Type: application/json' \
  -d '{"confirmation_code":"s3cret"}'
# 响应与移交结果一致：{"seq":2,"event_hash":"<新链尾>","giver":"alice","receiver":"bob", ...}

# 3) 再确认一次 → 409 offer_already_accepted；确认码错误 → 403 code_mismatch；
#    过期 → 409 offer_expired；期间链尾被其他移交推进 → 409 tail_mismatch；
#    以上均不写任何事件

# 4) 查询邀约状态（pending / accepted / expired / rejected / terminated / cancelled / replaced）
curl -s http://localhost:8000/offers/7c4e...
```

### 撤销邀约示例

```bash
# 1) alice 发起「可撤销」邀约：额外提供 cancellation_code（省略则不可撤销）
curl -s -X POST http://localhost:8000/offers \
  -H 'Content-Type: application/json' \
  -d '{"chain_id":"ab12...","tail_hash":"<当前链尾>","receiver":"bob",
       "expires_in_seconds":3600,"confirmation_code":"s3cret",
       "cancellation_code":"v0uch-99"}'
# 响应：{"offer_id":"5b8d...","status":"pending","cancellable":true, ...}
# 服务端只保存绑定 offer_id 做域分隔的撤销码 SHA-256 摘要，明文不落库

# 2) bob 确认之前，alice 凭撤销码撤销（不追加任何链上事件）
curl -s -X POST http://localhost:8000/offers/5b8d.../cancel \
  -H 'Content-Type: application/json' \
  -d '{"cancellation_code":"v0uch-99"}'
# {"offer_id":"5b8d...","status":"cancelled","cancellable":true,
#  "cancelled_at":"2026-...","tail_hash":"<邀约锁定的链尾，仍是当前链尾>"}

# 3) 撤销码错误 → 403 cancel_code_mismatch
curl -i -X POST http://localhost:8000/offers/5b8d.../cancel \
  -H 'Content-Type: application/json' \
  -d '{"cancellation_code":"wrong"}'

# 4) 已撤销后再撤销 / 接收方再确认 / 见证人再表态
#    → 409 offer_not_cancellable（不写事件，链尾不变）
curl -i -X POST http://localhost:8000/offers/5b8d.../confirm \
  -H 'Content-Type: application/json' \
  -d '{"confirmation_code":"s3cret"}'

# 5) 旧邀约（创建时省略 cancellation_code，cancellable=false）撤销
#    → 409 offer_not_cancellable；邀约过期、有见证人拒绝或链尾被推进后
#      撤销同样返回 409 offer_not_cancellable

# 6) 详情与见证查询返回 cancelled 状态，但不返回撤销码摘要
curl -s http://localhost:8000/offers/5b8d...
# {"status":"cancelled","cancellable":true,"cancelled_at":"...", ...}
```

### 原子替换邀约示例

适用于「邀约仍有效，但接收方/有效期/确认码/见证配置需要整体更换」的场景：
原邀约进入 `replaced` 终态，同时生成一条锁定**同一链尾**的全新邀约，两步在同一
SQLite 立即事务内完成。

```bash
# 1) alice 发起带撤销码的邀约（只有带撤销码的邀约才可替换）
curl -s -X POST http://localhost:8000/offers \
  -H 'Content-Type: application/json' \
  -d '{"chain_id":"ab12...","tail_hash":"<当前链尾>","receiver":"bob",
       "expires_in_seconds":3600,"confirmation_code":"s3cret",
       "cancellation_code":"v0uch-99"}'
# {"offer_id":"5b8d...","status":"pending","cancellable":true, ...}

# 2) bob 确认之前，alice 凭原撤销码原子替换：换新接收方 carol、新有效期、
#    新确认码、新撤销码与全新见证配置（见证配置可省略）
curl -s -X POST http://localhost:8000/offers/5b8d.../replace \
  -H 'Content-Type: application/json' \
  -d '{"cancellation_code":"v0uch-99",
       "receiver":"carol",
       "expires_in_seconds":7200,
       "confirmation_code":"n3w-secret",
       "new_cancellation_code":"n3w-vouch",
       "witness_threshold":2,
       "witnesses":[
         {"name":"carol-w1","token":"new-tok-1"},
         {"name":"carol-w2","token":"new-tok-2"}]}'
# 201 响应即「新邀约」，与 POST /offers 响应同构，额外带 replaces：
# {"offer_id":"9f21...","status":"pending","replaces":"5b8d...",
#  "receiver":"carol","expires_at":"...","cancellable":true,
#  "witness_mode":true,"witnesses":[{"witness_index":0,...,"token":"new-tok-1"}, ...]}

# 3) 原邀约已终止为 replaced：旧确认码/旧撤销码/旧见证令牌全部作废
curl -s http://localhost:8000/offers/5b8d...
# {"status":"replaced","replaces":null,"replaced_by":"9f21...",
#  "replaced_at":"2026-...", ...}            ← 只有关系，不含任何凭证摘要
curl -s http://localhost:8000/offers/9f21...
# {"status":"pending","replaces":"5b8d...","replaced_by":null, ...}

# 4) carol 对新邀约走正常流程（见证表态 → 凭新确认码确认）
curl -s -X POST http://localhost:8000/offers/9f21.../witness-decisions \
  -H 'Content-Type: application/json' \
  -d '{"token":"new-tok-1","decision":"approved"}'
curl -s -X POST http://localhost:8000/offers/9f21.../witness-decisions \
  -H 'Content-Type: application/json' \
  -d '{"token":"new-tok-2","decision":"approved"}'
curl -s -X POST http://localhost:8000/offers/9f21.../confirm \
  -H 'Content-Type: application/json' \
  -d '{"confirmation_code":"n3w-secret"}'
# {"seq":1,"giver":"alice","receiver":"carol", ...}  ← 事件只追加一次

# 5) 错误情形：
#    原撤销码错误            → 403 cancel_code_mismatch（原邀约保持 pending）
#    已 accepted/cancelled/replaced、已过期、链尾已推进、有见证人拒绝、
#    或创建时未提供撤销码    → 409 offer_not_replaceable（整体回滚，无半替换）
#    替换可链式进行：A→B→C，每次都须凭「上一个邀约」自己的撤销码；
#    省略 new_cancellation_code 时新邀约不可撤销、也不可再次替换。
```

### 见证确认示例

```bash
# 1) alice 发起见证邀约：3 名见证人（各持独立令牌），阈值 2
curl -s -X POST http://localhost:8000/offers \
  -H 'Content-Type: application/json' \
  -d '{"chain_id":"ab12...","tail_hash":"<当前链尾>","receiver":"bob",
       "expires_in_seconds":3600,"confirmation_code":"s3cret",
       "witness_threshold":2,
       "witnesses":[
         {"name":"carol","token":"tok-carol-001"},
         {"name":"dave","token":"tok-dave-002"},
         {"name":"erin","token":"tok-erin-003"}]}'
# 响应（令牌明文仅此一次返回，服务端只存摘要）：
# {"offer_id":"9d1a...","status":"pending","witness_mode":true,
#  "witness_threshold":2,
#  "witnesses":[{"witness_index":0,"name":"carol","token":"tok-carol-001"}, ...]}

# 2) 各见证人凭自己的令牌表态（同意 / 拒绝）
curl -s -X POST http://localhost:8000/offers/9d1a.../witness-decisions \
  -H 'Content-Type: application/json' \
  -d '{"token":"tok-carol-001","decision":"approved"}'
# {"witness_index":0,"name":"carol","decision":"approved",
#  "status":"pending","approved_count":1,"rejected_count":0,"pending_count":2, ...}

# 3) 同意数未达阈值时接收方确认 -> 409 witness_threshold_not_met，不写事件
#    任一见证人拒绝 -> 邀约即终止：确认返回 409 offer_rejected，不写事件
#    同一令牌再表态 -> 409 witness_already_decided；令牌错误 -> 403 token_mismatch

# 4) 第二名见证人同意，同意数达到阈值 2 且无拒绝 -> bob 确认，原子追加事件
curl -s -X POST http://localhost:8000/offers/9d1a.../witness-decisions \
  -H 'Content-Type: application/json' \
  -d '{"token":"tok-dave-002","decision":"approved"}'
curl -s -X POST http://localhost:8000/offers/9d1a.../confirm \
  -H 'Content-Type: application/json' \
  -d '{"confirmation_code":"s3cret"}'

# 5) 查询见证状态（响应不含令牌及其摘要）
curl -s http://localhost:8000/offers/9d1a.../witnesses
# {"offer_id":"9d1a...","witness_mode":true,"status":"accepted",
#  "witness_threshold":2,"witness_count":3,
#  "approved_count":2,"rejected_count":0,"pending_count":1,
#  "witnesses":[{"witness_index":0,"name":"carol","decision":"approved","decided_at":"..."},
#               {"witness_index":1,"name":"dave","decision":"approved","decided_at":"..."},
#               {"witness_index":2,"name":"erin","decision":null,"decided_at":null}]}
```

### 争议封存示例

```bash
# 1) alice（当前持有人）发现争议，携带当前链尾创建封存：
#    原因、有效期 24 小时、一次性解封码（服务端只存绑定 seal_id 的摘要）
curl -s -X POST http://localhost:8000/chains/ab12.../seals \
  -H 'Content-Type: application/json' \
  -d '{"tail_hash":"<当前链尾>","reason":"文件完整性存疑，等待争议处理",
       "expires_in_seconds":86400,"release_code":"unseal-2026-001"}'
# 201 响应（封存不追加任何链上事件）：
# {"seal_id":"3af2...","chain_id":"ab12...","status":"active",
#  "reason":"文件完整性存疑，等待争议处理","tail_hash":"<当前链尾>",
#  "created_at":"2026-09-20T03:00:00+00:00",
#  "expires_at":"2026-09-21T03:00:00+00:00","released_at":null}

# 2) 同一链同时只能有一个生效封存 -> 409 chain_seal_active
curl -i -X POST http://localhost:8000/chains/ab12.../seals \
  -H 'Content-Type: application/json' \
  -d '{"tail_hash":"<当前链尾>","reason":"再次封存",
       "expires_in_seconds":60,"release_code":"other"}'

# 3) 生效期间四类操作一律 409 chain_on_hold，且不写事件/新邀约：
#    直接移交
curl -i -X POST http://localhost:8000/handovers \
  -H 'Content-Type: application/json' \
  -d '{"chain_id":"ab12...","tail_hash":"<当前链尾>","receiver":"bob","nonce":"n-9"}'
#    创建邀约 / 替换邀约 / 确认邀约 同样返回 409 chain_on_hold
curl -i -X POST http://localhost:8000/offers \
  -H 'Content-Type: application/json' \
  -d '{"chain_id":"ab12...","tail_hash":"<当前链尾>","receiver":"bob",
       "expires_in_seconds":3600,"confirmation_code":"s3cret"}'

# 4) 查询封存（按 ID 或按链查最新）：只返回状态/原因/时间，绝不返回解封码摘要
curl -s http://localhost:8000/seals/3af2...
curl -s http://localhost:8000/chains/ab12.../seal
# {"seal_id":"3af2...","status":"active","reason":"...",
#  "created_at":"...","expires_at":"...","released_at":null, ...}

# 5) 争议处理完毕，持有人凭一次性解封码提前解封
curl -s -X POST http://localhost:8000/seals/3af2.../release \
  -H 'Content-Type: application/json' \
  -d '{"release_code":"unseal-2026-001"}'
# {"seal_id":"3af2...","status":"released",
#  "released_at":"2026-09-20T05:30:00+00:00", ...}

# 6) 错误情形：
#    解封码错误   -> 403 release_code_mismatch（封存保持 active）
#    重复解封     -> 409 seal_already_released
#    封存已过期   -> 409 seal_expired（到期已自动失效，无需解封）
# 过期后无需任何操作，四类操作自动恢复，且可重新创建封存。

# 7) 链尾不是最新 -> 409 tail_mismatch（仅当前持有人可凭最新链尾封存）
```

### Merkle 包含证明（可选择披露）示例

```bash
# 1) 为序号 1 的交接事件生成包含证明（事件与链尾在同一读事务快照取得）
curl -s http://localhost:8000/chains/ab12.../events/1/proof
# 200 响应（单事件链 path 为空、merkle_root 等于 leaf_hash）：
# {
#   "chain_id": "ab12...",
#   "event": {"seq": 1, "event_hash": "6aae...", "giver": "alice",
#             "receiver": "bob", "nonce": "n1", "prev_hash": "...", ...},
#   "leaf_hash": "743d...",                       # 绑定 chain_id/seq/event_hash
#   "path": [{"direction": "left", "sibling_hash": "a142..."}, ...],
#   "event_count": 2,
#   "tail_hash": "6aae...",                       # 同一快照下的当前链尾
#   "merkle_root": "9112...",
#   "snapshot_digest": "3a3d..."                  # 规范 JSON 快照摘要
# }

# 2) 离线校验：把证明响应原样回填即可（服务端不读数据库，只重算）
curl -s -X POST http://localhost:8000/proofs/verify \
  -H 'Content-Type: application/json' \
  -d @proof.json
# {"valid": true, "failed_stage": null, "reason": null,
#  "chain_id": "ab12...", "seq": 1,
#  "recomputed_leaf_hash": "743d...",
#  "recomputed_merkle_root": "9112...",
#  "recomputed_snapshot_digest": "3a3d..."}

# 3) 篡改任一关键字段都会给出首个失败阶段（仍返回 200，valid=false）：
#    event_hash / chain_id / seq 被改      -> failed_stage="merkle_root"
#    tail_hash 被改（snapshot_digest 未改） -> failed_stage="snapshot_digest"
#    path 少一层 / 左右方向颠倒             -> failed_stage="path"
#    event_count <= 0                       -> failed_stage="event_count"
#    seq 不在 [0, event_count)              -> failed_stage="seq"

# 4) seq 越界 -> 404 event_not_found；链条不存在 -> 404 not_found
curl -i http://localhost:8000/chains/ab12.../events/999/proof
```

### 交接事件审计检索示例

```bash
# 1) 跨链按 created_at,id 升序取首页（limit 1～100，默认 50）
curl -s "http://localhost:8000/audit/events?limit=50"
# {
#   "events": [
#     {"id":1,"chain_id":"ab12...","seq":0,"giver":"","receiver":"",
#      "nonce":"","prev_hash":"","event_hash":"9f0c...",
#      "created_at":"2026-09-20T03:00:00+00:00"},
#     {"id":2,"chain_id":"ab12...","seq":1,"giver":"alice","receiver":"bob", ...}
#   ],
#   "next_cursor": "eyJmIjoi....<HMAC 签名>"
# }

# 2) 按持有人与角色过滤：创世事件仅在 role=either 时按首位持有人匹配
curl -s "http://localhost:8000/audit/events?holder=alice&role=either"
#   命中：A 链创世（first_holder=alice）+ alice 作为 giver 的移交
curl -s "http://localhost:8000/audit/events?holder=alice&role=giver"
#   只命中 alice 作为交出方的移交（创世事件不入选）
curl -s "http://localhost:8000/audit/events?holder=alice&role=receiver"
#   只命中 alice 作为接收方的移交（无结果则 events=[]、next_cursor=null）

# 3) 时间窗口（ISO-8601，闭区间；Z 与 +00:00 等价）+ 限定链
curl -s "http://localhost:8000/audit/events?chain_id=ab12...&\
created_from=2026-09-01T00:00:00Z&created_to=2026-09-30T23:59:59Z"

# 4) 翻页：原样回传 next_cursor，过滤条件必须与首次查询一致（仅 limit 可变）
curl -s "http://localhost:8000/audit/events?limit=50&next_cursor=eyJmIjoi...."
# 翻页期间新追加的移交在本次快照之外，不会重复或漏项；
# 取完快照内全部事件后 next_cursor 为 null。

# 5) 错误情形（均为 400，且不会回退为新查询）：
#    游标被篡改/密钥已轮换        -> invalid_cursor_signature
#    游标格式非法                -> invalid_cursor
#    换了过滤条件继续翻页          -> cursor_filter_mismatch
#    只给 role 不给 holder       -> role_requires_holder
#    时间非法 / from 晚于 to     -> invalid_time_range
# 重新发起一次不带 next_cursor 的查询才会获得当前时刻的新快照。
```

### 保管负荷报表示例

适用于「统计某位持有人在一段时间内同时保管多少条链」的场景：报表在**同一个
读事务**内固定事件快照，仅用该快照重建区间，只读、不改变任何交接流程。

```bash
# 1) 查询 alice 在 9 月（半开 [start, end)，UTC）的保管负荷
curl -s "http://localhost:8000/reports/custody-load?holder=alice&\
start=2026-09-01T00:00:00Z&end=2026-10-01T00:00:00Z"
# {
#   "holder": "alice",
#   "start": "2026-09-01T00:00:00+00:00",
#   "end": "2026-10-01T00:00:00+00:00",
#   "snapshot_event_id": 42,
#   "peak_chain_count": 3,
#   "total_chain_seconds": 684000,
#   "peak_intervals": [
#     {"start": "2026-09-05T08:00:00+00:00", "end": "2026-09-06T02:00:00+00:00"},
#     {"start": "2026-09-12T00:00:00+00:00", "end": "2026-09-12T06:00:00+00:00"}
#   ]
# }
# 含义：同一读事务锚定到第 42 号事件；窗口内最多同时持有 3 条链；
# 达到 3 条的连续时段共两段（相邻同峰值段已合并）；
# 持有链数对时间积分共 684000 链·秒（=190 链·小时）。

# 2) 携带非 UTC 时区：服务端自动换算（+08:00 的 08:00 即 UTC 00:00）
curl -s "http://localhost:8000/reports/custody-load?holder=alice&\
start=2026-09-01T08:00:00%2B08:00&end=2026-10-01T08:00:00%2B08:00"

# 3) 该人窗口内从未持有（或全部为同秒瞬态）：
# {"holder":"dave", ..., "peak_chain_count":0,
#  "total_chain_seconds":0, "peak_intervals":[], "snapshot_event_id":42}

# 4) 错误情形（均为 400 invalid_time_range）：
#    缺时区（朴素时间） / 时间非法 / start 不早于 end / end 晚于当前时刻
curl -i "http://localhost:8000/reports/custody-load?holder=alice&\
start=2026-09-01T00:00:00&end=2026-10-01T00:00:00Z"
# 400 {"error":"invalid_time_range",
#      "detail":"start 必须携带时区偏移量（如 +08:00 或 Z），朴素时间不被接受: ..."}
```

重建语义补充：同一秒内链 A→B→C 的连续移交，B 的持有是零时长瞬态，**不贡献
峰值与积分**；B 自转交（A→A）持有连续，**不增加负荷**；窗口开始前已在持有的
链会正确还原并自 `start` 起计入（末段开放区间裁至 `end`）。

### 批量 Merkle multiproof（批量可选择披露）示例

```bash
# 1) 一次为序号 1、3、5 的交接事件生成一份 multiproof（seq 可重复传入；
#    服务端去重并升序，事件与链尾在同一读事务快照取得）
curl -s "http://localhost:8000/chains/ab12.../events/proof?seq=5&seq=1&seq=3&seq=1"
# 200 响应（只含被选事件，proof_nodes 按 (level,index) 升序且不重复）：
# {
#   "chain_id": "ab12...",
#   "events": [{"seq": 1, "event_hash": "...", ...},
#              {"seq": 3, "event_hash": "...", ...},
#              {"seq": 5, "event_hash": "...", ...}],
#   "event_count": 7,
#   "tail_hash": "845f...",
#   "merkle_root": "3022...",
#   "snapshot_digest": "9396...",
#   "proof_nodes": [
#     {"level": 0, "index": 0, "node_hash": "3d90..."},
#     {"level": 0, "index": 2, "node_hash": "d19f..."},
#     {"level": 0, "index": 4, "node_hash": "6919..."},
#     {"level": 1, "index": 3, "node_hash": "92e6..."}
#   ]
# }
# 奇数层末节点与自身复制配对的位置不产生证明节点；选中全部叶子时
# proof_nodes 为空列表；单事件链同样为空。

# 2) 离线校验：把批量证明响应原样回填即可（不读数据库，只按坐标重建根）
curl -s -X POST http://localhost:8000/proofs/verify-batch \
  -H 'Content-Type: application/json' \
  -d @batch-proof.json
# {"valid": true, "failed_stage": null, "reason": null,
#  "chain_id": "ab12...", "seqs": [1, 3, 5],
#  "recomputed_leaf_hashes": ["4f59...", ...],
#  "recomputed_merkle_root": "3022...",
#  "recomputed_snapshot_digest": "9396..."}

# 3) 非法证明一律拒绝（仍返回 200，valid=false，给出首个失败阶段）：
#    events 重复序号 / 序号越界           -> failed_stage="events"
#    缺失必需节点 / 多余节点 / 坐标重复
#      / 坐标与重算值冲突 / 坐标越界
#      （含为奇数层末节点虚构配对节点）   -> failed_stage="proof_nodes"
#    篡改 event_hash / chain_id / seq    -> failed_stage="merkle_root"
#    篡改 tail_hash                      -> failed_stage="snapshot_digest"
#    event_count < 1                     -> failed_stage="event_count"

# 4) 去重后超过 20 个序号 -> 400 too_many_seqs；任一序号越界 -> 404 event_not_found
curl -i "http://localhost:8000/chains/ab12.../events/proof?seq=0&seq=1&...&seq=20"
```

哈希细节（三种固定域分隔，互不构成第二原像）：

- 叶子：`SHA256("merkle-leaf:v1:" + sort_keys_json({"chain_id":…,"event_hash":…,"seq":…}))`
- 内部节点：`SHA256("merkle-node:v1:" + left_hex + right_hex)`；奇数层末节点
  与自身复制配对（路径中体现为 `direction="right"` 且 `sibling_hash` 为当前节点）
- 快照：`SHA256("merkle-snapshot:v1:" + sort_keys_json({"chain_id":…,"event_count":…,"merkle_root":…,"tail_hash":…}))`

校验响应示例（正常）：

```json
{
  "chain_id": "ab12...",
  "valid": true,
  "length": 3,
  "expected_next_seq": 3,
  "first_broken_seq": null,
  "reason": null,
  "recomputed_tail_hash": "…",
  "stored_tail_hash": "…"
}
```

若中途某条事件被篡改，响应形如
`{"valid": false, "first_broken_seq": 2, "reason": "事件哈希不匹配：…"}`。

## 防分叉与并发保证

1. 每次追加都在 `BEGIN IMMEDIATE` 事务内完成，且进程内写操作由互斥锁串行化；
   「读链尾 → 比对 → 插入 → 推进」是一个原子步骤。**批量移交
   （`POST /handover-batches`）同样如此：同一批 2～20 条链的「逐链校验 → 追加
   事件 → 推进各链尾」全部在同一个立即事务内按请求顺序完成，任一链校验失败即
   整批回滚，任何链都不新增事件。**邀约确认同样如此：
   「验码 → 未过期 → 未使用 → 未撤销 → 链尾匹配 → （见证模式）无拒绝且同意数
   达阈值 → 追加事件 → 置 accepted」全部在同一立即事务内，任一校验失败即整体
   回滚。**邀约撤销、原子替换、接收方确认与见证人表态共用同一把写锁与同一立即
   事务语义，四者互斥**：撤销的「验撤销码 → 仍 pending → 未过期 → 链尾未推进 →
   无拒绝 → 置 cancelled（且不追加任何链上事件）」整体原子；原子替换的「验原
   撤销码 → 仍 pending → 未过期 → 链尾未推进 → 无拒绝 → 插入锁定同一链尾的新
   邀约（`replaces` 指向旧邀约）→ 旧邀约置 `replaced`（`replaced_by` 指向新
   邀约）」同样整体原子，并以 `UPDATE ... WHERE status='pending'` 的影响行数
   兜底。竞争终态（accepted / cancelled / replaced / rejected）中最多一个成功，
   任一校验失败或影响行数异常都整体回滚——旧邀约未置终态时新邀约绝不可能留下
   （不产生半替换），替换成功后哈希链完全不变（确认新邀约时才追加事件）。
   见证人表态的「按令牌摘要定位见证人 → 查未表态 → 写入 decision」也在同一
   立即事务内串行完成，同一令牌并发表态最多一次成功。
2. 数据库层设有 `UNIQUE(chain_id, seq)` 与 `UNIQUE(chain_id, nonce)` 唯一约束
   作为兜底，任何竞争写入都无法产生重号事件或重放随机数；邀约确认使用
   派生随机数 `offer:<offer_id>`，同一邀约并发确认最多成功一次。撤销与替换均以
   `UPDATE ... WHERE status='pending'` 的影响行数兜底，竞争落败即回滚返回 409；
   替换产生的新旧邀约由自引用外键 `replaces` / `replaced_by` 互相绑定（并以
   CHECK 保证二者与 `replaced_at`、`status='replaced'` 一致），不可能出现只建了
   新邀约却没终结旧邀约的悬挂行。见证人表设有 `UNIQUE(offer_id, witness_index)`
   与 `UNIQUE(offer_id, token_digest)`，且表态以「行已存在 decision」判定一次性，
   重复使用令牌不可能产生第二条表态。**争议封存由 `chain_seals` 表上的部分唯一
   索引 `uq_active_seal_per_chain`（`WHERE status = 'active'`）兜底同一链至多一个
   生效封存；直接移交、邀约创建/替换/确认都在各自立即事务的任何写入之前检查生效
   封存并返回 409 `chain_on_hold`，与封存创建共用同一把写锁——封存先提交则操作
   全部落败且不写事件/新邀约，操作先提交则封存因链尾推进（`tail_mismatch`）落败，
   并发双方至多一个成功；封存与解封均不追加哈希链事件，CHECK 约束还保证
   `released_at` 仅随 `released` 终态出现。**
3. WAL 日志模式 + busy timeout，兼顾读写并发与单写入者语义。
4. 数据库初始化（`init_db`）全部使用 `CREATE ... IF NOT EXISTS`，可重复执行；
   对已有数据文件只做增量补表（含 `offers`、`offer_witnesses`、`chain_seals` 表）；对旧版
   `offers` 表按 SQLite 官方「建新表 → 搬数据 → 改名」流程一次性就地重建，
   增补 `witness_threshold`（旧无见证邀约为 NULL）、`cancel_code_digest` /
   `cancelled_at`（旧邀约撤销码为 NULL，即不可撤销/不可替换）以及
   `replaces` / `replaced_by` / `replaced_at`（旧邀约之间无替换关系，均补 NULL），
   并把 status 的 CHECK 扩展为 pending / accepted / cancelled / replaced，
   存量邀约与事件不受影响。

## 项目结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由与启动
│   ├── audit.py       # 交接事件审计检索：快照上界翻页、过滤条件摘要与 HMAC 签名游标
│   ├── reports.py     # 保管负荷报表：同一读事务固定事件快照，重建持有人区间、裁剪窗口并统计峰值/区间/积分（只读）
│   ├── backup.py      # 数据库归档命令行：python -m app.backup create/verify（SQLite 在线备份一致快照 + 清单/摘要/结构核验，原子发布）
│   ├── service.py     # 哈希链核心逻辑（创建 / 追加 / 邀约确认 / 撤销 / 原子替换 / 见证 / 争议封存 / 查询 / 校验 / Merkle 包含证明与批量 multiproof 的生成与校验）
│   ├── merkle.py      # Merkle 包含证明纯函数：叶子/内部节点/快照摘要、单事件与批量 multiproof 的构造与离线重算（不读数据库）
│   ├── database.py    # SQLite 连接、建表与旧 offers 表重建迁移、串行写事务、业务表参考结构
│   ├── hashing.py     # 紧凑 JSON 规范与 SHA-256
│   ├── schemas.py     # Pydantic 请求/响应模型
│   └── config.py      # 路径与端口配置
├── Dockerfile
├── docker-compose.yml
├── docker-entrypoint.sh
├── requirements.txt
└── README.md
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATA_DIR` | `/data` | 数据目录 |
| `DB_PATH` | `$DATA_DIR/handoff.db` | SQLite 文件路径 |
| `PORT` | `8000` | 监听端口（容器内，Compose 映射同为 8000） |
| `HOST` | `0.0.0.0` | 监听地址 |
| `AUDIT_CURSOR_SECRET` | `dev-only-audit-cursor-secret-change-me`（仅开发默认值） | `GET /audit/events` 翻页游标的 HMAC-SHA256 签名密钥。Compose 与镜像内置同一**开发默认值**保证一键启动，可用同名环境变量或 `.env` 覆盖；**生产部署必须设置为强随机值**（如 `openssl rand -hex 32`）。密钥轮换后此前签发的游标全部返回 400 `invalid_cursor_signature`，重新发起首次查询即可 |
