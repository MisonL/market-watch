#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主机判死后的冷却期必须**指数退避**，且恢复后计数归零。

## 为什么值得单测

固定 300s 的冷却期在**长期不可用**的主机上会变成纯噪音。本机实测：

    主机                半开探测次数   探测后真的恢复
    push2test(em_test)        30             0(*)
    push2                     32             8

(*) `em_test` 在 11-17 11:01 之后**再没顶上过**，但仍每 5 分钟探一次，
    `watch.log` 里整天是「半开探测：push2test …」。

危害不是"多打几行日志"，而是**它把真信号淹了**：11-17 那次数据盲区
（TTL 30 分钟 → 6 轮无数据）之所以难查，正因为日志里混着大量同形的空探行。

## 本文件锁住什么

- 第 1 次判死仍是 300s（瞬时抖动要能快速恢复 —— 本机 push2 恢复率 25%）
- 冷却期按次数翻倍，且**封顶 1 小时**（不会无限增长到要人工干预）
- 恢复后计数**归零**（否则下次再挂会直接从 1 小时起步，白错过快速恢复）
- `#n` 计数键不会被"按时间清理过期条目"误删
- 生产状态文件 `dead_hosts.json` 的既有读者看到的仍是 `{host: ts}` 形状
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

import marketlib as M

_ok = 0


def check(cond, msg):
    global _ok
    if cond:
        _ok += 1
    else:
        print(f"  ❌ {msg}")
        sys.exit(1)


# ⚠️ 必须把状态重定向到临时文件 —— 否则会写**生产** dead_hosts.json，
# 并往 logs/watch.log 留假日志（_mark_dead 的 docstring 记着这个坑）。
_TD = tempfile.mkdtemp(prefix="mw_backoff_")
_ORIG_FILE = M.DEAD_HOST_FILE
_ORIG_LOG = M.log
M.DEAD_HOST_FILE = os.path.join(_TD, "dead_hosts.json")
M.log = lambda *a, **k: None
_H = "https://backoff-test.invalid"

try:
    # ---------- 冷却期逐次翻倍 ----------
    _seen = []
    for _i in range(1, 7):
        # ⚠️ 每次 `_round_begin()` 代表"新的一轮取数"（launchd 每轮起新进程）。
        # `#n` 的语义是**跨轮**连续失败，不是"调用次数" —— 见 `_mark_dead`
        # 的 P1 注释：一轮内 12 个板块逐个失败只该算 1 次。
        M._round_begin()
        M._mark_dead(_H)
        _d = json.load(open(M.DEAD_HOST_FILE, encoding="utf-8"))
        _n = _d.get(_H + "#n", 0)
        check(_n == _i, f"第 {_i} 次判死，计数应为 {_i}，实得 {_n}")
        # 复算一遍冷却期（与 _host_dead 内的公式同源）
        _cool = min(M._COOL_CAP, M._COOL_BASE * (2 ** (min(int(_n), 8) - 1)))
        _seen.append((_i, _cool))

    # 第 1 次必须还是 300s —— 瞬时抖动不能被退避惩罚
    check(_seen[0][1] == M.DEAD_TTL,
          f"首次判死的冷却期必须等于 DEAD_TTL={M.DEAD_TTL}，实得 {_seen[0][1]}")
    check(_seen[0][1] == 300, f"首次冷却应为 300s，实得 {_seen[0][1]}")

    # 逐次递增
    for _k in range(1, len(_seen)):
        check(_seen[_k][1] >= _seen[_k - 1][1],
              f"冷却期必须单调不减：{_seen}")

    # 封顶 1 小时（不能无限增长）
    check(_seen[-1][1] == M._COOL_CAP,
          f"连续失败 6 次后应封顶 {M._COOL_CAP}s，实得 {_seen[-1][1]}")
    check(M._COOL_CAP == 3600, f"封顶应是 1 小时，实得 {M._COOL_CAP}")

    # ---------- 恢复后计数归零 ----------
    M._clear_dead(_H)
    _d = json.load(open(M.DEAD_HOST_FILE, encoding="utf-8"))
    check(_H not in _d, "恢复后时间戳必须被清除")
    check(_H + "#n" not in _d,
          "恢复后**计数也必须清除** —— 否则下次再挂会直接从长冷却起步")

    M._round_begin()
    M._mark_dead(_H)
    _d = json.load(open(M.DEAD_HOST_FILE, encoding="utf-8"))
    check(_d.get(_H + "#n") == 1,
          f"恢复后再失败应重新从 1 计数，实得 {_d.get(_H + '#n')}")

    # ---------- `#n` 计数键不能被"按时间清理"误删 ----------
    # 造一个"时间戳很旧但计数健在"的状态，再触发一次 _mark_dead 的清理分支。
    _old = {"https://stale.invalid": 1.0,          # 远古时间戳 → 该清
            "https://stale.invalid#n": 5,          # 计数键 → **不该**被当时间判
            }
    with open(M.DEAD_HOST_FILE, "w", encoding="utf-8") as _f:
        json.dump(_old, _f)
    M._mark_dead("https://other.invalid")
    _d = json.load(open(M.DEAD_HOST_FILE, encoding="utf-8"))
    check("https://stale.invalid" not in _d,
          "远古时间戳的条目应被清理")
    check("https://other.invalid" in _d, "新判死的主机应写入")
    # ⚠️⚠️ 2026-11-19（P0）：**孤儿计数键必须一起清**。
    # 原实现只删 `host`，留下 `host#n: 8`。后果不是文件大一点 ——
    # 这台主机下次再抖一次，计数会从 8 加到 9，一上来就是 60 分钟冷却，
    # 而不是设计意图的 5 分钟。也就是说**历史失败永久拖累将来恢复**。
    # `[实测]` 沙箱：造 {old: 2小时前, old#n: 8} → 触发清理后 old#n 仍在。
    check("https://stale.invalid#n" not in _d,
          "时间戳过期被清理时，**必须连 #n 计数一起删** —— "
          "留下孤儿键会让该主机下次失败直接从最高档冷却起步")

    # ---------- _host_dead 认得出退避后的冷却期 ----------
    # 直接验：把时间戳设成"刚过 300s 但未过 600s"，计数=2 → 应仍判死。
    _now = __import__("time").time()
    with open(M.DEAD_HOST_FILE, "w", encoding="utf-8") as _f:
        json.dump({_H: _now - 350, _H + "#n": 2}, _f)
    M._HALF_OPEN_TRIED.clear()
    check(M._host_dead(_H) is True,
          "计数=2 时冷却应为 600s；过了 350s 仍应判死（证明退避真的生效）")

    # 计数=1（冷却 300s）时，同样 350s 应判活 —— 对比证明差别来自退避
    with open(M.DEAD_HOST_FILE, "w", encoding="utf-8") as _f:
        json.dump({_H: _now - 350, _H + "#n": 1}, _f)
    M._HALF_OPEN_TRIED.clear()
    check(M._host_dead(_H) is False,
          "计数=1 时冷却 300s；过了 350s 应放行探测（与上一条对照）")

    # ---------- 同一轮内重复失败**不得**累加计数 ----------
    #
    # ⚠️⚠️ 2026-11-19（P1）：`#n` 语义是「跨**轮**连续失败」，但原实现
    # 每调一次 `_mark_dead()` 就 +1。而一轮 `quotes()` 里同一台主机会被
    # 判死多次（9 个指数 + 12 个板块逐个取，每个失败都算一次）。
    # `[实测]` 沙箱：一轮 quotes() 全失败 → `_mark_dead` 共 9 次，
    # push2(https) 计 3 次 → n 从 0 直接跳到 3（冷却 1200s，跳过前两档）。
    # 两轮就封顶 3600s —— 这正是生产 13:06 清零后 13:20 又爬回 n=8 的原因。
    M._round_begin()
    with open(M.DEAD_HOST_FILE, "w", encoding="utf-8") as _f:
        json.dump({}, _f)
    for _i in range(5):
        M._mark_dead(_H)                    # 同一轮内连失 5 次
    _d = json.load(open(M.DEAD_HOST_FILE, encoding="utf-8"))
    check(_d.get(_H + "#n") == 1,
          f"同一轮内连失 5 次只应计 1 次，实得 {_d.get(_H + '#n')}")
    # 跨轮则必须正常累加
    M._round_begin()
    M._mark_dead(_H)
    _d = json.load(open(M.DEAD_HOST_FILE, encoding="utf-8"))
    check(_d.get(_H + "#n") == 2,
          f"进入新一轮后再失应累加到 2，实得 {_d.get(_H + '#n')}")

    # ---------- 排序函数**不得消费**半开探测令牌 ----------
    #
    # ⚠️⚠️ 2026-11-19（P0 生产事故）：`_host_dead()` 冷却期过后会消费掉
    # 本轮唯一的探测令牌（`_HALF_OPEN_TRIED.add`）表示"放行一次"。
    # 而 `_ordered_em_hosts()` 每轮要对 5 台主机各调它一次 —— 令牌在
    # **排序阶段就被吃光**，等 `cj()` 真正要发包时判定已变成
    # "本轮探过了，继续判死"，直连被跳过、直接砸兜底。
    #
    # `[实测]` 沙箱：同一份状态（n=7、冷却已过）
    #     不先排序 → _host_dead() = False（放行直连）
    #     先排序后 → _host_dead() = True （直连被跳过）
    # 生产后果：2026-11-19 10:00 板块 0/12 全灭、「数据本轮不可用」，
    # 而判死计数只增不减（7→8→…）—— 因为直连永远没机会成功，
    # `_clear_dead()` 永远不被调用，退避一路爬到 60 分钟。
    # ⚠️ 必须用**真实在 EM_HOSTS 里**的主机 —— 否则 `_ordered_em_hosts()`
    # 压根不会去看它，这个断言就成了空转（`[实测]` 第一版用的是
    # `backoff-test.invalid`，注入缺陷后测试照样全绿，正是这个原因）。
    _RH = M._host_key(M.EM_TEST)
    _now3 = __import__("time").time()
    with open(M.DEAD_HOST_FILE, "w", encoding="utf-8") as _f:
        json.dump({_RH: _now3 - 3601, _RH + "#n": 7}, _f)
    M._HALF_OPEN_TRIED.clear()
    M._ordered_em_hosts()                    # 排序：只读，不该动令牌
    check(_RH in [M._host_key(h) for h in M.EM_HOSTS],
          "测试主机必须在 EM_HOSTS 里，否则本项是空转")
    check(_RH not in M._HALF_OPEN_TRIED,
          "排序函数**不得**消费半开探测令牌 —— "
          f"否则直连永远轮不到发包，实得 {M._HALF_OPEN_TRIED}")
    check(M._host_dead(_RH) is False,
          "排序之后 _host_dead() 仍须**放行探测**（False）—— "
          "排序函数不得消费探测令牌，否则直连永远轮不到")
    # 反面：探测令牌用掉后，同一轮再判应返回 True（不重复发包）
    check(M._host_dead(_RH) is True,
          "探测令牌用掉后，同一轮再判应返回 True（继续判死，不重复发包）")

    # ---------- 形状兼容：既有读者只看 {host: ts} ----------
    _d = json.load(open(M.DEAD_HOST_FILE, encoding="utf-8"))
    _non_n = {k: v for k, v in _d.items() if not k.endswith("#n")}
    check(all(M.isnum(v) for v in _non_n.values()),
          f"非 `#n` 键的值必须都是数值时间戳，实得 {_non_n}")

finally:
    M.DEAD_HOST_FILE = _ORIG_FILE
    M.log = _ORIG_LOG
    import shutil
    shutil.rmtree(_TD, ignore_errors=True)

print(f"ALL PASS (主机冷却指数退避 {_ok} 个断言)")