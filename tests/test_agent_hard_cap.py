#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调研 Agent 卡死时**必须有人把它踢死** —— 否则整轮收盘窗口白扔。

## 2026-11-23 的生产时间线（实测）

    17:50  launchd 触发本轮
    17:53  SDK initialize 超时，`_mark_run` 已记下 rc=1
    18:20  run.py 的 1800s 超时**才**把它杀掉

异常 17:53 就抛出来了、状态也落盘了，进程却**又活了 26 分钟**。
收盘后窗口只有 16:05~17:55，一轮报废就少一次机会。

为什么异常没让它退出：`run()` 里 `raise` 之后确实没代码了，但
`asyncio.run()` 的收尾要等事件循环把 SDK 的 transport / 子进程 /
后台任务全部取消干净。SDK（`_internal/query.py`、`subprocess_cli.py`）
在初始化失败时**没有**保证杀掉它起的 `claude` 子进程，收尾可能挂住。
这是第三方库的内部行为，改不了，也不该改（升级即失效）。

所以由我们自己提供有界兜底：daemon 线程到点 `os._exit(1)`。
daemon 线程在主线程还活着时一直在跑，正好覆盖"卡在收尾里"这种形态 ——
`asyncio.wait_for` 在收尾阶段是拦不住的。

## 同时修掉的上游成因

`claude_agent_sdk/client.py`：

    initialize_timeout_ms = int(os.environ.get(
        "CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", "60000"))
    initialize_timeout = max(initialize_timeout_ms / 1000.0, 60.0)

默认 **60 秒**且不允许配更短，本仓库从未设置 → 一直是 60s。
而失败那一轮是收盘后 17:50，机上有 27 个 claude 进程、
单个占 200~600MB、swap 已用 4.9G/6G —— node CLI 冷启动被拖到
分钟级完全可能，60s 不够。现在显式给到 180s。

## 本文件锁住什么

① `CLAUDE_CODE_STREAM_CLOSE_TIMEOUT` 真的被设成**远大于** SDK 默认的 60s
② 且用的是 `setdefault` —— 运维仍可用环境变量覆盖（别写成硬赋值）
③ 硬上界常量**严格小于** run.py 的 1800s（否则又回到「由 run.py 来杀」，
   而它只会记一行「agent 超时」，不知道卡在哪个阶段）
④ 看门狗**真的会让进程退出**（端到端，子进程实测，不是只调函数看返回值）
⑤ 退出**之前**状态已落盘（`os._exit` 不会替你 flush）
⑥ 未到点时**不早退**（限 3s，睡 1s 后仍在跑）
⑦ 接线：`__main__` 里真的调了 `_arm_hard_cap()`，且**在** `asyncio.run` 之前
"""
import os
import re
import subprocess
import sys
import tempfile

_AGENT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "..", "agent")
_PY = os.path.join(_AGENT, ".venv", "bin", "python")
_MAIN = os.path.join(_AGENT, "src", "mw_agent", "__main__.py")

_ok = 0


def check(cond, msg):
    global _ok
    if cond:
        _ok += 1
    else:
        print(f"  ❌ {msg}")
        sys.exit(1)


_src = open(_MAIN, encoding="utf-8").read()

# ---------- ① initialize 超时必须给足 ----------
_m = re.search(r'os\.environ\.setdefault\(\s*'
               r'"CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"\s*,\s*"(\d+)"\s*\)', _src)
check(_m is not None,
      "没有显式设置 CLAUDE_CODE_STREAM_CLOSE_TIMEOUT —— "
      "SDK 会吃默认 60s，高负载下 initialize 必然超时（2026-11-23 实测）")
_val = int(_m.group(1))
check(_val > 60000,
      f"initialize 超时只给了 {_val}ms，不比 SDK 默认的 60000ms 宽 —— 白设")
check(_val >= 120000,
      f"initialize 超时 {_val}ms 偏窄：失败那轮机上 27 个 claude 进程、"
      f"swap 4.9G/6G，node 冷启动到分钟级完全可能")

# ---------- ② 必须是 setdefault，不能硬赋值 ----------
check('os.environ["CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"] =' not in _src,
      "用了硬赋值 —— 运维无法用环境变量临时覆盖")

# ---------- ③ 硬上界必须小于 run.py 的 1800s ----------
_mc = re.search(r"^AGENT_HARD_CAP\s*=\s*(\d+)", _src, re.M)
check(_mc is not None, "缺少 AGENT_HARD_CAP 常量")
_cap = int(_mc.group(1))
_runpy = open(os.path.join(_AGENT, "run.py"), encoding="utf-8").read()
_mp = re.search(r"_p\.wait\(timeout=(\d+)\)", _runpy)
check(_mp is not None, "run.py 里没找到 _p.wait(timeout=...)，判据失效")
_wait = int(_mp.group(1))
check(_cap < _wait,
      f"硬上界 {_cap}s 不小于 run.py 的 {_wait}s —— 又回到「由 run.py 来杀」，"
      f"它只记一行『agent 超时』，看不出卡在哪个阶段")
check(_cap > 600,
      f"硬上界 {_cap}s 太短：正常一轮实测 5~10 分钟，会误杀")
check("AGENT_HARD_CAP" in _src.split("def _arm_hard_cap")[1][:400],
      "`_arm_hard_cap` 的默认值没有引用 AGENT_HARD_CAP —— 两处会分叉")

# ---------- ④⑤⑥ 端到端：真能退出、真先落盘、真不早退 ----------
_RUNNER = r'''
import json, os, sys, time
sys.path.insert(0, {agent_src!r})
import mw_agent.__main__ as A

_MARK = {mark!r}

def fake_mark(rc, push, note):
    # 如实模拟 `_write_status`：with 块内写、出块即关文件（关 = flush）
    with open(_MARK, "w", encoding="utf-8") as f:
        json.dump({{"rc": rc, "push": push, "note": note}}, f)

A._arm_hard_cap(limit={limit!r}, _mark=fake_mark)
time.sleep({sleep!r})
print("NEVER_REACHED_IF_WATCHDOG_OK", flush=True)
'''

_markf = os.path.join(tempfile.mkdtemp(), "mark.json")


def _run(limit, sleep):
    code = _RUNNER.format(agent_src=os.path.join(_AGENT, "src"),
                          mark=_markf, limit=limit, sleep=sleep)
    return subprocess.run([_PY, "-c", code], capture_output=True,
                          text=True, timeout=120)


# ⑥ 未到点不早退
if os.path.exists(_markf):
    os.unlink(_markf)
_r6 = _run(limit=6.0, sleep=1.0)
check(_r6.returncode == 0,
      f"限 6s、只睡 1s，进程却提前以 rc={_r6.returncode} 退出 —— 看门狗早退了")
check("NEVER_REACHED_IF_WATCHDOG_OK" in (_r6.stdout or ""),
      "睡醒后没走到 print，说明看门狗在中途就把进程杀了")
check(not os.path.exists(_markf),
      "还没到点就写了失败状态 —— 会把成功的轮次误记成硬上界")

# ④⑤ 到点必须退出，且退出前已落盘
_r4 = _run(limit=2.0, sleep=30.0)
check(_r4.returncode == 1,
      f"限 2s、睡 30s，进程 rc={_r4.returncode} —— 看门狗没把它踢死"
      f"（生产上就是 17:53 卡到 18:20 那个形态）")
check("NEVER_REACHED_IF_WATCHDOG_OK" not in (_r4.stdout or ""),
      "卡住后的 print 居然执行了 —— 说明 sleep 没被真的打断，测法无效")
check(os.path.exists(_markf),
      "进程退了但状态没落盘 —— `os._exit` 抢在写盘前面，"
      "自检那头只会看到上一次的旧结果")
_raw = open(_markf, encoding="utf-8").read()
check('"push": "hard_cap"' in _raw,
      f"落盘的 push 标签不是 hard_cap，自检无法区分「卡死」与「AI 报错」：{_raw}")
check('"rc": 1' in _raw, f"落盘的 rc 不是 1：{_raw}")

# ---------- ⑥b dry 下**不得**写生产状态文件 ----------
# `_mark_run` 的契约是"记一次真跑的结果"。若 dry 卡住也去写 rc=1，
# 自检会据此报「今天收盘后那轮调研失败了」—— 那是在编造一次
# **从未发生过的**生产失败。
if os.path.exists(_markf):
    os.unlink(_markf)
_RUNNER_DRY = r'''
import json, os, sys, time
sys.path.insert(0, {agent_src!r})
sys.argv = ["mw_agent", "--dry"]          # 让 dry 走"按 argv 现判"的分支
import mw_agent.__main__ as A

_MARK = {mark!r}

# ⚠️ 必须把 `_mark_run` 替成**真的写文件**的那个，否则这条断言恒真：
# 我第一版替成了 `_real.append(...)`（只记在内存列表里），于是无论
# dry 守卫在不在，`_MARK` 文件都不会出现 —— 删掉守卫测试照样绿。
# 一个测不到缺陷的用例比没有更糟：它让人以为这里有保护。
def _writing_mark(rc, push, note=""):
    with open(_MARK, "w", encoding="utf-8") as f:
        json.dump({{"rc": rc, "push": push}}, f)

A._mark_run = _writing_mark
A._arm_hard_cap(limit=2.0)
time.sleep(30)
'''
_rd = subprocess.run(
    [_PY, "-c", _RUNNER_DRY.format(agent_src=os.path.join(_AGENT, "src"),
                                   mark=_markf)],
    capture_output=True, text=True, timeout=120)
check(_rd.returncode == 1,
      f"dry 模式卡住后 rc={_rd.returncode} —— 看门狗照样该踢死（只是别写状态）")
check(not os.path.exists(_markf),
      "dry 模式卡住却写了 last_run_result.json —— "
      "一次人工演练会伪造出一次生产失败")

# ---------- ⑦ 接线：__main__ 真的调了，且在 asyncio.run 之前 ----------
_tail = _src[_src.index('if __name__ == "__main__":'):]
check("_arm_hard_cap()" in _tail,
      "`__main__` 里没有调用 `_arm_hard_cap()` —— 写了不用等于没有")
check(_tail.index("_arm_hard_cap()") < _tail.index("asyncio.run("),
      "看门狗装在 `asyncio.run()` **之后** —— 卡在 connect/initialize 时"
      "主线程根本走不到那一行，看门狗永远装不上")

print(f"ALL PASS (调研 Agent 硬上界看门狗 {_ok} 个断言)")
