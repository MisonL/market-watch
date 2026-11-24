#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""firecrawl 网关 5xx 不能按 900s 冷却 —— 一次抖动毁掉整个上午的板块数据。

## 2026-11-24 实测

09:35 起 firecrawl 开始间歇返回它**自己的** 502 页（注意
`charset=windows-1252`、`<title>502 Bad Gateway</title>` —— 是 firecrawl
的网关，不是东财的错误页）。代码走的是通用失败分支 → 武装 900s 冷却 →
此后每次 clist 调用都在 0.0s 内空回。

代价，从 `logs/watch.log` 量出来的：

```
全天 7 次 502，每次 900s  →  累计约 105 分钟冷却
上午交易时段约 240 分钟   →  近一半时间没有任何板块数据
因冷却被直接放弃的 clist 页：84 次
```

而同一时段我用同一个 URL 连打 **23 次**（含 11 次无间隔突发），
**全部成功**。502 就是几秒级的抖动，900s 是严重过度惩罚。

## 为什么这与「限流」是两个入口

2026-11-23 已经修过一次同类错误（限流 → 60s）。那次的教训写在常量上方，
但**只覆盖了"stdout 为空 + stderr 含 Rate limit"**这一条路径。
网关 5xx 走的是另一条：stdout 非空、内容是 HTML 错误页、rc 可能是 0。
所以它不是"没改"，是**当时没被想到的那个入口**。

## 本文件锁住什么

① 识别式认得出真实的 502/503/504（含 504 `Gateway Time-out` —— 第一版
   词元类漏了连字符，只认 502/503，是反例跑红才发现的）
② 识别式**不误认**业务错误页（`{"error":"no data"}`、中文错误页、
   `<title>数据不存在</title>`）—— 那些是真故障，套短冷却会让每轮白打请求
③ 网关冷却必须**远短于**通用冷却（120s vs 900s），否则等于没分档
④ 接线：`firecrawl_get` 真的调了识别式，且用的是**未剥壳原文**
   （剥壳只认完整的 `<html>…<body>…</body></html>`，502 页骨架常不完整）
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

import marketlib as M

_ok = 0


def check(cond, msg):
    global _ok
    if cond:
        _ok += 1
    else:
        print(f"  ❌ {msg}")
        sys.exit(1)


# ---------- ① 认得出 ----------
_REAL_502 = ('<html><head><meta http-equiv="Content-Type" content="text/html; '
             'charset=windows-1252"><title>502 Bad Gateway</title></he')
for _txt, _name in (
    (_REAL_502, "生产真实 502 页（从 watch.log 原样取回）"),
    ("<title>503 Service Temporarily Unavailable</title>", "503"),
    ("<title>504 Gateway Time-out</title>", "504"),
    ("HTTP/1.1 502 Bad Gateway\r\n", "状态行形态"),
    ("<h1>502 Bad Gateway</h1>", "h1 形态"),
    ("<h1>Bad Gateway</h1>", "h1 无状态码"),
):
    check(bool(M._FIRECRAWL_GW_RE.search(_txt)),
          f"认不出网关错误：{_name}")

# ---------- ② 不误认 ----------
for _txt, _name in (
    ('{"rc":0,"data":{"diff":[]}}', "正常空 JSON"),
    ('{"error":"no data"}', "业务错误 JSON"),
    ('<html><body>参数错误</body></html>', "中文错误页"),
    ('<title>数据不存在</title>', "中文 title"),
    ('v_sh000001="1~上证指数~3555.49~"', "正常行情文本"),
    ('{"data":{"total":554,"diff":[]}}', "正常 clist 响应"),
):
    check(not M._FIRECRAWL_GW_RE.search(_txt),
          f"误认成网关抖动（会套短冷却、每轮白打）：{_name}")

# ---------- ③ 分档必须真的分开 ----------
check(hasattr(M, "FIRECRAWL_GATEWAY_COOLDOWN"),
      "缺少 FIRECRAWL_GATEWAY_COOLDOWN —— 网关抖动又回到 900s 冷却")
_gw = M.FIRECRAWL_GATEWAY_COOLDOWN
_gen = M.FIRECRAWL_COOLDOWN
check(_gw < _gen / 3,
      f"网关冷却 {_gw}s 没有显著短于通用 {_gen}s —— 分档等于没做"
      f"（今日 7 次 502 会吃掉 105 分钟，上午只有 240 分钟）")
check(_gw >= 30,
      f"网关冷却 {_gw}s 太短：连火线重试会在抖动窗口内反复白打")
check(_gw <= 300,
      f"网关冷却 {_gw}s 仍偏长：一次抖动会漏掉 2~3 个调度周期")

# ---------- ③c 重试间隔必须有上界 ----------
# ⚠️ 行为段把间隔临时设成 0 秒（否则要真睡），所以**间隔本身的合理性
# 必须另有静态断言** —— 否则把它改成 60s，行为段照样全绿。
_d = getattr(M, "FIRECRAWL_GW_RETRY_DELAY", None)
check(_d is not None,
      "缺少 FIRECRAWL_GW_RETRY_DELAY —— 网关重试没定义等待时长")
check(0 < _d <= 15,
      f"网关重试间隔 {_d}s 不合理：抖动通常几秒自愈，间隔过长等于让"
      f"一次扫描被单个坏页拖垮（19 页各拖 {_d}s 会撑爆 DATA_BUDGET=120s）")
check(_d < M.FIRECRAWL_GATEWAY_COOLDOWN,
      f"重试间隔 {_d}s 不小于冷却 {M.FIRECRAWL_GATEWAY_COOLDOWN}s —— "
      f"那不如直接放弃，重试没有意义")

# ---------- ④ 接线 ----------
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "src", "marketlib.py"), encoding="utf-8").read()
check("_FIRECRAWL_GW_RE.search(_raw)" in _src,
      "`firecrawl_get` 没有调用网关识别式 —— 写了不用等于没有")
check("_raw = out" in _src,
      "没有保留未剥壳原文 `_raw`：剥壳只认完整的 "
      "`<html>…<body>…</body></html>`，502 页骨架常不完整，"
      "用剥后的文本匹配会漏")
check("_fc_fail_set(time.time() + FIRECRAWL_GATEWAY_COOLDOWN)" in _src,
      "识别到网关 5xx 后没有武装**短**冷却 —— 等于识别了不处置")

# ---------- ⑤ 行为：真的走短冷却 ----------
#
# ⚠️⚠️ 这一段必须把**可用性闸门**也桩掉，否则在 CI 上恒真地"通过失败"。
#
# 第一版只桩了 `subprocess.run`，本地全绿、CI 却红：
#     ❌ 冷却被设成 -1795492853s，不是网关档的 120s
# 负值意味着 `_fc_fail_get()` 返回 0 —— 冷却根本没被写。
# 原因是 `firecrawl_get` 开头还有一道闸门：
#     if not (os.path.exists(_FIRECRAWL_NODE)
#             and os.path.exists(_FIRECRAWL_CLI)):
#         return None
# CI 的 runner 上没有 node、也没有 firecrawl CLI，于是**在到达分类逻辑之前
# 就返回了**，`_fc_fail_set` 一次都没调用。
#
# 这不是被测代码的缺陷，是**测试依赖了环境**（本仓库今天第三次栽在这个
# 形态上：agent 用例依赖 agent/ 目录、性能用例依赖 GC 抖动）。修法是把
# 这两个路径指向一个必然存在的文件，让流程真正走到分类分支 ——
# 我们要验的是"分类对不对"，不是"这台机器装没装 firecrawl"。
import tempfile
_mark = os.path.join(tempfile.mkdtemp(), "fc.json")
_orig_file = M._FC_FAIL_FILE
_orig_node, _orig_cli = M._FIRECRAWL_NODE, M._FIRECRAWL_CLI
try:
    M._FC_FAIL_FILE = _mark
    # 让可用性闸门放行（`sys.executable` 必然存在）
    M._FIRECRAWL_NODE = sys.executable
    M._FIRECRAWL_CLI = sys.executable
    M._fc_fail_set(0)                      # 清冷却
    check(M._fc_fail_get() == 0, "前置：冷却未清空")

    class _R:
        returncode = 0
        stdout = _REAL_502
        stderr = ""

    # ⚠️ 2026-11-24：网关 5xx 现在**就地重试一次**（不立刻武装冷却）。
    # 所以桩必须"每次都返 502"才能走到武装冷却那一步。
    _real_run = M.subprocess.run
    _calls = []

    def _run(*a, **k):
        _calls.append(1)
        return _R()
    M.subprocess.run = _run
    _dl0 = M.FIRECRAWL_GW_RETRY_DELAY
    M.FIRECRAWL_GW_RETRY_DELAY = 0.0      # 别真睡 8 秒
    try:
        _got = M.firecrawl_get("https://example.invalid/probe")
    finally:
        M.subprocess.run = _real_run
        M.FIRECRAWL_GW_RETRY_DELAY = _dl0
    check(_got is None, "网关 5xx 应返回 None（不能把错误页当数据交出去）")
    # ---------- ⑥ 就地重试：先试 2 次再认账 ----------
    check(len(_calls) == 2,
          f"网关 5xx 只发起了 {len(_calls)} 次请求，应重试 1 次（共 2 次）—— "
          f"一次几秒的抖动会直接让本轮剩余分页全部作废"
          f"（实测 13:35:04 就是这样丢掉整轮板块数据的）")
    import time as _t
    _until = M._fc_fail_get()
    check(_until > 0,
          "冷却一次都没被武装 —— 流程八成在可用性闸门就返回了，"
          "本段断言没测到分类逻辑")
    _left = _until - _t.time()
    check(0 < _left <= M.FIRECRAWL_GATEWAY_COOLDOWN + 5,
          f"冷却被设成 {_left:.0f}s，不是网关档的 "
          f"{M.FIRECRAWL_GATEWAY_COOLDOWN}s —— 又按通用 900s 处置了")
finally:
    M._FC_FAIL_FILE = _orig_file
    M._FIRECRAWL_NODE, M._FIRECRAWL_CLI = _orig_node, _orig_cli
    try:
        os.unlink(_mark)
    except OSError:
        pass

print(f"ALL PASS (firecrawl 网关 5xx 分档 {_ok} 个断言)")
