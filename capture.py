"""捕获层 · 记账员（A1+ 旁观监听）。

订阅 ``@filter.on_using_llm_tool``：在主代理/子代理调用函数工具的当场，
从 ``tool_args`` 提取路径实体并落户口本。纯旁观——不改 request、不注入任何
上下文、不返回任何内容，任何异常内部消化，绝不向事件环抛出。

捕获广度：主代理与子代理的工具调用均触发 OnUsingLLMToolEvent（独立事件类型），
因此覆盖全。避免解析历史消息，路径当场直取，比轮后扫描更准更省。
"""

from __future__ import annotations

import os
from typing import Any

from astrbot import logger

from .db import PathStore, extract_paths_from_args, _eventually_real


def plugin_from_module_path(module_path: str | None) -> str:
    """从函数来源模块路径推断插件名（如 .../astrbot_plugin_LLMPerception/main.py → astrbot_plugin_LLMPerception）。"""
    if not module_path:
        return ""
    # 兼容形如 "astrbot_plugin_XX.main" 或绝对路径
    if "astrbot_plugin_" in module_path:
        head = module_path.rsplit("astrbot_plugin_", 1)[1]
        name = head.split("/")[0].split("\\")[0].split(".")[0]
        return f"astrbot_plugin_{name}"
    if "/" in module_path or "\\" in module_path:
        return module_path.replace("\\", "/").rsplit("/", 1)[0].rsplit("/", 1)[-1]
    return ""


def capture(store: PathStore, args: dict | None) -> None:
    """单次工具调用→入库。内部全防御：任何异常只见日志不见事件环。"""
    if not args:
        return
    tool_name = str(args.get("tool_name") or "")
    tool_args = args.get("tool_args") or {}
    module_path = args.get("handler_module_path") or ""
    plugin = plugin_from_module_path(module_path)

    paths = extract_paths_from_args(tool_name, tool_args)
    if not paths:
        return
    # A3 批量：收集本调用内全部命中，一次 upsert_many 单事务落库（每条 fsync → 整批一次）
    pending: list[dict] = []
    for p, src in paths:
        try:
            # A2 快判归一：路径存在才 realpath，不存在走 abspath（省整链 syscall）
            real = _eventually_real(p)
            try:
                st = os.stat(real)
                size, mtime = st.st_size, st.st_mtime
            except OSError:
                # 路径暂不存在：仍可建档，快照置 0，由巡检判活
                size, mtime = 0, 0.0
            pending.append(
                {
                    "path": real,
                    "plugin_name": plugin if plugin != "astrpathcompass" else "",
                    "role": _infer_role(real),
                    "source": src,
                    "size": size,
                    "mtime": mtime,
                }
            )
        except Exception:
            logger.warning("[astrpathcompass] 捕获提取异常(已隔离): %r", p, exc_info=True)
    if pending:
        try:
            store.upsert_many(pending)
        except Exception:
            logger.warning("[astrpathcompass] 批量入库异常(已隔离): %d 条", len(pending), exc_info=True)


def _infer_role(path: str) -> str:
    """按路径形态推角色：/data/config→config, 日志→log, .db→db, /data/plugins→code 等。"""
    low = path.lower()
    if low.startswith("/proc/") or low.startswith("/sys/") or low.startswith("/dev/"):
        return "misc"
    if "/data/config/" in low:
        return "config"
    if "/data/skills/" in low:
        return "skill"
    if "/data/plugin_data/" in low:
        return "db" if low.endswith(".db") else "misc"
    if low.endswith((".db", ".sqlite", ".sqlite3")):
        return "db"
    if "log" in low.rsplit("/", 1)[-1]:
        return "log"
    if "/data/plugins/" in low:
        return "code"
    if any(low.endswith(x) for x in (".py", ".js", ".ts", ".go", ".json", ".yaml", ".yml")):
        return "code"
    return "misc"