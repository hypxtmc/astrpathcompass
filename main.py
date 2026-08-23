"""astrpathcompass 主装配入口.

装饰器模型遵守 AstrBot 约定：``@filter.on_using_llm_tool`` 与其他钩子全部
以插件类方法形式定义（handler_module_path 匹配插件主模块），本模块负责
组装 capture / search / sweeper 三位一体的实现。

结构：
   main.py            入口 + 组装（唯一 define 钩子的文件）
   db.py              户口本（SQLite records + FTS5 + 复活性）
   capture.py         记账员（tool_args → 路径落库）
   sweeper.py         AliveSweeper（小时级存活校验）

三位一体：翻到即记（capture）→ 随用随考（search）→ 定期验活（sweeper）。
"""

from __future__ import annotations
import re

from typing import Any

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .db import PathStore
from .capture import capture
from .sweeper import AliveSweeper, sweep_once

SWEEP_JOB_NAME = "astrpathcompass_alive_sweep"
SWEEP_CRON = "17 * * * *"  # 每小时第 17 分，避开整点高峰


@register("astrpathcompass", "hypxtmc", "路径罗盘：翻阅即记，检索直达，失效根治",
          "v0.2.5", "https://github.com/hypxtmc/astrpathcompass")
class AstrPathCompass(Star):
    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._data_dir = str(StarTools.get_data_dir("astrpathcompass"))
        self.store: PathStore | None = None
        self._capture_enabled: bool = True
        self._search_limit: int = 8
        self._sweep_enabled: bool = True
        self._sweep_cron: str = SWEEP_CRON
        self._min_importance: float = 0.40
        self._sweeper: AliveSweeper | None = None

    @staticmethod
    def _as_bool(v: Any, default: bool = True) -> bool:
        """宽容解析布尔开关：True/False、0/1、"true"/"false" 均可。"""
        if v is None:
            return default
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v != 0
        return str(v).strip().lower() in ("1", "true", "yes", "on", "开", "真")

    async def initialize(self) -> None:
        """建库 + 装配开关 + 注册 AliveSweeper cron（失败仅降级，不阻断加载）。"""
        try:
            min_imp = self.config.get("min_importance", 0.40)
            try:
                self._min_importance = max(0.05, min(1.0, float(min_imp)))
            except (TypeError, ValueError):
                self._min_importance = 0.40
            self.store = PathStore(self._data_dir, min_importance=self._min_importance)
            enabled = self.config.get("capture_enabled", True)
            self._capture_enabled = self._as_bool(enabled, True)
            lim = self.config.get("search_limit", 8)
            try:
                self._search_limit = max(1, min(int(lim), 20))
            except (TypeError, ValueError):
                self._search_limit = 8
            sweep_on = self.config.get("sweep_enabled", True)
            self._sweep_enabled = self._as_bool(sweep_on, True)
            cron = self.config.get("sweep_cron", SWEEP_CRON)
            self._sweep_cron = str(cron).strip() or SWEEP_CRON
        except Exception as e:
            logger.warning("[astrpathcompass] 初始化库失败(仅捕获不可用): %s", e)
            return
        try:
            await self._register_sweeper()
        except Exception as e:
            logger.warning("[astrpathcompass] sweeper 注册失败: %s", e)

    async def _register_sweeper(self) -> None:
        """通过 Context.cron_manager 注册小时存活巡检器（幂等 + 可开关 + 可调 cron）。

        AstrBot 4.x 的插件定时任务正道是 ``context.cron_manager.add_basic_job``，
        注册 basic 类型 job；先清理同名旧 job 再新增，保证热重载不重复堆积。
        """
        if self.store is None:
            return
        cm = getattr(getattr(self, "context", None), "cron_manager", None)
        if cm is None:
            logger.debug("[astrpathcompass] cron_manager 未就绪，跳过自动巡检注册")
            return
        try:
            jobs = await cm.list_jobs(job_type="basic") or []
            for j in jobs:
                if (j.name if hasattr(j, "name") else "") == SWEEP_JOB_NAME:
                    try:
                        await cm.delete_job(j.job_id)
                    except Exception:
                        pass
        except Exception as e:
            logger.debug("[astrpathcompass] 旧 job 清理跳过: %s", e)
        self._sweeper = AliveSweeper(self.store, self._sweep_cron)
        await cm.add_basic_job(
            name=SWEEP_JOB_NAME,
            cron_expression=self._sweep_cron,
            handler=self._sweeper.run,
            description="astrpathcompass 存活巡检：失效路径标记 ✗",
            enabled=self._sweep_enabled,
            persistent=False,
        )
        logger.info(
            "[astrpathcompass] AliveSweeper 已注册: cron=%s enabled=%s",
            self._sweep_cron, self._sweep_enabled,
        )

    # ── 成份钩子：LLM 工具调用前捕获 ──────────────────────
    @filter.on_using_llm_tool()
    async def on_using_llm_tool(self, event, tool, tool_args):
        """钩子入口：LLM 调用函数工具前，把路径记入户口本（旁观，不改上下文）。"""
        if (not self._capture_enabled) or self.store is None:
            return
        try:
            name = getattr(tool, "name", "") or ""
            module_path = getattr(tool, "handler_module_path", "") or ""
            capture(
                self.store,
                {
                    "tool_name": name,
                    "tool_args": tool_args or {},
                    "handler_module_path": module_path,
                },
            )
        except Exception as e:
            logger.debug("[astrpathcompass] 捕获跳过: %s", e)

    # ── 命令：检索 ─────────────────────────────────────
    @filter.command("pathsearch")
    async def path_search(self, event: AstrMessageEvent, msg: list = None):
        """检索户口本：/pathsearch <词>。空/缺词返回全部。"""
        if self.store is None:
            yield event.plain_result("[罗盘] 户口本未就绪")
            return
        q = " ".join(msg or []).strip() if msg else ""
        rows = self.store.search(q, limit=self._search_limit)
        if not rows:
            yield event.plain_result(f"[罗盘] 没找到与「{q or '全部'}」相关的路径")
            return
        lines = [f"[罗盘] 命中 {len(rows)} 条（{q or '全部'}）:"]
        for r in rows:
            alive = "✓" if r.get("alive", 1) else "✗"
            role = r.get("role", "misc")
            hit = r.get("hit_count", 0)
            mark = " [变更]" if r.get("changed", 0) else ""
            hl = self._highlight(str(r["path"]), q) if q else r["path"]
            recent = self._fmt_recent(r.get("last_hit") or "")
            lines.append(f"{alive} [{role}]{mark} ✕{hit} {hl} · 最近{recent}")
        yield event.plain_result("\n".join(lines))

    @staticmethod
    def _fmt_recent(last_hit: str) -> str:
        """把 ISO 时间短化成相对时间（B3 上下文锚），无法解析则回退空串。"""
        if not last_hit:
            return "未记"
        try:
            from datetime import datetime, timezone

            t = datetime.fromisoformat(last_hit)
            secs = (datetime.now(timezone.utc) - t).total_seconds()
            if secs < 0:
                return "刚刚"
            if secs < 60:
                return f"{int(secs)}秒前"
            if secs < 3600:
                return f"{int(secs // 60)}分前"
            if secs < 86400:
                return f"{int(secs // 3600)}小时前"
            if secs < 2592000:
                return f"{int(secs // 86400)}天前"
            return f"{int(secs // 2592000)}月前"
        except Exception:
            return "?"

    @staticmethod
    def _highlight(text: str, query: str) -> str:
        """将查询词在路径里的命中片段用 ⟪⟫ 包裹，纯文本可见高亮（不改存档只改展示）。"""
        if not query:
            return text
        terms = [t for t in query.split() if t]
        if not terms:
            return text
        pat = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
        return pat.sub(lambda m: f"⟪{m.group(0)}⟫", text)

    # ── 命令：手动巡检 ─────────────────────────────────
    @filter.command("pathsweep")
    async def path_sweep(self, event: AstrMessageEvent, msg: list = None):
        """手动跑一轮存活巡检：/pathsweep，或 /pathsweep prune <N天> 清理孤儿。"""
        if self.store is None:
            yield event.plain_result("[罗盘] 户口本未就绪")
            return
        parts = msg or []
        if parts and str(parts[0]).lower() == "prune":
            days = int(parts[1]) if len(parts) > 1 and str(parts[1]).isdigit() else 30
            n = self.store.prune_orphans(older_than_days=days) if hasattr(
                self.store, "prune_orphans"
            ) else 0
            yield event.plain_result(f"[罗盘] 清理孤儿 {n} 条（{days} 天前的一次性访问）")
            return
        stat = await sweep_once(self.store)
        yield event.plain_result(
            f"[罗盘] 巡检完成：检查 {stat['checked']} · 失效 {stat['missing']} · 变更 {stat.get('changed', 0)} · 异常 {stat['errored']}"
        )

    # ── 命令：状态 ────────────────────────────────────
    @filter.command("pathstatus")
    async def path_status(self, event: AstrMessageEvent):
        """查看罗盘工作状态：/pathstatus。"""
        if self.store is None:
            yield event.plain_result("[罗盘] 户口本未就绪（捕获不可用）")
            return
        st = self.store.stats() if hasattr(self.store, "stats") else {}
        cap = "开" if self._capture_enabled else "关"
        sweep = "开" if self._sweep_enabled else "关"
        yield event.plain_result(
            f"[罗盘] 捕获{cap}·巡检{sweep} · 门槛{self._min_importance:.2f} · 库 {self._data_dir}\n"
            f"记录 {st.get('total', '?')} 条 · 存活{st.get('alive', '?')} · 失效{st.get('dead', '?')} · 变更{st.get('changed', '?')}"
        )

    # ── 命令：按前缀导出 ────────────────────────────────
    @filter.command("pathexport")
    async def path_export(self, event: AstrMessageEvent, msg: list = None):
        """按前缀导出：/pathexport <保存路径> [目录前缀]。无前缀=全量。"""
        if self.store is None:
            yield event.plain_result("[罗盘] 户口本未就绪")
            return
        parts = msg or []
        dest = str(parts[0]) if parts else ""
        if not dest:
            yield event.plain_result("[罗盘] 用法：/pathexport <保存路径> [目录前缀]")
            return
        prefix = str(parts[1]) if len(parts) > 1 else None
        n = self.store.export_jsonl(dest, prefix)
        spec = f"前缀「{prefix}」" if prefix else "全部"
        if n < 0:
            yield event.plain_result(f"[罗盘] 导出失败（{dest}）")
        else:
            yield event.plain_result(f"[罗盘] 已导出 {n} 条（{spec}）→ {dest}")

    # ── 命令：按前缀清理 ────────────────────────────────
    @filter.command("pathclean")
    async def path_clean(self, event: AstrMessageEvent, msg: list = None):
        """按前缀清理：/pathclean <目录前缀> 预览；加 --yes 确认执行。"""
        if self.store is None:
            yield event.plain_result("[罗盘] 户口本未就绪")
            return
        parts = msg or []
        prefix = str(parts[0]) if parts else ""
        if not prefix:
            yield event.plain_result("[罗盘] 用法：/pathclean <目录前缀> [--yes]")
            return
        confirm = any(str(p).lower() == "--yes" for p in parts[1:])
        n = self.store.clean_by_prefix(prefix, count_only=not confirm)
        if n < 0:
            yield event.plain_result(f"[罗盘] 清理失败（前缀：{prefix}）")
        elif confirm:
            yield event.plain_result(f"[罗盘] 已清理 {n} 条（前缀：{prefix}）")
        elif n == 0:
            yield event.plain_result(f"[罗盘] 该前缀下无记录：{prefix}")
        else:
            yield event.plain_result(f"[罗盘] 将清理 {n} 条（前缀：{prefix}）。确认请加 --yes")