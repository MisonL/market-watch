"""生产默认预算、剩余预算透传与主动超时不熔断。无网络。"""
import atexit
import json
import os
import shutil
import sys
import tempfile
from unittest.mock import patch

root = tempfile.mkdtemp(prefix="mw_deadline_")
os.environ["MW_DIR"] = root
atexit.register(shutil.rmtree, root, ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import analyze as A
# ⚠⚠ 必须在**任何赋值之前**读真实生产默认。
# 原写法是先 `A.AI_ROUND_BUDGET = 240`、后心 `assert A.AI_ROUND_BUDGET == 240`
# —— **自证断言，恒真**。实测把生产默认改成 30 也照样绿、
# 还打印“生产默认240s”。现在拿这个快照去对比。
_PROD_BUDGET_DEFAULT = A.AI_ROUND_BUDGET

seen = []
seen_local = []
reply = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "可用正文"}]}})

def invoke(*args, **kw):
    seen.append(kw["total_limit"])
    if len(seen) == 1:
        seen_local.append(kw["total_limit"])
    return 0, reply, "", False

with patch.object(A.M, "claude_bin", return_value=("/bin/true", None)), \
     patch.object(A.M, "run_with_idle_watchdog", side_effect=invoke), \
     patch.object(A.M, "_AI_FAIL_FILE", os.path.join(root, "breaker.json")):
    A._ROUND_DATA_MS = 40_000
    A._ROUND_AI_MS[:] = [50_000]
    A.AI_ROUND_BUDGET = 250   # 夹具额度，仅为观察剩余预算透传
    assert A.ai_analyze("事实", [], "", slot="10:00") == "可用正文"
    assert seen and isinstance(seen[0], (int, float)), "生产调用没有总时长上界（AI_ROUND_BUDGET 可能被误设回 None）"
    assert seen[0] == 160.0, f"已用90秒后剩余预算应为160s: {seen}"
    A._ROUND_AI_MS[:] = [240_000]
    A.AI_ROUND_BUDGET = 240
    seen.clear()
    assert A.ai_analyze("事实", [], "", slot="10:00") == ""
    assert not seen, "预算用完仍启动下一次AI调用"
    import time
    if os.path.exists(A.M._AI_FAIL_FILE):
        with open(A.M._AI_FAIL_FILE, encoding="utf-8") as f:
            assert json.load(f).get("until", 0) <= time.time(), "我方预算耗尽被误记为通道熔断"

# 真正运行看门狗：即使不断输出，也必须终止，且保留已读内容。
code = "import time\nfor i in range(100):\n print('正文'+str(i), flush=True)\n time.sleep(.1)"
rc, out, err, timed = A.M.run_with_idle_watchdog(
    [sys.executable, "-u", "-c", code], total_limit=.5, first_byte_limit=5,
    idle_limit=5)
assert timed and "正文" in out and "[total-limit-gate " in err, (rc, out, err, timed)
assert _PROD_BUDGET_DEFAULT == 240, f"生产默认预算应为240，实际 {_PROD_BUDGET_DEFAULT!r}"
assert seen_local[0] == 160.0
print("ALL PASS (生产默认240s、剩余预算透传、耗尽不调用不熔断、活跃流硬上界保留正文)")
