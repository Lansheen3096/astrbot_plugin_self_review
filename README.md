# astrbot_plugin_self_review

**自我回顾**：每天定点把「昨天」想一遍 —— 提炼 ≤3 条反思写进独立知识库，再写一篇日记。

版本 `0.1.1` · AstrBot `>= 4.28.2`

---

## 它解决什么

普通记忆是「用户说过什么」，这里补的是另一半：**这些事跟现在的 bot 有没有关系**。
人设里写着「我会想一想昨天」，但没有任何链路真的让它想 —— 于是它只能在对话里编。

## 输入（都只读）

| 来源 | 用途 |
|---|---|
| `data_v4.db` 最近一轮对话 | 用户说过的话，按时间锚渲染 |
| 事实知识库里 `event_date = 昨天` 的条目 | 昨天新入库的事实 |
| `events_path`（jsonl） | 昨天的行为记录 |
| `notes_dir/{diary,notes}` | 自己写过的东西 |

## 输出两件

- **反思 ≤3 条** → 写进独立知识库（默认 `自我回顾`）
- **日记 1 篇** → `<notes_dir>/diary/YYYY-MM-DD.md`

**为什么单独一个库**：事实回答「这句话答得上哪条记忆」，反思回答「这件事跟现在的 bot 有没有关系」。
信任级不同、衰减不同、注入位置不同。混在一起，推断会顶掉用户说过的话。

## 三条硬约束（代码里，不只是提示词）

1. **反思必须指得回来源**：`sources` 只能用本次实际出现的编号，对不上整条丢；
   `pattern` 类必须至少 3 个来源。
2. **bot 碰不到实物**：反思和日记都过物理动词黑名单（擦、拿、放到、递给、收拾…）。
3. **默认少产出**：没有值得想的，反思可以 0 条，日记照写。

## 注入

每轮把最近几天的反思按 trigger 词匹配，命中就临时注入（`mark_as_temp()`，不写进历史）。
没有 trigger 的老条目退回二元组匹配，要求至少两个共同组，避免一个词就命中。

## 指令

```
/回顾          看今天已经想出来的东西（反思 + 日记）
/回顾 现在     立刻跑一次，整理今天
```

## 配置

| 配置项 | 默认 | 说明 |
|---|---|---|
| `hour` / `minute` | `5` / `20` | 每天几点回顾（容器时区） |
| `fact_kb_name` | `长期记忆` | 事实库名，**必须和写入侧插件一致** |
| `self_kb_name` | `自我回顾` | 回顾库名，不存在时按需创建 |
| `notes_dir` | 空 | 留空 = 数据目录下 `notes/` |
| `events_path` | 空 | 留空 = 数据目录下 `events.jsonl` |
| `max_reflections` | `3` | 每次最多几条反思，`0` = 只写日记 |
| `max_text` | `80` | 单条反思字数上限 |
| `max_diary` | `400` | 日记字数上限 |
| `inject_max` | `2` | 每轮最多注入几条，`0` = 关闭注入 |

默认值是自用部署的档位，想更克制就往下调。

## 前置依赖（必须先做）

**先装 [`astrbot_plugin_memory_digest`](https://github.com/Lansheen3096/astrbot_plugin_memory_digest)
（长期记忆·写入侧）** —— 它就是本插件依赖的 **embedding 插件**，embedding 能力全在它那里。
顺序照下面来，缺一步本插件就写不进库：

1. 用它在 WebUI 建好**事实知识库**（默认名 `长期记忆`），本插件的 `fact_kb_name` 要填同一个名字；
2. 在它的**插件配置页**，或插件详情页的「**Embedding 控制台**」里，**配好 embedding 模型**
   （跟随知识库 / 云端 API / 本机服务三选一），保存后由它创建并绑定 `memory_layer_embedding`；
3. 回顾库（默认名 `自我回顾`）由本插件借这套 provider 按需创建。

⚠️ **embedding 没配好 = 建不了回顾库**。表现是「反思 0 条、日记照写」，
日志里能看到 `[回顾] 建库失败`。事实库本身不存在时也会跳过写库，同样只写日记。

其余：

- 事实库建议配一个 rerank provider，否则检索侧注入质量下降（不影响写库）。
- 日记与笔记目录默认落在 AstrBot 数据目录下，容器部署注意挂载卷。

## 安装

```bash
# AstrBot 插件市场搜索「自我回顾」，或：
cd <AstrBot>/data/plugins
git clone --depth 1 --branch v0.1.1 https://github.com/Lansheen3096/astrbot_plugin_self_review
# docker 部署：拷进宿主机挂载的数据目录后重启容器
docker compose restart astrbot
```

装之前先确认 `astrbot_plugin_memory_digest` 已装、embedding 模型已配好（见上一节）。
装完要**重启 AstrBot** 才生效（插件目录不热加载）。

## 日志

`[回顾]` 前缀：下次几点跑、这次留了几条反思、为什么没写日记、注入命中了几条。

## 自测

`selftest.py` 只跑纯函数与渲染，不连线上服务：

```bash
python3 selftest.py     # 37 项，全过返回 0
```

## 相关插件

- [astrbot_plugin_memory_digest](https://github.com/Lansheen3096/astrbot_plugin_memory_digest)：事实写入侧（本插件读它写的事实库）
- [astrbot_plugin_memory_recall](https://github.com/Lansheen3096/astrbot_plugin_memory_recall)：事实注入侧
- [astrbot-memory-layer](https://github.com/Lansheen3096/astrbot-memory-layer)：整套记忆管线的聚合入口

## License

MIT
