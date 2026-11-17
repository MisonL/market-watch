"""AI 本地启动失败（Popen 抛异常）是**瞬时**故障，必须重试一次再放弃。

`[生产实测]` 2026-11-10 11:32:02，claude 正在被自动更新（包目录
11:31 刚重建），那一刻：

    Popen 失败: [Errno 8] Exec format error: '.../bin/claude'

而同一路径在 11:32:30 就能正常执行 `--version`（2.1.322）。也就是说这是一个
**几十秒的窗口**，却让那一轮直接 `return ""` 退兜底模板 ——
`analyze.py:4276-4278` 的启动失败分支只记日志、**不重试**。

对比：空输出、卡死都有各自的重试/熔断路径，唯独"本地起不来"这条
（进程还没跑起来）零重试。

契约：启动失败后短暂等待再试一次；成功则正常返回正文。
不写熔断（这不是通道故障），重试仍失败才放弃。
"""
import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import patch

_old = os.environ.get("MW_DIR")
os.environ["MW_DIR"] = tempfile.mkdtemp(prefix="mw_exec_")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import analyze as A

_OK = json.dumps({"type": "assistant",
                  "message": {"content": [{"type": "text", "text": "可用正文OK"}]}})

bad = 0


def check(cond, msg):
    global bad
    if not cond:
        bad += 1
        print(f"  ❌ {msg}")


calls = {"n": 0}


def _wd(*a, **k):
    """第一次模拟 Popen 起不来，第二次正常。"""
    calls["n"] += 1
    if calls["n"] == 1:
        # 与 marketlib 真实包装一致：Popen 异常 -> (None, "", "Popen 失败: ...", False)
        return (None, "", "Popen 失败: [Errno 8] Exec format error: "
                          "'/Users/x/.nvm/versions/node/v24.22.1/bin/claude'", False)
    return (0, _OK + "\n", "", False)


with patch.object(A.M, "claude_bin", return_value=("/bin/true", None)), \
     patch.object(A.M, "run_with_idle_watchdog", side_effect=_wd), \
     patch.object(A.M, "_AI_FAIL_FILE", os.path.join(os.environ["MW_DIR"], "f.json")):
    A._ROUND_AI_MS[:] = []
    out = A.ai_analyze("事实", [], "", slot="11:30")
    check(calls["n"] >= 2, f"启动失败后没有重试（只调用了 {calls['n']} 次）")
    check("可用正文OK" in out, f"重试成功却没拿到正文，实际 {out!r}")
    check(not os.path.exists(A.M._AI_FAIL_FILE), "启动失败被写成了通道熔断")

# 连续失败 → 放弃但不熔断
calls["n"] = 0
with patch.object(A.M, "claude_bin", return_value=("/bin/true", None)), \
     patch.object(A.M, "run_with_idle_watchdog",
                  return_value=(None, "", "Popen 失败: [Errno 8] Exec format error", False)), \
     patch.object(A.M, "_AI_FAIL_FILE", os.path.join(os.environ["MW_DIR"], "f2.json")):
    A._ROUND_AI_MS[:] = []
    out2 = A.ai_analyze("事实", [], "", slot="11:30")
    check(out2 == "", f"一直起不来应返回空串，实际 {out2!r}")
    check(not os.path.exists(A.M._AI_FAIL_FILE), "持续启动失败被写成了通道熔断")


assert bad == 0, f"{bad} 处不符合预期"
print("ALL PASS (启动失败会重试；成功取到正文；不误写熔断)")
