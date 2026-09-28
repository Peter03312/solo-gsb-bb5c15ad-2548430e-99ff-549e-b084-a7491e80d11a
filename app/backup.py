"""数据库归档命令行工具：python -m app.backup create|verify <目录>。

create —— 用 SQLite 在线备份机制（backup API）为 DB_PATH 生成一致快照归档：
  * 源库固定取配置 DB_PATH，以只读方式打开；API 持续写入、WAL 未合并时
    backup API 会在源库被并发修改时自动重取页面，快照依然完整一致；
  * 归档目录包含独立可读的数据库文件 handoff.db（快照已转回 delete 日志
    模式，无需 -wal/-shm 伴生文件即可只读打开）与清单 manifest.json
    （记录格式版本、创建时间、各文件 SHA-256 与大小、已执行的校验项）；
  * 先在目标父目录下暂存（.<名称>.staging-*），快照通过 integrity_check、
    foreign_key_check 与五张业务表结构校验后，才以原子不覆盖重命名
    （renameat2 的 RENAME_NOREPLACE）发布整个归档目录——“目标不存在”
    是重命名成功的原子前提，不存在“检查后又被创建”的竞态窗口；
  * 目标已存在（含竞争方刚占下的同名目录）、或与源库路径（含
    -wal/-shm/-journal 伴生文件）冲突时拒绝，绝不动已存在的目标；
  * 任一步失败即清理暂存目录，不改动源库与任何既有归档。

verify —— 只读核验既有归档（不触碰源库，可离线运行）：
  * 只接受**普通归档目录**：归档目录本身、清单 manifest.json 以及清单所列的
    每个文件都必须是普通文件；任一为符号链接（含指向目录外同字节文件的
    链接）一律拒绝（openat 的 O_NOFOLLOW 在打开环节即报 ELOOP）；
  * 解析并校验清单（格式、版本、必备条目），逐一重算清单内文件的
    SHA-256 并比对大小，拒绝清单之外的文件；
  * 归档目录以固定目录 fd 打开，每个文件以该目录 fd + O_NOFOLLOW 打开为
    文件 fd 后固定；大小（fstat）与 SHA-256 直接取自已固定的 fd；摘要对
    同一 fd 比对通过后，把该 fd 的字节密封复制进仅本进程可访问的 0700
    私有暂存，再对该密封副本只读执行 integrity_check、foreign_key_check、
    五张业务表结构校验（暂存校验后即删，攻击者无从替换）——三项检查与
    摘要锚定同一份已固定内容，核验期间路径被替换也拼不出成功结果；
  * 全部通过退出码 0；任何损坏/缺项以非零退出码退出并在 stderr 列出具体原因。
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from . import config, database

ARCHIVE_FORMAT = "handoff-backup"
ARCHIVE_FORMAT_VERSION = 1
DB_FILENAME = "handoff.db"
MANIFEST_FILENAME = "manifest.json"

# 归档发布前 / 核验时执行的数据库检查项（写入清单，供 verify 对照）
_CHECKS = ("integrity_check", "foreign_key_check", "business_table_schema")

_HEX = frozenset("0123456789abcdef")


class BackupError(Exception):
    """归档操作失败（消息面向操作者，随非零退出码报告）。"""


def _connect_ro(path: Path) -> sqlite3.Connection:
    """只读打开数据库：绝不写入，也不产生 -wal/-shm 伴生文件。"""
    conn = sqlite3.connect(
        f"file:{quote(str(path))}?mode=ro", uri=True, timeout=30.0
    )
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA query_only = ON")
    return conn


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_fd(fd: int) -> str:
    """对已固定的文件 fd 从头计算 SHA-256；结束后把偏移复位到开头。"""
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    for chunk in iter(lambda: os.read(fd, 1024 * 1024), b""):
        digest.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


# ---- 核验期的路径固定 -----------------------------------------------------
#
# verify 面对的归档可能是攻击者构造/事后篡改的：把 handoff.db 换成指向
# 目录外同字节库的符号链接时，若按路径逐一“is_file / 读摘要 / sqlite 打开”，
# 三次解析可以落到不同文件（TOCTOU），外部文件一移走归档即失效。因此核验时：
#   * 归档目录用 O_DIRECTORY|O_NOFOLLOW 打开为目录 fd 并固定；
#   * 清单及清单内每个文件都以 openat(目录fd, 名称, O_RDONLY|O_NOFOLLOW)
#     打开为文件 fd —— 符号链接在打开环节即 ELOOP，且不允许任何中间路径
#     组件为符号链接；fstat 再确认其为普通文件；
#   * 大小（fstat）与 SHA-256 直接取自已固定的文件 fd；
#   * SQLite 无法直接锚定一个 fd（打开以路径名为准），故在摘要对同一 fd
#     比对通过后，把该 fd 的字节密封复制进 0700 私有暂存（O_EXCL、校验后
#     即删）再做库检查——攻击者接触不到暂存，归档路径被替换也无从影响。

_O_DIR_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
_O_FILE_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW
_SYMLINK_ERRNOS = {errno.ELOOP}
if hasattr(errno, "EMLINK"):  # macOS 上 O_NOFOLLOW 命中末段符号链接时可能返回 EMLINK
    _SYMLINK_ERRNOS.add(errno.EMLINK)


def _open_archive_dir(target: Path) -> int:
    """以“不跟随符号链接”方式打开归档目录 fd；目录本身为符号链接时拒绝。"""
    fd = os.open(os.fsencode(target), _O_DIR_OPEN_FLAGS)
    try:
        st = os.fstat(fd)
    except BaseException:
        os.close(fd)
        raise
    if not stat.S_ISDIR(st.st_mode):
        os.close(fd)
        raise NotADirectoryError(20, os.strerror(errno.ENOTDIR), str(target))
    return fd


def _open_regular_at(dir_fd: int, name: str) -> int:
    """在固定目录 fd 下以 O_NOFOLLOW 打开普通文件；符号链接/非普通文件一律拒绝。"""
    fd = os.open(name, _O_FILE_OPEN_FLAGS, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
    except BaseException:
        os.close(fd)
        raise
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise BackupError(f"不是普通文件（拒绝符号链接及特殊文件）: {name}")
    return fd


def _sealed_copy_fd(fd: int) -> Path:
    """把已固定文件 fd 的全部字节复制进私有暂存，供 SQLite 只读核验。

    SQLite 的打开以路径名为准（即便经 /dev/fd/<n>，原路径被 rename 替换后
    也会失效），无法直接锚定一个 fd。因此在 SHA-256 已经对**同一 fd** 比对
    通过后，把该 fd 的字节复制到 mkdtemp(0700) 下的 O_EXCL 私有文件——除
    root 外无人可进入该目录、更不可能替换其中文件——SQLite 只打开这份密封
    副本；调用方负责在用完后 rmtree 其所在暂存目录。
    """
    staging_dir = Path(tempfile.mkdtemp(prefix=".verify-db-"))
    sealed = staging_dir / DB_FILENAME
    out_fd = os.open(os.fsencode(sealed), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            os.write(out_fd, chunk)
        os.fsync(out_fd)
    finally:
        os.close(out_fd)
    return sealed


def _online_backup(source: Path, dest: Path) -> None:
    """用 SQLite 在线备份 API 把 source 的一致快照写入 dest。

    源库以只读方式打开，备份期间 API 可持续写入、WAL 无需合并；
    备份产物默认继承源库的 WAL 日志模式，这里转回 delete 模式，
    保证快照文件脱离源库独立可读（只读打开不需要 -wal/-shm）。
    """
    src = _connect_ro(source)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst, sleep=0.25)
            dst.execute("PRAGMA journal_mode = DELETE").fetchone()
        finally:
            dst.close()
    except sqlite3.Error as exc:
        raise BackupError(f"在线备份失败: {exc}") from exc
    finally:
        src.close()


def _schema_problems(conn: sqlite3.Connection) -> list[str]:
    """对照当前代码的参考结构，校验五张业务表的表结构。"""
    problems = []
    for table, want in database.reference_table_structures().items():
        got = [tuple(row) for row in conn.execute(f"PRAGMA table_info({table})")]
        if not got:
            problems.append(f"缺少业务表: {table}")
            continue
        if got == want:
            continue
        want_cols = [row[1] for row in want]
        got_cols = [row[1] for row in got]
        if want_cols != got_cols:
            problems.append(
                f"业务表 {table} 列清单不一致: 期望 {want_cols}, 实际 {got_cols}"
            )
        else:
            first = next(pair for pair in zip(want, got) if pair[0] != pair[1])
            problems.append(
                f"业务表 {table} 列定义不一致: 期望 {first[0]}, 实际 {first[1]}"
            )
    return problems


def _run_db_checks(db_file: Path) -> list[str]:
    """对数据库文件只读执行 integrity_check / foreign_key_check / 业务表结构校验。

    返回问题列表，空列表表示全部通过。
    """
    try:
        conn = _connect_ro(db_file)
    except sqlite3.Error as exc:
        return [f"数据库无法打开: {db_file}: {exc}"]
    return _run_db_checks_on(conn)


def _run_db_checks_on(conn: sqlite3.Connection) -> list[str]:
    """在已打开的只读连接上执行三项检查并关闭连接；返回问题列表。"""
    problems: list[str] = []
    try:
        rows = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        if rows != ["ok"]:
            problems.append(
                f"integrity_check 未通过（{len(rows)} 条）: {'; '.join(rows[:3])}"
            )
        fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_rows:
            first = tuple(fk_rows[0])
            problems.append(
                f"foreign_key_check 发现 {len(fk_rows)} 处违规，"
                f"首条: 表 {first[0]} 行号 {first[1]}"
            )
        problems.extend(_schema_problems(conn))
    except sqlite3.Error as exc:
        problems.append(f"数据库检查执行失败: {exc}")
    finally:
        conn.close()
    return problems


def _run_db_checks_fd(fd: int) -> list[str]:
    """对已固定的数据库文件 fd 只读执行三项检查。

    打开前先把该 fd 的字节密封复制到私有暂存文件（攻击者无法替换），
    SQLite 只读打开这份副本——与已通过的 SHA-256 锚定同一次 fd 读取，
    归档路径在核验期间被替换不影响结果。副本在连接打开后立刻解除目录
    链接（SQLite 持有其 fd，检查照常），不留任何可被换入的路径。
    """
    try:
        sealed = _sealed_copy_fd(fd)
    except OSError as exc:
        return [f"数据库核验副本准备失败: {exc}"]
    try:
        try:
            conn = _connect_ro(sealed)
        except sqlite3.Error as exc:
            return [f"数据库无法打开: {exc}"]
        try:
            # 已转为 delete 模式的只读快照：解除链接后，只读连接不再需要
            # 按路径访问任何伴生文件，未链接的 inode 在连接关闭时即回收。
            os.unlink(sealed)
        except OSError:
            pass
        return _run_db_checks_on(conn)
    finally:
        shutil.rmtree(sealed.parent, ignore_errors=True)


# ---- 原子不覆盖发布 -------------------------------------------------------
#
# os.rename 在目标为已存在的空目录时会将其替换，无法在“目标已存在即拒绝”
# 的语义下使用；这里改用平台原生的不覆盖重命名原语，把“目标不存在”作为
# 重命名成功的原子前提（Linux: renameat2(RENAME_NOREPLACE)，macOS:
# renamex_np(RENAME_EXCL)）。平台不支持时宁可报错也绝不退回会覆盖的
# os.rename。

_AT_FDCWD = -100
_RENAME_NOREPLACE = 0x1
_RENAME_EXCL = 0x4

# libc 未封装 renameat2（如旧 musl）时直接发系统调用的兜底调用号
_RENAMEAT2_SYSCALL_NR = {
    "x86_64": 316,
    "aarch64": 276,
    "riscv64": 276,
    "loongarch64": 276,
    "armv7l": 382,
    "i386": 353,
    "i686": 353,
    "ppc64": 357,
    "ppc64le": 357,
    "s390x": 347,
}

_rename_noreplace_impl = None  # 惰性定位到的平台原语；False 表示平台不支持
_rename_noreplace_ready = False


def _find_rename_noreplace():
    """定位平台原生的不覆盖重命名原语；返回调用入口，无支持时返回 None。"""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None

    renameat2 = getattr(libc, "renameat2", None)  # glibc >= 2.28
    if renameat2 is not None:
        renameat2.restype = ctypes.c_int
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]

        def call(src: bytes, dst: bytes) -> int:
            return renameat2(_AT_FDCWD, src, _AT_FDCWD, dst, _RENAME_NOREPLACE)

        return call

    if sys.platform == "darwin":
        renamex_np = getattr(libc, "renamex_np", None)
        if renamex_np is not None:
            renamex_np.restype = ctypes.c_int
            renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]

            def call(src: bytes, dst: bytes) -> int:
                return renamex_np(src, dst, _RENAME_EXCL)

            return call
        return None

    if sys.platform.startswith("linux"):
        nr = _RENAMEAT2_SYSCALL_NR.get(platform.machine())
        syscall = getattr(libc, "syscall", None)
        if nr is not None and syscall is not None:
            syscall.restype = ctypes.c_int

            def call(src: bytes, dst: bytes) -> int:
                return syscall(nr, _AT_FDCWD, src, _AT_FDCWD, dst, _RENAME_NOREPLACE)

            return call
    return None


def _rename_noreplace(src: Path, dst: Path) -> None:
    """原子地把 src 发布为 dst；dst 已存在（含空目录）时失败且不覆盖。"""
    global _rename_noreplace_impl, _rename_noreplace_ready
    if not _rename_noreplace_ready:
        _rename_noreplace_impl = _find_rename_noreplace() or False
        _rename_noreplace_ready = True
    if not _rename_noreplace_impl:
        raise BackupError(
            "当前平台不支持原子不覆盖重命名（renameat2/renamex_np），拒绝发布归档"
        )
    src_b, dst_b = os.fsencode(src), os.fsencode(dst)
    while True:
        if _rename_noreplace_impl(src_b, dst_b) == 0:
            return
        err = ctypes.get_errno()
        if err == errno.EINTR:
            continue
        if err in (errno.EEXIST, errno.ENOTEMPTY):
            raise BackupError(f"目标路径已存在，拒绝覆盖: {dst}")
        raise OSError(err, f"原子发布归档失败: {os.strerror(err)}", str(dst))


def create_archive(target: Path) -> dict:
    """创建归档：暂存 -> 校验 -> 原子发布。失败抛 BackupError 并清理暂存。"""
    db_path = Path(config.DB_PATH)
    if not db_path.is_file():
        raise BackupError(
            f"源数据库不存在（DB_PATH={db_path}），请先启动服务或执行数据库初始化"
        )

    target_resolved = target.resolve()
    db_resolved = db_path.resolve()

    # 与源库路径冲突：目标即库文件/伴生文件，或与库文件互含
    sidecars = {Path(str(db_resolved) + s) for s in ("-wal", "-shm", "-journal")}
    if target_resolved == db_resolved or target_resolved in sidecars:
        raise BackupError(f"目标路径与源数据库文件冲突，拒绝归档: {target_resolved}")
    if target_resolved in db_resolved.parents or db_resolved in target_resolved.parents:
        raise BackupError(
            f"目标路径与源数据库路径（{db_resolved}）存在包含关系，拒绝归档"
        )
    if os.path.lexists(target_resolved):
        raise BackupError(f"目标路径已存在，拒绝覆盖: {target_resolved}")
    parent = target_resolved.parent
    if not parent.is_dir():
        raise BackupError(f"目标父目录不存在: {parent}")

    # 在目标父目录下暂存，全部校验通过后原子不覆盖发布（同一文件系统）
    staging: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{target_resolved.name}.staging-", dir=parent)
    )
    try:
        snapshot = staging / DB_FILENAME
        _online_backup(db_resolved, snapshot)
        problems = _run_db_checks(snapshot)
        if problems:
            raise BackupError("快照校验未通过，已放弃发布: " + "；".join(problems))
        manifest = {
            "format": ARCHIVE_FORMAT,
            "format_version": ARCHIVE_FORMAT_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_db_path": str(db_resolved),
            "db_file": DB_FILENAME,
            "business_tables": list(database.BUSINESS_TABLES),
            "checks": list(_CHECKS),
            "files": {
                DB_FILENAME: {
                    "sha256": _sha256_file(snapshot),
                    "size": snapshot.stat().st_size,
                }
            },
        }
        (staging / MANIFEST_FILENAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        # 原子不覆盖发布：若竞争方已先占目标路径（哪怕是空目录），重命名
        # 以 EEXIST 失败，对方目录及其内容原样保留，己方暂存由 finally 清理
        _rename_noreplace(staging, target_resolved)
        staging = None
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)

    result = dict(manifest)
    result["archive"] = str(target_resolved)
    return result


def verify_archive(target: Path) -> list[str]:
    """只读核验归档目录，返回问题列表（空列表表示通过）。

    归档目录、清单与清单所列文件都必须是普通文件（拒绝符号链接）；
    每个文件固定为文件 fd 后，大小（fstat）与 SHA-256 直接取自已固定的
    fd；摘要对同一 fd 比对通过后，SQLite 三项检查在该 fd 内容的密封副本
    上进行。核验期间路径被替换无法影响结果。
    """
    problems: list[str] = []

    # 归档目录本身必须是普通目录：O_NOFOLLOW 拒绝符号链接（含指向同字节
    # 目录的链接）；后续一切打开都锚定在这个目录 fd 上。
    try:
        archive_fd = _open_archive_dir(target)
    except OSError as exc:
        if isinstance(exc, NotADirectoryError) or (
            exc.errno in _SYMLINK_ERRNOS or exc.errno == errno.ENOTDIR
        ):
            return [f"归档目录不是普通目录（拒绝符号链接）: {target}"]
        return [f"归档目录无法打开: {target}: {exc}"]

    try:
        # 目录 fd 会在条目被 rename 删除后从 getdents 漏出 "<名> (deleted)"
        # 的陈旧伪名，统一滤除（正常文件名不会以该后缀出现在清单里）。
        def _entries(fd: int) -> set[str]:
            return {
                n for n in os.listdir(fd)
                if not n.endswith(" (deleted)")
            }

        entries_before = _entries(archive_fd)

        # 清单必须是该目录下的普通文件：符号链接在 openat 阶段即被拒绝。
        try:
            manifest_fd = _open_regular_at(archive_fd, MANIFEST_FILENAME)
        except BackupError as exc:
            return [str(exc)]
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return [f"缺少清单文件: {target / MANIFEST_FILENAME}"]
            if exc.errno in _SYMLINK_ERRNOS:
                return [f"清单为符号链接，拒绝核验: {target / MANIFEST_FILENAME}"]
            return [f"清单文件无法打开: {exc}"]
        try:
            try:
                raw = b""
                for chunk in iter(lambda: os.read(manifest_fd, 1024 * 1024), b""):
                    raw += chunk
                manifest = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeDecodeError) as exc:
                return [f"清单文件无法读取: {exc}"]
            except json.JSONDecodeError as exc:
                return [f"清单文件无法解析: {exc}"]
        finally:
            os.close(manifest_fd)

        if not isinstance(manifest, dict):
            return ["清单格式非法：顶层不是 JSON 对象"]

        if manifest.get("format") != ARCHIVE_FORMAT:
            problems.append(
                f"清单 format 非法: {manifest.get('format')!r}（期望 {ARCHIVE_FORMAT!r}）"
            )
        version = manifest.get("format_version")
        if version != ARCHIVE_FORMAT_VERSION:
            problems.append(
                f"不支持的归档格式版本: {version!r}（本工具支持 {ARCHIVE_FORMAT_VERSION}）"
            )

        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            problems.append("清单缺少 files 文件列表")
            files = {}
        if manifest.get("db_file", DB_FILENAME) != DB_FILENAME or DB_FILENAME not in files:
            problems.append(f"清单缺少数据库文件条目: {DB_FILENAME}")

        verified_names = set()
        for name, meta in files.items():
            if not isinstance(name, str) or not name or Path(name).name != name:
                problems.append(f"清单包含非法文件名: {name!r}")
                continue
            # openat(archive_fd, name, O_NOFOLLOW)：名称中含路径分隔符已在
            # 上面排除；末段或任何解析环节为符号链接都直接 ELOOP/EMLINK。
            try:
                file_fd = _open_regular_at(archive_fd, name)
            except BackupError as exc:
                problems.append(str(exc))
                continue
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    problems.append(f"清单文件缺失: {name}")
                elif exc.errno in _SYMLINK_ERRNOS:
                    problems.append(f"清单所列文件为符号链接，拒绝核验: {name}")
                elif exc.errno == errno.EISDIR:
                    problems.append(f"清单所列文件不是普通文件（为目录）: {name}")
                else:
                    problems.append(f"文件无法打开: {name}: {exc}")
                continue

            with os.fdopen(file_fd, "rb", closefd=True) as fh:
                verified_names.add(name)
                if not isinstance(meta, dict):
                    problems.append(f"清单中 {name} 的条目非法（应为对象）")
                    continue
                want_sha = meta.get("sha256")
                if not (
                    isinstance(want_sha, str)
                    and len(want_sha) == 64
                    and all(ch in _HEX for ch in want_sha)
                ):
                    problems.append(f"清单中 {name} 的 sha256 字段非法: {want_sha!r}")
                    continue
                # 大小取自对已固定 fd 的 fstat；后续摘要与库检查都用同一 fd。
                actual_size = os.fstat(fh.fileno()).st_size
                want_size = meta.get("size")
                if want_size is not None and actual_size != want_size:
                    problems.append(
                        f"文件大小不匹配: {name}: 清单 {want_size}, 实际 {actual_size}"
                    )
                    continue
                got_sha = _sha256_fd(fh.fileno())
                if got_sha != want_sha:
                    problems.append(
                        f"文件 SHA-256 不匹配: {name}: 清单 {want_sha}, 实际 {got_sha}"
                    )
                    continue
                if name == DB_FILENAME:
                    # 摘要、大小与 SQLite 三项检查锚定同一份已固定内容：
                    # _run_db_checks_fd 复制的是同一 fd 的字节，路径被替换
                    # 也无法影响检查结果。
                    problems.extend(_run_db_checks_fd(fh.fileno()))

        # 在同一目录 fd 上再列一次：核验开始后被换入/塞入的额外文件也不放过。
        # 清单本身列出但未能打开/核验的名字已单独报错，不再重复计为额外文件。
        listed_names = {
            n for n in files if isinstance(n, str) and n and Path(n).name == n
        }
        entries_after = _entries(archive_fd)
        extras = sorted(
            entry
            for entry in entries_before | entries_after
            if entry not in verified_names
            and entry not in listed_names
            and entry != MANIFEST_FILENAME
        )
        if extras:
            problems.append(f"存在清单之外的文件: {', '.join(extras)}")
    finally:
        os.close(archive_fd)

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.backup",
        description=(
            "交接留痕数据库归档：create 以 SQLite 在线备份生成一致快照归档，"
            "verify 只读核验归档。源库固定取 DB_PATH。"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_create = sub.add_parser(
        "create", help="创建归档（在线备份 + 三项校验 + 原子发布）"
    )
    p_create.add_argument("target", type=Path, help="归档目标目录（不得预先存在）")
    p_verify = sub.add_parser(
        "verify", help="只读核验归档（清单 / 摘要 / 数据库检查）"
    )
    p_verify.add_argument("target", type=Path, help="待核验的归档目录")
    args = parser.parse_args(argv)

    if args.command == "create":
        try:
            result = create_archive(args.target)
        except (BackupError, OSError) as exc:
            print(f"create 失败: {exc}", file=sys.stderr)
            return 1
        print(
            json.dumps(
                {"status": "ok", **result},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    try:
        problems = verify_archive(args.target)
    except OSError as exc:
        problems = [f"读取归档失败: {exc}"]
    if problems:
        print(f"verify 失败: {args.target}", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "archive": str(args.target),
                "checks": list(_CHECKS),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
