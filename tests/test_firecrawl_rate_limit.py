#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""firecrawl **限流** 不得按故障套 900s 冷却。

## 实测事实

firecrawl 有每分钟请求上限。超限时 CLI 的形态是：

    rc=1, stdout 为空,
    stderr="Error: Rate limit exceeded. Consumed (req/min): 12,
            Remaining (req/min): 0. Upgrade yo..."

而 `firecrawl_get` 原先只看 `stdout 为空`，一律当失败 -> 套
`FIRECRAWL_COOLDOWN = 900`（15 分钟）。

## 为什么这会造成**静默截断**（不是"慢一点"）

`[实测]` 一次全量 clist 要 6 页 = 6 次 firecrawl 调用；行业+概念两轮
共 12 次，**刚好压在每分钟上限上**。概念板块 554 个，第 5 页撞上限后：

    剩余页在 900s 冷却期内**立即失败**（0.0s）
    -> clist 拿到 400/554 条，直接当成功返回
    -> 按 fid=f62 降序时，缺的正是**流出最猛**的那批板块
    -> 「外资/融资盘整体撤离」这类最该报的紧急项永远看不见

限流窗口是分钟级，恢复很快；用 15 分钟冷却属于惩罚过度。

## 本文件锁住什么

- 限流的 stderr 必须被识别，冷却时长走短档（60s），不是 900s
- 其余失败（真故障）仍走 900s —— 不能为了限流把真故障也放宽
- 两种情况都必须留日志，且文案要能区分
"""
import os
import subprocess
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


_TD = tempfile.mkdtemp(prefix="mw_fcrate_")
_orig = (M._fc_fail_get, M._fc_fail_set, M.log,
         M._FIRECRAWL_NODE, M._FIRECRAWL_CLI, subprocess.run)
_logs = []


class _R:
    def __init__(self, out="", err="", rc=0):
        self.stdout, self.stderr, self.returncode = out, err, rc


def _mk_run(out, err, rc):
    def _run(*a, **k):
        return _R(out, err, rc)
    return _run


try:
    M.log = lambda msg, kind=None: _logs.append(str(msg))
    # ⚠️ 必须用**真实存在**的文件路径 —— `firecrawl_get` 开头就判
    # `os.path.exists(node) and os.path.exists(cli)`，不存在则**直接返回 None**
    # 且不设冷却。踩过：macOS 上 `/bin/true` 不存在（在 `/usr/bin/true`），
    # 于是三条断言全部读到 until=0，看起来像"限流没分档"，其实是桩没生效。
    # 用 `sys.executable` 最可移植（Python 自己就是可执行文件）。
    M._FIRECRAWL_NODE = sys.executable
    M._FIRECRAWL_CLI = sys.executable
    # 冷却用内存桩，避免写生产 firecrawl_fail.json
    _state = {"until": 0.0}
    M._fc_fail_get = lambda: _state["until"]

    def _set(u):
        _state["until"] = u
    M._fc_fail_set = _set

    # ---------- ①限流 -> 短冷却 ----------
    _state["until"] = 0.0
    _logs.clear()
    subprocess.run = _mk_run(
        "", "Error: Rate limit exceeded. Consumed (req/min): 12, "
            "Remaining (req/min): 0. Upgrade your plan.", 1)
    _t0 = __import__("time").time()
    out = M.firecrawl_get("https://x.invalid/a")
    check(out is None, f"限流时应返回 None，实得 {out!r}")
    _cool = _state["until"] - _t0
    check(_cool <= M.FIRECRAWL_RATE_LIMIT_COOLDOWN + 2,
          f"限流却套了 {_cool:.0f}s 冷却"
          f"（应 ≈{M.FIRECRAWL_RATE_LIMIT_COOLDOWN}s）")
    check(_cool >= M.FIRECRAWL_RATE_LIMIT_COOLDOWN - 2,
          f"限流冷却过短：{_cool:.0f}s（应 ≈{M.FIRECRAWL_RATE_LIMIT_COOLDOWN}s）")
    check(M.FIRECRAWL_RATE_LIMIT_COOLDOWN < M.FIRECRAWL_COOLDOWN,
          "限流冷却必须严格短于故障冷却，否则分档没有意义")
    check(any("限流" in s for s in _logs),
          "限流没有留痕：" + repr(_logs))
    check(any("不是故障" in s for s in _logs),
          "限流日志没说清它不是故障，运维会误以为站点挂了：" + repr(_logs))
    check(not any("无输出" in s for s in _logs),
          "限流被当成了通用「无输出」失败：" + repr(_logs))

    # ---------- ②真故障 -> 仍是 900s ----------
    _state["until"] = 0.0
    _logs.clear()
    subprocess.run = _mk_run("", "some other error", 1)
    _t0 = __import__("time").time()
    out2 = M.firecrawl_get("https://x.invalid/b")
    check(out2 is None, f"故障时应返回 None，实得 {out2!r}")
    _cool2 = _state["until"] - _t0
    check(_cool2 >= M.FIRECRAWL_COOLDOWN - 2,
          f"真故障的冷却被缩短了：{_cool2:.0f}s < {M.FIRECRAWL_COOLDOWN}s —— "
          "为了限流放宽真故障，会让坏主机每轮重付 90s")
    check(any("无输出" in s for s in _logs),
          "真故障没有沿用「无输出」文案：" + repr(_logs))

    # ---------- ③冷却期内直接返回，不发请求 ----------
    _state["until"] = __import__("time").time() + 300
    _calls = []

    def _boom(*a, **k):
        _calls.append(1)
        raise AssertionError("冷却期内不该发请求")
    subprocess.run = _boom
    out3 = M.firecrawl_get("https://x.invalid/c")
    check(out3 is None and not _calls, "冷却期内仍发了请求")

finally:
    (M._fc_fail_get, M._fc_fail_set, M.log,
     M._FIRECRAWL_NODE, M._FIRECRAWL_CLI, subprocess.run) = _orig
    import shutil
    shutil.rmtree(_TD, ignore_errors=True)

print(f"ALL PASS (firecrawl 限流冷却分档 {_ok} 个断言)")