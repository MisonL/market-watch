#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`count 与盘上 rate.json 对账` 必须**分方向**判定，不能要求严格相等。

## 2026-11-24 实测的假 ❌

自检报：

    ❌ count 与盘上 rate.json 分叉：内存=224 盘上=225（_roll 的跨进程同步已坏）

差 1，却报「跨进程同步已坏」。这是**假红**：自检要跑 10~20 分钟，
这期间 launchd 的 collect/analyze 还在 300s 一轮地跑。`stats()` 读到
count=224 之后、对账处再读盘上时，另一进程完全可能已经 `_reserve()`
了一次 —— 竞态合法，不是缺陷。

## 两个方向含义完全不同

· **盘上 > 内存** —— 两次读之间别的进程加了配额。完全正常。
  只留一行 ℹ️ 便于排查，**永不置红**。
· **盘上 < 内存** —— 盘上的计数**倒退**了。这才是 `_roll` 跨进程同步
  真的坏掉（别人的占额看不见 -> 会超发 -> 本仓库有过被目标站封 IP 的
  历史）。**必须红**。

原判据 `_dc_rl != _cnt_rl` 把这两个方向混成一个，于是正常并发也报红。
后果不是"多一条日志"：一条会随机变红的判据，运行久了就会被人当噪声
忽略 —— 而它本该盯的正是"计数倒退"那个真故障。

## 本文件锁住什么

① 盘上 > 内存 → 放行（且留痕）
② 盘上 < 内存 → 红，且措辞点明「倒退」
③ 相等 → 放行
④ 变异体 `quota-warn-written-outside-lock` 仍然抓得住
   （它造成的正是"盘上被旧快照覆盖"= 盘上倒退）
"""
import os
import re
import sys

_OK = 0


def check(cond, msg):
    global _OK
    if cond:
        _OK += 1
    else:
        print(f"  ❌ {msg}")
        sys.exit(1)


_HERE = os.path.dirname(os.path.abspath(__file__))
_SC = open(os.path.join(_HERE, "..", "src", "selfcheck.py"),
           encoding="utf-8").read()

# ---------- ① 判据必须是**方向性**的，不能是 != ----------
# ⚠️ 取窗口要用**整段对账代码**的边界，不能从 "分叉" 那个字符串往前截。
# 第一版写 `_SC[i-1400:i]` —— 而 "分叉" 出现在 `<`（倒退）分支的措辞里，
# 往**前**截恰好把 `> `（良性）分支漏在外面（它在 `elif` 里、位于其后），
# 于是断言恒红。窗口边界本身就是判据的一部分。
_i0 = _SC.find("_disk_rl = M._load_json(M.RL.RATE_FILE")
check(_i0 > 0, "自检里找不到 rate.json 对账段 —— 本文件的前提失效了")
_i1 = _SC.find("except Exception as _rl_e:", _i0)
check(_i1 > _i0, "找不到对账段的结尾 —— 判据失效")
_win = _SC[_i0:_i1]

check("_dc_rl < _cnt_rl" in _win,
      "对账判据没有区分方向（缺 `_dc_rl < _cnt_rl`）—— "
      "正常并发会把自检打红（2026-11-24 实测：内存=224 盘上=225）")
check("_dc_rl != _cnt_rl" not in _win,
      "对账判据仍在用 `!=` 严格相等 —— 并发增量会被误判成同步坏掉")
check("_dc_rl > _cnt_rl" in _win,
      "没有为「盘上 > 内存」单独留痕 —— 良性的那个方向应当留一行 ℹ️ "
      "（不留痕就分不清『没发生』和『没检查』）")
check("倒退" in _win,
      "红的时候没说清是**倒退** —— 报错只说「分叉」，"
      "排查者分不出是并发增量还是真丢计数")

# ---------- ②③ 行为：三个方向各自判定 ----------
# 复刻判据本体（与源码同构，改动源码后这里也必须跟着改 —— 若两者漂移，
# 上面那组静态断言会先红，不会出现"测试绿着但测的不是生产代码"）。
def _verdict(mem, disk):
    bad = []
    if isinstance(disk, int) and disk < mem:
        bad.append(f"倒退 {mem}->{disk}")
    elif isinstance(disk, int) and disk > mem:
        pass                                    # 良性并发增量
    return bad


check(_verdict(224, 225) == [], "盘上 > 内存 被误判成缺陷（正常并发增量）")
check(_verdict(100, 100) == [], "相等被误判成缺陷")
check(_verdict(224, 300) == [], "盘上大幅超前被误判成缺陷")
check(_verdict(225, 224) != [], "盘上 < 内存（计数倒退）居然放行了 —— "
                                "那正是 _roll 真的坏掉时的形态")

# ---------- ④ 变异体仍然抓得住 ----------
_MUT = open(os.path.join(_HERE, "..", "src", "selfcheck_mutation.py"),
            encoding="utf-8").read()
check('"与盘上 rate.json 分叉"' in _MUT,
      "变异体 `quota-warn-written-outside-lock` 的期望串丢了 —— "
      "它依赖这条判据抓「盘上被旧快照覆盖」")
# 该变异体造成的正是盘上**倒退**（旧快照覆盖新计数），方向判据必须认它
check(_verdict(5, 4) != [],
      "变异体本身的形态（内存=5 盘上=4）在新判据下放行了 —— "
      "真故障被漏掉，比假红更糟")

print(f"ALL PASS (rate.json 对账方向判定 {_OK} 个断言)")
