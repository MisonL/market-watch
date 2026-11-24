#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「由资金派发而非资金买入推动」是事实，不是买入指令。

## 实测

全量扫 476 条历史推送，逐行过 `_advice_hit`，`买入` 只命中 **1 条**：

    2026-11-03 10:06
    **资金** 开盘半小时主力整体净流出：…；印制电路板自 09:29 回升
    +2.11pp 但资金仍 -11.15 亿，反弹由资金派发而**非资金买入**推动。

这句明确在说"**不是**买入推动"，却被判成「买入」指令 ——
一旦落在 09:25 / 10:00 槽位，`_advice_lines` 会把整条删掉，
**把一句否定事实换成"仅报告市场事实"的模板**。

## 成因

逐子句判据：

    if any(k in _seg for k in _ADVICE_TONE):   # "反弹" 是语气词
        return _w                              # "买入" 被当指令

「反弹」在语气词表里，「买入」前面是「资金派发而非资金」，
而"资金"不在 `_ADVICE_SUBJECT`（里面只有"主力/北向/ETF"等），
所以主体豁免也没兜住。

## 修法：否定词**紧邻**动作词才豁免

    _pre = _seg[:_seg.find(_w)]
    if re.search(r"(?:而非|不是|并非|非)[^，。；！？]{0,6}$", _pre):
        continue

⚠️ 不能用「子句里出现一个「非」就整条跳过」——
`非但不能追高反而应该减仓` 里的 `减仓` 是**真指令**，那样会漏。
判据必须落在否定词与动作词之间的**距离**上。

## 本文件锁住什么

① 三条真实否定事实判非指令（`而非买入`/`不是买入`/`并非减仓`）
② **反向**：`非但不能追高反而应该减仓` / `非但不宜追高反而应该减仓`
   仍必须命中（钉住"紧邻"而不是"整句")
③ 原误伤行的**事实分句**必须逐字保留（`_advice_trim_line` 行为级）
④ 真指令行（`反弹无量宜减仓` / `逢反弹分批减仓` / `反弹可分批减仓`）
   仍必须命中（不能因为加了否定豁免就把语气词族一起关掉）
⑤ 全量扫历史推送：`买入` 命中数必须是 0
"""
import json
import os
import re
import sys
import tempfile

os.environ.setdefault("MW_DIR", tempfile.mkdtemp(prefix="mw_t_negation_"))

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

import analyze as A

_ok = 0


def check(cond, msg):
    global _ok
    if cond:
        _ok += 1
    else:
        print(f"  ❌ {msg}")
        sys.exit(1)


# ---------- ① 否定事实 ----------
_NEG = (
    "反弹由资金派发而非资金买入推动。",
    "反弹不是买入机会",
    "低位并非减仓时机",
)
for _t in _NEG:
    _h = A._advice_hit(_t)
    check(_h is None,
          f"否定事实被当指令（命中 {_h!r}）：{_t}")

# 原误伤行全文（2026-11-03 10:06 的真实推送）
_ORIG = ("**资金** 开盘半小时主力整体净流出：上证 -60.93 亿（占 -2.7%）、"
         "沪深300 -39.98 亿（占 -2.8%）；创业板指仅 -20.59 亿（占 -1.5%），"
         "跌幅与流出都最轻。印制电路板自 09:29 回升 +2.11pp 但资金仍 -11.15 亿，"
         "反弹由资金派发而非资金买入推动。")
check(A._advice_hit(_ORIG) is None,
      f"原误伤行仍被判成指令（命中 {A._advice_hit(_ORIG)!r}）")

# ---------- ② 反向：同子句有"非"但动作是真指令 ----------
for _t, _w in (("非但不能追高反而应该减仓", "追高"),
               ("非但不宜追高反而应该减仓", "不宜追"),
               ("不但不该加仓反而应该减仓", "减仓")):
    _h = A._advice_hit(_t)
    check(_h is not None,
          f"`非但…反而应该减仓` 里的真指令没被拦下：{_t}（命中 {_h!r}）")

# ---------- ③ 行为级：事实分句逐字保留 ----------
_o = A._advice_trim_line(_ORIG)
check(_o is not None, f"_advice_trim_line 对全事实行返回 None：{_ORIG}")
check(_o == _ORIG,
      f"全事实行被改动了 —— 应该逐字保留：\n  IN : {_ORIG}\n  OUT: {_o}")
check("而非资金买入" in (_o or ""),
      f"「而非资金买入」这个否定事实被删掉了：{_o}")

# 真指令行仍必须被删
_t2 = "> 风险 反弹无量宜减仓"
_o2 = A._advice_trim_line(_t2)
check(_o2 is not None and "减仓" not in _o2,
      f"真指令行没被处理：{_o2!r}")

# ---------- ④ 语气词族不受影响 ----------
for _t in ("反弹无量宜减仓", "逢反弹分批减仓", "反弹可分批减仓"):
    check(A._advice_hit(_t) is not None,
          f"加了否定豁免后，语气词族失效：{_t!r}")

# ---------- ④b 形态正则本身不许因“加否定守卫”而废掉 ----------
# ⚠️ 第一版写成了 `[ ^，。；！？\n]`（`^` 前多了个空格），字符类里
# `^` 不在首位就不取反 —— 变成"匹配任意字符（含标点）"。
# 结果是「开盘后减仓」这类**真指令**也会漏，判据被自己加的守卫拆掉。
# 必须钉住：过渡段的标点要能阻断匹配。
for _t in ("回踩后加仓", "反弹时减仓", "明日开盘减一半",
           "逢低加仓", "高位减仓一半", "开盘后减仓"):
    check(A._advice_hit(_t) is not None,
          f"形态正则被否定守卫误伤，真指令漏网：{_t!r}")
_pat = A._ADVICE_SHAPE_RE.pattern
check("[ ^" not in _pat and "[^，。；！？" in _pat,
      f"过渡段字符类写错了（`^` 前多了空格 → 不取反）：{_pat!r}")

# ---------- ⑤ 全量历史推送：`买入` 命中 0 ----------
_pushes = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "state", "history", "pushes.jsonl")
if os.path.exists(_pushes):
    _n_buy = 0
    with open(_pushes, encoding="utf-8") as _f:
        for _ln in _f:
            try:
                _txt = json.loads(_ln).get("text") or ""
            except Exception:
                continue
            for _line in str(_txt).split("\n"):
                if A._advice_hit(_line.strip() or "") == "买入":
                    _n_buy += 1
    check(_n_buy == 0,
          f"历史推送里仍有 {_n_buy} 条被判成「买入」指令 —— "
          f"全量扫描应该归零（原误伤就是那条 '')")

print(f"ALL PASS (否定事实不是买入指令 {_ok} 个断言)")
