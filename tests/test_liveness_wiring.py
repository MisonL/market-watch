"""心跳/停摆告警的**接线**必须被测到，而不只是测纯函数。

`[审查实测]` 注入以下缺陷后 `run_all.sh` 仍全绿：

  · 删掉 `runlog.run()` 里的 `beat(script)`      → 心跳永不记录
  · 把 `run("analyze")` 拼成 `"analzye"`         → 键名错位
  · 删掉 `collect._boot()` 里的 `_maybe_stall_alert()` → 告警永不触发
  · `beat.json` 权限 0o600 → 0o644                → 权限回退

而这几条正是 11-08「45 分钟零通知」事故的防线。纯函数测试守住的是
"函数本身对不对"，守不住"有没有人调用它"。

契约（本文件逐条钉住）：
  ① runlog.run() 真的会落心跳，且文件权限 0600；
  ② collect._maybe_stall_alert() 在盘中 + 心跳陈旧时**真的发出**一次告警；
  ③ 同一天第二次调用不再发（按天去重）；
  ④ webhook 返回 errcode≠0 时不写去重（下轮可重试）；
  ⑤ 盘外不调用 curl（不制造假警报）。
"""
import json
import os
import stat
import sys
import tempfile
import time

_ROOT = tempfile.mkdtemp(prefix="mw_wire_")
os.environ["MW_DIR"] = _ROOT
# 必须先建好目录：`runlog.BEAT_FILE` 指向 `state/history/beat.json`，
# 不存在时 `json.dump(open(..., "w"))` 直接 FileNotFoundError。
for _d in ("state/history", "state/cache", "conf", "logs"):
    os.makedirs(os.path.join(_ROOT, _d), exist_ok=True)
# ⚠ 必须有 webhook，否则 `_maybe_stall_alert()` 会“没有 webhook 就静默 return” ——
# 测试会以为“接线断了”，而真正原因是配置缺失。
with open(os.path.join(_ROOT, "conf", "config.json"), "w", encoding="utf-8") as _f:
    json.dump({"webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=TEST"},
              _f)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import marketlib as M
import runlog

bad = 0


def check(cond, msg):
    global bad
    if not cond:
        bad += 1
        print(f"  ❌ {msg}")


# ---------- ① 接线：run() 真的落心跳，且 0600 ----------
runlog.run("analyze", "ok", phase="start")
try:
    _beat = json.load(open(runlog.BEAT_FILE, encoding="utf-8"))
except Exception as e:
    _beat = {}
    check(False, f"runlog.run() 之后读不到 beat.json: {e}")
check(isinstance(_beat.get("analyze"), (int, float)),
      f"runlog.run() 没有落 analyze 心跳（接线断了）: {_beat}")
_mode = stat.S_IMODE(os.stat(runlog.BEAT_FILE).st_mode) if os.path.exists(runlog.BEAT_FILE) else 0
check(_mode == 0o600, f"beat.json 权限应为 0600，实际 {oct(_mode)}")

# ---------- ②③④⑤ 停摆告警的真实发送路径 ----------
import collect

_sent = []


class _R:
    def __init__(self, out):
        self.returncode = 0
        self.stdout = out
        self.stderr = ""


def _fake_curl(args, **kw):
    if isinstance(args, list) and args and "curl" in args[0]:
        # ⚠ 2026-11-11：请求体改走 stdin（kw["input"]），argv 里不再有正文
        # —— 原先 `"".join(args)` 连 URL/正文一起拼，加固后必然查不到文案。
        # 顺带把 argv 也收进来，供「URL 不进 argv」这类断言用。
        _sent.append({"argv": args, "input": kw.get("input", "")})
        return _R('{"errcode":0,"errmsg":"ok"}')
    return _R("")


_real_run = M.subprocess.run
M.subprocess.run = _fake_curl

# 盘中 + 心跳陈旧：patch 掉时间判断
_real_trading = M.is_trading_day
_real_should = M.stall_alert_should_fire
try:
    import datetime as _dt
    # ⚠ 必须**重写**心跳：上面的 runlog.run() 已写过一条新鲜心跳，
    # 不覆盖的话 age≈0 、告警永远不会触发（测试会假红）。
    json.dump({"analyze": time.time() - 3600}, open(runlog.BEAT_FILE, "w"))

    # 用 10:30 的模拟时间（盘中窗口内）。
    # ⚠ 必须先把真类存进 `_RealDT`：否则 `_FakeDT.now()` 里的 `_dt.datetime`
    # 已被换成 `_FakeDT` 自身，无限递归。
    _RealDT = _dt.datetime
    class _FakeDT(_RealDT):
        @classmethod
        def now(cls, tz=None):
            return _RealDT(2026, 11, 10, 10, 30)
    _saved_dt = _dt.datetime
    _dt.datetime = _FakeDT
    try:
        collect._maybe_stall_alert()
    finally:
        _dt.datetime = _saved_dt

    check(len(_sent) == 1, f"盘中停摆未发出告警（接线断了）: 发出 {len(_sent)} 次")
    if _sent:
        _in = _sent[0].get("input") or b""
        if isinstance(_in, bytes):
            _in = _in.decode("utf-8", "ignore")
        _payload = str(_in)
        check("停摆" in _payload, f"告警文案不含「停摆」: {_payload[:120]}")
        _argv_j = " ".join(str(x) for x in _sent[0].get("argv") or [])
        check("https://" not in _argv_j,
              f"webhook 地址不该出现在 argv（加固失效）: {_argv_j[:120]}")

    # ③ 同一天第二次：不再发
    _n1 = len(_sent)
    _dt.datetime = _FakeDT
    try:
        collect._maybe_stall_alert()
    finally:
        _dt.datetime = _saved_dt
    check(len(_sent) == _n1, f"同一天重复发送（按天去重失效）: 又从 {_n1} 增到 {len(_sent)}")

    # ④ errcode≠0 时不写去重 → 删除去重文件后重试必须还能发
    _dedupe = M.p_cache("stall_alert_date.json")
    try:
        os.remove(_dedupe)
    except Exception:
        pass
    _sent.clear()

    def _fake_curl_err(args, **kw):
        if isinstance(args, list) and args and "curl" in args[0]:
            _sent.append(args)
            return _R('{"errcode":40001}')
        return _R("")
    M.subprocess.run = _fake_curl_err
    _dt.datetime = _FakeDT
    try:
        collect._maybe_stall_alert()
    finally:
        _dt.datetime = _saved_dt
    check(not os.path.exists(_dedupe),
          "errcode≠0 仍写了去重文件 —— 一次失败会把当天告警吞掉")

    # ⑤ 盘外：不应发
    _sent.clear()
    M.subprocess.run = _fake_curl

    class _NightDT(_RealDT):
        @classmethod
        def now(cls, tz=None):
            return _RealDT(2026, 11, 10, 22, 0)
    try:
        os.remove(_dedupe)
    except Exception:
        pass
    _dt.datetime = _NightDT
    try:
        collect._maybe_stall_alert()
    finally:
        _dt.datetime = _saved_dt
    check(len(_sent) == 0, f"盘外也发了告警（假警报）: {len(_sent)} 次")
finally:
    M.subprocess.run = _real_run
    M.is_trading_day = _real_trading

# ---------- ⑥ 接线：_boot() 必须真的调用 _maybe_stall_alert() ----------
# ⚠ 前面那五条都是**直接调用**函数，测不到“有没有人调它”。
# 把 `_boot()` 里那一行删掉，上面全部照样绿 —— 而那正是 11-08 事故的形态。
_sent.clear()
_called = []
_real_maybe = collect._maybe_stall_alert
collect._maybe_stall_alert = lambda: _called.append(1)
_real_main = collect.main
collect.main = lambda: "ok"
_real_harden = M.harden_logs
_real_rotate = M.rotate_logs
M.harden_logs = lambda *a, **k: None
M.rotate_logs = lambda *a, **k: None
try:
    collect._boot()
finally:
    collect._maybe_stall_alert = _real_maybe
    collect.main = _real_main
    M.harden_logs = _real_harden
    M.rotate_logs = _real_rotate
check(len(_called) == 1,
      f"_boot() 没有调用 _maybe_stall_alert()（告警接线断了）: {len(_called)}")

assert bad == 0, f"{bad} 处不符合预期"
print("ALL PASS (心跳接线/0600；停摆真实发送；按天去重；errcode≠0 不吞；盘外不发；_boot 接线)")
