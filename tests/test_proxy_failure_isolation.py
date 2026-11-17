"""代理「失败」不能由**目标站点**的问题株连。

`[生产实测]` 2026-11-10 10:50:27：`push2test.eastmoney.com` 上游挂了，
经代理访问时 TLS 握手被**对端**断开（curl `SSL_ERROR_SYSCALL`），
`proxy_get` 拿到空响应 → 写入 `proxy_fail.json` → **冷却 600 秒**。

问题在于：代理本身完全健康（同期 `curl -x proxy https://example.com`
连续 200，0.4-0.6s）。而 `_proxy_fail_set` 是**全局的** —— 一次目标站点
故障会连带封掉代理这条兜底链，接下来 10 分钟内：

    · 所有 `cj()` 的代理兜底失效（只剩 firecrawl，慢 12 倍且耗额度）
    · 腾讯日K 被风控时的代理绕行失效

契约：一次空响应不足以判定代理故障；必须**连续**失败才冷却，
且成功一次即清零。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

_old = os.environ.get("MW_DIR")
os.environ["MW_DIR"] = tempfile.mkdtemp(prefix="mw_proxy_")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import marketlib as M

bad = 0


def check(cond, msg):
    global bad
    if not cond:
        bad += 1
        print(f"  ❌ {msg}")


def _clear_all():
    """清空全部主机级冷却记录（替代已删除的全局 `_proxy_fail_set(0)`）。"""
    try:
        import os as _os
        _f = M._PROXY_FAIL_FILE
        if _os.path.exists(_f):
            _os.remove(_f)
    except Exception:
        pass


def _run(stdout=b"", returncode=0):
    return subprocess.CompletedProcess(args=["curl"], returncode=returncode,
                                       stdout=stdout, stderr=b"")


_SAVED = (M.PROXY, M.proxy_get.__globals__["_PROXY_FAIL_FILE"])

try:
    M.PROXY = "http://127.0.0.1:7890"
    # 清掉冷却
    _clear_all()

    # ① 单次空响应（目标站点挂了）→ 不应凗却代理
    with patch.object(M.subprocess, "run", return_value=_run(b"")):
        M.proxy_get("https://push2test.eastmoney.com/api/qt/x")
    check(time.time() >= M._proxy_fail_get("push2test.eastmoney.com"),
          "一次空响应就把代理冷却了 —— 目标站点故障株连了健康代理")

    # ② 连续失败到阈值之后才冷却。
    # ⚠ 先把该 host 的计数清零（① 已累积 1 次），再完整失败 阈值 次。
    M._proxy_fail_reset("push2test.eastmoney.com")
    for _ in range(getattr(M, "PROXY_FAIL_THRESHOLD", 3)):
        with patch.object(M.subprocess, "run", return_value=_run(b"")):
            M.proxy_get("https://push2test.eastmoney.com/api/qt/x")
    check(time.time() < M._proxy_fail_get("push2test.eastmoney.com"),
          "连续失败达到阈值后仍未冷却 —— 真故障不会被隔离")

    # ③ 一次成功就清零，恢复正常使用
    _clear_all()
    with patch.object(M.subprocess, "run", return_value=_run(b'{"ok":1}')):
        out = M.proxy_get("https://example.com/")
    check(out == '{"ok":1}', f"正常响应没被返回: {out!r}")
    check(time.time() >= M._proxy_fail_get("example.com"), "成功之后仍处于冷却")
finally:
    M.PROXY = _SAVED[0]
    _clear_all()

assert bad == 0, f"{bad} 处不符合预期"
print("ALL PASS (单次空响应不株连代理；连续失败才冷却；成功即清零)")
