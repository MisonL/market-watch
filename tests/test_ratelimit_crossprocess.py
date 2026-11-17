"""限流器的节流必须是**跨进程**的，否则并发会把它击穿。

`[实测]` 2026-11-10：两个并发工作流各 6 次请求，全局最小间隔 **2ms**
（配置下限 150ms）。因为 `self.last` 是**进程内**变量 —— 每个进程/线程
各记各的，合并后频率翻倍。

现实对应：`marketwatch` 与 `marketcollect` 都是 300s 一轮，几乎同时启动；
各自还会开线程取隔夜行情。单进程看是 0.15s 一次，合并看远低于此。

风控看的正是**同一 IP 的总请求速率**，不分进程。

契约：跨进程共享节流状态；并发下全局最小间隔不低于配置值（容差 20%）。
"""
import json
import os
import subprocess
import sys
import tempfile

_SRC = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
_ROOT = tempfile.mkdtemp(prefix="mw_rl_")

_CODE = """
import sys, time, json
sys.path.insert(0, SRC_PATH)
import marketlib as M
rl = M.RateLimiter(min_interval=0.15, daily_cap=100000)
ts = []
for _ in range(NTIMES):
    if rl.wait("test.host"):
        ts.append(time.time())
print(json.dumps(ts))
"""

N = 6
code = _CODE.replace("SRC_PATH", repr(_SRC)).replace("NTIMES", str(N))
env = dict(os.environ, MW_DIR=_ROOT, PYTHONDONTWRITEBYTECODE="1")

all_ts = []
procs = [subprocess.Popen([sys.executable, "-B", "-c", code],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, env=env) for _ in range(2)]
for p in procs:
    out, err = p.communicate(timeout=180)
    try:
        all_ts.extend(json.loads((out or "").strip().splitlines()[-1]))
    except Exception:
        print(f"  子进程输出异常: {(out or '')[:80]} | {(err or '')[:200]}")

all_ts.sort()
bad = 0
if len(all_ts) < 2:
    print(f"  X 只收到 {len(all_ts)} 个时间戳")
    bad += 1
else:
    gaps = [all_ts[i + 1] - all_ts[i] for i in range(len(all_ts) - 1)]
    mn = min(gaps)
    print(f"  两进程共 {len(all_ts)} 次：最小间隔 {mn*1000:.0f}ms（下限 150ms）")
    if mn < 0.15 * 0.8:
        print(f"  X 并发把节流击穿了：{mn*1000:.0f}ms < 120ms")
        bad += 1

assert bad == 0, f"{bad} 处不符合预期"
print("ALL PASS (跨进程节流：并发下全局间隔仍守住 150ms)")
