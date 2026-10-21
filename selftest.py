"""self_review 离线自测：只跑纯函数与渲染，不连线上服务。

    python3 selftest.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="self_review_")
os.environ["REVIEW_DATA_DIR"] = TMP
os.environ["REVIEW_STATE_DIR"] = os.path.join(TMP, "state")
os.environ["REVIEW_KB_ROOT"] = os.path.join(TMP, "kb")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import main as m  # noqa: E402

m.NOTES = os.path.join(TMP, "notes")

PASS = 0
FAIL = 0


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {extra}")


KNOWN = {"M0001", "M0002", "M0003", "E1", "E2", "N1"}

print("■ 解析模型输出")
ok(m.parse_review('{"reflections":[],"diary":"x"}')["diary"] == "x", "裸 JSON")
ok(m.parse_review('```json\n{"diary":"y"}\n```')["diary"] == "y", "代码围栏")
ok(m.parse_review('好的，结果如下：\n{"diary":"z"}\n以上。')["diary"] == "z", "前后废话")
ok(m.parse_review("我觉得用户挺好的") is None, "不是 JSON 返回 None")
ok(m.parse_review("") is None, "空返回 None")
ok(m.parse_review('{"a":1}') is not None, "没有 reflections 也不崩")

print("■ 校验：kind")
obj = {
    "reflections": [
        {"kind": "pattern", "text": "用户每次说随便的时候其实是想吃面", "sources": ["M0001", "M0002", "M0003"], "trigger": ["随便", "吃面"]},
        {"kind": "胡闹", "text": "不该留下", "sources": ["M0001"]},
    ],
    "diary": "今天不错。",
}
refs, diary, st = m.validate_review(obj, KNOWN)
ok(len(refs) == 1, "只留合法 kind", st)
ok(st["bad_kind"] == 1, "记了 bad_kind")

print("■ 校验：pattern 至少要 3 个来源")
obj = {"reflections": [{"kind": "pattern", "text": "用户似乎偏爱下午", "sources": ["M0001", "M0002"]}], "diary": ""}
refs, _, st = m.validate_review(obj, KNOWN)
ok(refs == [], "两条来源的 pattern 被拒", st)
ok(st["few_src"] == 1, "记了 few_src")

print("■ 校验：来源必须真实存在")
obj = {"reflections": [{"kind": "consolidation", "text": "用户最近在忙一件事", "sources": ["M9999"]}], "diary": ""}
refs, _, st = m.validate_review(obj, KNOWN)
ok(refs == [], "假来源被拒", st)
ok(st["no_src"] == 1, "记了 no_src")

print("■ 校验：物理动作")
obj = {"reflections": [{"kind": "consolidation", "text": "我把杯子放到桌上", "sources": ["M0001"]}], "diary": ""}
refs, _, st = m.validate_review(obj, KNOWN)
ok(refs == [], "物理动作被拒", st)
obj = {"reflections": [], "diary": "我把窗户擦了一遍。"}
refs, diary, _ = m.validate_review(obj, KNOWN)
ok(diary == "", "日记里的物理动作被清掉")

print("■ 校验：条数与长度")
obj = {"reflections": [{"kind": "consolidation", "text": f"第{i}件事", "sources": ["M0001"]} for i in range(6)], "diary": ""}
refs, _, _ = m.validate_review(obj, KNOWN)
ok(len(refs) == m.MAX_REFLECTIONS, "最多 3 条", len(refs))
obj = {"reflections": [{"kind": "consolidation", "text": "长" * 200, "sources": ["M0001"]}], "diary": ""}
refs, _, _ = m.validate_review(obj, KNOWN)
ok(refs == [], "超长被拒")
obj = {"reflections": [], "diary": "字" * 900}
_, diary, _ = m.validate_review(obj, KNOWN)
ok(len(diary) <= m.MAX_DIARY + 2, "日记截断", len(diary))

print("■ 相关度（注入用）")
items = [
    {"text": "用户每次说随便的时候其实是想吃面", "trigger": ["随便", "吃面"]},
    {"text": "用户下午更容易想事情", "trigger": ["下午", "效率"]},
    {"text": "用户说过要给我看一张照片", "trigger": ["照片"]},
]
hits = m.pick_relevant("晚上吃什么呀，随便吗", items)
ok(hits and "随便" in hits[0]["text"], "触发词命中（随便）", [h["text"] for h in hits])
ok(m.pick_relevant("今天天气不错", items) == [], "无关的不硬塞")
ok(m.pick_relevant("", items) == [], "空查询不返回")
ok(len(m.pick_relevant("下午好累，随便吃点", items)) <= m.INJECT_MAX, "最多两条")
old = [{"text": "用户每次说随便的时候其实是想吃面"}]
ok(m.pick_relevant("用户今天又说随便了", old), "没触发词的老条目退回二元组")

print("■ 相对时间")
today = time.strftime("%Y-%m-%d")
ok(m._days_ago(today, today) == "", "当天不标")
y = (__import__("datetime").date.today() - __import__("datetime").timedelta(days=1)).strftime("%Y-%m-%d")
ok(m._days_ago(y, today) == "昨天想的", "昨天")
d3 = (__import__("datetime").date.today() - __import__("datetime").timedelta(days=3)).strftime("%Y-%m-%d")
ok(m._days_ago(d3, today) == "3 天前想的", "三天")
d20 = (__import__("datetime").date.today() - __import__("datetime").timedelta(days=20)).strftime("%Y-%m-%d")
ok(m._days_ago(d20, today) == "2 周前想的", "二十天")
ok(m._days_ago("", today) == "", "空日期不崩")
ok(m._days_ago("坏日期", today) == "", "坏日期不崩")

print("■ 对话渲染")
msgs = [
    {"role": "user", "content": [{"type": "text", "text": "[21:03:11] 今天好累\n<system_reminder>Current datetime: 2026-10-18 21:03 (CST), Weekday: Sunday</system_reminder>"}]},
    {"role": "assistant", "content": [{"type": "text", "text": "那就早点睡。"}]},
]
txt = m.render_turns(msgs)
ok("2026-10-18 21:03" in txt, "时间锚留下来了")
ok("用户：今天好累" in txt and "我：那就早点睡" in txt, "人称对")
ok("<system_reminder>" not in txt, "提醒块被剥掉")
ok("[21:03:11]" not in txt, "前缀时刻被剥掉")

print("■ 日记")
p = m.write_diary("今天用户回来得早。", "2026-10-17")
ok(p and os.path.exists(p), "文件写了", p)
body = open(p, encoding="utf-8").read()
ok(body.startswith("# 2026-10-17"), "有日期标题")
ok("今天用户回来得早" in body, "正文对")
ok(oct(os.stat(p).st_mode)[-3:] == "644", "权限 644")
ok(m.write_diary("   ") is None, "空日记不写")

print()
print(f"通过 {PASS} · 失败 {FAIL}")
sys.exit(1 if FAIL else 0)
