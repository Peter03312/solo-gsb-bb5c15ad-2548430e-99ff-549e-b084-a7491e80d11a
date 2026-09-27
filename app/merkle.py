"""Merkle 包含证明：生成与「仅依据请求体」的离线重算。

本模块是无状态纯函数集合，**不读取数据库**：生成侧（service 层）在同一读事务
内取齐事件与链尾快照后调用 build_inclusion_proof；校验侧（POST /proofs/verify）
仅凭请求体重算叶子、兄弟路径与 snapshot_digest，两侧共用同一套哈希规则，因此
校验请求甚至可以直接回填证明响应。

哈希规则（全部为 SHA-256 的 64 位十六进制摘要）
------------------------------------------------
叶子（绑定 chain_id、seq、event_hash，防止跨链/跨序号替换证明）::

    leaf = SHA256("merkle-leaf:v1:" + sort_keys_json({
        "chain_id": ..., "event_hash": ..., "seq": ...,
    }))

内部节点（左/右子节点均为定长 64 位十六进制，拼接无歧义；域前缀与叶子、
快照分离，防第二原像）::

    node = SHA256("merkle-node:v1:" + left_hex + right_hex)

奇数层末节点复制配对：某一层节点数为奇数时，最后一个节点与自身配对
（right 方向兄弟就是它自己），不引入其他填充值。单叶树（event_count=1）的
merkle_root 即该叶哈希，兄弟路径为空。

链尾快照摘要（把证明锚定到「某条链在某次读事务下的事件集合 + 当前链尾」）::

    snapshot_digest = SHA256("merkle-snapshot:v1:" + sort_keys_json({
        "chain_id": ..., "event_count": ...,
        "merkle_root": ..., "tail_hash": ...,
    }))
"""
from __future__ import annotations

from typing import Any, Optional

from .hashing import canonical_json, digest_hex

# 固定域分隔前缀：叶子 / 内部节点 / 链尾快照分属不同域，互不构成第二原像。
LEAF_DOMAIN = "merkle-leaf:v1:"
NODE_DOMAIN = "merkle-node:v1:"
SNAPSHOT_DOMAIN = "merkle-snapshot:v1:"

# 校验失败阶段（首个失败阶段即停止，顺序见 verify_event_proof）。
STAGE_EVENT_COUNT = "event_count"
STAGE_SEQ = "seq"
STAGE_PATH = "path"
STAGE_ROOT = "merkle_root"
STAGE_SNAPSHOT = "snapshot_digest"

# 批量证明（multiproof）专用的失败阶段：events 取代单证明的 seq，
# proof_nodes 取代单证明的 path，其余阶段与单证明一致。
STAGE_EVENTS = "events"
STAGE_PROOF_NODES = "proof_nodes"

# 批量证明单次选择披露的事件序号上限（去重后计）。
MAX_MULTI_PROOF_LEAVES = 20


def leaf_hash(chain_id: str, seq: int, event_hash: str) -> str:
    """叶子哈希：域分隔前缀 + 绑定 chain_id/seq/event_hash 的规范 JSON。"""
    payload = {
        "chain_id": chain_id,
        "event_hash": event_hash,
        "seq": seq,
    }
    return digest_hex(LEAF_DOMAIN + canonical_json(payload))


def internal_hash(left: str, right: str) -> str:
    """内部节点哈希：域分隔前缀 + 左、右子节点定长十六进制摘要拼接。"""
    return digest_hex(f"{NODE_DOMAIN}{left}{right}")


def snapshot_digest(
    chain_id: str, event_count: int, tail_hash: str, merkle_root: str
) -> str:
    """链尾快照摘要：把 root 锚定到 chain_id / 事件数 / 当前链尾。"""
    payload = {
        "chain_id": chain_id,
        "event_count": event_count,
        "merkle_root": merkle_root,
        "tail_hash": tail_hash,
    }
    return digest_hex(SNAPSHOT_DOMAIN + canonical_json(payload))


def tree_depth(event_count: int) -> int:
    """从叶子到根的层数（即兄弟路径长度）：n=1 时为 0。"""
    if event_count < 1:
        raise ValueError("event_count 必须 >= 1")
    return (event_count - 1).bit_length()


def _parent_level(level: list[str]) -> list[str]:
    """上一层：两两配对；奇数层末节点与自身复制配对。"""
    nxt: list[str] = []
    for i in range(0, len(level), 2):
        left = level[i]
        right = level[i + 1] if i + 1 < len(level) else level[i]
        nxt.append(internal_hash(left, right))
    return nxt


def build_inclusion_proof(
    chain_id: str, event_hashes: list[str], seq: int
) -> dict[str, Any]:
    """按当前事件集合（seq 升序的 event_hash 列表）构造包含证明。

    返回 {"event_count", "leaf_hash", "merkle_root", "path"}，path 自叶子向根，
    每项 {"direction": "left"|"right", "sibling_hash": ...}：direction 表示兄弟
    相对当前节点的方向（left=先拼兄弟再拼当前节点）。调用方须保证 seq 在界内、
    事件与链尾取自同一读事务快照。
    """
    event_count = len(event_hashes)
    if event_count < 1:
        raise ValueError("事件集合为空，无法构造 Merkle 证明")
    if not isinstance(seq, int) or not (0 <= seq < event_count):
        raise ValueError(f"seq 越界: {seq}（事件数 {event_count}）")

    level = [
        leaf_hash(chain_id, index, event_hash)
        for index, event_hash in enumerate(event_hashes)
    ]
    target_leaf = level[seq]

    path: list[dict[str, str]] = []
    pos = seq
    while len(level) > 1:
        size = len(level)
        if pos % 2 == 1:
            # 当前节点是右孩子：兄弟在左。
            path.append(
                {"direction": "left", "sibling_hash": level[pos - 1]}
            )
        elif size % 2 == 1 and pos == size - 1:
            # 奇数层末节点：与自身复制配对，兄弟（右）即当前节点。
            path.append(
                {"direction": "right", "sibling_hash": level[pos]}
            )
        else:
            path.append(
                {"direction": "right", "sibling_hash": level[pos + 1]}
            )
        level = _parent_level(level)
        pos //= 2

    return {
        "event_count": event_count,
        "leaf_hash": target_leaf,
        "merkle_root": level[0],
        "path": path,
    }


def evaluate_inclusion(
    leaf: str, seq: int, path: list[tuple[str, str]], event_count: int
) -> tuple[Optional[str], Optional[str]]:
    """仅依据请求体重算 Merkle 包含关系（不访问数据库）。

    返回 (recomputed_root, failed_stage)：成功时 failed_stage 为 None；
    路径结构不合法（层数不符、方向与序号不一致、奇数末位未正确复制配对或在
    非复制位置伪造复制）时阶段为 "path"；event_count < 1 / seq 越界分别为
    "event_count" / "seq"，此时 root 为 None。
    """
    if not isinstance(event_count, int) or event_count < 1:
        return None, STAGE_EVENT_COUNT
    if not isinstance(seq, int) or not (0 <= seq < event_count):
        return None, STAGE_SEQ

    if len(path) != tree_depth(event_count):
        return None, STAGE_PATH

    node = leaf
    pos = seq
    level_size = event_count
    for direction, sibling in path:
        if direction == "left":
            # 兄弟在左意味着当前节点必须是右孩子（pos 为奇数）。
            if pos % 2 != 1:
                return None, STAGE_PATH
            node = internal_hash(sibling, node)
        elif direction == "right":
            if pos % 2 != 0:
                return None, STAGE_PATH
            odd_last = level_size % 2 == 1 and pos == level_size - 1
            if odd_last:
                # 奇数层末节点必须与自身复制配对。
                if sibling != node:
                    return None, STAGE_PATH
            elif sibling == node:
                # 其余位置不允许复制配对（不同节点哈希相同仅在碰撞时发生）。
                return None, STAGE_PATH
            node = internal_hash(node, sibling)
        else:
            return None, STAGE_PATH

        pos //= 2
        level_size = (level_size + 1) // 2

    return node, None


# ---------------------------------------------------------------------------
# 批量包含证明（Merkle multiproof）：一次证明多个叶子，共享兄弟节点
# ---------------------------------------------------------------------------


def tree_level_size(event_count: int, level: int) -> int:
    """第 level 层的节点数：每层两两配对（奇数层末节点自复制），向上取整。

    level 0 即叶子层（event_count 个节点）；level 超过树深时恒为 1（根）。
    """
    if event_count < 1:
        raise ValueError("event_count 必须 >= 1")
    if level < 0:
        raise ValueError("level 必须 >= 0")
    size = event_count
    for _ in range(level):
        if size == 1:
            break
        size = (size + 1) // 2
    return size


def build_multi_proof(
    chain_id: str, event_hashes: list[str], seqs: list[int]
) -> dict[str, Any]:
    """为去重且升序的多个序号构造一份 Merkle multiproof。

    返回 {"merkle_root", "proof_nodes"}；proof_nodes 是重建根所需的**最小**
    兄弟节点集合，每项 {"level", "index", "node_hash"}（level 0 为叶子层），
    按 (level, index) 升序且不重复。奇数层末节点与自身复制配对，其「兄弟」
    即其自身、永远已知，因此自复制位置绝不产生证明节点。调用方须保证 seqs
    非空、去重、升序且全部在 [0, len(event_hashes)) 内。
    """
    event_count = len(event_hashes)
    if event_count < 1:
        raise ValueError("事件集合为空，无法构造 Merkle 证明")
    if not seqs or len(seqs) > MAX_MULTI_PROOF_LEAVES:
        raise ValueError(
            f"批量证明的序号数量必须在 1～{MAX_MULTI_PROOF_LEAVES} 之间"
        )
    if len(set(seqs)) != len(seqs) or seqs != sorted(seqs):
        raise ValueError("seqs 必须去重且升序")
    if any(not isinstance(s, int) or s < 0 or s >= event_count for s in seqs):
        raise ValueError(f"seqs 越界（事件数 {event_count}）")

    # 自叶子向根逐层构造整棵树（奇数层末节点与自身复制配对）。
    levels = [
        [leaf_hash(chain_id, index, eh) for index, eh in enumerate(event_hashes)]
    ]
    while len(levels[-1]) > 1:
        levels.append(_parent_level(levels[-1]))

    # 逐层收集「已知节点（被选叶子的祖先）所缺失的兄弟」：
    # 兄弟本身已知（同为被选叶子的祖先）时无需提供；奇数层末节点与自身
    # 配对，其兄弟即自身，同样无需提供。
    known = set(seqs)
    nodes: list[dict[str, Any]] = []
    level = 0
    while len(levels[level]) > 1:
        size = len(levels[level])
        next_known: set[int] = set()
        needed: set[int] = set()
        for index in known:
            next_known.add(index // 2)
            sibling = index + 1 if index % 2 == 0 else index - 1
            if sibling < size and sibling not in known:
                needed.add(sibling)
        for sibling in sorted(needed):
            nodes.append(
                {
                    "level": level,
                    "index": sibling,
                    "node_hash": levels[level][sibling],
                }
            )
        known = next_known
        level += 1

    nodes.sort(key=lambda node: (node["level"], node["index"]))
    return {"merkle_root": levels[-1][0], "proof_nodes": nodes}


def evaluate_multi_inclusion(
    chain_id: str,
    event_count: int,
    events: list[tuple[int, str]],
    proof_nodes: list[tuple[int, int, str]],
) -> tuple[Optional[str], Optional[str], dict[str, Any]]:
    """仅依据请求体按节点坐标重建 Merkle 根（不访问数据库）。

    events 为 (seq, event_hash) 列表；proof_nodes 为 (level, index, node_hash)
    列表。返回 (recomputed_root, failed_stage, context)：成功时 failed_stage
    为 None；否则 root 为 None，context 携带定位失败位置的坐标/序号信息。
    失败阶段顺序为 event_count -> events -> proof_nodes；根与快照摘要的比对
    由调用方在本函数返回后继续（与单事件证明一致）。

    proof_nodes 阶段的拒绝情形：坐标越界（level/index 超出树形范围，含为奇数
    层末节点虚构「第 size 个节点」等违反自复制规则的情形）、坐标重复、缺失
    必需节点、与重算值冲突的坐标，以及任何未被消费的多余节点。
    """
    if not isinstance(event_count, int) or event_count < 1:
        return None, STAGE_EVENT_COUNT, {"event_count": event_count}

    if not events or len(events) > MAX_MULTI_PROOF_LEAVES:
        return None, STAGE_EVENTS, {"event_count": len(events)}
    seqs = [seq for seq, _ in events]
    if len(set(seqs)) != len(seqs):
        return None, STAGE_EVENTS, {"duplicate_seq": True}
    for seq in seqs:
        if not isinstance(seq, int) or seq < 0 or seq >= event_count:
            return None, STAGE_EVENTS, {"seq": seq}

    # 坐标合法性：必须在树形范围内（level 0..depth-1，index < 该层节点数；
    # 根坐标与「奇数层末节点之外虚构的配对节点」均不在范围内），且不重复。
    depth = tree_depth(event_count)
    provided: dict[tuple[int, int], str] = {}
    for level, index, node_hash in proof_nodes:
        if (
            not isinstance(level, int)
            or not isinstance(index, int)
            or level < 0
            or level >= depth
            or index < 0
            or index >= tree_level_size(event_count, level)
        ):
            return (
                None,
                STAGE_PROOF_NODES,
                {"out_of_bounds": True, "level": level, "index": index},
            )
        if (level, index) in provided:
            return (
                None,
                STAGE_PROOF_NODES,
                {"duplicate_coordinate": True, "level": level, "index": index},
            )
        provided[(level, index)] = node_hash

    # 逐层重建：已知节点（被选叶子及其祖先）的兄弟依次取「同层已知 -> 证明
    # 节点 -> 奇数层末节点自复制」，三者都拿不到即缺失节点。
    known: dict[int, str] = {
        seq: leaf_hash(chain_id, seq, event_hash) for seq, event_hash in events
    }
    known_hashes: dict[tuple[int, int], str] = {
        (0, seq): node for seq, node in known.items()
    }
    consumed: set[tuple[int, int]] = set()
    size = event_count
    level = 0
    while size > 1:
        next_known: dict[int, str] = {}
        for index in sorted(known):
            current = known[index]
            sibling_index = index + 1 if index % 2 == 0 else index - 1
            if sibling_index >= size:
                # 奇数层末节点：与自身复制配对，无需任何证明节点。
                sibling = current
            elif sibling_index in known:
                sibling = known[sibling_index]
            elif (level, sibling_index) in provided:
                sibling = provided[(level, sibling_index)]
                consumed.add((level, sibling_index))
            else:
                return (
                    None,
                    STAGE_PROOF_NODES,
                    {"missing": True, "level": level, "index": sibling_index},
                )
            if index % 2 == 0:
                parent = internal_hash(current, sibling)
            else:
                parent = internal_hash(sibling, current)
            next_known[index // 2] = parent
        known = next_known
        for index, node in next_known.items():
            known_hashes[(level + 1, index)] = node
        size = (size + 1) // 2
        level += 1
    root = known[0]

    # 证明节点必须最小且互不冲突：落在已知坐标上的，与重算值不一致即坐标
    # 冲突、一致也属多余；其余未被消费的同样多余。
    for (node_level, node_index), node_hash in sorted(provided.items()):
        computed = known_hashes.get((node_level, node_index))
        if computed is not None:
            if computed != node_hash:
                return (
                    None,
                    STAGE_PROOF_NODES,
                    {
                        "conflict": True,
                        "level": node_level,
                        "index": node_index,
                    },
                )
            return (
                None,
                STAGE_PROOF_NODES,
                {"extra": True, "level": node_level, "index": node_index},
            )
        if (node_level, node_index) not in consumed:
            return (
                None,
                STAGE_PROOF_NODES,
                {"extra": True, "level": node_level, "index": node_index},
            )

    return root, None, {}
