"""SQLite 持久化层。

五张表：
- chains：链条头信息与链尾缓存（tail_hash / current_holder 是事件表的可信投影）
- events：链上的全部事件，序号 0 为创世记录（文件摘要 + 首位持有人），
  序号 >=1 为移交记录。自增 id 单调递增，除链内 (chain_id, seq) 索引外还建有
  (created_at, id) 复合索引，供交接事件审计检索（GET /audit/events）跨链按
  创建时间与 id 稳定排序、键集翻页。
- offers：待接收移交邀约。只保存一次性确认码的 SHA-256 摘要（code_digest），
  不保存明文；tail_hash 记录邀约发起时锁定的链尾，确认时必须仍然匹配。
  witness_threshold 为 NULL 表示无见证邀约（兼容存量数据），否则为通过阈值。
  cancel_code_digest 为可选撤销码的 SHA-256 摘要（绑定 offer_id 做域分隔），
  为 NULL 表示该邀约创建时未提供撤销码、不可撤销（兼容存量邀约）；
  status 取值 pending / accepted / cancelled / replaced（rejected / expired /
  terminated 为派生状态，不额外改写邀约行），cancelled_at 记录撤销时刻。
  replaces 指向被本邀约原子替换掉的旧邀约（由替换产生的新邀约行非空），
  replaced_by 指向替换本邀约而产生的新邀约（被替换的旧邀约行非空）；链式
  替换（A→B→C）时中间邀约两列同时非空，replaced_at 记录替换时刻。替换与
  确认/撤销/见证表态共用同一立即事务，竞争终态中至多一个成功，绝不产生
  半替换。
- offer_witnesses：见证邀约的见证人。只保存一次性令牌的 SHA-256 摘要
  （token_digest，与 offer_id 做域分隔），不存明文；decision 为 NULL 表示
  尚未表态，同一见证人至多表态一次（令牌本身即一次性）。拒绝/过期/链尾推进
  等终止状态为派生状态，不额外改写邀约行。
- chain_seals：交接链的争议封存。只保存「绑定 seal_id 做域分隔」的一次性
  解封码 SHA-256 摘要（release_code_digest），明文绝不落库；封存不追加任何
  哈希链事件。status 仅存储 active / released 两种：released 为凭解封码提前
  解封后的终态；封存行一旦写入，其过期时刻 expires_at 不再变化，故「过期」
  不在写路径上落库，而由读/写事务按 now > expires_at 实时派生为 expired
  （与邀约过期同样是派生状态）。创建新封存前，同链所有已过期的 active 旧封存
  会在同一立即事务内物化为 expired，随后再插入新行；部分唯一索引
  uq_active_seal_per_chain 保证「同一链同时至多一个 active 存储行」，与立即
  事务共同兜底：封存创建与直接移交、邀约创建/替换/确认竞争时只有一方成功。

防分叉保障：
1. 所有写操作在进程内串行锁 + SQLite `BEGIN IMMEDIATE` 中完成，
   追加前校验链尾哈希，校验与插入在同一事务内原子完成；
2. (chain_id, seq) 唯一、(chain_id, nonce) 唯一，由数据库兜底，
   任何竞争写入都不可能产生第二条分叉链或重放随机数；
3. chain_seals 上的部分唯一索引 uq_active_seal_per_chain
   （WHERE status = 'active'）兜底「同一链同时至多一个生效封存」，
   封存创建与移交/邀约写操作并发时必有一方失败。

建表语句全部为 CREATE ... IF NOT EXISTS，init_db() 可重复执行，
对已有数据库文件只做增量补表/补列/重建，不触碰存量数据。
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from . import config

# 进程内写锁：保证“读链尾 -> 校验 -> 追加提交”整体串行
_write_lock = threading.RLock()

# offers 表列定义（建表与旧表重建共用，保证两处结构一致）
_OFFERS_COLUMNS = """
    offer_id            TEXT PRIMARY KEY,
    chain_id            TEXT NOT NULL,
    tail_hash           TEXT NOT NULL,
    receiver            TEXT NOT NULL,
    code_digest         TEXT NOT NULL,
    witness_threshold   INTEGER,
    status              TEXT NOT NULL DEFAULT 'pending',
    expires_at          TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    accepted_at         TEXT,
    accepted_seq        INTEGER,
    cancel_code_digest  TEXT,
    cancelled_at        TEXT,
    replaces            TEXT,
    replaced_by         TEXT,
    replaced_at         TEXT,
    CHECK(status IN ('pending', 'accepted', 'cancelled', 'replaced')),
    CHECK(witness_threshold IS NULL OR witness_threshold >= 1),
    -- replaced_by 与 replaced_at 成对出现：仅当邀约被原子替换时同时写入
    CHECK((replaced_by IS NULL) = (replaced_at IS NULL)),
    -- 进入 replaced 存储状态的邀约必须指向替换它的新邀约
    CHECK(status <> 'replaced' OR replaced_by IS NOT NULL),
    FOREIGN KEY(chain_id) REFERENCES chains(chain_id),
    FOREIGN KEY(replaces) REFERENCES offers(offer_id),
    FOREIGN KEY(replaced_by) REFERENCES offers(offer_id)
"""

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS chains (
    chain_id        TEXT PRIMARY KEY,
    file_hash       TEXT NOT NULL,
    first_holder    TEXT NOT NULL,
    tail_hash       TEXT NOT NULL,
    current_holder  TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id      TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    prev_hash     TEXT NOT NULL DEFAULT '',
    giver         TEXT NOT NULL DEFAULT '',
    receiver      TEXT NOT NULL DEFAULT '',
    nonce         TEXT NOT NULL DEFAULT '',
    event_hash    TEXT NOT NULL,
    canonical     TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE(chain_id, seq),
    UNIQUE(chain_id, nonce),
    FOREIGN KEY(chain_id) REFERENCES chains(chain_id)
);

CREATE INDEX IF NOT EXISTS idx_events_chain_seq
    ON events(chain_id, seq);

-- 交接事件审计检索（GET /audit/events）跨链按 (created_at, id) 升序翻页：
-- 复合索引覆盖排序与键集翻页的 created_at/id 比较；事件只追加，建索引无写放大顾虑。
CREATE INDEX IF NOT EXISTS idx_events_created_id
    ON events(created_at, id);

CREATE TABLE IF NOT EXISTS offers (
{_OFFERS_COLUMNS}
);

CREATE INDEX IF NOT EXISTS idx_offers_chain
    ON offers(chain_id);

-- idx_offers_replaces 在 init_db 的迁移检查之后创建：旧版 offers 表尚无
-- replaces 列，直接在此建索引会失败；迁移完成（或确认无需迁移）后补建。

CREATE TABLE IF NOT EXISTS offer_witnesses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id        TEXT NOT NULL,
    witness_index   INTEGER NOT NULL,
    name            TEXT NOT NULL,
    token_digest    TEXT NOT NULL,
    decision        TEXT,
    decided_at      TEXT,
    CHECK(decision IS NULL OR decision IN ('approved', 'rejected')),
    UNIQUE(offer_id, witness_index),
    UNIQUE(offer_id, token_digest),
    FOREIGN KEY(offer_id) REFERENCES offers(offer_id)
);

CREATE INDEX IF NOT EXISTS idx_offer_witnesses_offer
    ON offer_witnesses(offer_id, witness_index);

CREATE TABLE IF NOT EXISTS chain_seals (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    seal_id               TEXT NOT NULL UNIQUE,
    chain_id              TEXT NOT NULL,
    tail_hash             TEXT NOT NULL,
    reason                TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'active',
    release_code_digest   TEXT NOT NULL,
    expires_at            TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    released_at           TEXT,
    CHECK(status IN ('active', 'released', 'expired')),
    -- released_at 仅属于 released 终态：active / expired 必须为 NULL，
    -- released 必须非空（过期时刻由固定的 expires_at 表达，不另存时间戳）
    CHECK((released_at IS NULL) = (status <> 'released')),
    FOREIGN KEY(chain_id) REFERENCES chains(chain_id)
);

-- 自增 id 单调递增：同一 created_at 时刻的多个封存（含解封后再封存）
-- 一律以 id 判定先后，保证「最新封存」查询稳定。
CREATE INDEX IF NOT EXISTS idx_chain_seals_chain
    ON chain_seals(chain_id, id);

-- 同一链同时至多一个「存储态 active」封存。过期 active 行会在创建新封存的
-- 同一立即事务内先物化为 expired，因此该索引长期约束的就是当前生效封存；
-- 它与 BEGIN IMMEDIATE 共同兜底并发创建（含跨进程），竞争插入必有一方失败。
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_seal_per_chain
    ON chain_seals(chain_id)
    WHERE status = 'active';
"""

# 旧表重建时必须具备的列；缺失列在搬数据时补 NULL（见 _migrate_offers_if_needed）
_OFFERS_REQUIRED_COLUMNS = (
    "offer_id", "chain_id", "tail_hash", "receiver", "code_digest",
    "witness_threshold", "status", "expires_at", "created_at",
    "accepted_at", "accepted_seq", "cancel_code_digest", "cancelled_at",
    "replaces", "replaced_by", "replaced_at",
)

# 五张业务表：备份归档（python -m app.backup）的结构校验以此清单为准
BUSINESS_TABLES = ("chains", "events", "offers", "offer_witnesses", "chain_seals")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        config.DB_PATH,
        timeout=10.0,
        isolation_level=None,  # 手动事务
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def _get_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def _migrate_offers_if_needed(conn: sqlite3.Connection) -> None:
    """把旧版 offers 表就地重建为最新结构，存量行原样保留。

    SQLite 无法直接 ALTER 修改 CHECK 约束 / 追加 NOT NULL 列，故采用官方
    推荐的「建新表 -> 搬数据 -> 删旧表 -> 改名」流程，全程关闭外键约束。
    逐列判断旧表是否拥有该列，缺失列在搬数据时补 NULL：
    - 缺少 witness_threshold 的更早期版本：该列补 NULL（无见证邀约）；
    - cancel_code_digest / cancelled_at 缺失：补 NULL（旧邀约不可撤销）；
    - replaces / replaced_by / replaced_at 缺失：补 NULL（旧邀约之间没有
      替换关系，也不与新邀约的替换链发生外键关联）。
    全新数据库（offers 已按 _SCHEMA 建出且列齐全）无需重建。
    """
    offer_cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(offers)").fetchall()
    }
    if not offer_cols:
        return
    if all(col in offer_cols for col in _OFFERS_REQUIRED_COLUMNS):
        return

    select_list = ", ".join(
        name if name in offer_cols else f"NULL AS {name}"
        for name in _OFFERS_REQUIRED_COLUMNS
    )
    # 关闭/开启外键须在事务外；按 SQLite 官方「重建表」流程显式包一层
    # BEGIN ... COMMIT。新表先以临时名创建、删旧表后再改名：
    # offer_witnesses 的外键始终指向表名 offers，改名后引用自然恢复。
    conn.executescript(
        f"""
        PRAGMA foreign_keys = OFF;
        BEGIN;
        CREATE TABLE offers__new (
{_OFFERS_COLUMNS}
        );
        INSERT INTO offers__new
            ({", ".join(_OFFERS_REQUIRED_COLUMNS)})
        SELECT {select_list}
        FROM offers;
        DROP TABLE offers;
        ALTER TABLE offers__new RENAME TO offers;
        CREATE INDEX IF NOT EXISTS idx_offers_chain ON offers(chain_id);
        CREATE INDEX IF NOT EXISTS idx_offers_replaces ON offers(replaces);
        COMMIT;
        PRAGMA foreign_keys = ON;
        """
    )
    failures = conn.execute("PRAGMA foreign_key_check").fetchall()
    if failures:
        raise RuntimeError(
            f"offers 表迁移后外键校验失败: {[tuple(r) for r in failures]}"
        )


def init_db() -> None:
    """初始化（或校验）数据库结构。可重复执行。

    对更早版本创建的 offers 表做一次性就地重建：增补 witness_threshold
    （无见证旧邀约为 NULL）、cancel_code_digest / cancelled_at（旧邀约
    撤销码为 NULL，即不可撤销）以及 replaces / replaced_by / replaced_at
    （旧邀约之间无替换关系），并把 status 的 CHECK 扩展为 pending /
    accepted / cancelled / replaced；存量邀约数据原样搬运，行为兼容。
    """
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _get_conn() as conn:
        conn.executescript(_SCHEMA)
        _migrate_offers_if_needed(conn)
        # 此刻 offers 必为最新结构（全新建表或刚完成旧表重建），补建替换列索引。
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_offers_replaces ON offers(replaces)"
        )


def reference_table_structures() -> dict[str, list[tuple]]:
    """在内存数据库中按 _SCHEMA 建出参考结构，返回五张业务表的 table_info 行。

    备份归档（python -m app.backup）以此校验快照/归档中的业务表结构：
    列名、类型、NOT NULL、默认值与主键定义（PRAGMA table_info，按 cid
    顺序）必须与当前代码建出的参考结构完全一致。结构基准由此处统一给出，
    避免归档工具另存一份会漂移的表结构副本。
    """
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(_SCHEMA)
        return {
            table: [
                tuple(row) for row in conn.execute(f"PRAGMA table_info({table})")
            ]
            for table in BUSINESS_TABLES
        }
    finally:
        conn.close()


@contextmanager
def write_tx() -> Iterator[sqlite3.Connection]:
    """串行化的写事务：BEGIN IMMEDIATE 立即取得写锁，提交前不会被插队。"""
    with _write_lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


@contextmanager
def read_tx() -> Iterator[sqlite3.Connection]:
    with _get_conn() as conn:
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
