#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""动态并发调度 —— 按机器**实际负载**决定并行度，而不是写死一个数。

## 为什么需要它（2026-11-17）

两处并发原先都是固定值，各自踩过不同的坑：

- `tests/run_all.sh`：写死 4 路。它只是"看起来合理"——机器空闲时太保守，
  被别的程序占满时又不肯退让。
- `src/selfcheck_mutation.py`：默认 3 路，注释里记着 **8 路曾把本机 CPU
  打到 97%、load 冲到 229~302**，用户当场手动杀进程要求"不要一次开那么多"。
  那条注释的结论是"改成 6 路要用环境变量显式指定"——这等于把判断责任
  推给使用者，而使用者并不知道此刻机器上还有谁在跑。

两种极端都不对。正确的依据是**此刻还能用几个核**，而这个数字是变化的：
同一台机器，凌晨和盘中差得很远；跑测试时可能别的进程正在抢 CPU。

## 判据

用 load average 而不是 CPU 使用率：

- `load1` 是**可运行队列长度**，直接反映"有多少活想跑但没核跑"
- CPU 使用率是**过去一段时间的平均**，看不出一瞬间的拥挤
- macOS 上 `os.getloadavg()` 直接可用，不必起 `sysctl` 子进程

静态值只当**起点**，真正的仲裁是 `AdaptiveThrottle`（本模块下半部分）。

⚠️ 一条实测教训（2026-11-17）：最初用 `可用核 = 有效核 - load1` 直接算，
在本机算出 **1 路**，测试套件耗时从固定 4 路的 15~20s 劣化到 **82.8s**。
原因是这台机器的 load 常态就有 60~113（一台常驻虚拟机占 428% CPU），
而 macOS 的 load average **把 IO 等待也算进可运行队列** —— 它不等于
"CPU 被占用多少"。所以现在只做温和折减，不再用 load 做硬算术。

## 为什么按 CPU 核数而不是按"逻辑核"

`os.cpu_count()` 在 x86 上返回**逻辑核**（这台 i7-10700 是 16），但
超线程的两个逻辑核并不等价于两个物理核 —— 对 CPU-bound 的 Python 子进程，
真实吞吐贴近物理核数（8）。`os.sched_getaffinity` 在 macOS 上不存在，
`cpu_count // 2` 又对新机器的非超线程配置是错的。

取舍：**用逻辑核数，但把一个逻辑核只按半个算**
（`_EFFECTIVE = cpu_count // 2`，且至少 1）。这与实测相符：
在本机（16 逻辑核）上 4 路是甜点、8 路开始伤负载，而 16/2/2 = 4。
`MW_TEST_JOBS` / `MW_JOBS` 仍可显式覆盖，环境变量永远优先。

## 用法

    from concurrency import dynamic_workers
    n = dynamic_workers(min_workers=1, max_workers=6)
"""
import os


# 每个"有效核"能承受多少个并发 worker。
#
# = 2 的含义："一个逻辑核按半个算"（见模块 docstring 的取舍说明）。
# 本机 16 逻辑核 → 有效核 8 → 空闲时 4 路（与实测甜点一致）。
_WORKER_PER_EFFECTIVE_CORE = 1


def _in_container():
    """是否跑在容器里（Docker / Podman / k8s 都满足其一）。

    判据取"有没有 cgroup 限额文件"，而不是 `/.dockerenv`：
    后者只覆盖 Docker，k8s 的 containerd 容器就没有；而 cgroup 文件
    在两种运行时都有（v1 / v2 任一）。
    """
    for _p in ("/sys/fs/cgroup/cpu.max",                    # cgroup v2
               "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"):      # cgroup v1
        if os.path.exists(_p):
            return True
    return False


def _cgroup_cpu_limit():
    """容器被允许用几个 CPU（可带小数）；读不到返回 None。

    ⚠️⚠️ 2026-11-18（Docker 部署）：**容器里 `os.cpu_count()` 返回的是
    宿主机核数，不是容器限额**。`--cpus=2` 跑在 16 核机器上照样报 16，
    于是并发度按 16 核算 → 一次开十几个 worker，全部在 2 个核上抢 CPU，
    比串行还慢。

    cgroup v2 的 `cpu.max` 形如 `200000 100000`（配额/周期）或 `max 100000`；
    v1 用两个文件 `cfs_quota_us` / `cfs_period_us`。两者都按
    `quota / period` 折成核数。
    """
    # ---- cgroup v2 ----
    try:
        with open("/sys/fs/cgroup/cpu.max", encoding="utf-8") as _f:
            _parts = _f.read().split()
        if len(_parts) >= 2 and _parts[0] != "max":
            _q, _p = float(_parts[0]), float(_parts[1])
            if _q > 0 and _p > 0:
                return _q / _p
    except Exception:
        pass
    # ---- cgroup v1 ----
    try:
        with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", encoding="utf-8") as _f:
            _q = float(_f.read().strip())
        with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us", encoding="utf-8") as _f:
            _p = float(_f.read().strip())
        # v1 用 -1 表示"不限额"
        if _q > 0 and _p > 0:
            return _q / _p
    except Exception:
        pass
    return None


def _effective_cores():
    """有效核数：容器内按**限额**算，裸机按逻辑核 // 2（超线程折算）。

    `os.cpu_count()` 在某些环境返回 None —— 那就不折半，直接当 1 个用。
    """
    _lim = _cgroup_cpu_limit()
    if _lim is not None:
        # 容器：限额就是硬上限，不再折半 —— cgroup 的 2 核本来就是实打实的
        # 2 核，不像超线程那样两个逻辑核共用一个物理核。向上取整，
        # 因为 1.5 核的限额至少还能跑 2 个 worker 的 IO 重叠。
        return max(1, int(-(-_lim // 1)))
    try:
        n = os.cpu_count() or 0
    except Exception:
        n = 0
    if n <= 0:
        return 1
    return max(1, n // 2)


def load1():
    """1 分钟负载；读不到返回 None（与 selfcheck 的 fail-closed 约定一致）。

    ⚠️⚠️ 2026-11-18（Docker 部署）：**容器里这个值没有意义，返回 None**。

    `os.getloadavg()` 读的是**宿主内核**的可运行队列长度，容器看不到自己的。
    在一台 load 60~100 的宿主机上（本机实测常态，一台虚拟机占 428% CPU），
    容器里的进程会以为自己过载 —— 而实际上 cgroup 给它的核是空的。

    返回 None 不是"信息缺失"，是**这个指标在容器里不适用**。调用方
    `dynamic_workers()` 对 None 的处理是"用下限、不冒进"，而真正的仲裁交给
    `AdaptiveThrottle` —— 它量的是本进程组实际拿到多少 CPU，那是可靠的。
    """
    if _in_container():
        return None
    try:
        return float(os.getloadavg()[0])
    except Exception:
        return None


def dynamic_workers(min_workers=1, max_workers=8, reserve=1.0):
    """按当前负载算出目标并发度。

    Args:
        min_workers:  下限。**至少 1** —— 退到 0 会永远等不到负载下降。
        max_workers:  上限。给"机器其实很闲"的情况封顶，避免一次开太多
                      子进程（每条变异体自己还会起 selfcheck 子进程，
                      实际并发是 worker 数的倍数）。
        reserve:      给别的进程预留的核数。默认 1 —— 不要吃满,
                      否则连 shell、编辑器都卡。

    Returns:
        整数 worker 数，落在 [min_workers, max_workers]。
    """
    min_workers = max(1, int(min_workers))
    max_workers = max(min_workers, int(max_workers))

    avail = _effective_cores()
    cur = load1()
    if cur is None:
        # 读不到负载时分两种情形：
        #
        # ① **容器内**（2026-11-18 加）：loadavg 读不到不等于没有余量 ——
        #    容器有明确的 cgroup 限额，那个数就是可用的全部算力。
        #    单元测试是 IO 密集型（实测 8 个 sleep 子进程 CPU 并发 0.9），
        #    在限额内开满反而最快。所以按"限额 × 2"给，仍夹在上下限内。
        # ② 裸机读不到 loadavg：真·不知道余量 → 退下限，不冒进。
        if _in_container():
            _cap = avail * 2
            return max(min_workers, min(max_workers, _cap))
        # 运行时还有 `AdaptiveThrottle` 会根据实际吞吐往上调。
        return min_workers

    # ⚠️⚠️ 2026-11-17 实测修正：**不能直接用 load 算术**。
    #
    # 原始公式 `free = avail - load` 在本机算出 1 路，实测耗时从固定 4 路的
    # 15~20s 劣化到 **82.8s**。原因：
    #
    #   本机 load1 常态 60~113，而 CPU 空闲率也有 2~5%，
    #   占用最高的是 `Virtualization.framework`（一台常驻虚拟机，428% CPU）。
    #   load average 在 macOS 上**把 IO 等等待也算进可运行队列**，
    #   它衡量的不是"CPU 被占用多少"，而是"有多少线程没在跑"。
    #   所以 `avail - load` 在机器"忙但能跑"时会严重低估可用算力。
    #
    # 现在改成**只做温和折减**：负载越高收紧得越多，但**不归到 1**。
    #   真正的仲裁交给 `AdaptiveThrottle` —— 它量的是"我们这一轮实际拿到
    #   多少 CPU"，那是可观测的事实，不是对机器状态的猜测。
    # ⚠️⚠️ 2026-11-17 第二轮实测修正：静态折减**基本没有意义**，原因是
    # 这两类任务的"负载性质"完全不同：
    #
    #   单元测试（本函数的主要使用者）：**IO 密集型**。
    #     `[实测]` 8 个 `time.sleep(0.5)` 的子进程跑完，CPU 累计只有 0.65s、
    #     墙钟 0.75s —— 平均 CPU 并发 0.9（8 路里只用了不到 1 核）。
    #     它们的时间花在**进程启动和文件 IO** 上，几乎不抢 CPU。
    #     所以并发越高越快：
    #       2 路 66s · 4 路 37s · 6 路 27s · 8 路 30s · 12 路 **25s**
    #     （12 路最快，且机器没被拖坏 —— 因为它压根没吃 CPU）
    #
    #   变异测试：**CPU 密集型**（每条跑一整个 selfcheck）。
    #     那条路必须保守，用 `_DEFAULT_JOBS=3`，不该套用这里的逻辑。
    #
    # 结论：本函数**按 IO 密集型的场景**给足并发，只做很轻的折减；
    # 真正的仲裁交给 `AdaptiveThrottle`（量实际拿到的 CPU）。
    # 之前 `avail - load` 算出 1 路、实测劣化到 82.8s 的教训见上方 docstring。
    cap = avail * _WORKER_PER_EFFECTIVE_CORE
    if cur is None or cur < avail * 2:
        n = cap                        # 正常 / 中等负载：吃满
    else:
        n = max(min_workers, cap // 2)  # 极端过载（load > 2×有效核）：让一半
    return max(min_workers, min(max_workers, n))


def resolve_workers(env_name, min_workers=1, max_workers=8, default=None):
    """环境变量优先，否则动态算。

    环境变量是**显式指令**，必须无条件尊重 —— 使用者写
    `MW_JOBS=1` 就是要串行（排查竞态时很常见），不能因为"机器很闲"
    就自作主张开 4 路。解析失败按未设置处理。
    """
    raw = os.environ.get(env_name)
    if raw not in (None, ""):
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    if default is not None:
        return max(1, int(default))
    return dynamic_workers(min_workers, max_workers)


if __name__ == "__main__":   # 便于手工核对当前判断
    import sys
    lo = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    hi = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    print(f"逻辑核 {os.cpu_count()} · 有效核 {_effective_cores()} · "
          f"load1 {load1()} → 建议并发 {dynamic_workers(lo, hi)}"
          f"（范围 {lo}~{hi}）")


# ---------- 运行时反馈（2026-11-17） ----------
#
# 静态判据（上面的 dynamic_workers）有个前提：**机器上有多少余量是可知的**。
# 但这台机器的实测情况推翻了这个前提：
#
#     load average    : 62 / 101 / 85
#     CPU 空闲率      : 2~5%
#     占用最高进程    : Virtualization.framework 428%（一台常驻虚拟机）
#
# 也就是说"负载 60"不是瞬时尖峰，而是**常态**。按 `available = cores - load`
# 去算，并发会永远退到 1 —— 那比原来的固定 4 路还慢。
# 根因是 load average 在 macOS 上会把 IO 等待也算进去，不能当"CPU 占用"用。
#
# 所以换一个**可观测、可反馈**的判据：**我们自己的进程组实际拿到了多少 CPU**。
#   - 拿到的 ≈ 并发数  → 还没吃满，可以加
#   - 拿到的 << 并发数  → 在排队，该减
# 这不是猜机器状态，而是直接量"我这一轮跑得多快"。

def group_cpu_seconds():
    """本进程 + 所有**已回收**子进程的累计 CPU 秒。"""
    import resource
    s = resource.getrusage(resource.RUSAGE_SELF)
    c = resource.getrusage(resource.RUSAGE_CHILDREN)
    return (s.ru_utime + s.ru_stime + c.ru_utime + c.ru_stime)


class AdaptiveThrottle:
    """按"实测 CPU 兑现率"增量调整并发度。

    用法：

        th = AdaptiveThrottle(lo=1, hi=8, start=4)
        while 还有任务:
            n = th.workers()          # 当前建议并发
            ... 派发 n 个任务，等它们完成 ...
            th.observe(elapsed)       # 报告这一轮花了多少墙钟秒
            th.adjust()               # 调整下一轮的并发

    `observe()` 每轮记录一次"墙钟 vs CPU"，`adjust()` 据此决定增/减/保持。

    ⚠️ 只做**缓慢**调整（每轮最多 ±1），原因：
      ① 采样噪声大（进程启动/IO 抖动），激进调整会来回震荡
      ② 减少并发要等已跑的任务结束，突变会浪费
      ③ 目标是"别把机器打死"，不是"榨到最后一个百分点"
    """

    def __init__(self, lo=1, hi=8, start=None, target=0.75):
        self.lo = max(1, int(lo))
        self.hi = max(self.lo, int(hi))
        # 目标兑现率：拿到 75% 的并发数就算"吃得比较满"。
        # 留 25% 是给 IO 等待、进程启动等非 CPU 时间 —— 那部分本来就不是并发的锅。
        self.target = float(target)
        self._n = self.lo if start is None else max(self.lo, min(self.hi, int(start)))
        self._t0 = None
        self._c0 = None
        self._last_ratio = None
        self._history = []

    def workers(self):
        return self._n

    def mark(self):
        """记录本轮起点（CPU 计数 + 墙钟）。"""
        import time as _t
        self._t0 = _t.time()
        self._c0 = group_cpu_seconds()

    def observe(self, elapsed=None):
        """结束本轮，返回本轮兑现率（CPU 秒 / 墙钟秒 / 并发数）。

        未 `mark()` 时用 `elapsed` 参数；两者都没有就返回 None（不动）。
        """
        import time as _t
        if self._c0 is None:
            return None
        dt = elapsed if elapsed is not None else (_t.time() - self._t0)
        if dt <= 0:
            return None
        cpu = group_cpu_seconds() - self._c0
        ratio = (cpu / dt) / max(1, self._n)
        self._last_ratio = ratio
        self._history.append((round(dt, 2), self._n, round(ratio, 2)))
        self._c0 = None
        return ratio

    def adjust(self):
        """据最近一次的兑现率调整并发，返回调整后的值。"""
        r = self._last_ratio
        if r is None:
            return self._n
        if r >= self.target and self._n < self.hi:
            self._n += 1          # 还吃得下 → 加一路
        elif r < self.target * 0.5 and self._n > self.lo:
            self._n -= 1          # 明显在排队 → 减一路
        # 中间地带（50%~75%）不调整 —— 避免在边界来回抖
        return self._n

    def report(self):
        """可打印的轨迹，便于排查"为什么最后落在 N 路"。"""
        return " → ".join(f"{n}路({r:.0%})" for _, n, r in self._history[-6:])
