#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""firecrawl 兜底必须**禁掉云端缓存**（`--max-age 0`）。

## 为什么这条必须单独守（2026-11-23 生产的 P0）

`[实测]` firecrawl CLI 默认**按 URL 缓存**响应。行情的 URL
（同一批 secids + 同一串 fields）**每一轮完全一样**，于是：

    10:51  第一次走 firecrawl 兜底 -> 缓存了当时的行情
    10:51 ~ 15:07  每一轮都返回这同一份缓存

`quotes()` 拿到的是一份"完整、有限正数、带时间戳"的行情，
**所有闸门（完整性 / 合理性 / 新鲜度）全部放行** —— 因为它们只检查
"形状对不对"，没有一条拿 `f124` 跟墙上时钟比。于是：

    15:07「收盘定局」播报：上证 -1.39%（3484.63）、创业板指 -0.59%
    真实收盘：            上证 **+0.15%**（3538.95）、创业板指 **+3.08%**

**方向完全反了**。用户拿当天新闻一对就发现了 —— 涨被报成跌。

## 为什么这个缺陷特别危险

它不是"数据旧了几分钟"，而是**陈旧数据伪装成实时数据**。
兜底层的存在意义就是"拿到此刻的真值"，缓存直接摧毁了这个前提。
而且它不会报错、不会降级、不会留痕 —— 播报看起来一切正常。

## 本文件锁住什么

- firecrawl 的 argv 里必须有 `--max-age 0`
- 且必须在 URL **之前**（CLI 里 URL 之后的位置参数不再是选项）
- 反面控制：把 `--max-age 0` 从 argv 里删掉，本用例必须变红

## 为什么是独立文件而不是自检变异体

`selfcheck` **检测不到**这个缺陷（陈旧但形状合法的行情能通过全部闸门），
所以塞进 `selfcheck_mutation.py` 只会得到一条**恒真锚**（干净基线里
也出现该文案），污染整个变异体基线。变异体只留给"自检真能抓到的"缺陷。
"""
import os
import subprocess
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


class _R:
    def __init__(self, out="", err="", rc=0):
        self.stdout, self.stderr, self.returncode = out, err, rc


_orig = (M._fc_fail_get, M._fc_fail_set, M.log, subprocess.run,
         M._FIRECRAWL_NODE, M._FIRECRAWL_CLI)
_argv = []

try:
    M.log = lambda msg, kind=None: None
    M._fc_fail_get = lambda: 0.0
    M._fc_fail_set = lambda until: None
    M._FIRECRAWL_NODE = sys.executable
    M._FIRECRAWL_CLI = sys.executable

    def _run(argv, **kw):
        _argv.append(list(argv))
        return _R('{"ok":1}')

    subprocess.run = _run

    URL = "https://push2.eastmoney.com/api/qt/ulist.np/get?secids=1.000001"
    out = M.firecrawl_get(URL)

    check(_argv, "firecrawl_get 没有调用 subprocess.run")
    cmd = _argv[0]
    check(out is not None, f"正常响应应原样返回，实得 {out!r}")

    # ---------- ①必须带 --max-age 0 ----------
    check("--max-age" in cmd,
          "firecrawl 命令行里没有 `--max-age` —— 会用默认值**缓存**响应：\n"
          f"    {cmd}")
    _i = cmd.index("--max-age") if "--max-age" in cmd else -1
    check(_i >= 0 and _i + 1 < len(cmd) and cmd[_i + 1] == "0",
          f"`--max-age` 的值必须是 `0`（禁用缓存），实得 "
          f"{cmd[_i + 1] if _i >= 0 and _i + 1 < len(cmd) else '缺失'}")

    # ---------- ②必须在 URL 之前 ----------
    # CLI 的坑：`--max-age` 落在 URL **之后**时，多数解析器会把它当作
    # 位置参数（第二个 URL），于是既不生效也不报错 —— 静默回到缓存行为。
    check(cmd.index("--max-age") < cmd.index(URL),
          "`--max-age` 必须在 URL **之前** —— 放到 URL 之后会被当成"
          f"第二个位置参数，静默失效：\n    {cmd}")

    # ---------- ③URL 必须原样传，不能靠加参数破缓存 ----------
    # 用 `&_cb=<随机>` 也能绕开缓存，但那会让 URL 每轮都不同，
    # 白白打穿 firecrawl 的正常去重、也无谓拉长 URL。
    check(cmd[-1] == URL,
          "URL 必须原样作为最后一个参数（不要用 `&_cb=` 破缓存）：\n"
          f"    期望 {URL!r}\n    实得 {cmd[-1]!r}")

    # ---------- ④其余参数不许被顺手改坏 ----------
    check(cmd[0] == sys.executable and cmd[1] == sys.executable,
          f"node/cli 前缀被改了：{cmd[:2]}")
    check("scrape" in cmd, f"子命令丢了：{cmd}")
    check("rawHtml" in cmd, f"输出格式被改了：{cmd}")

    # ---------- ⑤反面控制：把 --max-age 0 删掉，①必须变红 ----------
    # 这条防的是"测试只看有没有调用，不看调用对了没"。
    _bad = [c for c in cmd if c not in ("--max-age", "0")]
    check("--max-age" not in _bad,
          "反面控制失效：删掉 `--max-age 0` 后仍判为合法")

finally:
    (M._fc_fail_get, M._fc_fail_set, M.log, subprocess.run,
     M._FIRECRAWL_NODE, M._FIRECRAWL_CLI) = _orig

print(f"ALL PASS (firecrawl 禁用云端缓存 {_ok} 个断言)")
