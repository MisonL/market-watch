"""调研 Agent 摘要质量告警（2026-11-04 新增，配套 prompt 4 段契约）。

背景：用户 2026-11-04 反馈调研 Agent 推送「不够专业、实用性/参考性低」——
   11-04 那条只有三段复述（盘面数字 / plan 改动 / 触发线），没有任何
   驱动证据、没有验证昨日判断、没有带时间的明日事件。对比 10-28/10-29
   两条质量明显更高的推送，差距恰好都在这三处。

契约（`_summary_warn`，**只告警不抑制**）：
  ① 枚举段 < 3 → 告警；
  ② 无「明日/观察」段 → 告警；
  ③ 出现「资金回补/情绪修复/谨慎乐观」等空话且全文无任何证据来源 → 告警；
  ④ 无「昨日/上一交易日」验证痕迹 → 告警；
  ⑤ 明日观察段没有任何时间锚（HH:MM / 周X / 月日）→ 告警。
另：这些告警**不得**改变 `_prepare` 的 (文本, 抑制原因) 契约 —— 否则
会把「质量偏薄」升级成「当天没有播报」，与既有闸门的取舍相反。

⚠️ 本文件需要 `claude_agent_sdk`（模块级 import）。用 agent 自带 venv 跑：
     cd agent && MW_DIR=$(mktemp -d /tmp/mw_t_agent_XXXX) .venv/bin/python ../tests/test_agent_summary_quality.py
   用 /usr/local/bin/python3 跑时会自动 SKIP（不能把整个 tests 目录跑红）。
"""
import os, sys, tempfile, shutil, atexit

try:
    import claude_agent_sdk  # noqa: F401
except Exception:
    print("SKIP: 当前解释器没有 claude_agent_sdk（请用 agent/.venv/bin/python 跑）")
    sys.exit(0)

_d = tempfile.mkdtemp(prefix="mw_t_")
os.environ["MW_DIR"] = _d
atexit.register(shutil.rmtree, _d, ignore_errors=True)
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "agent", "src"))
import mw_agent.__main__ as A

_GOOD = (
    "①盘面与关键位：上证 3686.17（+2.16%）、科创50 1265.47（+9.92%），"
    "恒科盘中 3936.02（+1.77%），距上沿 3995 约 -1.5%。\n"
    "②驱动与证据：据公告，半导体设备三季报净利 +59.5%（已证实）；"
    "主力净流入 90 亿为推断，缺分时数据。反证：白酒逆势跌 0.8%。\n"
    "③昨日判断验证：昨日观察点「恒科守 3737」已验证（盘中低 3764）；"
    "触发线 3995/3737 维持不动。\n"
    "④明日检查清单：周四 21:30 美国初请失业金（来源：美劳工部）；"
    "恒科站上 3949 → 缺口回补确认；失守 3737 → hstech_down 触发、判断作废。"
)
_BAD = "①盘面：涨了。②资金回补、情绪修复。③无。明日关注市场情绪。"

_w_good = A._summary_warn(_GOOD)
assert _w_good == [], f"合格摘要不应告警，实际 {_w_good}"
print("PASS 1 good summary -> no warnings")

_w_bad = A._summary_warn(_BAD)
assert any("空话" in x for x in _w_bad), _w_bad
assert any("昨日" in x for x in _w_bad), _w_bad
assert any("时间锚" in x for x in _w_bad), _w_bad
print("PASS 2 bad summary -> warnings:", len(_w_bad))

# 空话**带**证据来源时不再报（避免把正常写法误报）
_mid = ("①盘面：上证 3686.17。②据财联社，半导体主力净流入 90 亿；"
        "另称资金回补（推断）。③昨日判断兑现。明日周四 21:30 数据。")
assert not any("空话" in x for x in A._summary_warn(_mid)), A._summary_warn(_mid)
print("PASS 3 vague phrase with evidence is not flagged")

# 契约不变：_prepare 仍返回二元组；质量合格的正文不被抑制
_txt, _why = A._prepare(_GOOD)
assert _why == "", f"合格摘要不应被抑制：{_why!r}"
assert _txt.strip()
print("PASS 4 _prepare contract unchanged")

# --- 2026-11-05 加固：证据正则不再被单个「据」字恒真欺骗 ---
# 原判据含裸「据」→ 命中「数据」的「据」→ _has_evidence 恒真，
# 「空话」告警从上线起从未触发过（bug）。下面两条必须都成立：
_nodata = ("①盘面：上证 3686.17。②资金回补、情绪修复，数据转好。"
           "③无。明日看盘。")
assert any("空话" in x for x in A._summary_warn(_nodata)), \
    "含「数据」二字但无来源的空话必须仍被告警（裸「据」恒真 bug 的回归）"
print("PASS 5 bare-'据'-in-'数据' no longer counts as evidence")

# 否定语境豁免：「未现情绪修复」是可核对的事实描述，不该告警
_neg = ("①盘面：上证 3686.17。②盘面未现资金回补，也并非情绪修复。"
        "③昨日观察点已验证。明日周四 21:30 数据。")
assert not any("空话" in x for x in A._summary_warn(_neg)), \
    "否定语境的空话词不该被告警（避免惩罚严谨写法）"
print("PASS 6 negated vague phrase exempt")

# 结构判据必须数**不同**序号：①①① 不是三段落
_dup = "①盘面：上证 3686.17。①再说一句。①还是这句。"
assert any("段落结构不足" in x for x in A._summary_warn(_dup)), \
    "同一序号重复三次不算结构"
print("PASS 7 duplicate section markers not counted as structure")

# 安全闸门剔除面必须可见：_safe_summary 剔句后，_LAST_SAFE_DROPPED 有内容，
# 且 _summary_warn 把它报出来（否则"归因段为什么没了"无从解释）
_cand = A._safe_summary(
    "①盘面：上证 3686.17（+2.16%），科创50 1265.47（+9.92%），成交 2.05 万亿。"
    "我的持仓今日减仓。"
    "②驱动：半导体设备三季报净利 +59.5% 为公告口径（已证实）。"
    "③昨日观察点恒科守 3737 已验证。"
    "④明日周四 21:30 美国初请失业金，恒科站上 3949 确认缺口回补。")
assert A._LAST_SAFE_DROPPED, "剔句后必须留下剔除面记录"
assert any("安全闸门剔除" in x
           for x in A._summary_warn(_cand, dropped=A._LAST_SAFE_DROPPED)), \
    "剔除面必须出现在质量告警里"
print("PASS 8 dropped-sentence surface is reported")

print("ALL PASS")
