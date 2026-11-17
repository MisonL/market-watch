"""日配额必须**在耗尽前**给出预警，而不是只在耗尽那一刻报警。

`[生产实测]` 2026-11-10 13:15：日志只出现一句

    日配额耗尽，本轮放弃取数（不记为主机故障）

就再也没有任何提前量。当天此后的取数全部被闸门拦下，板块/港股整段失明，
而在此之前**一条预警都没有**。

原始教训就是"请求量过大导致 IP 被封"（daily_cap=2000 为此而设），
所以接近上限时必须让运维看得见，而不是等到闸门落下才发现。

契约：用量达到阈值的某一档时记一次 WARN（按档去重，不刷屏）。
"""
import os
import sys
import tempfile

os.environ["MW_DIR"] = tempfile.mkdtemp(prefix="mw_quota_")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import marketlib as M

bad = 0
logs = []


def check(cond, msg):
    global bad
    if not cond:
        bad += 1
        print(f"  ❌ {msg}")


_saved_log = M.log
M.log = lambda msg, tag="": logs.append((tag, msg))
try:
    import datetime, json
    _today = datetime.date.today().isoformat()

    def _seed(n):
        """把用量写进 rate.json（_roll 会从盘上读，进程内赋值会被覆盖）。"""
        M._atomic_write_json(M.RateLimiter.RATE_FILE,
                             {"date": _today, "count": n})

    # 用量刚到 50%：不该报
    _seed(49)
    rl = M.RateLimiter(min_interval=0.0, daily_cap=100)
    rl.wait("h")
    check(not any("配额" in m for _, m in logs),
          f"50% 用量就报了预警: {logs}")

    # 越过 80%：必须报一次
    logs.clear()
    _seed(80)
    rl2 = M.RateLimiter(min_interval=0.0, daily_cap=100)
    rl2.wait("h")
    check(any("配额" in m and "接近上限" in m for _, m in logs),
          f"用量到 80% 未预警: {logs}")

    # 再越过同档：不重复报（防刷屏）—— 同一进程内继续累加
    logs.clear()
    rl2.wait("h")
    check(not any("配额" in m and "接近上限" in m for _, m in logs),
          f"同一档位重复刷屏: {logs}")
finally:
    M.log = _saved_log

assert bad == 0, f"{bad} 处不符合预期"
print("ALL PASS (配额 80% 预警一次、低用量不报、同档不刷屏)")
