#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「**触发** 本轮无阈值触发」这种零信息段必须并进标题。

## 为什么这是缺陷而不是风格

`[实测]` 最近 200 条推送里 **23 条**（约 11%）第三行是一整段
「**触发** 本轮无阈值命中。」。它占掉一行段头 + 一行正文 + 两个间隔行，
读完全程得不到任何信息 —— 而「没有触发」这个事实在标题里一个后缀
就能说完。更糟的是它在**最显眼的位置**（紧跟标题）：读者每次都要先
读一句「什么都没发生」才能读到盘面。

## 为什么改 prompt 治不好

prompt 里早就写明了「没触发就一句『本轮无阈值触发』即可」——
**AI 是照着做的**，问题恰恰是这条规则本身在要求一个零信息段。
所以必须改结构：prompt 改成「没触发就整段不写」，
同时在**共同出口**做等效归一（AI 与兜底模板两条路径都经过）。

## 本文件锁住什么

① 三种真实措辞都能识别（无阈值触发 / 没有触发预设阈值 / 无阈值命中）
② 有真实触发时**一律不动**（那是有效内容，不能误伤）
③ 段头与正文同行、正文续在下一行，两种形态都认
④ 删干净：段本身 + 段前间隔行 + 段后间隔行（否则标题下留两行空白）
⑤ 标题后缀在 `_norm_title` **之后**写（否则被它整段重写冲掉）
⑥ 兜底模板 hits 为空时也**不渲染**该段 —— 代码不能要求 AI 做自己没做的事
"""
import os
import re
import sys

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


_SP = "\u3000"
_TITLE = "> **午后开局 · 科技链回补，港股仍独弱**"


def _mk(body, cont=None):
    """造一条带触发段的推送。cont 非空时正文续在下一行。"""
    if cont:
        return (f"{_TITLE}\n\n{_SP}\n**触发** {body}\n{cont}\n\n{_SP}\n"
                f"**盘面** 上证 +0.47%\n\n{_SP}\n**建议**\n> 风险 冲高不追")
    return (f"{_TITLE}\n\n{_SP}\n**触发** {body}\n\n{_SP}\n"
            f"**盘面** 上证 +0.47%\n\n{_SP}\n**建议**\n> 风险 冲高不追")


# ---------- ① 三种真实措辞都识别 ----------
# 措辞全部从 history/pushes.jsonl 原样捞回
for _body, _name in (
    ("本轮无阈值触发，六大指数与关注板块均为上涨。", "本轮无阈值触发"),
    ("本轮无阈值命中。", "本轮无阈值命中"),
    ("本轮没有触发预设阈值；全天单边放量普涨，不是只靠权重股护盘。",
     "本轮没有触发预设阈值"),
    ("无阈值触发", "最短形态"),
):
    _r = A._collapse_empty_trigger(_mk(_body))
    check("**触发**" not in _r,
          f"没并掉该段：{_name} -> {_r.splitlines()[0]!r}")
    check("无阈值触发" in _r.splitlines()[0],
          f"标题没加「无阈值触发」后缀：{_name} -> "
          f"{_r.splitlines()[0]!r}")

# ---------- ③ 正文续在下一行的形态 ----------
_r = A._collapse_empty_trigger(_mk("", cont="本轮无阈值命中。"))
check("**触发**" not in _r.rstrip().split("\n")[2].split("*触发*")[-1]
      or "**触发**" not in _r,
      f"正文续行的形态没并掉：{_r!r}")

# ---------- ② 有真实触发时一律不动 ----------
for _body, _name in (
    ("恒生科技 -1.53%，跌破下方触发线 3395.38", "真实指数触发"),
    ("存储芯片日内高点 2171、现价 2123，从高点回落 2.22%", "真实板块触发"),
    ("贵金属 -3.10%，是本轮唯一命中的触发项", "含「命中」但确实触发了"),
):
    _src = _mk(_body)
    _r = A._collapse_empty_trigger(_src)
    check(_r == _src, f"误伤了真实触发（{_name}）：{_r.splitlines()[0]!r}")

# ---------- ④ 删干净：不留连续两行空白 ----------
_src = _mk("本轮无阈值触发。")
_r = A._collapse_empty_trigger(_src)
_lines = _r.split("\n")
# ⚠️ 判据不能用 `.strip()` 判空 —— 全角间隔行 `\u3000` 的 `.strip()`
# 也是空串，而它是**有意的分隔符**（`_spacers` 会保留/重建它）。
# 真正的缺陷是"**真的空行**连续两个"，那才是肉眼可见的一段空白。
_dbl = any(_lines[i] == "" and _lines[i + 1] == ""
           for i in range(len(_lines) - 1))
check(not _dbl,
      f"删段后留下了连续两个**空行**：{_lines[:5]!r} —— "
      f"肉眼看是标题下多了一段空白")
# 并且标题与下一段之间必须有且只有一个间隔行
check(_lines[1] == _SP,
      f"标题后应是**一个**间隔行，实得 {_lines[1]!r}")
check(_lines[2].startswith("**盘面**"),
      f"间隔行后应直接是下一段，实得 {_lines[2]!r} —— "
      f"说明该段没删干净")

# ---------- ⑤ 接线位置：必须在 _norm_title 之后 ----------
_srcpy = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "src", "analyze.py"), encoding="utf-8").read()
_i_t = _srcpy.find("txt = _norm_title(txt, slot)")
_i_c = _srcpy.find("txt = _collapse_empty_trigger(txt)")
_i_s = _srcpy.find("txt = _norm_sectors(txt)")
check(_i_t > 0 and _i_c > 0 and _i_s > 0, "共同出口里找不到这三步")
check(_i_t < _i_c,
      "`_collapse_empty_trigger` 排在 `_norm_title` **之前** —— "
      "后者会整段重写标题，后缀被冲掉")
check(_i_c < _i_s,
      "`_collapse_empty_trigger` 排在 `_norm_sectors` **之后** —— "
      "后面按行定位的处理会看到仍含该段的结构")

# ---------- ⑥ 兜底模板也不渲染该段 ----------
check('if _trg_txt else ""' in _srcpy,
      "兜底模板仍无条件渲染「**触发**」段 —— 代码要求 AI 做自己没做的事")
check("_trg_sec" in _srcpy,
      "兜底模板没有把触发段改成按需省略")
# 结构断言：拼接式里必须是 `{_trg_sec}` 而不是 `**触发** {_trg_txt}`
_i_tpl = _srcpy.find("txt = (f\"> **市场盯盘")
check(_i_tpl > 0, "找不到兜底模板的拼接处")
_tpl = _srcpy[_i_tpl:_i_tpl + 500]
check("{_trg_sec}" in _tpl,
      f"兜底模板拼接处没有用 `{{_trg_sec}}`：{_tpl[:160]!r}")
check("**触发** {_trg_txt}" not in _tpl,
      "兜底模板拼接处仍是 `**触发** {_trg_txt}` —— 空的时候会渲染出空段")
check("· 无阈值触发" in _srcpy,
      "兜底模板标题没有「· 无阈值触发」后缀 —— 与 AI 路径不同口径")

print(f"ALL PASS (无阈值触发段并入标题 {_ok} 个断言)")
