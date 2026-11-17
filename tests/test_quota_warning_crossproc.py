"""配额预警必须**跨进程去重** —— 否则 80% 那一档全天会报上百次。

`[实测]` analyze/collect 每 5 分钟一轮、各自是**全新进程**，而
`_cap_warned_tier` 是进程内变量 → 每轮都重置 → 80%~90% 这一档
每轮都报一条。collect 每天 183 次，就是上百条重复警告。

这与 573e56a 把**限流**改成跨进程是同一个设计原则：跨轮状态不能放进程内存。

契约：同一档位全天只报一次（落盘）。
"""
import json
import os
import subprocess
import sys
import tempfile

_SRC = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
_ROOT = tempfile.mkdtemp(prefix="mw_qx_")

_CODE = """
import sys, datetime
sys.path.insert(0, SRC_PATH)
import marketlib as M
import paths, os, json
# 播种：今日已用 85%（已存在就不覆盖 —— 否则每轮都把 warned_tier 抹掉）
_rf = M.RateLimiter.RATE_FILE
if not os.path.exists(_rf):
    M._atomic_write_json(_rf, {"date": datetime.date.today().isoformat(), "count": 85})
logs = []
M.log = lambda m, t="": logs.append(m)
rl = M.RateLimiter(min_interval=0.0, daily_cap=100)
rl.wait("h")
print(json.dumps([x for x in logs if "接近上限" in x]))
"""

code = _CODE.replace("SRC_PATH", repr(_SRC))
env = dict(os.environ, MW_DIR=_ROOT, PYTHONDONTWRITEBYTECODE="1")

total = 0
for i in range(3):          # 三个独立进程，模拟连续三轮
    r = subprocess.run([sys.executable, "-B", "-c", code],
                       capture_output=True, text=True, env=env, timeout=60)
    try:
        hits = json.loads((r.stdout or "").strip().splitlines()[-1])
    except Exception:
        print(f"  子进程输出异常: {(r.stdout or '')[:80]} | {(r.stderr or '')[:200]}")
        hits = []
    total += len(hits)
    print(f"  第 {i+1} 轮进程：报了 {len(hits)} 条")

bad = 0
if total != 1:
    print(f"  ❌ 三轮共报 {total} 条，应恰好 1 条（跨进程去重失效 = 刷屏）")
    bad += 1

assert bad == 0, f"{bad} 处不符合预期"
print("ALL PASS (配额预警跨进程只报一次)")
