"""代理冷却必须按**目标主机**分账，否则单站点故障仍会株连所有源。

`[实测]` 2026-11-10：`_proxy_fail_bump()` 只有一个全局计数 `n`，
对同一台挂掉的 push2test 连打 3 个端点就凑满 PROXY_FAIL_THRESHOLD=3，
于是**所有**走代理的源（含健康的腾讯）在 600 秒内全部被跳过。

而 PROXY_FAIL_THRESHOLD 的注释写的是「单个目标站点的故障不会牵连其他源」——
**注释描述的行为在实现里根本不存在**。这是 11-09 修「单次就冷却」时
留下的半成品：把阈值从 1 提到 3 只降低了概率，没有换掉全局口径。

契约：计数与冷却都按 host 分账。A 主机连续失败不该让 B 主机也失去代理。
"""
import json
import os
import sys
import tempfile
import time

_ROOT = tempfile.mkdtemp(prefix="mw_ph_")
os.environ["MW_DIR"] = _ROOT
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import marketlib as M

bad = 0


def check(cond, msg):
    global bad
    if not cond:
        bad += 1
        print(f"  ❌ {msg}")


# ① A 主机失败到阈值 → A 自己被冷却
for _ in range(M.PROXY_FAIL_THRESHOLD):
    M._proxy_fail_bump("a.example.com")
check(M._proxy_fail_get("a.example.com") > time.time(),
      "A 主机连续失败达到阈值后未被冷却")

# ② 关键：B 主机**不该**受牵连
check(M._proxy_fail_get("b.example.com") <= time.time(),
      "B 主机被 A 主机的失败株连了（全局冷却未消除）")

# ③ B 主机成功后，A 的冷却不受影响
M._proxy_fail_reset("b.example.com")
check(M._proxy_fail_get("a.example.com") > time.time(),
      "B 成功把 A 的冷却一起清掉了（分账被 reset 破坏）")

# ④ 老格式（无 host 的全局记录）仍能读，不能因升级而崩
try:
    M._atomic_write_json(M._PROXY_FAIL_FILE, {"until": time.time() + 300, "n": 2})
    M._proxy_fail_get("c.example.com")
except Exception as e:
    check(False, f"读到旧格式记录就崩了: {e}")

assert bad == 0, f"{bad} 处不符合预期"
print("ALL PASS (代理冷却按 host 分账；跨主机不株连；兼容旧格式)")
