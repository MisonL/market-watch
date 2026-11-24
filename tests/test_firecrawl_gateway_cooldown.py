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
import tempfile
_mark = os.path.join(tempfile.mkdtemp(), "fc.json")
_orig_file = M._FC_FAIL_FILE
try:
    M._FC_FAIL_FILE = _mark
    M._fc_fail_set(0)                      # 清冷却
    check(M._fc_fail_get() == 0, "前置：冷却未清空")

    class _R:
        returncode = 0
        stdout = _REAL_502
        stderr = ""

    _real_run = M.subprocess.run
    M.subprocess.run = lambda *a, **k: _R()
    try:
        _got = M.firecrawl_get("https://example.invalid/probe")
    finally:
        M.subprocess.run = _real_run
    check(_got is None, "网关 5xx 应返回 None（不能把错误页当数据交出去）")
    import time as _t
    _until = M._fc_fail_get()
    _left = _until - _t.time()
    check(0 < _left <= M.FIRECRAWL_GATEWAY_COOLDOWN + 5,
          f"冷却被设成 {_left:.0f}s，不是网关档的 "
          f"{M.FIRECRAWL_GATEWAY_COOLDOWN}s —— 又按通用 900s 处置了")
finally:
    M._FC_FAIL_FILE = _orig_file
    try:
        os.unlink(_mark)
    except OSError:
        pass

print(f"ALL PASS (firecrawl 网关 5xx 分档 {_ok} 个断言)")
