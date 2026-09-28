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
  * 归档目录本身必须是普通目录，目录内的清单与清单所列文件必须是普通文件：
    目录、清单或任何条目为符号链接一律拒绝（符号链接可指向归档外的同字节
    文件，外部文件移走后归档即失效，并非自包含归档）；
  * 目录与各文件分别以 O_NOFOLLOW 的 open/openat 固定 inode 后才读取，
    核验期间路径被替换（rename）不会改变正在读取的对象；
  * 每个文件经单遍读取同步完成 SHA-256 计算与大小核对；数据库文件还会在
    同一次读取中固化到核验方私有的临时副本（0700 目录、0600 文件），
    integrity_check、foreign_key_check 与五张业务表结构检查只对这份副本
    执行——摘要与库级检查锚定同一份已固定的字节内容，核验期间路径替换或
    文件被原地截断/改写都无法把两阶段结果拼成“通过”；
  * 解析并校验清单（格式、版本、必备条目），逐一重算清单内文件的
    SHA-256 并比对大小，拒绝清单之外的文件；
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


# ---- 核验期的 inode 固定与单遍固化 ----------------------------------------
#
# “先 stat/哈希、再按路径打开做 SQLite 检查”存在两扇窗口：路径可在两次打开
# 之间被符号链接/rename 替换到归档外的同字节文件（TOCTOU）；即便不替换路径，
# 文件内容也可在两阶段之间被原地改写。因此核验对每个文件只做一次打开：
# openat(dir_fd, name, O_RDONLY|O_NOFOLLOW) 固定普通文件的 inode（符号链接
# 直接 ELOOP），随后在同一次顺序读取中同步完成 SHA-256、大小核对；数据库
# 文件在这同一次读取中固化到核验方私有的临时副本，库级检查只针对副本进行，
# 摘要与库级检查因此锚定同一份已固定的字节内容。

_OPEN_NOFOLLOW_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_OPEN_DIR_NOFOLLOW_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
    os, "O_NOFOLLOW", 0
)


def _hash_pinned_file(
    fd: int, want_size: int, sink: str | Path | None = None
) -> tuple[str, int]:
    """对已固定 inode 的打开文件做单遍顺序读取。

    读取过程中同步更新 SHA-256、累计字节数；sink 给定时把同一份字节写入
    （用于数据库文件的私有副本固化）。返回 (sha256, 实读字节数)。
    调用方保证读取期间持有 fd，路径替换或文件被 unlink 都不影响本次读取。
    """
    digest = hashlib.sha256()
    total = 0
    if sink is not None:
        # 私有副本仅属主可读写（忽略 umask）
        out = open(
            sink,
            "wb",
            opener=lambda p, flags: os.open(p, flags, 0o600),
        )
    else:
        out = None
    try:
        for chunk in iter(lambda: os.read(fd, 1024 * 1024), b""):
            digest.update(chunk)
            total += len(chunk)
            if out is not None:
                out.write(chunk)
    finally:
        if out is not None:
            out.close()
    if total != want_size:
        raise BackupError(
            f"文件在核验过程中大小发生变化（固定时 {want_size} 字节，"
            f"实读 {total} 字节），拒绝拼装核验结果"
        )
    return digest.hexdigest(), total


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
    problems: list[str] = []
    try:
        conn = _connect_ro(db_file)
    except sqlite3.Error as exc:
        return [f"数据库无法打开: {db_file}: {exc}"]
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

    全程固定 inode：归档目录与每个条目都以 O_NOFOLLOW 的 open/openat 固定后
    才读取，符号链接一律拒绝；数据库经单遍读取固化到私有临时副本后再做
    库级检查，摘要与库级检查基于同一份已固定的字节内容。
    """
    # 1) 归档目录本身必须是普通目录（拒绝符号链接与其他非目录）
    try:
        lst = target.lstat()
    except OSError as exc:
        return [f"归档目录不存在或无法访问: {target}: {exc}"]
    if stat.S_ISLNK(lst.st_mode):
        return [f"归档目录本身是符号链接，拒绝核验（归档必须为普通目录）: {target}"]
    if not stat.S_ISDIR(lst.st_mode):
        return [f"归档目录不存在或不是目录: {target}"]

    dir_fd: int | None = None
    staging: Path | None = None
    try:
        # 固定归档目录 inode：此后即便目录路径被 rename/替换，本句柄仍指向
        # 开始核验时的那个目录；O_NOFOLLOW 拒绝最后一级为符号链接
        try:
            dir_fd = os.open(target, _OPEN_DIR_NOFOLLOW_FLAGS)
        except OSError as exc:
            return [f"归档目录无法打开: {target}: {exc}"]

        # 2) 一次性枚举目录条目，逐条 lstat 分类；符号链接与非普通文件先拦下。
        #    仅接受清单与清单所列文件，因此任何非普通文件（子目录、设备文件、
        #    FIFO、符号链接等）都不能出现在归档内。
        entries: dict[str, os.stat_result] = {}
        for entry in os.scandir(dir_fd):
            try:
                est = entry.stat(follow_symlinks=False)
            except OSError as exc:
                return [f"归档条目无法读取属性: {entry.name}: {exc}"]
            entries[entry.name] = est
            if stat.S_ISLNK(est.st_mode):
                return [
                    f"归档内存在符号链接，拒绝核验（归档必须自包含）: {entry.name}"
                ]
        if MANIFEST_FILENAME not in entries:
            return [f"缺少清单文件: {target / MANIFEST_FILENAME}"]
        if not stat.S_ISREG(entries[MANIFEST_FILENAME].st_mode):
            return [f"清单不是普通文件: {MANIFEST_FILENAME}"]

        # 3) 经固定目录句柄以 O_NOFOLLOW 打开清单并读入（拒绝符号链接竞态）
        try:
            mf = os.open(
                MANIFEST_FILENAME, _OPEN_NOFOLLOW_FLAGS, dir_fd=dir_fd
            )
        except OSError as exc:
            return [f"清单文件无法打开: {MANIFEST_FILENAME}: {exc}"]
        try:
            mst = os.fstat(mf)
            if not stat.S_ISREG(mst.st_mode):
                return [f"清单不是普通文件: {MANIFEST_FILENAME}"]
            manifest_data = b""
            for chunk in iter(lambda: os.read(mf, 1024 * 1024), b""):
                manifest_data += chunk
        finally:
            os.close(mf)
        try:
            manifest = json.loads(manifest_data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return [f"清单文件无法解析: {exc}"]
        if not isinstance(manifest, dict):
            return ["清单格式非法：顶层不是 JSON 对象"]

        problems: list[str] = []
        if manifest.get("format") != ARCHIVE_FORMAT:
            problems.append(
                f"清单 format 非法: {manifest.get('format')!r}"
                f"（期望 {ARCHIVE_FORMAT!r}）"
            )
        version = manifest.get("format_version")
        if version != ARCHIVE_FORMAT_VERSION:
            problems.append(
                f"不支持的归档格式版本: {version!r}"
                f"（本工具支持 {ARCHIVE_FORMAT_VERSION}）"
            )

        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            problems.append("清单缺少 files 文件列表")
            files = {}
        if (
            manifest.get("db_file", DB_FILENAME) != DB_FILENAME
            or DB_FILENAME not in files
        ):
            problems.append(f"清单缺少数据库文件条目: {DB_FILENAME}")

        # 私有暂存目录：数据库文件固化副本的存放处（核验结束即删除）
        staging = Path(
            tempfile.mkdtemp(prefix="handoff-verify-", dir=None)
        )
        os.chmod(staging, 0o700)
        db_pinned_copy: Path | None = None

        verified_names = set()
        db_digest_ok = False
        for name, meta in files.items():
            if not isinstance(name, str) or not name or Path(name).name != name:
                problems.append(f"清单包含非法文件名: {name!r}")
                continue
            verified_names.add(name)
            est = entries.get(name)
            if est is None:
                problems.append(f"清单文件缺失: {name}")
                continue
            if not stat.S_ISREG(est.st_mode):
                problems.append(
                    f"清单所列文件不是普通文件（归档内不允许符号链接/目录等）: {name}"
                )
                continue
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
            want_size = meta.get("size")
            if want_size is not None and (
                not isinstance(want_size, int)
                or isinstance(want_size, bool)
                or want_size < 0
            ):
                problems.append(f"清单中 {name} 的 size 字段非法: {want_size!r}")
                continue
            fixed_size = est.st_size
            if want_size is not None and fixed_size != want_size:
                problems.append(
                    f"文件大小不匹配: {name}: 清单 {want_size}, 实际 {fixed_size}"
                )
                continue

            # 经固定目录句柄打开此文件：O_NOFOLLOW 拒绝符号链接竞态；打开后
            # inode 固定，单遍读取同步算摘要（数据库还同步固化私有副本）。
            try:
                fd = os.open(name, _OPEN_NOFOLLOW_FLAGS, dir_fd=dir_fd)
            except OSError as exc:
                problems.append(f"清单文件无法打开: {name}: {exc}")
                continue
            try:
                fst = os.fstat(fd)
                if not stat.S_ISREG(fst.st_mode):
                    problems.append(f"清单所列文件不是普通文件: {name}")
                    continue
                if fst.st_size != fixed_size:
                    # lstat 与 open 之间条目被替换：以“已固定 inode”的大小为准
                    # 复核清单，杜绝两阶段之间换入同路径不同文件
                    if want_size is not None and fst.st_size != want_size:
                        problems.append(
                            f"文件大小不匹配: {name}: 清单 {want_size}, "
                            f"实际 {fst.st_size}"
                        )
                    else:
                        problems.append(
                            f"文件在核验过程中被替换（大小 {fixed_size} -> "
                            f"{fst.st_size}），拒绝核验: {name}"
                        )
                    continue
                # sink 由 _hash_pinned_file 以 0600 新建，写入的就是本次摘要
                # 所用的同一批字节
                sink = staging / DB_FILENAME if name == DB_FILENAME else None
                try:
                    got_sha, got_size = _hash_pinned_file(fd, fixed_size, sink)
                except BackupError as exc:
                    problems.append(str(exc))
                    continue
            finally:
                os.close(fd)
            if got_size != fixed_size:  # 兜底：理论上 _hash_pinned_file 已拦
                problems.append(
                    f"文件大小在读取中发生变化: {name}: "
                    f"{fixed_size} -> {got_size}"
                )
                continue
            if got_sha != want_sha:
                problems.append(
                    f"文件 SHA-256 不匹配: {name}: 清单 {want_sha}, 实际 {got_sha}"
                )
                continue
            if name == DB_FILENAME:
                db_digest_ok = True
                db_pinned_copy = sink

        extras = sorted(
            name
            for name in entries
            if name not in verified_names and name != MANIFEST_FILENAME
        )
        if extras:
            problems.append(f"存在清单之外的文件: {', '.join(extras)}")

        # 4) 库级检查只对“摘要所用同一批字节”的私有副本执行：检查期间即便
        #    归档路径被替换、条目被改写，也检查不到归档之外或另一份内容。
        if db_digest_ok:
            assert db_pinned_copy is not None
            problems.extend(_run_db_checks(db_pinned_copy))
        return problems
    finally:
        if dir_fd is not None:
            os.close(dir_fd)
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


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
