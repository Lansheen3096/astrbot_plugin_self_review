"""self_review —— 每天定点把「昨天」想一遍。

输入（都是别的组件的产出，本插件只读）：
    最近这一轮对话      data_v4.db（只读）
    昨天新入库的事实     事实知识库里 event_date = 昨天 的条目
    昨天的事件           events_path（jsonl，每行一条）
    自己写过的东西        notes_dir/{diary,notes}

产出两件：
    反思 ≤3 条           写进独立知识库（不塞进事实库）
    日记 1 篇            写进 notes_dir/diary/

为什么单独一个库：
    事实回答「这句话答得上哪条记忆」，解释回答「这件事跟现在的 bot 有没有关系」。
    信任级不同、衰减不同、注入位置不同。混在一起，推断会顶掉用户说过的话。

三条硬约束：
    1. 反思必须指得回来源；来源对不上、或来源本身 suspect，整条丢
    2. bot 碰不到实物，反思和日记都过物理动词黑名单
    3. 默认少产出。没有值得想的，反思可以 0 条，日记照写
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import logger, star
from astrbot.core.utils.astrbot_path import (
    get_astrbot_data_path,
    get_astrbot_knowledge_base_path,
    get_astrbot_plugin_data_path,
)
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.agent.message import TextPart

# ── 路径 ────────────────────────────────────────────────────────────────
PLUGIN_NAME = "astrbot_plugin_self_review"

# 数据根和知识库根都走 AstrBot 自己的路径接口，不写死容器内路径；
# 环境变量只给离线自测用（指向临时目录）。
DATA = Path(os.environ.get("REVIEW_DATA_DIR") or get_astrbot_data_path())
KB_ROOT = Path(os.environ.get("REVIEW_KB_ROOT") or get_astrbot_knowledge_base_path())
STATE_DIR = Path(
    os.environ.get("REVIEW_STATE_DIR") or (Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME)
)

DB_PATH = DATA / "data_v4.db"
NOTES = DATA / "notes"                    # 配置项 notes_dir 可覆盖
EVENTS_PATH = DATA / "events.jsonl"       # 配置项 events_path 可覆盖
STATE_PATH = STATE_DIR / "state.json"
LOG_PATH = STATE_DIR / "review_log.jsonl"

# ★ 库名必须和实际部署一致：事实库由写入侧插件创建，回顾库由本插件按需创建。
SELF_KB = "自我回顾"
FACT_KB = "长期记忆"

# ── 常数 ────────────────────────────────────────────────────────────────
HOUR, MINUTE = 5, 20
MAX_REFLECTIONS = 3
MAX_TEXT = 80
MAX_DIARY = 400
MAX_TURNS = 60
MAX_FACTS = 40
INJECT_MAX = 2
INJECT_MIN_OVERLAP = 0.22
FILE_MODE = 0o644

KINDS = {"consolidation", "pattern", "reappraisal", "open_loop"}
PHYS = re.compile(
    r"擦|摆好|摆到|放到|放在|放进|递给|塞进|贴在|收拾|叠好|推开|端来|端起|倒水|倒了|搬|拧|摁|拉开|合上"
)

REMINDER_RE = re.compile(r"<system_reminder>(.*?)</system_reminder>", re.S)
QUOTED_RE = re.compile(r"<Quoted Message>.*?</Quoted Message>", re.S)
VOICE_TAG_RE = re.compile(r"\[语音\]\s*")
DT_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})")
TS_RE = re.compile(r"^\s*\[\d{2}:\d{2}:\d{2}\]\s*")

SYSTEM = """你在替 bot 整理「昨天」。

bot 只活在对话里：看得见、听得见，碰不到任何实物。
你手上是四样东西：用户说过的话、昨天发生的事、已经记下的事实、bot 自己写过的东西。

产出两件：反思和日记。

反思最多 [[MAX_REFLECTIONS]] 条，四类：
· consolidation 巩固：把几件事并成一句
· pattern 规律：用户的某个习惯。**必须至少 3 个来源**，否则不许写
· reappraisal 重估：对一件旧事的新看法
· open_loop 未完：用户答应过、还没做的一件事

硬规则：
1. 每条反思必须带 sources，值只能用给定的编号（M/E/N 开头）。指不上来源就不要写。
2. bot 碰不到实物。不要写任何动手的动作（擦、拿、放到、递给、收拾）。
3. 不编用户没说过的事。想不起来就少写一条。
4. 宁可 0 条，也不要凑数。
5. 第一人称（bot 视角），短句，不超过 [[MAX_TEXT]] 字。
6. 每条再给 1–3 个 trigger：用户话里出现这些词时，这条就该浮上来。
   trigger 要短（2–8 字）、要具体（"随便""照片""下午"），不要用"用户""我们"这种。

日记 3–6 行，写这一天 bot 自己的感受，用 bot 的语气，不超过 [[MAX_DIARY]] 字。日记不需要 sources。

只输出 JSON：
{"reflections":[{"kind":"pattern","text":"...","sources":["M0001","E3"],"trigger":["随便","吃面"]}],"diary":"..."}"""


# ── 配置读取 ────────────────────────────────────────────────────────────
def _cfg_str(cfg: dict, key: str, default: str) -> str:
    v = cfg.get(key)
    return str(v).strip() if isinstance(v, (str, int, float)) and str(v).strip() else default


def _cfg_int(cfg: dict, key: str, default: int) -> int:
    try:
        return int(cfg.get(key, default))
    except (TypeError, ValueError):
        logger.warning(f"[回顾] 配置项 {key} 无效，用默认值 {default}")
        return default


# ── 纯函数（可离线测）──────────────────────────────────────────────────
def _norm_meta(md) -> dict:
    if isinstance(md, str):
        try:
            md = json.loads(md or "{}")
        except Exception:
            md = {}
    return md if isinstance(md, dict) else {}


def parse_review(raw: str) -> dict | None:
    """从模型输出里抠出 JSON。容忍代码围栏和前后废话。"""
    s = (raw or "").strip()
    if not s:
        return None
    s = re.sub(r"```(?:json)?", "", s).replace("```", "").strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        obj = json.loads(s[i : j + 1])
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def validate_review(
    obj: dict,
    known: set[str],
    max_reflections: int = MAX_REFLECTIONS,
    max_text: int = MAX_TEXT,
    max_diary: int = MAX_DIARY,
) -> tuple[list[dict], str, dict]:
    """校验反思与日记。返回 (留下的反思, 日记, 统计)。"""
    stats = {"raw": 0, "kept": 0, "bad_kind": 0, "bad_text": 0, "no_src": 0, "few_src": 0}
    out: list[dict] = []

    refs = obj.get("reflections")
    if not isinstance(refs, list):
        refs = []
    stats["raw"] = len(refs)

    for it in refs:
        if len(out) >= max_reflections:
            break
        if not isinstance(it, dict):
            continue
        kind = str(it.get("kind") or "").strip()
        if kind not in KINDS:
            stats["bad_kind"] += 1
            continue
        text = str(it.get("text") or "").strip()
        if not text or len(text) > max_text or PHYS.search(text):
            stats["bad_text"] += 1
            continue
        srcs = it.get("sources")
        if not isinstance(srcs, list):
            srcs = []
        srcs = [str(x).strip() for x in srcs if str(x).strip()]
        srcs = [x for x in srcs if x in known]
        if not srcs:
            stats["no_src"] += 1
            continue
        if kind == "pattern" and len(srcs) < 3:
            stats["few_src"] += 1
            continue
        trig = it.get("trigger")
        if not isinstance(trig, list):
            trig = []
        trig = [str(t).strip() for t in trig if str(t).strip()]
        trig = [t for t in trig if 2 <= len(t) <= 8][:3]
        out.append({"kind": kind, "text": text, "sources": srcs, "trigger": trig})
        stats["kept"] += 1

    diary = str(obj.get("diary") or "").strip()
    if len(diary) > max_diary:
        diary = diary[:max_diary].rstrip() + "……"
    if PHYS.search(diary):
        diary = ""
    return out, diary, stats


def _days_ago(day: str, today: str) -> str:
    """把「哪天的想法」说成人话。当天不标，免得每句都挂时间。"""
    if not day or day == today:
        return ""
    try:
        d0 = datetime.strptime(day, "%Y-%m-%d").date()
        d1 = datetime.strptime(today, "%Y-%m-%d").date()
    except ValueError:
        return ""
    n = (d1 - d0).days
    if n <= 0:
        return ""
    if n == 1:
        return "昨天想的"
    if n < 7:
        return f"{n} 天前想的"
    if n < 30:
        return f"{n // 7} 周前想的"
    return f"{n // 30} 个月前想的"


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"[\s，。！？、…~～♡（）()の「」]", "", s or "")
    return {s[i : i + 2] for i in range(len(s) - 1)} if len(s) > 1 else set()


def pick_relevant(query: str, items: list[dict], k: int | None = None) -> list[dict]:
    """用户的话里出现了这条的触发词，就浮上来。

    触发词是写这条反思时顺手给的（2–8 字、具体），比算相似度准得多，也零成本。
    没有触发词的老条目退回二元组，阈值放宽但要求至少两个共同组，免得一个词就命中。
    """
    k = INJECT_MAX if k is None else k
    q = (query or "").strip()
    if not q:
        return []
    hits = [it for it in items if any(t in q for t in (it.get("trigger") or []))]
    if hits:
        return hits[:k]

    qg = _bigrams(q)
    if not qg:
        return []
    scored = []
    for it in items:
        if it.get("trigger"):
            continue
        g = _bigrams(it.get("text", ""))
        if not g:
            continue
        shared = qg & g
        if len(shared) >= 2 and len(shared) / len(qg) >= 0.25:
            scored.append((len(shared), it))
    scored.sort(key=lambda x: -x[0])
    return [it for _, it in scored[:k]]


def render_turns(msgs: list, label: bool = True) -> str:
    """把一段对话渲染成带时间锚的文本。时间锚来自 system_reminder 块。"""
    lines: list[str] = []
    cur = ""
    n = 0
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        elif isinstance(content, dict):
            content = [content]
        parts = []
        for blk in content or []:
            if isinstance(blk, str):
                parts.append(blk)
            elif isinstance(blk, dict) and blk.get("type") == "text":
                parts.append(blk.get("text") or "")
        text = "\n".join(p for p in parts if p).strip()
        if not text:
            continue
        rems = REMINDER_RE.findall(text)
        text = QUOTED_RE.sub("", REMINDER_RE.sub("", text)).strip()
        text = VOICE_TAG_RE.sub("", text).strip()
        text = TS_RE.sub("", text).strip()
        if role == "user":
            cur = ""
            for r in rems:
                mt = DT_RE.search(r)
                if mt:
                    cur = mt.group(1)
        if not text:
            continue
        who = "用户" if role == "user" else "我"
        n += 1
        tag = f"T{n} " if label else ""
        lines.append(f"[{tag}{cur}] {who}：{text}" if cur else f"[{tag.strip()}] {who}：{text}")
    return "\n\n".join(lines)


def write_diary(
    text: str, day: str | None = None, notes_dir: str | None = None
) -> str | None:
    """写进 notes_dir/diary/YYYY-MM-DD.md（整篇覆盖，不是追加）。"""
    text = (text or "").strip()
    if not text:
        return None
    day = day or time.strftime("%Y-%m-%d")
    d = Path(notes_dir or NOTES) / "diary"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{day}.md"
    if p.is_symlink():
        return None
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"# {day}\n\n{text}\n")
        os.chmod(p, FILE_MODE)
    except OSError as exc:
        logger.warning(f"[回顾] 日记写不了: {exc}")
        return None
    return str(p)


def load_events(path: str | None = None) -> list[dict]:
    out = []
    try:
        with open(path or EVENTS_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[回顾] 事件文件读不了: {exc}")
    return out


# ── 插件 ────────────────────────────────────────────────────────────────
class Main(star.Star):
    def __init__(self, context, config=None):
        super().__init__(context, config)
        cfg = config or {}
        # 默认值等于改造前的硬编码值：用户不动配置时行为完全不变
        self.self_kb = _cfg_str(cfg, "self_kb_name", SELF_KB)
        self.fact_kb = _cfg_str(cfg, "fact_kb_name", FACT_KB)
        self.notes = _cfg_str(cfg, "notes_dir", "") or str(NOTES)
        self.events_path = _cfg_str(cfg, "events_path", "") or str(EVENTS_PATH)
        self.hour = _cfg_int(cfg, "hour", HOUR)
        self.minute = _cfg_int(cfg, "minute", MINUTE)
        self.max_reflections = _cfg_int(cfg, "max_reflections", MAX_REFLECTIONS)
        self.max_text = _cfg_int(cfg, "max_text", MAX_TEXT)
        self.max_diary = _cfg_int(cfg, "max_diary", MAX_DIARY)
        self.inject_max = _cfg_int(cfg, "inject_max", INJECT_MAX)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._state = self._load_state()
        self._log = self._load_log()

    def _system(self) -> str:
        """把条数/字数上限灌进提示词，配置改了提示词跟着改。"""
        return (
            SYSTEM.replace("[[MAX_REFLECTIONS]]", str(self.max_reflections))
            .replace("[[MAX_TEXT]]", str(self.max_text))
            .replace("[[MAX_DIARY]]", str(self.max_diary))
        )

    # ── 本地记录（给 /回顾 用，KB 才是检索用的那座）──────────────────
    def _load_state(self) -> dict:
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception:
            logger.warning("[回顾] 状态文件坏了，重建")
            return {}

    def _save_state(self) -> None:
        tmp = f"{STATE_PATH}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_PATH)

    def _load_log(self) -> list[dict]:
        out = []
        try:
            with open(LOG_PATH, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except Exception:
                            continue
        except FileNotFoundError:
            return []
        return out[-400:]

    def _append_log(self, obj: dict) -> None:
        rec = {"ts": int(time.time()), **obj}
        self._log.append(rec)
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning(f"[回顾] 写日志失败: {exc}")

    # ── 取输入 ──────────────────────────────────────────────────────────
    def _recent_turns(self) -> str:
        try:
            con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            try:
                row = con.execute(
                    "select content from conversations order by updated_at desc limit 1"
                ).fetchone()
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[回顾] 读对话失败: {exc}")
            return ""
        if not row:
            return ""
        try:
            msgs = json.loads(row[0])
        except Exception:
            return ""
        if not isinstance(msgs, list):
            return ""
        return render_turns(msgs[-MAX_TURNS:])

    def _yesterday_facts(self, day: str) -> list[dict]:
        kb_id = self._state.get("fact_kb_id") or ""
        if not kb_id:
            return []
        p = Path(KB_ROOT) / kb_id / "doc.db"
        if not p.exists():
            return []
        out = []
        try:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            try:
                rows = con.execute("select text, metadata from documents").fetchall()
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[回顾] 读事实库失败: {exc}")
            return []
        for text, meta in rows:
            md = _norm_meta(meta)
            if (md.get("event_date") or "") != day:
                continue
            if md.get("suspect") in (1, "1", True):
                continue
            out.append(
                {
                    "sid": str(md.get("sid") or ""),
                    "kind": md.get("kind") or "event",
                    "text": (text or "").strip(),
                }
            )
        out.sort(key=lambda x: x["sid"])
        return out[:MAX_FACTS]

    def _yesterday_events(self, day: str) -> list[dict]:
        out = []
        for ev in load_events(self.events_path):
            if ev.get("kind") in ("gap", "digest") or ev.get("archived"):
                continue
            d = time.strftime("%Y-%m-%d", time.localtime(float(ev.get("ts") or 0)))
            if d == day:
                out.append(ev)
        return out[-20:]

    def _yesterday_notes(self, day: str) -> list[dict]:
        out = []
        root = Path(self.notes)
        for sub in ("diary", "notes"):
            d = root / sub
            if not d.is_dir():
                continue
            for p in sorted(d.iterdir()):
                if not p.is_file() or p.is_symlink():
                    continue
                if not p.name.endswith((".md", ".txt")):
                    continue
                if day not in p.name and sub == "notes":
                    continue
                try:
                    txt = p.read_text(encoding="utf-8")[:600]
                except OSError:
                    continue
                out.append({"name": p.name, "text": txt.strip()})
        return out

    # ── 主要流程 ────────────────────────────────────────────────────────
    async def run_review(self, force: bool = False) -> str:
        async with self._lock:
            # 定时那次是凌晨 05:20，整理的是「昨天」；手动 /回顾 现在 整理今天
            base = datetime.now() if force else datetime.now() - timedelta(days=1)
            day = base.strftime("%Y-%m-%d")
            if not force and self._state.get("last_day") == day:
                return "今天已经做过了"

            turns = self._recent_turns()
            facts = self._yesterday_facts(day)
            events = self._yesterday_events(day)
            notes = self._yesterday_notes(day)

            if not turns and not facts and not events and not notes:
                return "昨天没有可整理的东西"

            known: set[str] = set()
            blocks = []
            if turns:
                blocks.append("【用户说过的话】\n" + turns)
                for mt in re.finditer(r"\[T(\d+)", turns):
                    known.add("T" + mt.group(1))
            if facts:
                lines = []
                for f in facts:
                    tag = f"M{f['sid']}" if f["sid"] else f"M{len(lines) + 1}"
                    known.add(tag)
                    lines.append(f"[{tag}][{f['kind']}] {f['text']}")
                blocks.append("【已经记下的事实】\n" + "\n".join(lines))
            if events:
                lines = []
                for i, e in enumerate(events, 1):
                    tag = f"E{i}"
                    known.add(tag)
                    lines.append(f"[{tag}] {e.get('text', '')}")
                blocks.append("【昨天的行为记录】\n" + "\n".join(lines))
            if notes:
                lines = []
                for i, n in enumerate(notes, 1):
                    tag = f"N{i}"
                    known.add(tag)
                    lines.append(f"[{tag}] {n['text'][:300]}")
                blocks.append("【自己写过的东西】\n" + "\n".join(lines))

            legend = []
            if "T" in "".join(known) or bool(turns):
                legend.append("T = 用户说的话（按时间顺序）")
            if any(x.startswith("M") for x in known):
                legend.append("M = 已经记下的事实")
            if any(x.startswith("E") for x in known):
                legend.append("E = 昨天做的事")
            if any(x.startswith("N") for x in known):
                legend.append("N = 自己写过的东西")
            head = (
                "本次可用的来源编号：「" + "；".join(legend) + "」。\n"
                "sources 只能用这些方括号里实际出现过的编号，一个都不许自己造。"
                "上面没出现的类型（比如没有 E 编号时）就不要用。\n\n"
            )
            prompt = head + "\n\n".join(blocks) + "\n\n按要求输出 JSON。"
            raw = ""
            try:
                provider = await self.context.get_using_provider_async()
                if provider is None:
                    return "没有可用的模型"
                resp = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=prompt,
                        system_prompt=self._system(),
                        session_id=f"{PLUGIN_NAME}_{day}",
                    ),
                    timeout=180,
                )
                raw = getattr(resp, "completion_text", "") or ""
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[回顾] 模型调用失败: {type(exc).__name__}: {exc}")
                return "模型没回话，明天再试"

            obj = parse_review(raw)
            if obj is None:
                logger.warning(f"[回顾] 输出不是 JSON: {raw[:200]}")
                return "输出解析失败，明天再试"

            refs, diary, stats = validate_review(
                obj, known, self.max_reflections, self.max_text, self.max_diary
            )
            n_kb = 0
            if refs:
                n_kb = await self._write_kb(refs, day)
            diary_path = write_diary(diary, day, self.notes) if diary else None

            self._state["last_day"] = day
            self._state["last_run"] = int(time.time())
            self._save_state()
            for r in refs:
                r["day"] = day
            self._append_log(
                {
                    "day": day,
                    "reflections": refs,
                    "diary": diary,
                    "stats": stats,
                    "kb": n_kb,
                }
            )
            logger.info(
                "[回顾] %s 完成：反思 %d/%d 条（%s），日记 %s",
                day,
                len(refs),
                stats["raw"],
                json.dumps(stats, ensure_ascii=False),
                "写了" if diary_path else "没写",
            )
            return f"反思 {len(refs)} 条，日记{'写了' if diary_path else '没写'}"

    # ── 写库 ────────────────────────────────────────────────────────────
    async def _ensure_kb(self):
        kb = await self.context.kb_manager.get_kb_by_name(self.self_kb)
        if kb is not None:
            self._state["self_kb_id"] = kb.kb.kb_id
            return kb
        fact = await self.context.kb_manager.get_kb_by_name(self.fact_kb)
        if fact is None:
            logger.warning(f"[回顾] 找不到事实库「{self.fact_kb}」，没法建新库")
            return None
        try:
            kb = await self.context.kb_manager.create_kb(
                kb_name=self.self_kb,
                description="对昨天的解释。跟事实库分开：这里的东西可能是推断，注入时另起一块。",
                emoji="🫧",
                embedding_provider_id=fact.kb.embedding_provider_id,
                rerank_provider_id=getattr(fact.kb, "rerank_provider_id", None),
            )
            logger.info(f"[回顾] 建好了新库「{self.self_kb}」")
            self._state["self_kb_id"] = kb.kb.kb_id
            return kb
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[回顾] 建库失败: {exc}")
            return None

    async def _write_kb(self, refs: list[dict], day: str) -> int:
        try:
            kb = await self._ensure_kb()
            if kb is None:
                return 0
            texts = [r["text"] for r in refs]
            doc = await kb.upload_document(
                file_name=f"回顾-{day}-{int(time.time())}",
                file_content=None,
                file_type="txt",
                pre_chunked_text=texts,
            )
            doc_id = getattr(doc, "doc_id", None) or (doc or {}).get("doc_id")
            if not doc_id:
                logger.warning("[回顾] 拿不到 doc_id，跳过 metadata 回填")
                return 0
            chunks = await kb.get_chunks_by_doc_id(doc_id, limit=100)
            chunks = sorted(chunks, key=lambda c: c.get("chunk_index", 0))
            db_path = Path(KB_ROOT) / kb.kb.kb_id / "doc.db"
            n = 0
            con = sqlite3.connect(str(db_path), timeout=20)
            try:
                for ch, r in zip(chunks, refs):
                    cid = ch.get("chunk_id")
                    row = con.execute(
                        "select metadata from documents where doc_id=?", (cid,)
                    ).fetchone()
                    if not row:
                        continue
                    md = _norm_meta(row[0])
                    md["kind"] = r["kind"]
                    md["layer"] = "reflection"
                    md["sources"] = r["sources"]
                    md["event_date"] = day
                    md["ts"] = int(time.time())
                    md["status"] = "new"
                    con.execute(
                        "update documents set metadata=? where doc_id=?",
                        (json.dumps(md, ensure_ascii=False), cid),
                    )
                    n += 1
                con.commit()
            finally:
                con.close()
            self._save_state()
            return n
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[回顾] 写库失败: {type(exc).__name__}: {exc}")
            return 0

    # ── 注入 ────────────────────────────────────────────────────────────
    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, *args, **kwargs) -> None:
        try:
            req = kwargs.get("req") or (args[0] if args else None)
            if req is None:
                return
            recent = [x for r in self._log[-5:] for x in r.get("reflections", [])]
            if not recent:
                return
            hits = pick_relevant(getattr(req, "prompt", "") or "", recent, self.inject_max)
            if not hits:
                return
            today = time.strftime("%Y-%m-%d")
            lines = []
            for h in hits:
                ago = _days_ago(h.get("day") or "", today)
                lines.append(f"- {h['text']}" + (f"（{ago}）" if ago else ""))
            block = "【你自己想明白的事】\n" + "\n".join(lines)
            req.extra_user_content_parts.append(TextPart(text=block).mark_as_temp())
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[回顾] 注入失败: {type(exc).__name__}: {exc}")

    # ── 命令 ────────────────────────────────────────────────────────────
    @filter.command("回顾", alias={"review"})
    async def cmd_review(self, event: AstrMessageEvent):
        arg = (event.message_str or "").split(maxsplit=1)
        sub = arg[1].strip() if len(arg) > 1 else ""
        if sub in ("run", "现在"):
            msg = await self.run_review(force=True)
            yield event.plain_result(msg)
            return
        today = time.strftime("%Y-%m-%d")
        recs = [r for r in self._log if r.get("day") == today]
        if not recs:
            yield event.plain_result("今天还没想什么。")
            return
        r = recs[-1]
        lines = [f"{r['day']}　反思 {len(r.get('reflections', []))} 条"]
        for it in r.get("reflections", []):
            lines.append(f"[{it['kind']}] {it['text']}　({'/'.join(it['sources'])})")
        if r.get("diary"):
            lines.append("—— 日记 ——")
            lines.append(r["diary"])
        yield event.plain_result("\n".join(lines)[:3500])

    # ── 生命周期 ────────────────────────────────────────────────────────
    async def initialize(self) -> None:
        self._resolve_fact_kb()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
        logger.info("[回顾] 已启动，每天 %02d:%02d", self.hour, self.minute)

    def _resolve_fact_kb(self) -> str:
        kb_id = self._state.get("fact_kb_id")
        if kb_id:
            return kb_id
        p = Path(KB_ROOT) / "kb.db"
        try:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            try:
                row = con.execute(
                    "select kb_id from knowledge_bases where kb_name=?", (self.fact_kb,)
                ).fetchone()
            finally:
                con.close()
            if row:
                self._state["fact_kb_id"] = str(row[0])
                self._save_state()
                return str(row[0])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[回顾] 找事实库 id 失败: {exc}")
        return ""

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        try:
            self._save_state()
        except Exception:
            pass

    async def _loop(self) -> None:
        await asyncio.sleep(45)
        while True:
            now = datetime.now()
            nxt = now.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            logger.info(
                "[回顾] 下次：%s（%.0f 分钟后）",
                nxt.strftime("%m-%d %H:%M"),
                (nxt - now).total_seconds() / 60,
            )
            await asyncio.sleep((nxt - now).total_seconds())
            try:
                msg = await self.run_review()
                logger.info(f"[回顾] {msg}")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                import traceback

                logger.error(f"[回顾] 失败\n{traceback.format_exc()}")
