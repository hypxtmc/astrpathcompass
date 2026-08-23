# 🧭 路径罗盘 · AstrPathCompass

> 每一次翻文件都在烧 token —— 直到某个会话里同一路径来来回回被翻五六遍,你才意识到该有一张"已翻阅地图"。

AstrPathCompass 为基于缓存的 LLM 会话提供**路径位点索引**:自动旁观每一次 LLM 工具调用,把翻过的文件路径悄然记进 SQLite 户口本(FTS5 全文索引),一条 `/pathsearch` 直达。**零上下文注入、不破坏前缀缓存**。

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.27-blue)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/python-%3E%3D3.10-blue)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-v0.2.4-blue)]()
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## ✨ 它能做什么

- **翻到即记(capture)** —— 旁观 LLM 工具调用,从参数位 / shell 命令 / Python 代码里提取路径,按"来源 × 重要度门槛"建档,**全程只读旁路,不注入任何上下文、不动 messenger**
- **随用随考(search)** —— FTS5 模糊检索 + role 维度聚合 + 命中热度计数,一句话 `✕5` 告诉你这条路径被用过几次,免重翻
- **定期验活(sweeper)** —— 周期 lstat 存活校验:失效打 ✗ **不删数据**,内容被覆盖打 `[变更]` 旗标,文件回归自动复活
- **软链归一** —— `realpath()` 归一软链/别名,同目标自动合行,杜绝死胡同重复建档
- **按前缀导出/清理** —— `pathexport` / `pathclean`(带 --yes 二次确认),迁移旧机、清理局部目录一条命令
- **本轮零额外依赖** —— SQLite/FTS5 均为 Python 标准库能力,装上即跑,不装任何第三方包

---

## 🚀 快速开始

**前提**:AstrBot ≥ 4.27,Python ≥ 3.10,无需任何额外依赖。

1. `git clone https://github.com/hypxtmc/astrpathcompass` 并把目录放进 `AstrBot/data/plugins/`
2. AstrBot 控制台「插件管理」启用 `astrpathcompass`——首次启用自动建库 `pathcompass.db`(WAL 模式),无需手工初始化
3. 正常聊天即可。翻几个文件后:

```
/pathsearch tool.py
[罗盘] 命中 3 条（tool.py）:
✓ [code] ✕5 /path/to/AstrBot/astrbot/core/pipeline/process_stage/tool.py
✓ [code] ✕2 /path/to/AstrBot/data/plugins/astrpathcompass/main.py
✗ [code] ✕1 /path/to/AstrBot/data/old_probe/tool.py
```

> LLM 侧另有 **skill 卡自动接入**:启用插件后,AstrBot 的技能目录里出现 `pathsearch` 技能,LLM 在需要"凭路径做事"时按需加载——人类用命令,LLM 用技能卡,同一本户口本。

---

## 🧠 设计说明

经典思路是"把路径直接塞进 system prompt"——这会破坏前缀缓存,每轮重复计费,路径一多 prompt 跟着膨胀。本插件反其道而行:

**设 计,不是注入,是旁路:**

```
翻到即记(capture) → 随用随考(search) → 定期验活(sweeper)
```

- **capture** 用 `on_using_llm_tool` 钩子在工具调用**前**旁观,提取路径实体入账——不修改任何进入 LLM 的 token
- **search** 由环境显式提问触发,`/pathsearch`(人类)或 skill(LLM),按需拉取,零常驻开销
- **sweeper** 由 AstrBot 官方 `cron_manager.add_basic_job` 注册(持久化 `False`、幂等清理,热重载不重复堆积)

三位一体落地于 4 个文件:`main.py` 装配 / `db.py` 户口本 / `capture.py` 记账员 / `sweeper.py` 巡检员——职责单一,可单独替换。

---

## 📦 安装

| 方式 | 步骤 |
|------|------|
| **Git / 手工** | `git clone` 或解压 Release → 放入 `data/plugins/` → 控制台启用 |
| **发布 zip** | 见下「从源码构建」,在插件市场/手动安装里导入 |

插件自己的数据注册在 `data/plugin_data/astrpathcompass/pathcompass.db`,与插件目录分离——卸载插件不误删户口本,重装即恢复。

---

## 🎯 收录规则(谁会被记)

捕获层按「来源 × 重要度门槛」双维决定建档。**已建档的路径只增热度、不受阈值变动影响**;阈值只拦新面孔。

### 来源矩阵

| 来源 | 说明 | 基础分 |
|------|------|--------|
| `exact` | 工具参数位(safe_read/file_*/db_query/rg_search 等白名单参数) | 0.42 |
| `shell` | 从 `astrbot_execute_shell` 的 command 文本中提取的多级绝对路径 | 0.32 |
| `code` | 从 `astrbot_execute_python` 的 code 文本中提取的多级绝对路径 | 0.28 |
| `url` | URL 类工具(http_get/http_post/web_fetch)——当前不建档 | 0.20 |

- **参数位优先**:白名单工具(`safe_read`、`file_*`、`db_query`、`rg_search`、`dir_*` 等 20+ 个)的 `path`/`filepath`/`db_path` 等参数直接取值,最权威
- **文本扫描兜底**:shell 命令、Python 代码内部的多级绝对路径(`/a/b/c` 形态,至少两级)也会被正则识别。URL(`https://`、`ftp://`)与单级路径、相对路径不入册
- **噪音过滤**:`/.venv/`、`/node_modules/`、`/__pycache__/`、`/.git/`、`.pyc` 等运行时代码片段直接跳过

### 重要度模型(min_importance)

每条候选路径先打一个「重要性分」(0.05~1.0),分数低于门槛则**不建档**:

```
得分 = 来源基础分 + 深度加成(每级+0.045,封顶8级) + 文件加成(+0.12) − 系统路径罚分(−0.25)
```

| 路径示例 | 来源 | 得分 |
|----------|------|------|
| `/path/to/AstrBot/data/plugins/astrpathcompass/main.py` (5级文件) | exact | 0.81 |
| 同上 | shell | 0.71 |
| `/path/to/AstrBot` (根目录) | shell | 0.41 |
| `/usr/lib/python3.11/site-packages/requests` (系统库) | shell | 0.30 |
| `/dev/null` (系统伪文件) | shell | 0.16 |

**阈值手感**(手测对照):
- `0.30`:连系统库路径和 `/dev/null` 都收——档案更大但噪音上涨
- `0.40`:全真路径入册、噪声干净滤除(**默认,发布前实测甜点**)
- `0.60`:连 `/path/to/AstrBot` 这种根锚点都丢掉,只留深层文件——过度精简

调大 → 档案更精(偏冷门复杂);调小 → 档案更全(简单高频也收)。

---

## 🖥 命令与技能

| 命令 | 说明 |
|------|------|
| `/pathsearch <词>` | 检索户口本里的路径(FTS5 模糊匹配)。命中片段用 `⟪⟫` 高亮标出,缺词返回全部 |
| `/pathsweep` | 手动跑一轮存活巡检,失效标 ✗、内容变更标 [变更] |
| `/pathstatus` | 查看罗盘工作状态(捕获/巡检开关、当前门槛、库路径、存活/失效/变更统计) |
| `/pathexport <保存路径> [前缀]` | 按目录前缀导出索引(无前缀=全量导出) |
| `/pathclean <前缀>` | 按前缀清理:不带 --yes 预览待删数,加 `--yes` 才执行删除 |

### 给 LLM 的技能卡

启用插件即自动注册 `pathsearch` skill 卡(AstrBot 技能目录中可见)。技能卡划分为三个区块,LLM 按需读取:

- **[铁律]** 「查完即记」:LLM 翻过文件后主动 `/pathsearch` 收录,防漏记
- **[首页地图]** 最近高频路径速览,YAML 表格降 token
- **[档案规则]** 重要度来源矩阵 + `_ref/paths.md` 小抄的更新纪律

由此路径索引沉淀为**长期记忆的免费外挂**:每次会话都是"续写地图",而非"重新探路"。

---

## ⚙️ 配置(设置)

所有配置在 **AstrBot 控制台 → 插件管理 → astrpathcompass → 设置** 里可视化修改,改完**重载插件**生效(热重载即好,无需重启 AstrBot)。配置 schema 定义在 [`_conf_schema.json`](_conf_schema.json),控制台设置页由它自动生成。

| 键 | 类型 | 默认值 | 说明 |
|----|------|--------|------|
| `capture_enabled` | bool | `true` | 自动捕获总开关。关闭后工具调用的路径不再自动入账(存量记录仍可检索) |
| `min_importance` | float | `0.40` | 建档重要度门槛(0.05~1.0)。高→只收冷门复杂路径,低→简单高频也收 |
| `search_limit` | int | `8` | `/pathsearch` 单次最大返回条数,范围 1–20 |
| `sweep_enabled` | bool | `true` | 存活巡检开关。关闭则跳过周期巡检与手动巡检(路径频繁变动的场景可用) |
| `sweep_cron` | string | `17 * * * *` | APScheduler 标准 cron,默认每小时第 17 分执行,避开整点高峰 |

**配置→组件映射**:

| 键 | 消费方 |
|----|--------|
| `capture_enabled` | `on_using_llm_tool` 捕获入口 |
| `min_importance` | `PathStore` 建档门槛(只影响新路径) |
| `search_limit` | `pathsearch` 返回条数上限 |
| `sweep_enabled` | 是否注册周期巡检任务 |
| `sweep_cron` | cron 表达式(`context.cron_manager.add_basic_job` 挂载) |

---

## 🔍 行为细节

- **捕获**:LLM 调用函数工具前提取 `tool_args` 里的路径,三条路径来源并行(参数位 / 文本扫描 / 兜底绝对路径),按重要度阈值建档。全程只读事件旁路,不注入任何上下文,不修改 messenger
- **检索**:FTS5 模糊匹配 + role 维度(code / config / log / db / skill / misc)聚合,`✕N` 标识命中热度,失效路径(✗)排序靠后但不隐藏。路径中的查询词命中片段用 `⟪⟫` 高亮包出(多词 OR、大小写不敏感),只改展示不改库
- **巡检**:AliveSweeper 每周期遍历 `alive=1` 记录做 `lstat` 存活校验,失效标记 ✗ **不删除数据**;文件重新出现时捕获层自动归位复活
- **数据位置**:`data/plugin_data/astrpathcompass/pathcompass.db`(WAL 模式)。删除该文件即清空户口本(会随下次工具调用自动重建)

### 软链归一与变更旗标(v0.2.1)

- 入库前 `os.path.realpath()` 归一:软链/挂载别名统一落到真实路径,同目标自动合行,杜绝死胡同
- 建档/命中时一次 `os.stat` 快照 `size`/`mtime` 入库(幂等迁移,旧库自动补列)
- AliveSweeper 巡检对比 `mtime`:内容可能被覆盖的路径打 `changed=1` 旗标,不判死、不删除;`pathsearch` 命中时以 `[变更]` 标注;再次访问确认后自动复位
- 旧库升级:`_ensure_columns` 幂等 ALTER 补列,无需手工迁移

### 检索高亮(v0.2.3)

- `/pathsearch` 返回路径中的命中词片段用 `⟪⟫` 包出:
  `/path/to/AstrBot/data/plugins/⟪astrpathcompass⟫/main.py`
- 多词 OR(空格分隔逐词高亮)、大小写不敏感、下划线词条完整命中;纯展示层改动,不改数据库

### 按前缀导出/清理(v0.2.2)

- `export_jsonl(dest, prefix)`:带 LIKE 通配符转义(`_` `%`),目录名含下划线也不误漂移
- `clean_by_prefix(prefix, count_only)`:记录与 FTS 同步清理,**默认预览不执行**,`--yes` 才落地——防误删三重保险

---

## 📁 文件结构

```
astrpathcompass/
├── main.py          入口 + 组装(唯一 define 钩子的文件,命令/捕获/cron 都在此装配)
├── db.py            户口本(SQLite records + FTS5 + 复活性;提取/评分/门槛核心)
├── capture.py       记账员(工具调用 → 路径提取 → 按门槛落库)
├── sweeper.py       AliveSweeper(周期存活校验 + 手动巡检实现)
├── _conf_schema.json  控制台设置页 schema(五项配置)
├── metadata.yaml    AstrBot 插件清单
├── LICENSE          MIT 许可证
└── README.md        本文档
```

---

## 🔨 从源码构建

```bash
# 安装(开发/手工发布):
cp -r astrpathcompass /path/to/AstrBot/data/plugins/

# 发布 zip(控制台「插件市场/手动安装」用的压缩包):
zip -r astrpathcompass.zip astrpathcompass/ -x "*/__pycache__/*" "*.bak_*"
```

---

## ❓ 常见问题

**Q: 为什么我的路径没被记下来?**
A: 先看 `/pathstatus` 里「捕获」是否「开」、门槛值是多少。若门槛偏高,把 `min_importance` 调低并重载插件。其次确认路径确实是通过 LLM 工具调用翻的——只调用命令、不走工具的路径,资料会在 shell 文本扫描下照样入账。绝对路径是多级的;单级 `/tmp`、纯相对路径不入册。

**Q: 巡检发现路径标了 ✗,会不会被删?**
A: 不会。sweeper 只翻转 `alive` 标记,记录完整保留,检索结果里 ✗ 靠后但可见。原文件恢复后下次捕获会自动归位 ✓。

**Q: 会用我的敏感文件内容建档吗?**
A: 不会。capture 只做 `os.stat`(size/mtime)与路径文本,不读文件正文;索引里只有"路径 + 热度 + 存活",没有内容。全库本地 SQLite,零出网——插件代码里没有任何网络请求。

**Q: 卸载插件后数据还在吗?**
A: 在。库文件随插件数据目录残留在 `data/plugin_data/astrpathcompass/`,重装后即恢复检索。

**Q: 能把阈值设成 1.0 吗?**
A: 能,但那样只有极深层+exact 少数路径能建档,`/path/to/AstrBot` 根锚点级别全部丢掉。不建议;想更精调成 0.5~0.6、想更全调 0.3 就好。

**Q: `pathexport`/`pathclean` 有什么用?**
A: 迁移场景(新机器建同名户口本)用 `pathexport` 按前缀带走过期区;离职/换库清冷门目录用 `pathclean`——先预览后 `--yes`,绝无手滑全删。

---

## 🗺 路线图

**已完成** ✅:`pathsearch` 检索高亮(v0.2.3) · 软链归一 + 变更旗标(v0.2.1) · 按前缀导出/清理(v0.2.2) · 捕获相似路径自动合并去重(入库 realpath 归一即同目标合行)

**进行中** 🚧:
- [ ] 多语言 README(EN)

有想法欢迎提 [Issue](https://github.com/hypxtmc/astrpathcompass/issues)。