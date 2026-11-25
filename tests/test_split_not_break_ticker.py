#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""超长段拆行把标的/点位劈成两半 —— 「沪深300」→「沪深30」+「0」。

## 实测（2026-11-25 13:12 真实推送）

`_split_wall_lines` 把**盘面**段按 88 字切开后，末行是：

    …恒生科技 -0.57%；沪深30
    0 -1.28%。

`沪深300` 被劈成 `沪深30` + `0`。读者会把它读成两个东西，而
`0 -1.28%。` 看起来像一个以 0 开头的数字。

## 成因：两条守卫共用一个 `_cut > 10` 的下限

原实现在硬切前有两道逐字回退：

    while _cut > 10 and 两侧都是数字:      _cut -= 1   # 不劈数字
    while _cut > 10 and 下一行首字符是标点: _cut -= 1   # 不给行首留 %

`_cut > 10` 是为了"别退过头把行切太碎"，但它同时意味着
**任何落在前 10 字内的硬切完全不受保护**。这个案例正好切在
`沪深30|0`（数字在右侧），两道守卫都因为 `<= 10` 的隐含位置逻辑失效。

同一天 10:04 那条还有另一个形态（已修过一次）：`+12.35` ⏎ `%，为各…`
—— 百分号被甩到行首。两者是**同一处代码同一条限制**造成的。

## 修法

把两道逐字回退收进 `_safe_split_cut(p, want)`，**不设下限**：

1. 第一轮只找**强边界**（切点前一个是标点/空格/`%`）—— 中文没有空格，
   否则逐字回退很容易停在词中间（`沪` + `深300`，与 `沪深30` + `0` 一样难读）；
2. 第二轮退到"任何不劈开数字/词"的位置：
   · 两侧都是数字（或右侧是小数点）→ 劈开了数
   · 数字后紧贴非分隔字符（`300亿`）→ 数值与量词被拆
   · 词内字符紧贴数字（`沪深300`、`科创50`）→ 名字被拆
3. 整段都放得下时返回全长（否则会丢字 —— 见下）；
4. **不往后找**：往后找会让这一行超过 `_LIMIT`，而"每行 ≤ 88 字"是
   既有硬不变量（`test_split_wall_hang` 按它做全形态扫描）。

另外两处配套修正：

· **剩余预算太小时先落地当前行**：当前行已 85 字、只剩 3 格时，
  原逻辑会往里塞一个「沪」，下一行从「深300」开始 —— 尾巴挂个孤字。
  判据：当前行已接近满行（差 ≤ 8）且剩余预算 < 8 格。
· `want >= len(p)` 时必须返回 `len(p)` 而不是 `want`：某段只剩 1 个字
  而 `_left` 有 86 时，返回 0 会让 `p[:0]` 取空、那个字被**丢弃**
  （`_flat()` 内容保真断言会红）。

## 本文件锁住什么

① 今天 13:12 的**真实盘面段**拆完不再劈开「沪深300」
② 10:04 的 `%` 行首形态仍被挡住（回归）
③ 拆完每行 ≤ `_LIMIT`、内容一个字不丢（`_flat` 比对）
④ 幂等
⑤ 反向：整段放得下时**原样返回**（不能为了保险硬拆短行）
⑥ `_safe_split_cut` 的三个判据各自有行为断言
⑦ 静态：不该再有 `_cut > 10` 这种"前 10 字不设防"的守卫
"""
import json
import os
import re
import sys
import tempfile

os.environ.setdefault("MW_DIR", tempfile.mkdtemp(prefix="mw_t_splitcut_"))

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "src")
sys.path.insert(0, _SRC)

import analyze as A

_ok = 0
_LIMIT = A._SPLIT_LIMIT


def check(cond, msg):
    global _ok
    if cond:
        _ok += 1
    else:
        print(f"  ❌ {msg}")
        sys.exit(1)


def _flat(s):
    return re.sub(r"\s+", "", s or "")


# ---------- ① 今天 13:12 的真实盘面段 ----------
_REAL = ("**盘面** 普跌且成长更重：上证 -1.35%（涨 923 跌 1357）、"
         "创业板指 -2.30%（涨 496 跌 867）、科创50 -3.32% 领跌，"
         "不是少数权重拖累。沪市成交 3725 亿，完成上日全天 54%，"
         "与时间进度基本同步，下跌未见恐慌放量。港股相对抗跌，"
         "恒生指数 -0.12%、恒生科技 -0.57%；沪深300 -1.28%。")
_out = A._split_wall_lines(_REAL)
_rows = _out.split("\n")
check("沪深30\n" not in _out and not any(
          r.strip() == "0 -1.28%。" for r in _rows),
      f"「沪深300」仍被劈开：{_rows!r}")
check(any("沪深300" in r for r in _rows),
      f"拆完找不到完整的「沪深300」：{_rows!r}")

# ---------- ② 10:04 的 `%` 行首形态（回归） ----------
_PCT = ("**关键位** 上证 3548.10（距 20 日低 +1.07%）、沪深300 3900.00、"
        "科创50 1186.14 距 20 日低 +12.35%，为各指数中最远；"
        "创业板指距 20 日高 -12.19%、恒生指数距 20 日低 +1.39%。")
_o2 = A._split_wall_lines(_PCT)
for _r in _o2.split("\n")[1:]:
    check(_r.strip()[:1] not in "%.％‰,，、。；;）)】」》",
          f"续行仍以标点开头（{_r.strip()[:1]!r}）：{_r!r}")

# ---------- ③ 每行 ≤ _LIMIT 且内容不丢 ----------
for _label, _txt in (("真实盘面", _REAL), ("百分号", _PCT)):
    _o = A._split_wall_lines(_txt)
    check(max(len(r) for r in _o.split("\n")) <= _LIMIT,
          f"[{_label}] 拆完仍有超宽行：{[len(r) for r in _o.split(chr(10))]}")
    check(_flat(_o) == _flat(_txt),
          f"[{_label}] 拆行丢了/多了字")

# ---------- ④ 幂等 ----------
check(A._split_wall_lines(_out) == _out, "拆行不幂等：第二遍又改了")

# ---------- ⑤ 反向：短行/整段放得下时原样返回 ----------
for _t in ("**盘面** 一句话。", "**触发** 本轮没有触发任何阈值。",
           "**资金** " + "甲" * 70):
    check(A._split_wall_lines(_t) == _t,
          f"整段放得下却被拆了（不能为了保险硬拆短行）：{_t[:20]!r}")

# ---------- ⑥ `_safe_split_cut` 三条判据 ----------
# 不劈数字：右侧是数字、左侧也是数字 → 必须往左退
_c = A._safe_split_cut("上证 3513.83 -0.97%", 12)
check(_c != 12, "切点落在数字中间（`3513.8|3`）没有被避开")
# 数字紧贴量词：`300亿` 不许拆
_c2 = A._safe_split_cut("成交 1234 亿，涨幅 300亿", 19)
check(not (_c2 > 0 and "成交 1234 亿，涨幅 300亿"[_c2 - 1] in "0123456789"
           and "成交 1234 亿，涨幅 300亿"[_c2] not in " ，、。；：！？）)】」』》%％‰·…—+-–/|>\""),
      "切点把数字和紧跟的量词劈开了")
# 词内字符紧贴数字：`沪深300` 不许拆
_c3 = A._safe_split_cut("恒生科技 -0.57% 沪深300 -1.28%", 17)
_t3 = "恒生科技 -0.57% 沪深300 -1.28%"
check(not (_t3[_c3 - 1] in "0123456789" and _t3[_c3] in "0123456789"),
      f"`沪深300` 被劈开（cut={_c3}）：{_t3[:_c3]!r} + {_t3[_c3:]!r}")
# ④ 词内字符紧贴数字：无强边界可退时，**续行不得以数字开头**
#    （`甲乙|300` 会让读者看到一行光秃秃的 `300…`，像是新的一句）
#    这一段没有任何标点/空格，强边界优先也找不到落点 —— 只有④能挡。
_B4 = "甲乙丙丁戊" * 16 + "甲乙" + "300" + "己庚辛壬癸" * 15
_c4 = A._safe_split_cut(_B4, 82)
check(_c4 != 82,
      f"`甲乙|300` 这种「词内字符紧贴数字」的切点没被避开（cut={_c4}）—— "
      f"续行会以 `{_B4[_c4:_c4 + 3]}` 开头，读者会当成新的一句")
check(not _B4[_c4].isdigit(),
      f"续行仍以数字开头：{_B4[_c4:_c4 + 6]!r}")

# 整段比 want 短 → 返回全长（不能返回 0 丢字）
check(A._safe_split_cut("尾", 86) == 1,
      "整段短于 want 时返回的不是全长 —— 调用方 `p[:0]` 会**丢掉这个字**")

# ---------- ⑦ 静态：不许再有"前 10 字不设防" ----------
_src = open(os.path.join(_SRC, "analyze.py"), encoding="utf-8").read()
check("_safe_split_cut(_p, _cut)" in _src,
      "硬切没走 `_safe_split_cut()`")
check(not re.search(r"while \(_cut > 10", _src),
      "仍有 `_cut > 10` 的守卫 —— 落在前 10 字内的硬切不受保护"
      "（「沪深30」+「0」就是这么来的）")

# ---------- ⑧ 真实推送回归：13:12 那段**还原成一行**后重拆 ----------
# ⚠️ 不能直接拿已发出的文本再拆一遍：它已经被拆成两行了，
# `_split_wall_lines` 只处理超长**单行**，不会把已断开的行接回去 ——
# 那样断言永远红（而且红的是历史数据，不是代码）。
# 正确做法：把那段的两行接回原文（去掉续行缩进），再走一次拆行，
# 断言「沪深300」完好。
_pushes = os.path.join(_HERE, "..", "state", "history", "pushes.jsonl")
if os.path.exists(_pushes):
    _hit = [r for r in (json.loads(l) for l in open(_pushes, encoding="utf-8"))
            if str(r.get("t", "")).startswith("2026-11-25 13:12")]
    check(bool(_hit), "找不到 2026-11-25 13:12 那条推送（数据变了？）")
    _txt = str(_hit[0].get("text") or "")
    _lines = _txt.split("\n")
    _p0 = next((i for i, l in enumerate(_lines) if l.startswith("**盘面**")), None)
    check(_p0 is not None, "那条推送里没有 **盘面** 段")
    _joined, _j = "", _p0
    while _j < len(_lines) and (_j == _p0 or _lines[_j].startswith("  ")):
        _joined += _lines[_j].lstrip() if _j > _p0 else _lines[_j]
        _j += 1
    check("沪深300" in _joined,
          f"接回来的原文里没有「沪深300」：{_joined[-40:]!r}")
    _o8 = A._split_wall_lines(_joined)
    check("沪深30\n" not in _o8,
          f"13:12 那段重拆后仍劈开「沪深300」：{_o8.split(chr(10))!r}")
    check(_flat(_o8) == _flat(_joined), "13:12 那段重拆后内容被改动")

print(f"ALL PASS (拆行不劈开标的/点位 {_ok} 个断言)")
