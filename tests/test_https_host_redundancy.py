"""板块源只走 HTTPS 主机；`push2test` 挂掉时必须有第二个 HTTPS 候选。

`[生产实测]` 2026-11-10 09:57 起 `push2test.eastmoney.com` 全部端点
**不可达**（curl 返回 000），而它是唯一能提供板块序列的 HTTPS 主机候选：

    HTTPS 候选 = ['push2test.eastmoney.com'(挂了),
                  'push2delay.eastmoney.com'(在 PERMANENTLY_DEAD 里)]

于是每轮都是「板块 0/12 → 全源失效 → 保留旧快照」，10:04 那条推送里
8 个板块全是 `+0.00%（暂无资金数据）`。

根因是 `_proto_label` 那段注释：「push2.eastmoney.com 只有明文可用
（HTTPS 全端点 rc=52）」—— 实测该结论**已过期**，HTTPS 三个端点
（ulist.np / clist / stock-kline）全部 200，板块 f2 与明文逐位一致。

契约：`push2.eastmoney.com` 必须出现在 **HTTPS** 候选里，
否则唯一板块源一挂就整段失明。
"""
import os
import sys
import tempfile

_old = os.environ.get("MW_DIR")
os.environ["MW_DIR"] = tempfile.mkdtemp(prefix="mw_host_")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import marketlib as M

bad = 0


def check(cond, msg):
    global bad
    if not cond:
        bad += 1
        print(f"  ❌ {msg}")


https_hosts = [h for h in M._ordered_em_hosts() if h.startswith("https://")]
names = [M._host_of(h) for h in https_hosts]
print(f"  HTTPS 候选: {names}")

# ① push2 必须能作为 HTTPS 候选提供板块（板块只认 HTTPS）
check("push2.eastmoney.com" in names,
      f"push2.eastmoney.com 不在 HTTPS 候选里 —— 唯一板块源 push2test 一挂就整段失明")

# ② 至少要有一个 HTTPS 候选不在永久死亡名单里（否则板块永远拿不到）
alive_https = [n for n in names if n not in M.PERMANENTLY_DEAD]
check(bool(alive_https),
      f"所有 HTTPS 候选都在 PERMANENTLY_DEAD 里: {names}")

# ③ 板块代码不得被 HTTP 主机过滤掉（既有不变量）
codes = ["90.BK1030", "1.000001"]
for h in M._ordered_em_hosts():
    got = M._codes_for_host(h, codes)
    if h.startswith("https://"):
        check("90.BK1030" in got, f"HTTPS 主机 {h} 丢了板块代码")
    else:
        check("90.BK1030" not in got, f"HTTP 主机 {h} 不该请求板块代码（基点不同）")

assert bad == 0, f"{bad} 处不符合预期"
print("ALL PASS (push2 可作 HTTPS 板块候选；板块只走 HTTPS；HTTP 仍排除板块)")
