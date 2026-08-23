"""astrpathcompass —— 路径位点索引罗盘.

自动捕获主代理/子代理调用组件时翻过的文件路径（旁观式监听，不改任何事件上下文，
不动 system/tools/extra_user_content，零注入零缓存破坏），落 SQLite 户口本，
检索走 FTS5 秒回，AliveSweeper 小时级校验路径存活。

线程安全：sqlite3 连接按线程惰性创建；写事务用短事务+WAL，读走独立连接。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# 角色枚举（保留字符串，避免 enum 序列化麻烦）
ROLES = ("code", "config", "log", "db", "skill", "misc")

# 工具/参数名 → 路径提取优先级（捕获层白名单精简，越靠前越优先取）。常量冻结。
TOOL_PATH_ARGS: dict[str, tuple[str, ...]] = {
    "safe_read": ("path",),
    "safe_edit": ("filepath",),
    "safe_write": ("filepath",),
    "safe_rollback": ("filepath",),
    "file_patch": ("filepath",),
    "file_preview": ("filepath",),
    "file_remove": ("path",),
    "file_move": ("sources", "dest"),
    "file_diff": ("file_a", "file_b"),
    "file_hash": ("filepath",),
    "file_zip": ("files_or_dir", "output"),
    "file_unzip": ("zip_file", "output_dir"),
    "db_query": ("db_path",),
    "safe_backups": ("filepath",),
    "astrbot_file_read_tool": ("path",),
    "astrbot_file_write_tool": ("path",),
    "rg_search": ("path",),
    "dir_list": ("path",),
    "dir_tree": ("path",),
    "es_search": ("path",),
}

# 命中此片段的绝对路径不记录（跳过运行时/缓存/版本库噪音）
_IGNORE_FRAGMENTS = (
    "/.venv/",
    "/venv/",
    "/node_modules/",
    "/__pycache__/",
    "/.git/",
    "/.mypy_cache/",
    "/.ruff_cache/",
    ".pyc",
)

# 文本型工具：整段值当作文本扫描绝对路径（不同于 TOOL_PATH_ARGS 的直接取参）。
# 结构: 工具名 -> (参数名, 来源标识)。来源影响重要度加权。
TEXT_SCAN_TOOLS: dict[str, tuple[str, str]] = {
    "astrbot_execute_shell": ("command", "shell"),
    "astrbot_execute_python": ("code", "code"),
    "astrbot_shell_session": ("chars", "shell"),
    "http_get": ("url", "url"),
    "http_post": ("url", "url"),
    "web_fetch": ("urls", "url"),
}

# 扫描绝对路径：多级 /a/b/c，负向后顾排除 URL(:、/ 前导) 与词内粘连。
_ABS_PATH_RE = re.compile(r"(?<![:/A-Za-z0-9_.])(?:/[A-Za-z0-9_.+~-]+){2,}")

# 来源基础分（越精准越值得记录）：工具参数位 > shell 文本 > code 文本
# 来源置信度主导：工具明确传入的参数远可信于文本流扫描（磁盘上真实打开 > 出现于命令脚本 > URL 引用）。
# exact/shell/code 档次差拉大，路径深度降为次要微调，避免"长得深就当回事"的凑分幻觉。
SOURCE_BASE = {"exact": 0.60, "shell": 0.45, "code": 0.35, "url": 0.20}

# 系统级前缀：命中则重罚（多为运行时代码路径，价值低）
_SYS_PREFIX = (
    "/usr/", "/lib/", "/bin/", "/sbin/", "/etc/", "/tmp/", "/var/tmp/",
    "/proc/", "/sys/", "/dev/", "/run/", "/var/", "/opt/", "/srv/",
)


def path_importance(path: str, source: str = "exact") -> float:
    """路径重要性评分（0.05~1.0）。

    分数 = 来源置信度(主导，见 SOURCE_BASE) + 深度微调(每级+0.03,封顶8级)
           + 文件加成(+0.12，带扩展名) − 系统路径罚分(−0.25)。
    来源为主、深度为辅：工具参数明确传入(exact)≫shell 命令文本≫代码字符串出现≫URL 引用。
    min_importance 阈值越高 → 只收高置信来源+深文件路径；阈值越低 → 浅层凑分也入册。
    """
    base = SOURCE_BASE.get(source, 0.25)
    segs = [seg for seg in str(path).split("/") if seg and seg not in (".", "..")]
    depth = len(segs)
    name = segs[-1] if segs else ""
    has_ext = "." in name and not name.startswith(".")
    sys_pen = 0.25 if str(path).lower().startswith(_SYS_PREFIX) else 0.0
    score = base + min(depth, 8) * 0.030 + (0.12 if has_ext else 0.0) - sys_pen
    # 弱来源封顶：低置信度来源（如 url 引用）就算深度占满也压不过门槛，杜绝"长深凑分"越线
    if base < 0.30:
        score = min(score, base + 0.12)
    return round(max(0.05, min(1.0, score)), 3)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _eventually_real(path: str) -> str:
    """快判归一：路径存在才付 realpath 的解析链，不存在走 abspath 纯字符串归一。

    realpath 会对不存在路径逐级 syscall 追查到底（整条 parent 链），对失效/未创建的
    路径是纯浪费。先 abspath（零系统调用），只有目标真实存在时才升级为 realpath。
    """
    ab = os.path.abspath((path or "").strip())
    try:
        if os.path.exists(ab):
            return os.path.realpath(ab)
    except (OSError, ValueError):
        pass
    return ab


class PathStore:
    """SQLite 户口本：records + FTS5 倒排，线程安全，防注入。"""

    def __init__(self, data_dir: str | Path, *, min_importance: float = 0.40) -> None:
        """min_importance：建档的重要度门槛（0.05~1.0）。

        数值越高 → 只收冷门复杂（深层/文件型）路径；数值越低 → 简单高频路径也收。
        仅影响新路径是否建档，已归档路径的命中计数不受影响。
        """
        self.data_dir = Path(data_dir)
        self.min_importance = max(0.05, min(1.0, float(min_importance)))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self.data_dir / "pathcompass.db"
        self._local = threading.local()
        self._lock = threading.RLock()
        self._ensure_schema()

    # ── 连接管理 ──────────────────────────────
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            # 显式语言能力（非关键，失败忽略）
            try:
                conn.execute("PRAGMA trusted_schema=OFF")
            except sqlite3.OperationalError:
                pass
            self._local.conn = conn
        return conn

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        """旧库升级：records 表无 v0.2.1 新增列时补列（幂等、失败隔离）。"""
        try:
            exist = {row[1] for row in conn.execute("PRAGMA table_info(records)").fetchall()}
            for col, ddl in (
                ("size", "ALTER TABLE records ADD COLUMN size INTEGER NOT NULL DEFAULT 0"),
                ("mtime", "ALTER TABLE records ADD COLUMN mtime REAL NOT NULL DEFAULT 0"),
                ("changed", "ALTER TABLE records ADD COLUMN changed INTEGER NOT NULL DEFAULT 0"),
            ):
                if col not in exist:
                    conn.execute(ddl)
            conn.commit()
        except Exception:
            pass

    def _reconcile_aliases(self, conn: sqlite3.Connection) -> None:
        """v0.2.1 对账：把旧版录入软链/别名路径的行归一为新版 realpath 语义。

        库内已存在与真实路径相同的记录 → 计数合并后删除别名行；
        没有真实路径记录 → 直接改写为该 realpath。失败隔离，不动用户数据。"""
        try:
            rows = conn.execute("SELECT id, path, hit_count FROM records").fetchall()
            fidx = {r["path"]: r for r in rows}
            for r in rows:
                rp = os.path.realpath(r["path"])
                if rp != r["path"] and rp in fidx and fidx[rp]["id"] != r["id"]:
                    conn.execute(
                        "UPDATE records SET hit_count = hit_count + ? WHERE id=?",
                        (r["hit_count"], fidx[rp]["id"]),
                    )
                    conn.execute("DELETE FROM records WHERE id=?", (r["id"],))
                    conn.execute("DELETE FROM records_fts WHERE record_id=?", (r["id"],))
                elif rp != r["path"]:
                    conn.execute("UPDATE records SET path=? WHERE id=?", (rp, r["id"]))
            conn.commit()
        except Exception:
            pass

    def _ensure_schema(self) -> None:
        with self._lock:
            conn = self._conn()
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id          INTEGER PRIMARY KEY,
                    path        TEXT NOT NULL UNIQUE,
                    plugin_name TEXT NOT NULL DEFAULT '',
                    role        TEXT NOT NULL DEFAULT 'misc',
                    keywords    TEXT NOT NULL DEFAULT '',
                    note        TEXT NOT NULL DEFAULT '',
                    hit_count   INTEGER NOT NULL DEFAULT 0,
                    first_hit   TEXT NOT NULL,
                    last_hit    TEXT NOT NULL,
                    alive       INTEGER NOT NULL DEFAULT 1,
                    size        INTEGER NOT NULL DEFAULT 0,
                    mtime       REAL    NOT NULL DEFAULT 0,
                    changed     INTEGER NOT NULL DEFAULT 0,
                    updated_at  TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_alive ON records(alive);
                CREATE INDEX IF NOT EXISTS idx_records_role  ON records(role);
                CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
                    record_id UNINDEXED, path, plugin_name, keywords, note,
                    tokenize = 'unicode61 remove_diacritics 2'
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', '2');
                """
            )
            conn.commit()
            self._ensure_columns(conn)
            self._reconcile_aliases(conn)

    # ── upsert ────────────────────────────────
    def upsert(
        self,
        path: str,
        *,
        plugin_name: str = "",
        role: str = "misc",
        keywords: str = "",
        note: str = "",
        source: str = "exact",
        size: int = 0,
        mtime: float = 0.0,
    ) -> None:
        """记录一次命中：不存在则建档，存在则 hit_count+1、更新 last_hit。

        新路径建档前按 path_importance 打分，低于 self.min_importance 门槛则跳过
        （重要度不足的浅/噪路径不入册）；已建档路径不受阈值影响继续计数。
        任意异常被内部消化（捕获层绝不能反向扰动主线程）。
        """
        path = _eventually_real(path)
        if not path:
            return
        if any(frag in path for frag in _IGNORE_FRAGMENTS):
            return
        if role not in ROLES:
            role = "misc"
        try:
            with self._lock:
                conn = self._conn()
                conn.execute("BEGIN IMMEDIATE")
                try:
                    self._upsert_one(
                        conn,
                        path,
                        plugin_name=plugin_name,
                        role=role,
                        keywords=keywords,
                        note=note,
                        source=source,
                        size=size,
                        mtime=mtime,
                    )
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                else:
                    conn.execute("COMMIT")
        except Exception:
            # 捕获层影响面为零：吞掉并仅离线标记，绝不抛出到事件环。
            try:
                import logging

                logging.getLogger("astrpathcompass").warning(
                    "[astrpathcompass] upsert 失败(已隔离): path=%r", path, exc_info=True
                )
            except Exception:
                pass

    def _upsert_one(
        self,
        conn: sqlite3.Connection,
        path: str,
        *,
        plugin_name: str = "",
        role: str = "misc",
        keywords: str = "",
        note: str = "",
        source: str = "exact",
        size: int = 0,
        mtime: float = 0.0,
    ) -> None:
        """单条增改原语（不管理事务，供 upsert 单条与 upsert_many 批量共用）。"""
        now = _utcnow()
        row = conn.execute(
            "SELECT id, keywords, note, plugin_name, role FROM records WHERE path = ?",
            (path,),
        ).fetchone()
        if row is None:
            if path_importance(path, source) < self.min_importance:
                _trace_threshold_skip(path, source)
                raise LookupError("low importance")
            cur = conn.execute(
                """INSERT INTO records
                   (path, plugin_name, role, keywords, note,
                    hit_count, first_hit, last_hit, alive,
                    size, mtime, updated_at)
                   VALUES (?,?,?,?,?, 1,?,?,1, ?,?,?)""",
                (path, plugin_name or "", role, keywords or "", note or "",
                 now, now, size or 0, mtime or 0.0, now),
            )
            rid = cur.lastrowid
            conn.execute(
                """INSERT INTO records_fts(record_id, path, plugin_name, keywords, note)
                   VALUES (?,?,?,?,?)""",
                (rid, path, plugin_name or "", keywords or "", note or ""),
            )
        else:
            rid = row["id"]
            conn.execute(
                "UPDATE records SET hit_count = hit_count + 1, last_hit = ?, alive = 1, size = ?, mtime = ?, changed = 0, updated_at = ? WHERE id = ?",
                (now, size or 0, mtime or 0.0, now, rid),
            )
            new_kw = keywords or row["keywords"]
            new_note = note or row["note"]
            new_pl = plugin_name or row["plugin_name"]
            new_role = role if role != "misc" or row["role"] == "misc" else row["role"]
            if (new_kw, new_note, new_pl, new_role) != (
                row["keywords"],
                row["note"],
                row["plugin_name"],
                row["role"],
            ):
                conn.execute(
                    """UPDATE records
                       SET keywords=?, note=?, plugin_name=?, role=?, updated_at=?
                       WHERE id=?""",
                    (new_kw, new_note, new_pl, new_role, now, rid),
                )
                conn.execute(
                    """UPDATE records_fts
                       SET path=?, plugin_name=?, keywords=?, note=?
                       WHERE record_id=?""",
                    (path, new_pl, new_kw, new_note, rid),
                )

    def upsert_many(self, items: list[dict[str, Any]]) -> int:
        """批量落库：整批单事务、单次 COMMIT。items 每项须含 path，可选其余 upsert 参数。

        高频工具调用场景替代逐条 upsert —— 把每路径一次 fsync 压缩为整批一次，
        WAL 红利真正吃满（A3 快准狠·批量落库）。返回落库/命中条数。
        """
        if not items:
            return 0
        n = 0
        try:
            with self._lock:
                conn = self._conn()
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for it in items:
                        try:
                            self._upsert_one(
                                conn,
                                it.get("path") or "",
                                plugin_name=it.get("plugin_name", ""),
                                role=it.get("role", "misc"),
                                keywords=it.get("keywords", ""),
                                note=it.get("note", ""),
                                source=it.get("source", "exact"),
                                size=it.get("size", 0),
                                mtime=it.get("mtime", 0.0),
                            )
                            n += 1
                        except LookupError:
                            pass  # 低重要度跳过不影响整批
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                else:
                    conn.execute("COMMIT")
        except Exception:
            try:
                import logging

                logging.getLogger("astrpathcompass").warning(
                    "[astrpathcompass] upsert_many 失败(已隔离): %d 条", len(items), exc_info=True
                )
            except Exception:
                pass
            return 0
        return n

    # ── note 更新 ─────────────────────────────
    def set_note(self, path: str, note: str) -> bool:
        """显式补注（path_note 工具走这里）。固定人话语义，不改工具描述。"""
        try:
            with self._lock:
                conn = self._conn()
                conn.execute("SELECT 1 FROM records WHERE path = ?", (path,))
                cur = conn.execute(
                    "UPDATE records SET note=?, updated_at=? WHERE path=?",
                    (note or "", _utcnow(), path),
                )
            return cur.rowcount > 0
        except Exception:
            return False

    def forget(self, path: str) -> bool:
        """删除一条记录及其 FTS 索引。路径不存在返回 False（不抛）。"""
        try:
            with self._lock:
                conn = self._conn()
                row = conn.execute(
                    "SELECT id FROM records WHERE path = ?", (path,)
                ).fetchone()
                if row is None:
                    return False
                conn.execute("DELETE FROM records_fts WHERE record_id = ?", (row["id"],))
                conn.execute("DELETE FROM records WHERE path = ?", (path,))
                conn.commit()
            return True
        except Exception:
            return False

    def prune_orphans(self, older_than_days: int = 30, hit_max: int = 1) -> int:
        """清理孤儿记录：命中数 ≤ hit_max 且 last_hit 距今超过 N 天。

        默认只动 hit_max=1 的一次性访问（防误伤高频主场），返回清理条数。
        同步清除 FTS 索引，失败隔离。"""
        try:
            bounds = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            with self._lock:
                conn = self._conn()
                rows = conn.execute(
                    "SELECT id FROM records WHERE hit_count <= ? AND last_hit < ?",
                    (hit_max, bounds),
                ).fetchall()
                ids = [r["id"] for r in rows]
                for rid in ids:
                    conn.execute("DELETE FROM records_fts WHERE record_id = ?", (rid,))
                conn.execute(
                    "DELETE FROM records WHERE id IN (%s)"
                    % ",".join("?" * len(ids)),
                    ids,
                )
                conn.commit()
            return len(ids)
        except Exception:
            return 0

    # ── 检索 ──────────────────────────────────
    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """FTS5 命中优先；空/含通配的查询降级为 LIKE 兜底。返回规范化 dict 列表。"""
        query = (query or "").strip()
        rows: list[sqlite3.Row]
        try:
            conn = self._conn()
            if query:
                rows = conn.execute(
                    """SELECT r.*, bm25(records_fts) AS rank,
                              r.hit_count * exp(-(julianday('now') - julianday(r.last_hit)) / 7.0) AS _decay
                       FROM records_fts f JOIN records r ON r.id = f.record_id
                       WHERE records_fts MATCH ?
                       ORDER BY rank, _decay DESC, r.hit_count DESC LIMIT ?""",
                    (self._fts_query(query), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT *, hit_count * exp(-(julianday('now') - julianday(last_hit)) / 7.0) AS _decay
                       FROM records ORDER BY _decay DESC, hit_count DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        # FTS5 对 CJK 视为整块 token，中文子串词必然空 → 空结果自动 LIKE 兜底
        if not rows:
            try:
                conn = self._conn()
                q = f"%{query}%"
                rows = conn.execute(
                    """SELECT *, hit_count * exp(-(julianday('now') - julianday(last_hit)) / 7.0) AS _decay
                       FROM records
                       WHERE path LIKE ? OR plugin_name LIKE ? OR keywords LIKE ? OR note LIKE ?
                       ORDER BY _decay DESC, hit_count DESC LIMIT ?""",
                    (q, q, q, q, limit),
                ).fetchall()
            except Exception:
                return []
        return [dict(r) for r in rows]

    @staticmethod
    def _fts_query(raw: str) -> str:
        """转义 FTS5 特殊字符；空格拆 AND（宽松取 OR 更稳）。"""
        esc = raw.replace('"', '""')
        # 简单查询全文短语
        return f'"{esc}"'

    def mark_missing(self, path: str) -> None:
        """AliveSweeper 命中失败置 alive=0。"""
        try:
            with self._lock:
                self._conn().execute(
                    "UPDATE records SET alive=0, updated_at=? WHERE path=?",
                    (_utcnow(), path),
                )
                self._conn().commit()
        except Exception:
            pass

    def set_changed(self, path: str) -> None:
        """巡检发现 mtime 与库内快照不一致 → 打变更旗标（不判死、不删）。"""
        try:
            with self._lock:
                self._conn().execute(
                    "UPDATE records SET changed=1, updated_at=? WHERE path=? AND changed=0",
                    (_utcnow(), path),
                )
                self._conn().commit()
        except Exception:
            pass

    def clear_changed(self, path: str) -> None:
        """命中确认后清除变更旗标（路还活着、内容会重新快照）。"""
        try:
            with self._lock:
                self._conn().execute(
                    "UPDATE records SET changed=0, updated_at=? WHERE path=? AND changed=1",
                    (_utcnow(), path),
                )
                self._conn().commit()
        except Exception:
            pass

    def mark_alive(self, path: str) -> None:
        """复活（用户/代理重探后自动归位）。"""
        try:
            with self._lock:
                self._conn().execute(
                    "UPDATE records SET alive=1, updated_at=? WHERE path=?",
                    (_utcnow(), path),
                )
                self._conn().commit()
        except Exception:
            pass

    # ── 校验 ──────────────────────────────────
    def list_alive(self) -> list[dict[str, Any]]:
        try:
            with self._lock:
                rows = self._conn().execute(
                    "SELECT path, mtime FROM records WHERE alive=1"
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def stats(self) -> dict[str, int]:
        try:
            with self._lock:
                conn = self._conn()
                total = conn.execute("SELECT COUNT(*) c FROM records").fetchone()["c"]
                alive = conn.execute(
                    "SELECT COUNT(*) c FROM records WHERE alive=1"
                ).fetchone()["c"]
                dead = conn.execute(
                    "SELECT COUNT(*) c FROM records WHERE alive=0"
                ).fetchone()["c"]
                changed = conn.execute(
                    "SELECT COUNT(*) c FROM records WHERE changed=1"
                ).fetchone()["c"]
            return {"total": total, "alive": alive, "dead": dead, "changed": changed}
        except Exception:
            return {"total": 0, "alive": 0, "dead": 0}

    # ── 迁移/导出 ─────────────────────────────
    def import_legacy(self, entries: list[dict[str, Any]]) -> int:
        """批量导入旧路径清单（_ref/paths.md 解析结果），返回导入条数（已存在则更新 info）。"""
        n = 0
        for e in entries:
            p = (e.get("path") or "").strip()
            if not p or any(f in p for f in _IGNORE_FRAGMENTS):
                continue
            # 用 hit_count=0 的首建标记（upsert 默认 +1 会把首建也算一次）
            self.upsert(
                p,
                plugin_name=e.get("plugin_name", ""),
                role=e.get("role", "misc"),
                keywords=e.get("keywords", ""),
                note=e.get("note", ""),
            )
            n += 1
        return n

    def export_jsonl(self, dest: str | Path, prefix: str | None = None) -> int:
        """导出 jsonl（GitHub 示例/迁移旧机）。prefix 给定时仅导出该目录前缀。返回行数。"""
        try:
            with self._lock:
                if prefix:
                    esc = self._like_escape(prefix)
                    rows = self._conn().execute(
                        "SELECT * FROM records WHERE path LIKE ? ESCAPE '\\' ORDER BY hit_count DESC",
                        (f"{esc}%",),
                    ).fetchall()
                else:
                    rows = self._conn().execute(
                        "SELECT * FROM records ORDER BY hit_count DESC"
                    ).fetchall()
            with open(dest, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(dict(r), ensure_ascii=False) + "\n")
            return len(rows)
        except Exception:
            return 0

    @staticmethod
    def _like_escape(prefix: str) -> str:
        """转义 LIKE 通配符，防目录名含 % _ 导致前缀误漂移。"""
        return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def count_by_prefix(self, prefix: str) -> int:
        """返回该目录前缀下记录数（预览用）。失败返回 -1。"""
        try:
            with self._lock:
                return self._conn().execute(
                    "SELECT COUNT(*) AS c FROM records WHERE path LIKE ? ESCAPE '\\'",
                    (f"{self._like_escape(prefix)}%",),
                ).fetchone()["c"]
        except Exception:
            return -1

    def clean_by_prefix(self, prefix: str, count_only: bool = False) -> int:
        """按目录前缀清理记录（含 FTS 同步）。count_only=True 仅统计不执行。失败返回 -1。"""
        try:
            like = f"{self._like_escape(prefix)}%"
            with self._lock:
                conn = self._conn()
                rows = conn.execute(
                    "SELECT id FROM records WHERE path LIKE ? ESCAPE '\\'", (like,)
                ).fetchall()
                if count_only:
                    return len(rows)
                for r in rows:
                    conn.execute("DELETE FROM records_fts WHERE record_id = ?", (r["id"],))
                conn.execute("DELETE FROM records WHERE path LIKE ? ESCAPE '\\'", (like,))
                conn.commit()
                return len(rows)
        except Exception:
            return -1

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None


# 纯函数：从 tool_args 抽取候选路径（capture 模块复用）
def _trace_threshold_skip(path: str, source: str) -> None:
    """重要度不足被建档门槛拦截时的轻量痕迹（避免认为漏记而误判插件故障）。"""
    try:
        import logging
        logging.getLogger("astrpathcompass").debug(
            "[astrpathcompass] 建档门槛拦截(重要度不足): imp=%.3f src=%s %s",
            path_importance(path, source), source, path,
        )
    except Exception:
        pass


def _looks_like_path(s: str) -> bool:
    """形似路径门：绝对开头或有分隔符才算；纯词、纯扩展名、URL 一律不算。"""
    s = s.strip()
    if s.startswith(("http://", "https://", "www.", "file://")):
        return False
    if s.startswith(("/", "./", "../", "~")):
        return True
    if len(s) > 1 and s[1] == ":":
        return True
    if "/" in s:
        return True
    return False


def extract_paths_from_args(tool_name: str, args: dict | None) -> list[tuple[str, str]]:
    """抽取候选 (绝对路径, 来源标识) 列表；来源决定建档重要度加权。

    策略矩阵：
      · TOOL_PATH_ARGS 白名单参数位   → 直接取值（exact，最权威）
      · 任意 path/file/db 开头键的值   → 形似路径即取（exact）
      · 任意值以绝对路径开头（避开 URL）→ 取（exact 兜底）
      · 文本型工具（shell 的 command、python 的 code 等）→ 正则扫描多级绝对路径
        （来源 shell/code/url，重要度加权低于 exact，扫描面大但分值净额低）
    返回去重、保序、过滤噪音的 (路径, 来源) 列表。
    """
    if not args:
        return []
    cands: list[tuple[str, str]] = []
    # 1) 白名单精确位（最权威）
    for key in TOOL_PATH_ARGS.get(tool_name, ()):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            cands.append((val.strip(), "exact"))
        elif isinstance(val, (list, tuple)):
            for item in val:
                if isinstance(item, str) and item.strip():
                    cands.append((item.strip(), "exact"))
    # 2) 兜底：任意 "path"/"file"/"db" 开头键的字符串值（值须形似路径，防 file_exts 之类杂键）
    for key, val in args.items():
        if isinstance(val, str) and val.strip() and key.lower().startswith(
            ("path", "file", "db_", "db")
        ) and _looks_like_path(val.strip()):
            cands.append((val.strip(), "exact"))
    # 3) 兜底：* 任意值像绝对路径（避开 URL/纯词）—— 相对纯词（无分隔符）不入册
    abs_like = re.compile(r"^(?:/|[A-Za-z]:[\\/])")
    for val in args.values():
        if isinstance(val, str) and abs_like.match(val.strip()) and len(val.strip()) > 3:
            cands.append((val.strip(), "exact"))
    # 4) 文本型工具：整段 value 扫描多级绝对路径（shell 命令 / py 代码 / 文本）
    #    排除 URL（http(s)://、ftp://）与 :// 粘连，负向后顾见 _ABS_PATH_RE。
    spec = TEXT_SCAN_TOOLS.get(tool_name)
    if spec is not None:
        field, base = spec
        val = args.get(field)
        if isinstance(val, str) and val.strip():
            for m in _ABS_PATH_RE.findall(val):
                if not any(f in m for f in _IGNORE_FRAGMENTS):
                    cands.append((m, base))
        elif isinstance(val, (list, tuple)):
            for item in val:
                if isinstance(item, str) and item.strip():
                    for m in _ABS_PATH_RE.findall(item):
                        if not any(f in m for f in _IGNORE_FRAGMENTS):
                            cands.append((m, base))
    # 去重保序 + 过滤噪音（略过 /dev/null 之类的伪文件由重要度自然过滤）
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for c, src in cands:
        c = c.strip()
        if not c or any(f in c for f in _IGNORE_FRAGMENTS):
            continue
        if c not in seen:
            seen.add(c)
            out.append((c, src))
    return out