"""2026-11-10「全维度审查」批次修复的行为契约。

每条都对应一个**可复现的真实缺陷**，不是风格偏好。
这里的断言只覆盖"修复本身"，各自模块的细节由各自的测试文件负责。
"""
import os
import sys
import tempfile

os.environ["MW_DIR"] = tempfile.mkdtemp(prefix="mw_rfx_")
for _d in ("state/history", "state/cache", "conf", "logs"):
    os.makedirs(os.path.join(os.environ["MW_DIR"], _d), exist_ok=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import marketlib as M

bad = 0


def check(cond, msg):
    global bad
    if not cond:
        bad += 1
        print(f"  ❌ {msg}")


# ---------- ① 主机健康键必须区分协议 ----------
_http_key = M._host_key("http://push2.eastmoney.com/api/qt/x")
_https_key = M._host_key("https://push2.eastmoney.com/api/qt/x")
check(_http_key != _https_key,
      f"HTTP/HTTPS 共用同一健康键（失败会互相株连）: {_http_key!r}")

M._mark_dead(_http_key)
check(not M._host_dead(_https_key),
      "标死 HTTP 后 HTTPS 也被判死 —— 一台协议故障株连另一种协议")
check(M._host_dead(_http_key), "标死的 HTTP 主机自己没被判死")

# PERMANENTLY_DEAD 仍按**纯主机名**判（与协议无关）
check("push2delay.eastmoney.com" in M.PERMANENTLY_DEAD,
      "PERMANENTLY_DEAD 指纹被改动（它必须与协议无关）")

# ---------- ② 主机熔断必须跨进程（落盘） ----------
_rl1 = M.RateLimiter(min_interval=0.0, daily_cap=2000)
for _ in range(M.HOST_FAIL_THRESHOLD):
    _rl1.fail("circuit-probe.example")
check(not _rl1.host_open("circuit-probe.example"),
      "达到阈值后本进程未熔断")
_rl2 = M.RateLimiter(min_interval=0.0, daily_cap=2000)
check(not _rl2.host_open("circuit-probe.example"),
      "熔断只在进程内生效（新进程立刻又能打 —— 而 launchd 每轮都起新进程）")

# ---------- ③ 代理冷却按 host 分账，且冷却期内继续计数 ----------
_h = "blackhole.example"
M._proxy_fail_reset(_h)
for _i in range(M.PROXY_FAIL_THRESHOLD):
    M._proxy_fail_bump(_h)
check(M._proxy_fail_get(_h) > 0, "达到阈值后未进入冷却")
_until_before = M._proxy_fail_get(_h)
for _j in range(3):          # 冷却期内再失败，不能把冷却抹掉
    M._proxy_fail_bump(_h)
check(M._proxy_fail_get(_h) >= _until_before,
      "冷却期内再失败把冷却**提前解除**了（until 被重置）")
check(M._proxy_fail_get("other.example") == 0.0,
      "一台主机冷却牵连了另一台（分账失效）")

# ---------- ④ clist 能回传 total（供判完整性） ----------
import inspect
_sig = inspect.signature(M.clist)
check("with_total" in _sig.parameters,
      "clist 未提供 with_total —— 调用方无法判断名单是否残缺")

# ---------- ⑤ 受限求值器不得逃逸 ----------
import selfcheck as SC
_ns = {"_last_t": 100, "_last_label": "15:00",
       "SLOT_TTL": {"15:00": 20}, "HOURLY_RETRY_GRACE": 15}
check(SC._safe_eval_arith(
    "_last_t + SLOT_TTL.get(_last_label, 20) + HOURLY_RETRY_GRACE", _ns) == 135,
    "受限求值器算错了真实表达式的值")
for _bad_expr in ("().__class__.__base__.__subclasses__()",
                  '"".format.__globals__',
                  "open('x')",
                  "_last_t.__class__"):
    try:
        SC._safe_eval_arith(_bad_expr, _ns)
        check(False, f"受限求值器未拦住逃逸: {_bad_expr}")
    except Exception:
        pass

# ---------- ⑥ webui 拒绝把无鉴权面板绑到非回环地址 ----------
_src_webui = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "src", "webui.py"), encoding="utf-8").read()
check("MW_HOST_ALLOW_REMOTE" in _src_webui,
      "webui 缺少 MW_HOST 非回环硬闸（一个环境变量即可暴露无鉴权面板）")
check("address_family = socket.AF_INET6" in _src_webui,
      "webui 未按地址形状选 IPv6 协议族（::1 绑定会 gaierror）")

# ---------- ⑦ 兜底模板剔占位行，但保留带段名的缺失说明 ----------
import analyze as A
_keep = "**盘面** 上证 3649\n**关注板块**（资金方向未提供）\n**建议**观望"
check(A._strip_placeholder_lines(_keep) == _keep,
      "把带段名的缺失说明也删了（读者会以为没有这一段）")
_drop = "**盘面** 上证 3649\n（暂无数据）\n**建议**观望"
check("（暂无数据）" not in A._strip_placeholder_lines(_drop),
      "整行只有占位标记时未剔除（占位行会被直接推送）")

assert bad == 0, f"{bad} 处不符合预期"
print("ALL PASS (协议分账/熔断跨进程/代理分账与不提前解除/clist total/"
      "受限求值/面板硬闸与IPv6/占位行剔除)")
