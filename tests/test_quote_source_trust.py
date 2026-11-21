#!/usr/bin/env python3
"""实时行情**绝不能**来自 `EM_TEST`（push2test）—— 2026-11-21，P0 数据正确性。

## 事故形态

`EM_HOSTS` 的注释说 EM 与 EM_TEST「等价、区别只在域名」。对**接口形态**
成立，对**数据内容**完全不成立 —— push2test 是**测试环境**，返回的是
模拟行情。`[实测]` 2026-11-21（EM 三台主机被出口 IP 封禁、只剩 push2test
通过的那个窗口）：

    push2test（直连）    f2=3701.71  f3=+4.75     ← 伪造
    push2（经 firecrawl） f2=3533.79  f3=-1.23     ← 与腾讯/新浪逐位一致
    quotes() 实际返回    3693.14 / 4221.32 / 2475.58  （三大指数全部错）

`_valid_quotes()` 拦不住（都是有限正数），新鲜度闸门也拦不住（时间戳是新的）。
这类错误只能靠**跨源比对**发现，而一旦推给用户就是直接的误导。

## 本文件锁两件事

  ① `EM_HOSTS` 仍要**保留** push2test —— `clist`（板块列表）只有它能取，
     删了会让板块整段失明。
  ② `quotes()` 的实时行情候选池**必须剔除**它；真主机全不可用时应落到
     `feeds.backup_quotes`（腾讯/新浪），而不是退回假源。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import marketlib as M                                  # noqa: E402

_ok = [0]


def check(cond, msg):
    if cond:
        _ok[0] += 1
    else:
        print(f"  ❌ {msg}")
        raise SystemExit(1)


_EM_TEST_HOST = M._host_of(M.EM_TEST)

# ---------- ① clist 的候选仍必须含 push2test ----------
check(_EM_TEST_HOST in [M._host_of(h) for h in M.EM_HOSTS],
      "EM_HOSTS 里没有 push2test —— clist（板块列表）只有它能取，"
      "删了会让板块整段失明")

# ---------- ② quotes() 的候选池必须排除它 ----------
_real = [M._host_of(h) for h in M._ordered_em_hosts()]
check(_EM_TEST_HOST in _real,
      "前提：_ordered_em_hosts() 本来含 push2test（它只是候选池之一）")

# 行为级：让 push2test 是**唯一**能"取到数据"的主机，且它返回伪造值；
# 真实主机全部失败。此时 quotes() 绝不能再把伪造值当成行情。
FAKE = {"f2": 9999.9, "f3": 88.88}
_seen = []


def _fake_fetch(host, ids, fields):
    """只有 push2test 返回伪造值，其余主机失败。"""
    if M._host_of(host) == _EM_TEST_HOST:
        _seen.append(M._host_of(host))
        return {i: dict(FAKE, _src="em_test") for i in ids}
    return {}


_sv = {"fetch": M._fetch, "ordered": M._ordered_em_hosts,
       "hk": M._hk_quotes_via_tencent, "log": M.log,
       "dlo": M._daily_log_once}
import feeds as _fd                                  # noqa: E402
_sv["backup"] = _fd.backup_quotes

try:
    M._fetch = _fake_fetch
    M._ordered_em_hosts = lambda: [M.EM, M.EM_TEST]
    M._hk_quotes_via_tencent = lambda secids: {}
    M.log = lambda m, lv=None: None
    M._daily_log_once = lambda *a, **k: None
    _fd.backup_quotes = lambda codes: {}      # 备用源也空，避免真实网络
    M.set_data_deadline(None)
    out = M.quotes(["1.000001"])
    vals = [v.get("f2") for v in (out or {}).values()]
    check(FAKE["f2"] not in vals,
          f"quotes() 采用了 push2test 的伪造行情 {FAKE['f2']} —— "
          f"该源是测试环境，值不可信（实测 3701.71 vs 真值 3533.79）")
    check(_EM_TEST_HOST not in _seen,
          f"quotes() 竟然向 push2test 发起了实时行情请求（{_seen}）—— "
          f"即使最后丢弃，也是白打一个必然给出假数据的包")
finally:
    M._fetch = _sv["fetch"]
    M._ordered_em_hosts = _sv["ordered"]
    M._hk_quotes_via_tencent = _sv["hk"]
    M.log = _sv["log"]
    M._daily_log_once = _sv["dlo"]
    _fd.backup_quotes = _sv["backup"]
    M.set_data_deadline(None)

# ---------- ③ 真主机全灭时必须落到备用源（腾讯/新浪），而非假源 ----------
# 这条用真实网络（腾讯/新浪），验证"降级但仍然正确"。
try:
    _sv2 = {"cj": M.cj, "log": M.log}
    M.cj = lambda url, **k: None
    M.log = lambda m, lv=None: None
    M.set_data_deadline(None)
    out = M.quotes(["1.000001"])
    vals = [v.get("f2") for v in (out or {}).values()]
    check(FAKE["f2"] not in vals, "全主机失败时又出现了伪造值")
    check(any(isinstance(v, float) and 1000 < v < 10000 for v in vals),
          f"全主机失败时应从备用源（腾讯/新浪）取到上证量级的点位，"
          f"实得 {vals}")
finally:
    M.cj = _sv2["cj"]
    M.log = _sv2["log"]
    M.set_data_deadline(None)

print(f"ALL PASS (实时行情禁用测试源 push2test {_ok[0]} 个断言)")