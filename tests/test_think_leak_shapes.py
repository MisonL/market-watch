#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推理块泄漏进群：两种真实形态 `_strip_think` 都漏。

## 实测（全部取自 state/history/pushes.jsonl 真实推送）

**① 开标签没有 `>`** —— 2026-11-25 16:55「调研 Agent」那条，2754 字节：

    > **调研 Agent · 11-25 16:55**
    <thinking关键位段已同步为「上方 4243.65，下方 3367.15」…
    保持原样。
    Now final message: 4 paragraphs…
    Let me count roughly final text: …

模型把 `<thinking` 和正文**粘在一起**（没写 `>`）。原正则
`<(?:think(?:ing)?|…)\\s*>` 要求那个 `>` 存在 → 整段思维链原样进群。
**那条推送里两段内容全是推理过程**，没有一句是给读者的结论。

**② 孤儿闭合标签** —— 2026-11-19 14:30 那条，1867 字节：

    **今日跌幅 TOP3**
    营销代理 -5.44%
    ---
    Write the message now. Only the message body, nothing else.</think>> **触发提醒 · 存储冲高回落**

模型吐了**两段**推理。`_THINK_RE` 非贪婪只吃掉第一对 `<think>…</think>`，
第二个 `</think>` 失去配对；`_THINK_OPEN_RE` 只管开标签 —— 它原样留在正文里。

## 修法

    _THINK_OPEN_RE  = <(?:think(?:ing)?|reasoning|analysis)\\s*>?.*\\Z   # `>` 可选
    _THINK_CLOSE_RE = </(?:think(?:ing)?|reasoning|analysis)\\s*>        # 无条件剥

⚠️ 顺序：先吃成对块与"未闭合的开标签到结尾"，**最后**剥残留闭合标签。
反过来会先把 `</think>` 删掉，让原本成对的块失去闭合端，`_THINK_RE`
匹配不上 → 整段推理留在正文里（实测确认）。

## 本文件锁住什么

① 缺 `>` 的开标签 → 剥到结尾
② 孤儿闭合标签 → 剥掉（**不**连带删掉它前面的正文）
③ 正常成对块仍剥干净（不能因为改了正则就把主路径弄坏）
④ `<0.5%` / `≤3500` 这类正常小于号**不得**被当成标签
⑤ 用真实推送原文回归：11-25 那条剥完只剩表头（触发 Agent 的质量闸门
   「过短」抑制），11-19 那条剥完 `<` 一个都不剩
⑥ 顺序守卫：注入"先剥闭合标签"必须让①的成对块漏网
"""
import json
import os
import re
import sys
import tempfile

os.environ.setdefault("MW_DIR", tempfile.mkdtemp(prefix="mw_t_think_"))

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "src")
sys.path.insert(0, _SRC)

import analyze as A

_ok = 0


def check(cond, msg):
    global _ok
    if cond:
        _ok += 1
    else:
        print(f"  ❌ {msg}")
        sys.exit(1)


# ---------- ① 开标签缺 `>` ----------
_LEAK_1125 = (
    '<thinking关键位段已同步为「上方 4243.65，下方 3367.15」，与 sectors.json 一致。\n'
    "保持原样。\nNow final message: 4 paragraphs, ~400-550 chars.\n"
    "Let me count roughly final text:\n上证 3476.47（-2.03%）。"
)
_o = A._strip_think(_LEAK_1125)
check("<thinking" not in _o.lower(),
      f"缺 `>` 的开标签没剥掉 —— 整段思维链会进群：{_o[:60]!r}")
check(_o.strip() == "",
      f"缺 `>` 的开标签应剥到结尾（后面全是推理），实得 {_o!r}")

# 开标签后接正文、且有换行
check(A._strip_think("<thinking\n推理第一行\n推理第二行").strip() == "",
      "缺 `>` 且带换行的开标签没剥干净")

# ---------- ② 孤儿闭合标签 ----------
_ORPHAN = ("**今日跌幅 TOP3**\n营销代理 -5.44%\n---\n"
           "Write the message now. Only the message body, nothing else."
           "</think>> **触发提醒 · 存储冲高回落**")
_o2 = A._strip_think(_ORPHAN)
check("</think>" not in _o2.lower(),
      f"孤儿闭合标签没剥掉（2026-11-19 真实形态）：{_o2[-60:]!r}")
check("**触发提醒 · 存储冲高回落**" in _o2,
      f"剥闭合标签时把**正文**一起删了：{_o2[-60:]!r}")
check("**今日跌幅 TOP3**" in _o2,
      f"剥闭合标签时把前面的正文一起删了：{_o2[:40]!r}")

# 双开双闭（模型吐两段推理）—— 第一对正常、第二对是孤儿
_DOUBLE = ("<think>第一段</think>尾巴\nWrite now.</think>> **标题**")
_o3 = A._strip_think(_DOUBLE)
check("</think>" not in _o3.lower() and "<think>" not in _o3.lower(),
      f"两段推理只剥掉一段：{_o3!r}")

# ---------- ③ 正常成对块仍剥干净 ----------
for _t, _want in (("<thinking>推理</thinking>正文在", "正文在"),
                  ("<think>推理</think>正文", "正文"),
                  ("<think\n推理\n</think>正文", "正文"),
                  ("<reasoning>推理</reasoning>正文", "正文"),
                  ("<analysis>推理</analysis>正文", "正文")):
    _g = A._strip_think(_t)
    check(_g == _want,
          f"正常成对块剥错了：{_t!r} -> {_g!r}（期望 {_want!r}）")

# ---------- ④ 正常小于号不得被当标签 ----------
for _t in ("半导体 <0.5% 的板块", "上证 ≤3500、<0.5%", "涨幅 <1% 的有 3 个"):
    check(A._strip_think(_t) == _t,
          f"正常小于号被当成推理标签删掉了：{_t!r} -> {A._strip_think(_t)!r}")

# ---------- ⑤ 真实推送回归 ----------
_pushes = os.path.join(_HERE, "..", "state", "history", "pushes.jsonl")
if os.path.exists(_pushes):
    _rows = [json.loads(l) for l in open(_pushes, encoding="utf-8")]
    _p1125 = [r for r in _rows if str(r.get("t", "")).startswith("2026-11-25 16:55")]
    check(bool(_p1125), "找不到 2026-11-25 16:55 那条推送（数据变了？）")
    _raw = str(_p1125[0].get("text") or "")
    _stripped = A._strip_think(_raw)
    check("<thinking" not in _stripped.lower(),
          f"11-25 真实推送剥完仍有推理：{_stripped[:60]!r}")
    check(len(_stripped.encode("utf-8")) < 120,
          f"11-25 那条剥完应只剩表头（推理全在标签后），"
          f"实得 {len(_stripped.encode('utf-8'))} 字节：{_stripped[:80]!r}")

    _p1119 = [r for r in _rows if str(r.get("t", "")).startswith("2026-11-19 14:30")]
    check(bool(_p1119), "找不到 2026-11-19 14:30 那条推送（数据变了？）")
    _raw2 = str(_p1119[0].get("text") or "")
    _st2 = A._strip_think(_raw2)
    check("<" not in _st2,
          f"11-19 真实推送剥完仍有 `<`："
          f"{[x for x in _st2.split(chr(10)) if '<' in x][:2]}")

# ---------- ⑥ 顺序守卫（静态）----------
_src = open(os.path.join(_SRC, "analyze.py"), encoding="utf-8").read()
check("_THINK_CLOSE_RE = re.compile(" in _src,
      "`_THINK_CLOSE_RE` 不存在 —— 孤儿闭合标签会原样进正文")
# `\s*>` 必须变成 `\s*>?`
_opens = re.findall(r'_\w*THINK\w*_RE = re\.compile\(\s*r"([^"]+)"', _src)
check(any(r"\s*>?" in _p for _p in _opens),
      f"没有哪条 think 正则允许缺 `>`：{_opens}")
# 闭合标签剥离必须排在最后
_i_pair = _src.find('t = _THINK_RE.sub(')
_i_open = _src.find('t = _THINK_OPEN_RE.sub(')
_i_close = _src.find('t = _THINK_CLOSE_RE.sub(')
check(0 < _i_pair < _i_open < _i_close,
      f"剥离顺序不对（应 成对块 -> 未闭合开标签 -> 残留闭合标签），"
      f"实际位置 pair={_i_pair} open={_i_open} close={_i_close}")

print(f"ALL PASS (推理块两种泄漏形态都要剥干净 {_ok} 个断言)")
