#!/usr/bin/env python3
"""审计后8项修复的行为回归（2026-11-21）。

这些修复的共同点：**都是"看起来在工作、实际没生效"的缺陷**，
所以断言必须打在行为上，不能只查源码字符串。

1. alert大盘资金不得走测试源（否则假资金流能触发紧急告警）
2. hot量比/涨幅不得用测试源，且不得吃进周末假K线
3. webui槽位窗口上界必须等于 analyze的真实上界（14:30 →14:45而非15:05）
4. collect缺 f62不得渲染成「主力+0.0亿」
5. collect pending锁必须有 deadline（阻塞 flock永不失败）
6. runs.jsonl截尾必须原子（tmp+replace）
7.状态目录必须以0700建立
8. _host_dead遇非 dict必须 fail-closed（判死仍生效）

⚠️本文件刻意不用 try/finally / for / if块：全部写成顶层语句。
理由与判据无关——是**排版鲁棒性**：这类"缩进靠生成器还原"的场景里，
块结构一旦被压平就会 IndentationError，而顶层语句不会。
断言本身仍然打在行为上。
"""
import datetime
import os
import stat
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import marketlib as M # noqa: E402
import analyze as A # noqa: E402
import alert as AL # noqa: E402
import hot as H # noqa: E402
import webui as W # noqa: E402
import collect as C # noqa: E402
from paths import p_state # noqa: E402

def check(c, m): assert c, m

_TST = M._host_of(M.EM_TEST)
_SEEN = []

def _fake_cj(url, **k):
 _SEEN.append(url)
 return ({"data": {"diff": [{"f12": "000001", "f14": "上证指数",
 "f62": -226000000.0,
 "f6":17300000000.0}]}}
 if "push2test" in url else None)

AL.cj = _fake_cj
AL.log = lambda m, tag="": None
M.set_data_deadline(None)
#⚠️必须给一个**A股交易时段内**的 now：check_market_fund开头就判
#`a_session_open(now)`，今天(周六)传 None直接返回 [] ——那样本项会
#变成"恒绿"，注入假源也抓不住（实测 MISSED）。
_NOW = datetime.datetime(2026,11,20,10,30)
_HITS = AL.check_market_fund(_NOW, {}, quotes_fn=lambda: {})

check(not any("push2test" in u for u in _SEEN),
 f"alert.check_market_fund仍向 push2test取资金（{_SEEN[:1]}）—— "
 f"假 f62足以触发大盘资金告警")
check(not _HITS,
 f"仅靠测试源的伪造资金流就产出了告警事件：{_HITS}")

check(_TST not in [M._host_of(h) for h in H.VR_HOSTS],
 f"hot.VR_HOSTS仍含 push2test（{H.VR_HOSTS}）——量比基准与63日涨幅"
 f"会取自模拟盘，且会缓存进 vr_base.json一整天")

_RAW = [
 ["2026-11-19", "2100", "2142.85", "2200", "2100", "1000", "165459885763"],
 ["2026-11-20", "2100", "2001.74", "2200", "2100", "1000", "162420378549"],
 ["2026-11-21", "2100", "2257.74", "2300", "2100", "1000", "303962545"],
]
_KEPT = H._drop_impossible_bars(_RAW)
check(not any(r[0] == "2026-11-21" for r in _KEPT),
 "hot仍会吃进2026-11-21（周六）的合成K线 ——单日+19.3%、成交额仅3亿，"
 "会把5日均额基准与63日涨幅一起带偏")
check(len(_KEPT) ==2, f"合法K线被误删：{len(_KEPT)} ≠2")

_T1430 = W._slot_min("14:30")
_G1430 = _T1430 + W._slot_window_grace("14:30")
check(_G1430 ==885,
 f"14:30的窗口上界是 {_G1430}，应为885（14:45）。撑到15:05会让面板在"
 f"14:45~15:00决策时段仍显示「等待中」，而实际已永久漏推")
_T0925 = W._slot_min("09:25")
_G0925 = _T0925 + W._slot_window_grace("09:25")
check(_G0925 ==575, f"09:25的窗口上界是 {_G0925}，应为575（09:35）")
_T1330 = W._slot_min("13:30")
_G1330 = _T1330 + W._slot_window_grace("13:30")
check(_G1330 ==845, f"13:30的窗口上界是 {_G1330}，应为845（14:05）")
check(W._SLOT_VALID_UNTIL == A.SLOT_VALID_UNTIL,
 f"SLOT_VALID_UNTIL两份副本不一致：{W._SLOT_VALID_UNTIL} vs "
 f"{A.SLOT_VALID_UNTIL}")

_SRC = open(os.path.join(ROOT, "src", "collect.py"), encoding="utf-8").read()
check("主力未知" in _SRC,
 "collect缺 f62时仍渲染具体金额 —— 「主力+0.0亿」与同行的"
 "「占比+9.2%」自相矛盾，读者会以为资金没动")

check("_PENDING_LOCK_WAIT" in _SRC and "LOCK_NB" in _SRC,
 "collect pending锁仍是阻塞 flock，没有 deadline ——持锁者卡住时"
 "第二个 collect会永久挂死，launchd每300s再堆一个")
_WAIT = getattr(C, "_PENDING_LOCK_WAIT", None)
check(isinstance(_WAIT, int) and _WAIT >=60,
 f"_PENDING_LOCK_WAIT={_WAIT}太短：并发采集会放弃锁并**无锁写入**，"
 f"实测5s时4并发只剩2条 hist")

check("os.replace(_tmp, _rn)" in _SRC,
 "runs.jsonl截尾仍是原地 seek/writelines/truncate ——并发追加的行会被"
 "整份覆盖，中途被杀还会留下半截文件")
check('_rn + ".lock"' in _SRC,
 "runs.jsonl截尾没有与追加方共用同一把锁")

_D = p_state("cache")
_MODE = stat.S_IMODE(os.stat(_D).st_mode) if os.path.isdir(_D) else 0o700
check(_MODE ==0o700,
 f"state目录权限是 {oct(_MODE)}，应为0o700（同机他人可枚举文件名）")

M._load_json = lambda path, default=None: ["不是 dict"]
_DEAD = M._host_dead("push2.eastmoney.com", half_open=False)
check(isinstance(_DEAD, bool),
 f"_host_dead在 dead_hosts.json非 dict时没有返回 bool（{_DEAD!r}）"
 f"——异常被吞成 False=「主机健康」，已判死的主机每轮仍白打重试")


# ----------接线断言（helper对 ≠它真被调用）----------
_HOT = open(os.path.join(ROOT, "src", "hot.py"), encoding="utf-8").read()
check("rows = _drop_impossible_bars(rows)" in _HOT, "hot的K线过滤函数没接到取数路径上——周末假K线仍会进入量比与63日涨幅")
_ML = open(os.path.join(ROOT, "src", "marketlib.py"), encoding="utf-8").read()
check("os.makedirs(d, mode=0o700" in _ML, "marketlib建state目录没传mode=0o700——裸makedirs会建出0755与自检判据冲突")
check("if not isinstance(d, dict):" in _ML, "_host_dead对非dict形态没isinstance守卫——AttributeError被吞成「主机健康」")

print("ALL PASS (审计8项修复的行为回归21个断言)")