#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""动态并发调度：判据必须随负载调整，且不能被环境变量绕过。

## 为什么值得单测

`dynamic_workers()` 的取值直接决定"跑测试会不会把机器打死"或"会不会
退化成串行"。它踩过两次真实的坑，都是**算错了但看起来合理**：

① 用 `可用核 = 有效核 - load1` 直接算 → 本机（常态 load 60~113）算出
   **1 路**，测试套件从 15~20s 劣化到 **82.8s**。
   根因：macOS 的 load average 把 IO 等待也算进可运行队列，它不是
   "CPU 被占用多少"。

② 改成固定 2 路的"温和折减" → 仍比 4 路慢一倍多。

`[实测]` 同一台机器（IO 密集型的单元测试）：

    2 路 66s · 4 路 37s · 6 路 27s · 8 路 30s · 12 路 25s

所以判据不能靠"猜机器状态"，得靠**实测反馈**（AdaptiveThrottle）。

## 本文件锁住什么

- 环境变量优先（显式写了数字就必须用它，不能被"机器很闲"改掉）
- 上下限被遵守、永不返回 < 1（退到 0 就永远等不到负载下降）
- `AdaptiveThrottle` 能升能降，且不会震荡
- 读不到 loadavg 时不冒进
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

import concurrency as C

_ok = 0


def check(cond, msg):
    global _ok
    if cond:
        _ok += 1
    else:
        print(f"  ❌ {msg}")
        sys.exit(1)


# ---------- dynamic_workers：边界与稳定性 ----------

for _load in (0.0, 1.0, 5.0, 20.0, 100.0, 500.0):
    _orig = C.load1
    C.load1 = lambda v=_load: v
    try:
        for _lo, _hi in ((1, 8), (2, 12), (3, 3)):
            _n = C.dynamic_workers(_lo, _hi)
            check(_lo <= _n <= _hi,
                  f"load={_load} lo={_lo} hi={_hi} 返回 {_n}，越界")
            check(_n >= 1, f"load={_load} 返回 {_n} —— 必须 ≥1（0 会死锁）")
    finally:
        C.load1 = _orig

# 读不到负载 → 不冒进（用 1，因为不知道有没有余量）
_orig = C.load1
C.load1 = lambda: None
try:
    check(C.dynamic_workers(1, 8) == 1,
          "loadavg 读不到时应退到下限 1，不能乐观开满")
finally:
    C.load1 = _orig

# 极端过载时仍要 ≥1（这是最容易写成 0 的地方）
_orig = C.load1
C.load1 = lambda: 10000.0
try:
    check(C.dynamic_workers(1, 8) >= 1, "极端过载时返回 0 —— 会死锁")
finally:
    C.load1 = _orig

# ---------- resolve_workers：环境变量必须优先 ----------

os.environ["MW_TEST_JOBS"] = "3"
check(C.resolve_workers("MW_TEST_JOBS", 1, 12) == 3,
      "显式设置的环境变量必须被尊重（排查竞态时常写 =1）")

os.environ["MW_TEST_JOBS"] = "1"
check(C.resolve_workers("MW_TEST_JOBS", 1, 12) == 1,
      "MW_TEST_JOBS=1 必须真的串行 —— 不能被动态逻辑改成更多")

os.environ["MW_TEST_JOBS"] = "abc"
check(C.resolve_workers("MW_TEST_JOBS", 1, 12) >= 1,
      "非法环境变量值应回退到动态判断，而不是抛异常")
del os.environ["MW_TEST_JOBS"]

# ---------- AdaptiveThrottle：能升能降、不震荡 ----------

_t = C.AdaptiveThrottle(lo=1, hi=8, start=2)
check(_t.workers() == 2, "start 未生效")

# 伪造"吃得满"→ 升
_t._last_ratio = 0.95
check(_t.adjust() == 3, "兑现率高时应 +1")
check(_t.adjust() == 4, "连续高应继续 +1")

# 伪造"明显排队"→ 降
_t._last_ratio = 0.1
check(_t.adjust() == 3, "兑现率过低时应 -1")

# 中间地带不调整（避免边界来回抖）
_before = _t.workers()
_t._last_ratio = 0.6
check(_t.adjust() == _before, "50%~75% 之间不应调整 —— 否则会震荡")

# 上下限
_t2 = C.AdaptiveThrottle(lo=2, hi=3, start=3)
_t2._last_ratio = 9.9
check(_t2.adjust() == 3, "不能超过 hi")
_t3 = C.AdaptiveThrottle(lo=2, hi=4, start=2)
_t3._last_ratio = 0.0
check(_t3.adjust() == 2, "不能低于 lo")

# observe() 未 mark() 时返回 None（不误改）
_t4 = C.AdaptiveThrottle(lo=1, hi=4, start=2)
check(_t4.observe() is None, "未 mark() 时应返回 None，不能瞎算一个比值")

# ---------- 容器（Docker）部署：cgroup 感知 ----------
#
# 为什么必须单测：容器里 `os.cpu_count()` 返回**宿主机**核数，不是限额。
# `--cpus=2` 跑在 16 核机上照样报 16 → 并发按 16 算 → 十几个 worker 抢 2 核。
# `os.getloadavg()` 同理读宿主机，宿主机忙就误判我方过载。
# 两个都不是"理论风险"：部署到 Docker 就会立刻踩到。
_TD = tempfile.mkdtemp(prefix="mw_cg_")
_CF = os.path.join(_TD, "cpu.max")
_REAL_EXISTS, _REAL_OPEN = os.path.exists, open


def _as_container(spec):
    """把 concurrency 的 cgroup 读路径指到临时文件。"""
    with open(_CF, "w", encoding="utf-8") as _f:
        _f.write(spec + "\n")
    C.os.path.exists = lambda p: True if p == "/sys/fs/cgroup/cpu.max" else _REAL_EXISTS(p)
    C.open = (lambda p, *a, **k: _REAL_OPEN(_CF, *a, **k)
              if p == "/sys/fs/cgroup/cpu.max" else _REAL_OPEN(p, *a, **k))


def _as_bare_metal():
    C.os.path.exists = _REAL_EXISTS
    C.open = _REAL_OPEN


try:
    # 本机（非容器）行为不能受影响 —— 这是最容易碰坏的
    _as_bare_metal()
    check(C._in_container() is False, "非容器环境不该判为容器")
    check(C._cgroup_cpu_limit() is None, "非容器环境不该读到 cgroup 限额")

    # cgroup v2：2 核
    _as_container("200000 100000")
    check(C._in_container() is True, "有 cpu.max 应判为容器")
    check(C._cgroup_cpu_limit() == 2.0, f"v2 应解析成 2.0，实得 {C._cgroup_cpu_limit()}")
    check(C._effective_cores() == 2, f"2 核限额的有效核应是 2，实得 {C._effective_cores()}")
    check(C.load1() is None,
          "容器内 load1 必须返回 None —— 宿主机负载与容器无关，"
          "拿它判过载会让容器空转")
    _n = C.dynamic_workers(2, 12)
    check(2 <= _n <= 12, f"容器内并发必须落在上下限内，实得 {_n}")

    # v2 的 `max` 表示不限额 → 按裸机处理
    _as_container("max 100000")
    check(C._cgroup_cpu_limit() is None, "v2 'max' 是不限额，应返回 None")

    # 小数限额向上取整
    _as_container("150000 100000")
    check(C._cgroup_cpu_limit() == 1.5, "应解析成 1.5")
    check(C._effective_cores() == 2,
          f"1.5 核应向上取整为 2（1 核跑不动 IO 重叠），实得 {C._effective_cores()}")

    # 4 核
    _as_container("400000 100000")
    check(C._effective_cores() == 4, f"4 核限额，实得 {C._effective_cores()}")

    # 极端：限额极小也不能返回 0
    _as_container("50000 100000")
    check(C._effective_cores() >= 1, "0.5 核也不能返回 0（会死锁）")
finally:
    _as_bare_metal()
    import shutil
    shutil.rmtree(_TD, ignore_errors=True)

print(f"ALL PASS (动态并发 {_ok} 个断言)")
