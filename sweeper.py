"""AliveSweeper · 即时存活性校事（定期问户口本“你还在不在”).

监听 on_using_llm_tool 仅在工具被调用的当场捕获；长生命周期的路径会逐渐失效
（插件重装、文件迁移、配置清理）。AliveSweeper 周期巡检 records.alive=1 的路径，
逐一 lstat 确认存活：不存在即 mark_missing；重新出现由捕获层 mark_alive 自动归位。

策略（节流优先）：
- 单批 ≤ 200 条，多批之间按 0.5s 存活性背压让出 IO
- 忽略 /proc /sys /dev 前缀（内核虚拟文件组稳定但无实文件语义）
- 纯读取：不删除记录，只翻转 alive 位，保留户口供人工追查
- mtime 与库内快照不一致 → 打 changed 旗标（内容可能被覆盖，不判死不删）
- 任何异常单条消化，不中断整轮
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from astrbot import logger

from .db import PathStore

# 忽略系统虚拟文件组前缀（lstat 语义不定，跳过判活）
_IGNORE_PREFIXES = ("/proc/", "/sys/", "/dev/")


async def sweep_once(store: PathStore, batch: int = 200) -> dict[str, int]:
    """巡检一轮全部 alive=1 记录。返回 (checked, missing, changed, errored) 统计。"""
    if store is None:
        return {"checked": 0, "missing": 0, "changed": 0, "errored": 0}
    rows = store.list_alive()
    if not rows:
        return {"checked": 0, "missing": 0, "changed": 0, "errored": 0}
    checked = missing = changed = errored = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        for r in chunk:
            p = r.get("path") or ""
            if not p:
                continue
            if p.startswith(_IGNORE_PREFIXES):
                continue
            checked += 1
            try:
                try:
                    st = await asyncio.to_thread(os.stat, p)
                    if r.get("mtime", 0) and abs(st.st_mtime - float(r.get("mtime", 0))) > 0.01:
                        store.set_changed(p)
                        changed += 1
                except OSError:
                    # 路径不存在 或 损坏符号链（lexists 在但在 stat 解析不出）→ 均为不可达
                    if not await asyncio.to_thread(os.path.lexists, p) or os.path.islink(p):
                        store.mark_missing(p)
                        missing += 1
            except Exception:
                errored += 1
        if i + batch < len(rows):
            await asyncio.sleep(0.5)
    return {"checked": checked, "missing": missing, "changed": changed, "errored": errored}


class AliveSweeper:
    """注册到 AstrBot cron_manager 的巡检作业（持久化=False，懒注册幂等）。"""

    def __init__(self, store: PathStore, cron_expression: str = "0 * * * *") -> None:
        self.store = store
        self.cron = cron_expression

    async def run(self) -> None:
        try:
            stat = await sweep_once(self.store)
            if stat["missing"] or stat["changed"]:
                logger.info(
                    "[astrpathcompass] AliveSweeper 巡检: 检查%d 失效%d 变更%d 异常%d",
                    stat["checked"], stat["missing"], stat["changed"], stat["errored"],
                )
            else:
                logger.debug(
                    "[astrpathcompass] AliveSweeper 巡检完成: 全部存活(%d)", stat["checked"]
                )
        except Exception as e:
            logger.warning("[astrpathcompass] AliveSweeper 巡检异常: %s", e)