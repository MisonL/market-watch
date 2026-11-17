"""主链路停摆必须在**几分钟内**被发现，而不是等一天一次的自检。

`[事故]` 2026-11-08 20:23:47 ~ 21:08:49：一次 import 清理写坏了 analyze，
`依赖导入失败，本轮未执行` 连续打了 45 分钟 —— **零推送、零通知**，
是人工翻日志才发现的。

现有防线都覆盖不到"主链路根本不跑"：
  · 推送 —— 挂了就不推，正是没有信号的地方
  · 自检 —— `StartInterval=86400` + `RunAtLoad`，一天一次且跑在开机时刻
  · 面板 —— 要人主动去看

契约：每轮 analyze/collect 都 bom 一次活着的证据（带时间戳），
提供一个纯函数判断"最近 N 分钟有没有活着的证据"。
"""
import json
import os
import sys
import tempfile
import time

os.environ["MW_DIR"] = tempfile.mkdtemp(prefix="mw_live_")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

bad = 0


def check(cond, msg):
    global bad
    if not cond:
        bad += 1
        print(f"  ❌ {msg}")


try:
    import paths
    import runlog
except Exception as e:
    print(f"  ❌ 导入失败: {e}")
    raise SystemExit(1)

# ① 有 beat / stale 判定入口
check(hasattr(runlog, "beat"), "runlog 缺少 beat() —— 无法记录存活证据")
check(hasattr(runlog, "is_stale"), "runlog 缺少 is_stale() —— 无法判断停摆")

if hasattr(runlog, "beat") and hasattr(runlog, "is_stale"):
    # ② 一次 beat 之后不算停摆
    runlog.beat("analyze")
    check(runlog.is_stale("analyze", max_age_s=300) is False,
          "刚 beat 过却判定为停摆")

    # ③ 超时后判定为停摆
    check(runlog.is_stale("analyze", max_age_s=0) is True,
          "max_age_s=0 时未判定为停摆")

    # ④ 从未 beat 过的任务 → 停摆（新装或从未跑起来，同样需要看到）
    check(runlog.is_stale("never_beat_job", max_age_s=300) is True,
          "从未 beat 的任务未被判定为停摆")

assert bad == 0, f"{bad} 处不符合预期"
print("ALL PASS (心跳可记录；新鲜/过期/从未运行三种判定均正确)")
