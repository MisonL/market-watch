"""analyze 停摆时，必须由**还活着的** collect 替它说话。

`[事故]` 2026-11-08 20:23:47~21:08:49：import 清理写坏 analyze，
零推送、零通知，45 分钟靠人工翻日志才发现。

心跳（`runlog.beat`）已经能记录"最近一次跑起来是什么时候"，但**谁来检查**
才是关键：
  · 让 analyze 自己检查 —— 它挂了就检查不了，循环依赖
  · 让自检检查 —— `StartInterval=86400`，一天一次，太慢
  · 让 collect 检查 —— 它**独立进程**、每 5 分钟一轮、今天实测跑了 183 次

契约：collect 每轮检查 analyze 心跳；盘中超时则推送一条告警进群。
告警必须**按天去重**（同样的停摆不刷屏），且**失败不能影响采集**。
"""
import json
import os
import sys
import tempfile
import time

os.environ["MW_DIR"] = tempfile.mkdtemp(prefix="mw_stall_")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

bad = 0


def check(cond, msg):
    global bad
    if not cond:
        bad += 1
        print(f"  ❌ {msg}")


try:
    import marketlib as M
except Exception as e:
    print(f"  ❌ 导入失败: {e}")
    raise SystemExit(1)

check(hasattr(M, "poll_stall_alert") or hasattr(M, "stall_alert_should_fire"),
      "缺少停摆告警入口（poll_stall_alert / stall_alert_should_fire）")

if hasattr(M, "stall_alert_should_fire"):
    import datetime as _dt
    # 盘中时间
    t_in = _dt.datetime(2026, 11, 10, 10, 30)
    t_out = _dt.datetime(2026, 11, 10, 20, 30)

    # ① 盘中 + analyze 心跳很旧 → 应告警
    check(M.stall_alert_should_fire(t_in, age_s=1800, trading=True) is True,
          "盘中停摆 30 分钟却没触发告警")

    # ② 盘中 + 心跳新鲜 → 不告警
    check(M.stall_alert_should_fire(t_in, age_s=60, trading=True) is False,
          "心跳新鲜却触发了告警")

    # ③ 盘外 → 不告警（盘外本来就不该有心跳）
    check(M.stall_alert_should_fire(t_out, age_s=99999, trading=False) is False,
          "盘外也告警了 —— 会天天制造假警报")

assert bad == 0, f"{bad} 处不符合预期"
print("ALL PASS (盘中停摆告警；新鲜不告警；盘外不告警)")
