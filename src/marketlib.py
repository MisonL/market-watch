#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
共享底座：常量、网络请求、行情抓取、交易日判断、统一阈值。
所有模块（collect / analyze / hot / alert / news）都从这里导入，
避免重复代码与阈值漂移。
"""
import fcntl
import json, os, re, shutil, subprocess, tempfile, time, datetime, unicodedata
from typing import Any, TypedDict
# ⚠️ `TypeIs` 是 **Python 3.12+** 才有的（PEP 742）。它在本文件里只用于
# **一处返回类型标注**（`isnum` 的 `-> TypeIs[int | float]`），运行时零依赖
# —— 所以缺了它不该让整个系统起不来。
#
# `[实测]` 2026-09-28 23:30:34 真实发生过一次：collect 被一个
# **Python 3.9** 解释器拉起（Xcode 自带的 `/usr/bin/python3` 是 3.9.6，
# 而四个 plist 都指向 `/usr/local/bin/python3`=3.14.7），
# `from typing import TypeIs` 直接抛 ImportError：
#     ImportError: cannot import name 'TypeIs' from 'typing'
# 那一次是**孤立事件**（23:29 与 23:31 两轮都正常），说明是解释器解析
# 在某一刻落到了别的 python3 上（Homebrew 升级窗口、PATH 变动等）。
# 但它暴露的结构性问题是：**代码隐含要求 3.12+，却没有任何地方声明**，
# 也没有启动时的版本检查 —— 一旦真的落到旧解释器，整个盯盘**静默全停**
# （四个 job 全 import 失败），而表现只是 runs.jsonl 里一行 import_fail。
#
# 修法：按可用性降级，优先 typing_extensions（backport，覆盖到 3.8）。
# 两者都没有时退回 `bool` —— 只损失类型收窄能力，运行时行为完全不变
# （`isnum` 的实现体不依赖 TypeIs）。
# ⚠⚠ 2026-11-18（CI 实测发现）：`TypeIs` 是 **3.13+**，不是 3.12+。
#
# PEP 742 在 **Python 3.13** 才进入标准库。3.11 / 3.12 上
# `from typing import TypeIs` 抛 ImportError —— 这没问题（有降级）。
#
# 真正的问题在**降级路径本身写得不能跑**：原兜底是
#
#     TypeIs = bool
#
# 而 `isnum` 的签名是 `-> TypeIs[int | float]`。函数定义时（模块导入期）
# 就会对标注求值 → `bool[int | float]` → **TypeError: type 'bool' is not
# subscriptable**。于是在 3.11/3.12 上，**整个 marketlib 导入即失败**，
# 38 个测试、以及生产里的 collect/analyze 全起不来。
#
# `[实测]` GitHub Actions 的 3.11 job：38 个测试失败，全部是这个错；
# 本机 `/Users/mison/.local/bin/python3.11` 可完整复现（traceback 指到
# 本文件的 `def isnum` 那一行）。
#
# 修法：兜底用一个**可下标**的代理对象 —— 它被调用时返回第一个参数，
# 也就是"标注按原样透传"，运行时零行为。这样 `TypeIs[int | float]`
# 求值成 `int | float`，函数正常工作。
#
# 注意：这只影响**运行时**。类型检查器（pyright/mypy）看到的是真正的
# TypeIs（它们自带 3.13 的 typing 存根，不受运行解释器版本影响）。
class _TypeIsFallback:
    """`TypeIs` 的运行时替身：`_TypeIsFallback[T]` 求值为 `T` 本身。"""

    def __class_getitem__(cls, item):
        return item


try:
    from typing import TypeIs                      # 3.13+
except ImportError:                                # pragma: no cover
    try:
        from typing_extensions import TypeIs       # backport（覆盖 3.8+）
    except ImportError:
        TypeIs = _TypeIsFallback  # type: ignore[assignment,misc]

# 允许 MW_DIR 覆盖：测试可在沙箱里跑而完全不碰线上状态文件
from paths import p_cache, p_state, p_conf, p_log, CACHE_DIR, LOGS_DIR
LOG = p_log("watch.log")   # 统一日志：analyze/collect/hot 共用

# ---------- 常量 ----------
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
UT = "bd1d9ddb04089700cf9c27f6f7426281"
EM = "https://push2delay.eastmoney.com/api/qt"
# 备用行情主机。实测：A股/板块数据与腾讯逐位一致，但港股(f18昨收)陈旧，
# 故港股不走此源，见 quotes() 的分流逻辑。
EM_TEST = "https://push2test.eastmoney.com/api/qt"
HIS = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
# ⚠️ 2026-10-22：`HIS`（HTTPS 的 push2his）实测 **0/8 全端点 rc=52**，
# 而同一主机换 `http://` 明文后 kline **8/8 可用**（见下方主机矩阵）。
# 由于 HTTPS 侧是硬封、明文可用，所有用 HIS 取日K的地方都改走
# `HIS_HTTP`（alert.py 的 _kline_closes 按 HTTP→HTTPS 顺序回退）；
# `HIS` 常量保留只为兼容外部引用与"解封后回切"。
HIS_HTTP = "http://push2his.eastmoney.com/api/qt/stock/kline/get"

# ---------- 明文 HTTP 备用主机（2026-10-22 加）----------
# `[实测]` 2026-10-22 逐主机 × 逐端点 × 两种协议扫描（各 8 轮）：
#
#   主机        HTTPS ulist  HTTPS clist   HTTP ulist   HTTP clist   HTTP kline
#   push2test    8/8          8/8           8/8          8/8          8/8
#   push2delay   0/8          0/8           1/8          0/8          0/8
#   push2        0/8          0/8           8/8          0/8          0/8
#   push2his     0/8          0/8           7/8          0/8          8/8
#
# 即：**封禁是按「主机 × 端点 × 协议」三元组生效的，不是按主机名整体封**。
# HTTPS 下除 push2test 外全部 rc=52（connection reset by peer），而同样的
# 主机换 http:// 明文就能通 —— 说明拦截发生在 TLS 层/SNI 侧。
#
# ⚠️ 但 **clist（板块列表）在 HTTP 下全部被拒**，静默 300s / 600s 后重测
# 仍是 rc=52 —— 不是速率触发，是端点级封锁。故 clist 仍只有 push2test
# 一条路，不要把 HTTP 主机加进 clist 的候选（加了也只是多打两次必然
# 失败的包，白扣配额，且会触发 _mark_dead 的假失败）。
#
# 字段级一致性 `[实测]`（同一 secid 逐字段比对）：
#   · 个股 / 指数：f2 f3 f6 f12 f14 f18 f62 f184 **逐位相同**
#   · 板块 90.BKxxxx：f3 f6 f62 f184 逐位相同，但 **f2/f18 系统性相差
#     约 -1.4%**（BK1030 电机Ⅱ：push2test 2304.54 / HTTP 2273.05）。
#     这不是陈旧 —— f3(涨跌幅) 完全一致，说明是指数**基点重标定**，
#     所有比率型字段都不受影响。但 f2 是绝对点位，混用会让"板块点位"
#     这一列在两次取数之间凭空跳变 1.4%。
#     ⚠️ 故 HTTP 主机**只用于比率型判断**（f3/f62/f6/f184），
#     quotes() 里板块的 f2 一旦来自 HTTP 主机就与另一台的 f2 不可比 ——
#     当前没有代码跨轮比较板块 f2 绝对值（热点榜只用 f3/f6/f62 排序），
#     故可用；将来若要加"板块点位"展示，必须固定用同一台。
EM_HTTP = "http://push2.eastmoney.com/api/qt"
# ⚠⚠ 2026-11-10（生产故障）：`push2` 的 **HTTPS** 也要当候选。
# 原先注释说“push2.eastmoney.com 只有明文可用（HTTPS 全端点 rc=52）”——
# 那是当时的封地状态，**现已解除**。`[2026-11-10 实测]` 三个端点全 200：
#   ulist.np/get、clist/get、stock/kline/get；且板块 f2 与明文逐位一致
#   （90.BK1030 = 2298.39、90.BK0447 = 27275.56）—— 不存在基点差异问题。
# 为什么必须加：板块**只走 HTTPS 主机**，而候选只有
# `push2test`（当日 09:57 起全端点 000 不可达）与 `push2delay`（永久封锁）。
# 两台都挂时，每轮“板块 0/12 → 全源失效”，推送里 8 个板块全是
# `+0.00%（暂无资金数据）`—— 读者会当成真实涨跌幅。
EM_HTTPS = "https://push2.eastmoney.com/api/qt"
EM_HIS_HTTP = "http://push2his.eastmoney.com/api/qt"
REF_EM = "https://quote.eastmoney.com/"
REF_NEWS = "https://kuaixun.eastmoney.com/"

# A股休市日（**法定节假日，不含周末** —— 周末由 is_trading_day 的
# weekday()<5 覆盖）。
#
# ⚠️ 每年 12 月国务院公布次年安排后必须回来更新，否则新的一年会把休市日
# 当成交易日：空跑一整天（每 5 分钟一轮），且若恰有异动触发还会往群里
# 推假消息。2027 年的春节/清明/五一/端午/中秋日期为按农历推算的**预填值**，
# 官方公布后请核对。
HOLIDAYS = {
    # ---- 2026 ----
    #
    # ⚠️ 2026-10-28 修正：原先这里只列了中秋+国庆，注释却写「已核对上交所
    # 公告〔2026〕22 号」—— 声明与内容不符（元旦/春节/清明/劳动/端午全缺，
    # 实测 is_trading_day 对它们全部返回 True）。本系统 2026-09-22 才上线，
    # 那几天已经过去、不会再有调度，所以**没有造成过实际影响**，但表必须
    # 是诚实的真值：补齐**确定**的休市日（农历日期与港股表交叉一致、公历
    # 固定节日），不确定的**宁可留空**（空跑无害、误判交易日更糟）。
    # 口径：本表是**上交所口径**，与港股表（HK_HOLIDAYS）只有春节/中秋/
    # 国庆重叠；列在这里的农历日期都与港股表交叉核对过。
    # ⚠️ 只列工作日；落在周末的（如 2026-04-05 清明恰为周日、
    # 02-21/02-22 周末）由 `weekday() < 5` 覆盖，不必重复列。
    # ⚠️ 标 `[推断]` 的是按"节假日逢周末顺延"惯例推的，官方公告原文未逐条
    # 核对过；这些日期都在系统上线之前，无调度影响，仅作表的完整性。
    "2026-01-01",                                   # 元旦（周四）
    "2026-02-16",                                   # [推断] 除夕（周一）
    "2026-02-17", "2026-02-18", "2026-02-19",       # 農曆年初一~初三（与港股表一致）
    "2026-04-06",                                   # [推断] 清明（4/5 为周日，惯例顺延到周一）
    "2026-05-01",                                   # 劳动节（周五）
    "2026-06-19",                                   # 端午（周五，与港股表一致）
    "2026-09-25",                                   # 中秋
    # ⚠️ 国庆**只到 10-07**，10-08（周四）是**交易日**。
    # 2026 年中秋在 9/25（与国庆相隔一周），两节**不合并**，国庆就是
    # 法定的 7 天（10/1–10/7）。原先这里多写了 "2026-10-08"，会把
    # **节后第一个交易日整天判成休市** —— 不采集、不推送、一条简报都没有，
    # 而那正是最该盯盘的一天。
    # 教训：2025 年中秋落在 10/06（与国庆重叠）故假期是 10/1–10/08 共 8 天；
    # 照抄上一年的日期范围就会多出一天。**每年必须回公告核对，不能沿用**。
    "2026-10-01", "2026-10-02", "2026-10-05",
    "2026-10-06", "2026-10-07",                     # 国庆
    # ---- 2027（预填，待官方核对）----
    "2027-01-01",                                   # 元旦
    "2027-02-05", "2027-02-08", "2027-02-09",
    "2027-02-10", "2027-02-11", "2027-02-12",       # 春节（除夕 2/5）
    "2027-04-05",                                   # 清明（2027 清明为周一）
    # ⚠️ 2026-11-01 删去原预填的 "2027-04-06"：那是照抄 2026 的模式
    # （2026 清明 4/5 恰为**周日**，惯例顺延到周一 4/6）。而 2027 清明
    # 落在**周一** 4/5，法定假日就是当天，4/6（周二）按惯例应为交易日。
    # 本表自己的原则是「不确定的宁可留空 —— 空跑无害、误判交易日更糟」：
    # 留着它一旦判错，那天会整天 `not_trading_day`、零采集零推送。
    # ⚠️ 这条是按惯例的**推断**，2027 官方安排（2026 年 12 月公布）后仍须核对。
    "2027-05-03", "2027-05-04", "2027-05-05",       # 五一
    "2027-06-09",                                   # 端午
    "2027-09-15",                                   # 中秋
    "2027-10-01", "2027-10-04", "2027-10-05",
    "2027-10-06", "2027-10-07",                     # 国庆
}
# 节假日表的覆盖上限。超过此日期说明表已过期，is_trading_day 会
# **保守地按周末规则判断**并写一条告警 —— 宁可漏判一个休市日
# （空跑一轮，无害），也不要静默地把休市日当交易日。
HOLIDAYS_COVERED_THROUGH = "2027-12-31"
# ⚠️ 原先是**进程内**一次性标志，但 analyze/collect 每轮各起一个**新进程**
# —— 过期后每天约 430 条相同告警，而 watch.log 只有 2MB 单代轮转，
# 真实诊断日志会在数小时内被自身告警刷掉。
# 改为落盘按日去重：同一天只报一次，跨进程有效。
_HOLIDAY_WARN_FILE = p_cache("holiday_warn.json")

# ---------- 港股交易日日历（与 A 股假期表分开） ----------
#
# ⚠️ 为什么必须单开一张表：A 股与港股的公众假期**只有国庆/中秋/春节重叠**，
# 其余完全不同。`HOLIDAYS`（上交所公告口径）里没有重阳、佛诞、耶稣受难节、
# 复活节星期一、圣诞 —— 而这几天港股休市、A 股照常交易。
#
# `[实测]` 2026-10-19（重阳节翌日）：港股全天休市，腾讯/新浪返回的港股行情
# 时间戳停在 **10-16**（上一交易日）。系统的 `_is_ts_stale` 新鲜度闸门把它
# 正确地拦了下来 —— 但**拦下之后没有下文**：`build_brief` 的【指数】
# 【港股】【关键位】段拿不到当日行情，推送里于是出现
# 「恒生指数 数据缺失、恒生科技指数 数据缺失」，兜底模板从 09:48 到 11:46
# 连写 11 轮。读者读到的信息是「数据源坏了」，而事实是「港股今天不开市」——
# 两者对下一步动作的含义完全相反（前者要查系统，后者什么都不用做）。
# 用户当天两次追问「为什么恒生的数据一直缺失」。
#
# 日期来源：香港特区政府一站通（GovHK）宪报公布的公众假期表。
# 2026 表 https://www.gov.hk/tc/about/abouthk/holiday/2026.htm
# 2027 表 https://www.gov.hk/tc/about/abouthk/holiday/2027.htm
# 只列**工作日**的假期：落在周末的（2026-04-04 / 09-26 / 12-26、
# 2027-02-06 / 03-27 / 05-01 / 12-25）港股本就不开市，列上能让本表成为
# 该年的**完整**真值，读表的人不用再查周末。
HK_HOLIDAYS = {
    # ---- 2026 ----
    "2026-01-01",                                   # 元旦
    "2026-02-17", "2026-02-18", "2026-02-19",       # 農曆年初一~初三
    "2026-04-03", "2026-04-04",                     # 耶穌受難節 / 翌日
    "2026-04-06",                                   # 清明節翌日
    "2026-04-07",                                   # 復活節星期一翌日
    "2026-05-01",                                   # 勞動節
    "2026-05-25",                                   # 佛誕翌日
    "2026-06-19",                                   # 端午節
    "2026-07-01",                                   # 香港特區成立紀念日
    "2026-09-26",                                   # 中秋節翌日（周六）
    "2026-10-01",                                   # 國慶日
    "2026-10-19",                                   # 重陽節翌日  ← 本轮事故当天
    "2026-12-25", "2026-12-26",                     # 聖誕節 / 後第一個周日
    # ---- 2027 ----
    "2027-01-01",                                   # 元旦
    "2027-02-06", "2027-02-08", "2027-02-09",       # 農曆年初一/三/四
    "2027-03-26", "2027-03-27",                     # 耶穌受難節 / 翌日
    "2027-03-29",                                   # 復活節星期一
    "2027-04-05",                                   # 清明節
    "2027-05-01",                                   # 勞動節（周六）
    "2027-05-13",                                   # 佛誕
    "2027-06-09",                                   # 端午節
    "2027-07-01",                                   # 香港特區成立紀念日
    "2027-09-16",                                   # 中秋節翌日
    "2027-10-01",                                   # 國慶日
    "2027-10-08",                                   # 重陽節
    "2027-12-25", "2027-12-27",                     # 聖誕節 / 後第一個周日
}
# 覆盖上限与 A 股表同款：过期后保守按周末规则判断（宁可漏判一个休市日
# ——推送里少一句"港股休市"，也不要静默地把休市日当成交易日 ——
# 那会把人引向"查系统"的错误方向）。
HK_HOLIDAYS_COVERED_THROUGH = "2027-12-31"

# 港股**半日市**（平安夜/除夕/农历年除夕）：12:00 收市、无下午盘。
#
# ⚠️ 为什么必须单列（2026-10-28 加，P1）：这类日子 `is_hk_trading_day`
# 返回 True（确实是交易日），但 13:00 之后港股行情时间戳冻结在 12:00
# 前后 → `quotes()` 末尾的新鲜度闸门（`_is_ts_stale`，阈值 20 分钟）
# 把它们整批丢弃 → 推送里写「恒生指数 数据缺失」。读者只会去查系统，
# 而事实是港股上午就收市了 —— 与 2026-10-19 重阳节那次事故
# （见 HK_HOLIDAYS 上方注释）**逐字同型**，只是触发日不同。
# `[实测]` 2026-12-24（周四）13:30 对 12:00 的时间戳判 stale=True、
# in_session=True、is_hk_trading_day=True —— 三个判据全成立，闸门必然误杀。
# 日期来源同 HK_HOLIDAYS（GovHK 宪报）；只列**工作日**的半日市。
HK_HALF_DAYS = {
    "2026-02-16",   # 農曆年除夕（周一；年初一为 2/17）—— 半日市，无下午盘
    "2026-12-24",   # 平安夜（周四）
    "2026-12-31",   # 除夕（周四）
    "2027-02-05",   # 農曆年除夕（周五；年初一为 2/6）—— 半日市，无下午盘
    "2027-12-24",   # 平安夜（周五）
    "2027-12-31",   # 除夕（周五）
}
# ⚠️⚠️ 2026-11-06（全量复核 P1-5）：原先这张表**只有公历**的圣诞/新年除夕，
# 漏了**農曆年除夕** —— 港交所规则里它同样是半日市（早市无延长、下午收盘）。
# `[实测]` 补之前：
#   hk_session_open(2026-02-16 14:00) -> True   ❌（实际 12:00 已收盘）
#   hk_session_open(2027-02-05 14:00) -> True   ❌
# 后果与 2026-10-28 已修的半日市事故同型：下午拿 12:00 的定格收盘价当
# 「午后盘中」判异动，事件键占住当日去重位，真跌破当天反而不报。
# 日期依据：GovHK 2026/2027 公众假期表（農曆年初一分别为 2/17、2/6），
# 除夕为前一自然日；港交所《交易时间》页确认農曆年除夕无下午盘。
# ⚠️ 半日市表**也必须有覆盖上限**（2026-10-29 补，P2）：
# 原先只有 `HK_HOLIDAYS_COVERED_THROUGH` 管整日休市，半日市没有任何
# 到期信号 —— 2028 起 `_is_ts_stale` 会把半日市的下午当普通交易日，
# 12:00 之后的新鲜度闸门重新开始误杀（「恒生指数 数据缺失」）。
# 两张表的覆盖期显式分开：它们是**不同来源**（宪报的整日假 vs 半日市
# 安排），将来可能不同年补齐，共用一个常量会让提前补的那张被误报过期。
HK_HALF_DAYS_COVERED_THROUGH = "2027-12-31"
# 港股假期表过期告警的**进程内**短路（跨进程的按日去重仍靠落盘文件）。
# 本函数每轮被 build_brief / 兜底模板逐项调用 10+ 次，每次都读一遍
# `cache/holiday_warn.json` 是纯浪费。
_HK_WARN_DONE = [False]

# ---------- 统一阈值（唯一真源）----------
class Thresholds(TypedDict):
    """TH 的结构。**这是全库类型误报的主要根源** —— 原先 TH 是个裸 dict，
    取值一律 Any，于是 basedpyright 在每一个 TH[...] / threshold() 的
    下游都报 reportAny / reportArgumentType，把真正的缺陷淹在噪音里。

    ⚠️ total=False：这些键并非全部必需 —— threshold(key, default) 的设计
    就是"缺键时用调用方给的默认值"。标成 total=True 会让 each-key-required
    变成新的噪音源。

    ⚠️ 键名必须与 sectors.json 的 thresholds 段保持一致（那里可覆盖此处）。
    selfcheck 的 [阈值默认值一致性] 会核对字面默认值，但**不核对键集** ——
    加键时记得两处都加。
    """
    # 恒生科技
    hstech_up: float
    hstech_down: float
    hstech_drop: float
    # 存储冲高回落
    storage_fade: float
    # 板块资金异动
    fund_abs: float
    fund_pct: float
    # 半导体（analyze.detect 用）
    semi_fund_out: float
    semi_fund_in: float
    semi_surge: float
    # 指数
    index_weak: float
    hs300_weak: float
    # 贵金属
    gold_up: float
    gold_dn: float
    # 大盘资金（alert.check_market_fund 用）
    market_fund_in: float
    market_fund_out: float
    market_fund_pct: float
    # 板块资金占比（collect.evaluate / analyze.detect 共用）
    watch_fund_pct: float
    # 保护时段与交易时段（元组：起、止，单位「距 0 点的分钟数」）
    guard_open: tuple[int, int]
    guard_mid: tuple[int, int]
    guard_close: tuple[int, int]
    session: tuple[int, int]
    # 整点播报槽位
    hourly: list[str]


TH: Thresholds = {
    # 指数 3σ 阈值（近250日实测）
    # ⚠️ 键必须与 sectors.json 的 index 列表一致（collect.evaluate 会做一次
    # 短码→secid 映射，但映射表就是从 index/hk_stocks 建的 —— 表里没有的
    # 指数拿不到数据，阈值再合理也用不上）。
    # "399001"(深证成指) 原先在这里有阈值，却从未出现在 sectors.json 的
    # index 列表中，即从未被请求 —— 是条死键，已删除。若日后要看深证成指，
    # 往 sectors.json 的 index 里加一条即可（此处同步加回阈值）。
    # "idx_3sigma" 已删除 —— 指数异动阈值统一由 idx_thresholds() 提供
    # （动态 EWMA σ，与 alert.check_index 同源）。原先是张固定 3σ 表，
    # 与 alert 的动态值不一致，导致"推送到群但面板不记"。
    # 恒生科技计划触发线
    # ⚠️ 真值在 `sectors.json` 的 thresholds（改配置即生效）；这里是
    # **配置缺失时的兜底常量**，数值与配置保持一致，避免读代码的人
    # 把兜底值当成现行触发线。
    # ⚠️ 兜底值必须随每次重校准一起改 —— 原先停在 4300/4070，而配置已
    # 改成 3995/3781（2026-10-23），一旦 sectors.json 缺键，触发线会
    # 静默错位 200+ 点。selfcheck 的 [阈值兜底一致性] 守护这条不变量。
    # 重校准史：4450/4300 →(2026-10-10) 4300/4070 →(2026-10-23) 3995/3781
    #           →(2026-10-29) 3995/3737 →(2026-11-05) 4243.65/3737
    #           →(2026-11-18) 4243.65/3695 →(2026-11-19) 4243.65/3630
    #           （11-18：收盘 3736.33 首次落在 3737 下方、盘中低 3695.00，
    #             旧线已高于收盘价、次日会退化为「每日常中」，改锚当日新低）
    #           （11-19：收盘 3646.93 首次收盘跌破 3695、盘中低 3630.46
    #             创 52 周新低，同一原则改锚 3630。⚠️ agency 侧
    #             `update_threshold` 只改 sectors.json 与 plan.md，**不碰
    #             本表与调用点的字面默认值** —— 本次就是漏了这 4 处，
    #             自检 [阈值兜底一致性] 报「2 处已与真值漂移」才发现的。）
    #           （11-05：恒科收盘 4152.37 站上 3995 +3.9%，旧上破线退化为
    #             每日常中；新锚=20 日高 4243.65）
    # ⚠️ 2026-10-29 那次的教训（本行就是补上的）：`update_threshold` 只改
    # sectors.json 与 plan.md，**不碰这里** —— 于是配置当天就变了，兜底
    # 隔天还停在 3781，`[阈值兜底一致性]` 必报 ❌。重校准后必须手工同步
    # 本表 + `threshold("hstech_down", N)` 的三处字面默认值（analyze 2 处、
    # collect 1 处），否则"配置缺键"那一刻触发线会退回一个旧数。
    "hstech_up": 4243.65,
    # 日期专用覆盖：hstech_up_threshold() 会按当日日期动态拼
    # "<键>_MMDD" 并优先取之（如 hstech_up_0925）。机制是通用的，
    # 加一个键即刻生效，无需改代码 —— 所以**不需要**在这里保留
    # 任何已过期的样例键（原先留了 hstech_up_0922 "作格式参考"，
    # 但它永远不会再被读到，只会让审阅者以为是活配置）。
    # 要设置某日的专用触发线，在 sectors.json 的 thresholds 里加即可。
    # ⚠️ 2026-11-20 同步：今日盘中恒科创 52 周新低 3482.43，配置已从
    # 3630 重校准到 3482.43（旧线高于现价会退化成每日常中）。
    # 本条注释上方的规矩（重校准后必须手工同步本表 + 三处字面默认值）
    # 这次**又被漏做**，是 `[阈值兜底一致性]` 当场抓出来的 ——
    # 说明"靠人记得"这个机制不可靠，但当前结构只能如此（配置是活真值）。
    "hstech_down": 3482.43,
    "hstech_drop": -1.5,      # 恒生科技单日跌幅（原先硬编码在 analyze.py）
    # 存储冲高回落
    "storage_fade": -1.5,     # 自日内高点回落
    # 板块资金异动
    "fund_abs": 50,           # 亿元
    "fund_pct": 15,           # 占成交额比 %
    # 指数保护时段
    "guard_open": (9*60+30, 10*60),
    "guard_mid": (11*60+25, 11*60+30),
    "guard_close": (14*60+55, 15*60+5),
    # 交易时段
    "session": (9*60+25, 15*60+5),
    # 整点播报槽位
    "hourly": ["09:25", "10:00", "11:30", "13:30", "14:30", "15:00"],
}

# hstech_up_threshold() 的**最后一道**兜底值（sectors.json 与 TH 都缺键时
# 才可达）。⚠️ 必须与 `TH["hstech_up"]` 及 sectors.json 的
# `thresholds.hstech_up` 三处同步 —— 它原先写死在函数末行（`return 4300.0`），
# 是**第三个独立真值源**，与另两处各改各的、无人守护。selfcheck 的
# [阈值兜底一致性] 现在断三处相等。
_HSTECH_UP_LAST_RESORT = 4243.65

LOG_MAX = 2 * 1024 * 1024      # 超过 2MB 就轮转，防止无界增长

def _harden(path):
    """把已存在的文件收紧到 0600；不存在则忽略。异常吞掉（纯加固）。"""
    try:
        if os.path.exists(path):
            os.chmod(path, 0o600)
    except Exception:
        pass

def _rotate_log():
    """单代轮转：LOG -> LOG.1。只保留一份，够定位最近问题即可。

    ⚠️ 必须持排他锁（2026-10-25 修 P2）：`log()` 被**五个进程**共用
    （collect / analyze / webui / selfcheck / agent），每写一行都调本函数。
    原实现无锁，两个进程同时看到"超阈值"时：
      · A 先 `os.replace` 成 .1，B 的 replace 再执行一次 —— 此时 LOG 可能
        已不存在（A 刚换走、新文件还没建），B 的 replace 抛 ENOENT 被吞；
      · 更坏：A 换完、还没写新行，B 也判超阈值 → 把 A 刚换出的 .1 覆盖掉，
        **上一代日志整份丢失**（轮转的本意是"留一份好定位问题"）。
    锁文件与 `_locked_update` 同一套 `_open_lockfile` 写法（0o600、不用裸
    open —— umask 022 会建 644，同机他人可 flock 造成 DoS）。
    锁**不长持**：`log()` 每行都要过这里，持锁只覆盖 stat+replace 两个 syscall。
    """
    try:
        with _open_lockfile(LOG + ".lock") as _lk:
            fcntl.flock(_lk, fcntl.LOCK_EX)
            try:
                if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX:
                    os.replace(LOG, LOG + ".1")
                    _harden(LOG + ".1")
            finally:
                try:
                    fcntl.flock(_lk, fcntl.LOCK_UN)
                except Exception:
                    pass
    except Exception:
        pass

def log(m, tag=""):
    try:
        _rotate_log()
        # ⚠️ 必须用 os.open(..., 0o600) 创建。原先 `open(LOG, "a")` 在文件
        # 不存在时按 umask(022) 建成 **644** —— 日志里有行情/配置片段，
        # 同机其他用户可读（2026-10-08 安全审查 P2）。os.open 的 mode 只在
        # **创建**时生效，故每次 append 都显式带上，轮转后重建的文件也是 600。
        fd = os.open(LOG, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now():%m-%d %H:%M:%S}]"
                    f"{'['+tag+']' if tag else ''} {m}\n")
    except Exception:
        pass

def harden_logs(*names):
    """把 launchd 重定向的 stdout/stderr（*.out/*.err）收紧到 0600。

    ⚠️ why：`webui.out`/`webui.err`/`collect.err` 等由 launchd 以
    StandardOutPath 创建，launchd 用 umask 022 → 644，且**每次进程重启
    都会重建**（轮转后新文件同样 644）。仅在部署时 chmod
    现有文件不顶用 —— 下一次重启就回退。故在**进程启动时**调本函数，
    与 `rotate_logs` 同一时机（每次运行各收敛一次）。
    """
    for n in names:
        _harden(os.path.join(LOGS_DIR, n))


# launchd 重定向日志（*.out/*.err）的单代轮转阈值。
ROTATE_MAX = 512 * 1024


def rotate_logs(*names):
    """单代轮转 launchd 重定向出来的日志（*.out/*.err）—— **copy + 原地截断**。

    ⚠️⚠️ 为什么不能用 `os.replace`（本函数/调用点原先就是这么写的，
    2026-10-09 改正）：launchd 在 exec **之前**就按 StandardOutPath/
    StandardErrorPath 打开文件，并把该 fd 交给子进程（探针实测其 flags
    为 `O_RDWR|O_APPEND`）。`os.replace` 只改目录项，**fd 仍指向那个
    inode** —— 于是"轮转"之后所有后续输出**继续写进 `.1`**，而 `.out`
    这个路径**整个消失**。等于既没腾出空间，又把正在看的日志藏了起来。
    `[实测]` 复刻 launchd 语义（O_APPEND fd + rename）：
        os.replace      → base 不存在，gen1 = 旧+新        ❌
        copy+truncate   → base = 只有新内容，gen1 = 旧内容  ✅

    正确做法：先 copy 到 `.1`，再**原地截断**（inode 不变 → fd 继续有效；
    O_APPEND 保证下一次写入落在新 EOF=0，不会留洞）。
    ⚠️ 顺序必须是"先 copy 后 truncate"：反过来的话截断与 copy 之间被 kill
    就丢数据；这个顺序最坏只是 .1 与 base 内容重复，不丢。
    ⚠️ 不是 `os.replace` 那种原子操作，但对日志足够：并发写者只有本进程
    自己，且轮转发生在启动早期（尚无多线程/子进程输出）。

    `watch.log` 走的是另一套（`_rotate_log`，用 `os.replace` 是对的）——
    它的写者是本进程 `os.open(...O_APPEND)` 每次新开 fd，rename 之后下次
    写入自然落到新文件。**两者的区别在 fd 的寿命**，不要"统一"成一种。
    """
    # 先把本进程缓冲刷出去 —— 否则缓冲里的旧内容会在 truncate 之后才落盘，
    # 混进新文件（flush 在前则它被算作"旧内容"一并进 .1）。
    try:
        import sys as _sys
        _sys.stdout.flush()
        _sys.stderr.flush()
    except Exception:
        pass
    for n in names:
        p = os.path.join(LOGS_DIR, n)
        try:
            if not (os.path.exists(p) and os.path.getsize(p) > ROTATE_MAX):
                continue
            shutil.copyfile(p, p + ".1")
            _harden(p + ".1")          # copyfile 按 umask 建 → 得自己收紧
            with open(p, "r+b") as f:
                f.truncate(0)
        except Exception:
            pass
    # 截断后把自己 stdout/stderr 的偏移挪到新末尾。O_APPEND 下是空操作，
    # 万一某个环境不是 O_APPEND（偏移停在旧 EOF），这一步避免写出 NUL 空洞。
    for _fd in (1, 2):
        try:
            os.lseek(_fd, 0, os.SEEK_END)
        except Exception:
            pass


def claude_bin():
    """定位 claude CLI，返回 (可执行路径, 其所在目录)。找不到返回 (None, None)。

    原先三处硬编码一个 nvm 下的 node 具体版本路径。
    nvm 升级 node（版本号一变）会让该路径失效，而调用点都是
    except Exception 吞掉后走兜底模板 —— 表现为"AI 突然只会说套话"，
    极难排查。故改为按优先级探测，并在失败时明确记日志。

    失败时**重试 6 次 × 1s** —— 符号链接在自动更新窗口内悬空是真实存在的
    瞬时状态，单次探测判 False 会给出"CLI 坏了"的错误印象（详见下方
    重试循环的注释）。
    """
    # ⚠️ 缓存必须带 TTL 且**校验仍然可执行**。
    # 原实现缓存了成功结果且永不失效（注释只说"不缓存失败结果"）。
    # claude CLI 自动更新时 ~/.nvm/versions/node/vX/bin/claude 会被替换或
    # 短暂消失（符号链接悬空），此时缓存住的旧路径会让后续调用全部
    # FileNotFoundError -> 走兜底模板。单轮 cron 只活几分钟影响有限，
    # 但 webui.py 是常驻进程，会一直用失效路径。
    #
    # ⚠️ 但**单次探测失败不能直接判"找不到"** —— 符号链接在自动更新窗口内
    # 悬空是**真实存在的瞬时状态**（npm 要写 ~236MB 的 claude.exe，窗口长度
    # 不定）。`[实测]` 2026-10-08：11:09:53 与 14:03:34 两轮各报一次
    # 「未找到 claude CLI」，而同一时段其它轮次全部正常 —— 两次都是
    # 假阳性，代价是**整条推送退化成兜底模板**（14:03 那次还连带丢掉了
    # 前一次 384s 跑出来的可用正文，见 AMT_RE 的假阳性注释）。
    # 这与 `feeds.health()` 对百度风控的处理是同一类问题
    # （见 [[market-watch-baidu-silent-fail]]：单次探测判 False 会给出
    # "源坏了"的错误印象）。
    # 修法：失败时**重试 6 次 × 1s**（与 selfcheck 的 [AI 通道] 段同一判据），
    # 仍失败才记 CFG 日志并返回空。重试只在失败时付代价，正常路径零开销。
    for _attempt in range(CLI_PROBE_ATTEMPTS + 1):   # 1 次首探 + CLI_PROBE_ATTEMPTS 次重试
        if _attempt:
            time.sleep(CLI_PROBE_DELAY)
        _p, _d = _claude_bin_probe()
        if _p:
            return (_p, _d)
        if _attempt == 0:
            # 清掉可能缓存的失败探测，避免下一次调用直接命中缓存
            _CLAUDE_BIN["v"] = None
    log(f"未找到 claude CLI（重试 {CLI_PROBE_ATTEMPTS} 次 / "
        f"约 {CLI_PROBE_ATTEMPTS * CLI_PROBE_DELAY:.0f}s 仍失败，"
        f"AI 将退化为兜底模板）", "CFG")
    return (None, None)


def _claude_bin_probe():
    """单次探测：返回 (可执行路径, 其所在目录)，找不到返回 (None, None)。

    带 TTL 缓存；缓存命中时校验路径仍然可执行。**不做重试** —— 重试在
    claude_bin() 里统一做，两处各写一遍必然漂移。
    """
    cache = _CLAUDE_BIN.get("v")
    if cache is not None:
        # ⚠️ 缓存里存的是 `((路径, 目录), 探测时刻)` —— 必须按这个**嵌套**结构解包。
        # 原先写 `_c, _ts = cache`，于是 _c 拿到的是**整个 (路径, 目录) 元组**，
        # 下面 os.path.isfile(_c) 收到 tuple 直接 TypeError，被
        # `except Exception: pass` 吞掉，然后走到 _CLAUDE_BIN["v"] = None ——
        # **TTL 缓存从未生效过**：每次调用都重新探测（shutil.which + 列 nvm 目录）。
        # 实测（2026-09-27）：第 2 次调用期间 isfile 被调用 2 次、且其中一次
        # 收到的参数是 tuple，证明缓存被丢弃、走了完整探测路径。
        # 同时 `return cache` 也是错的：它返回 (路径, 探测时刻)，而本函数的
        # 契约（docstring 与所有其它 return）是 **(可执行路径, 其所在目录)** ——
        # 调用方 `binp, bindir = claude_bin()` 会把时间戳当目录拼进 PATH。
        # 由于 isfile 先抛异常，那条 return 实际不可达，所以这个错误从未外泄；
        # 但两处一起修，才能让缓存真正生效且返回值正确。
        (_c, _dir), _ts = cache
        if time.time() - _ts < _CLAUDE_BIN_TTL:
            try:
                if _c and os.path.isfile(_c) and os.access(_c, os.X_OK):
                    return (_c, _dir)
            except Exception:
                pass
        _CLAUDE_BIN["v"] = None      # 过期或已失效 -> 重新探测
    cands = []
    # 1) 环境变量显式指定
    if os.environ.get("CLAUDE_BIN"):
        cands.append(os.environ["CLAUDE_BIN"])
    # 2) PATH 里直接能找到
    w = shutil.which("claude")
    if w: cands.append(w)
    # 3) nvm 各版本目录（取版本号最大的）
    nvm = os.path.expanduser("~/.nvm/versions/node")
    try:
        vs = sorted(os.listdir(nvm), key=lambda s: [int(x) for x in re.findall(r"\d+", s)] or [0])
        for v in reversed(vs):
            cands.append(os.path.join(nvm, v, "bin", "claude"))
    except Exception:
        pass
    # 4) 常见安装位置
    cands += ["/usr/local/bin/claude", "/opt/homebrew/bin/claude",
              os.path.expanduser("~/.claude/local/claude")]
    for c in cands:
        try:
            if c and os.path.isfile(c) and os.access(c, os.X_OK):
                _CLAUDE_BIN["v"] = ((c, os.path.dirname(c)), time.time())
                return _CLAUDE_BIN["v"][0]
        except Exception:
            continue
    # 不缓存失败结果：进程内可能因为临时原因（PATH 尚未就绪、目录刚被
    # 替换）探测失败，缓存住会让后续所有调用都跳过 AI，退化成兜底模板。
    # 找不到时每次重试的代价只是几次 stat，可忽略。
    # ⚠️ 这里**不记 CFG 日志** —— 真正的"找不到"由 claude_bin() 在重试
    # 耗尽后统一记（带"重试 6 次"字样）。若这里也记一次，同一次失败会
    # 在日志里出现两条"未找到 claude CLI"，排查者会误以为是两次独立故障。
    return (None, None)


# 缓存探测结果：{"v": ((路径, 目录), 探测时刻) | None}。
# 标注成 dict[str, Any] 是必需的：写成裸 dict 时 pyright 把它当
# dict[Unknown, Unknown]，于是第 182 行 `_c, _ts = cache` 被报成
# `"Never" is not iterable`（reportGeneralTypeIssues）—— 一个纯粹的
# 推断噪声，却混在真信号里。
_CLAUDE_BIN: dict[str, Any] = {"v": None}
# 缓存有效期。CLI 自动更新窗口内符号链接会短暂失效，过期即重新探测。
_CLAUDE_BIN_TTL = 300
# 抗瞬断重试预算（见 claude_bin 的注释）。写成模块常量是为了让自检能把它
# 调小到 0.01s 做行为测试 —— 硬编码 1.0 会让每次自检白等 2s。
#
# ⚠️ 2026-10-22 实测：6 次 × 1s（约 6s）**盖不住真实的更新窗口**。
#   `[实测]` 10-22 09:31:55 生产报「未找到 claude CLI（重试 6 次 / 约 6s
#   仍失败）」→ 该轮 AI 直接退化成兜底模板并推了一条模板消息；
#   而 09:37 的文件时间戳显示 claude-code 整包（285MB 的 claude.exe）
#   在 09:37 被重装 —— **重装窗口横跨了 09:31 那次探测**。
#   6 秒窗口 vs npm 写 285MB 的实际时长（几十秒），差了一个量级。
#   记忆 [[market-watch-claude-cli-transient]] 早就说过"符号链接在 CLI
#   自动更新窗口内失效"，但重试预算从没按真实窗口长度设过。
#
# 现在的取值：**总窗口约 30s**（10 次 × 3s）。理由：
#   · 正常路径零开销（探测成功即返回，不进重试循环）；
#   · 失败路径多等 30s 换一轮完整的 AI 产出 —— 明显划算（一轮 100~240s）；
#   · 30s 覆盖住 npm 解包 + 写 285MB 的典型窗口；
#   · 上限仍有界：30s 远小于调度间隔 300s，不会因此吞槽位。
CLI_PROBE_ATTEMPTS = 10
CLI_PROBE_DELAY = 3.0


# AI 调用**不设固定超时** —— 只要子进程还在产出，就无限等待。
# 理由：AI 组织一段有判断力的分析需要 100~150s，而实际耗时方差极大
# （实测 2026-09-28 同一台机器上从 15s 到 906s）。固定阈值必然两头都错：
#   · 设小了（140s）—— 每轮都超时，推送全是兜底模板的套话
#   · 设大了（900s）—— 仍会打断真正在工作的慢调用，而超时那一刻
#     进程被 kill，已生成的内容**全部丢弃**，白跑 15 分钟
# 现在改为**空闲看门狗**：只有连续 IDLE_LIMIT 秒**没有任何输出**才判定
# 为真实异常（进程卡死 / 网络挂起）并终止。跑得慢但在产出 -> 一直等。
AI_IDLE_LIMIT = 600          # 秒；连续多久无输出算卡死
AI_KILL_GRACE = 10           # 判定卡死后，留给进程收尾的时间

# 「首字节空闲上限」：stdout+stderr **合计 0 字节**时最多等这么多秒。
# 与 AI_IDLE_LIMIT 的区别：后者管"产出中途停了"，前者管"一个字节都没来"。
# 数据支撑（2026-10-12/13）：deepseek 链 9 次「卡死且无任何产出」全部等满
# 600s 才被杀 —— 挂起时第一字节永远不会来，等满 idle_limit 纯属浪费；
# 而慢但在产出的调用（246~685s 成功）首字节必然早已到达（CLI 启动握手
# 与模型首 token 都在秒级~分钟级）。
# 2026-10-20 复盘下调 360 -> 150：主链当天 4 次零字节挂起全部等满 360s
# 才被杀（watch.log 13:45/14:36/14:53/15:10，每次烧掉一整个调度间隔）。
# 上限的真正约束是**调度间隔**（300s）：150s 的首字节闸门留出 150s 余量，
# 让挂起轮仍能在下一个触发到来前退回兜底模板，不吞掉后续轮次。
# ⚠️ 这段论证原本还提到"主链烧光后备用再等 120s"—— 备用模型机制已于
# 2026-10-22 取消（常量与分支一并删除），那半句随之作废。
#
# ⚠️⚠️ 2026-10-23 实测推翻了下面这段"3.6 倍冗余"的论证，一并改写：
#   原注释写「本机实测主链首字节最长 42s，150s 是 3.6 倍冗余」。那个 42s
#   是在 **text 模式**下测的，而 text 模式的 stdout 是整段缓冲（见
#   `AI_OUTPUT_MODE` 的注释）—— 所谓"首字节"其实是**产物全部生成完的
#   时刻**，它当然接近调用总时长。实测同一条 prompt：text 首字节 114.8s /
#   总 115.7s（read1 全程只触发一次）。
#   于是 2026-10-23 当天 11 轮 AI 调用的耗时挤成
#   `[122,137,137,150,151,152,152,152,152,153,231]`：8 轮卡在 149~155s
#   （闸门致死），唯一成功的 150s 那轮距闸门只剩 0 秒 —— 不是"3.6 倍冗余"，
#   是**零冗余**。上游断流期尤其致命：CLI 一边等上游一边攒缓冲，闸门到点
#   就把一个正在正常工作的调用杀掉。
#   改法不是调大这个数字（那是拿调参掩盖判据错误），而是让 stdout 真的
#   边生成边吐：`AI_OUTPUT_MODE = "stream-json"` 后实测首字节 3.5~10.4s
#   （42KB prompt 也只要 8.4s），150s 重新回到"真·首字节"语义，冗余真实。
#   因此本值**维持 150 不变** —— 变的不是阈值，是它量的是什么。
# 设 0 或 None 关闭（回退到纯 AI_IDLE_LIMIT 语义）。
AI_FIRST_BYTE_LIMIT = 150

# 备用链的首字节上限 —— **已删除**（2026-10-22）。
# 备用模型机制按用户要求取消后，这里一度保留 `AI_BACKUP_FIRST_BYTE_LIMIT = 120`
# 的"废弃常量"，但自检的 [死代码] 段把它判成零引用常量并报 ❌ ——
# 那个判断是对的：留着它只会让人以为备用链还有半条命。机制没了，
# 常量、注释、判据一起删干净。

# AI 分析用的模型 id。
#
# ⚠️ None = **不传 `--model`**，用 claude CLI 自己的默认模型 —— 由本机
# `~/.claude/settings.json` 的 env.ANTHROPIC_MODEL 决定（经 modelOverrides
# 映射到实际上游）。2026-10-08 用户指定从固定 "claude-opus-5" 改为默认模型。
#
# 设为具体 id（如 "claude-opus-5"）即重新钉死某个模型 —— 三处调用点
# （analyze.ai_analyze / selfcheck 的 AI 冒烟）
# 统一经 ai_model_args() 拼参数，改这一处即全部生效，不会漂移。
#
# ⚠️ 本机 Claude Code 配置了**模型映射**：写在这里的 id 会先剥 `[1m]`
# 后缀再查 modelOverrides 表，实际落点由本地配置决定 —— 不要拿环境文档
# 里的 id 列表来"纠正"任何取值。
AI_MODEL = None

# 备用模型。主模型**拿不到可用产出**（空输出 / 卡死且零产出 / 异常）时，
# ai_analyze 用它再试一次。2026-10-13 用户配置 glm-5.3-flash —— 起因是
# 默认链（new-api → deepseek-v4.1-flash）在 10-12/10-13 两天出现 9 次
# 「AI 卡死（连续 600s 无输出）」，每次都整轮退兜底模板；备用模型走
# 同一个 new-api 网关但上游不同，主模型上游挂起时它多半还活着。
#
# ⚠️ 触发条件刻意**不含**「卡死但有部分产出」与「命中禁用词/结构不合格」：
#   · 部分产出已按既有逻辑保留给 produce_text 判断（有产出不换模型）；
#   · 禁用词/结构问题换模型救不了（同一 prompt 换模型大概率同样踩），那是
#     重写与兜底闸门的职责。备用模型只救「**一个字都没拿到**」的通道故障。
#
# ⚠️ 备用调用**同样不设总时长上限**（同一空闲看门狗）—— 备用模型也可能
# 慢，固定预算会重演「打断认真工作的慢调用」的旧病。代价是单轮最坏
# 耗时翻倍（两个模型各 600s 卡死），但看门狗保证它有界。
#
# 设为 None 关闭备用链。经 ai_model_args(model) 带参拼装，不另设全局状态。
#
# ⚠️ **id 必须取 `~/.claude/settings.json` 里 modelOverrides 认识的名字**
# （2026-10-19 实测）。原先写 "glm-5.3-flash" —— 网关对它的实测回应是
#   [claude-code:unrecognized_model] {"model":"glm-5.3-flash","query_source":"sdk"}
# 并**照常按默认模型把活干完**。于是「切备用」变成了"再跑一遍同一个模型"：
# 主调用卡死 600s 之后，备用再卡 600s，整轮白等 ~20 分钟才退回兜底模板，
# 而备用链一次都没真正救过场。
#
# 生产实证（2026-10-19 watch.log，当天 7 条推送里 5 条退化成兜底模板）：
#   09:38 AI 卡死（连续 600s 无输出，已终止），耗时 364s … 切备用模型 glm-5.3-flash
#   09:48 备用模型 glm-5.3-flash 仍无产出（卡死，耗时 618s）
#   10:44 / 11:46 同型各一次（608s / 606s）
# 当天单轮耗时 983/990/1026/1200s，每次都吞掉 ~3 个 launchd 触发
# （14 个整点槽位里只有「9 次被丢弃」记进了 starved）。
# 更早的迹象：2026-10-16 那次记录已写「备用链 3/4 次 unrecognized_model、
# 主备可能同时零产出」—— 当时按网关抖动理解，实为**永久性**：这个 id 网关
# 从来不认。
#
# ⚠️ **id 分派规则**（2026-10-19 实测）：`--model` 的取值先剥 `[1m]` 后缀，
# 再查 `~/.claude/settings.json` 的 `modelOverrides`；**不在表里的名字一律
# 回 unrecognized_model，并按 CLI 默认模型把活干完**（见
# [[model-overrides-mechanics]]）。所以想让备用走某个具体上游，必须先在
# settings.json 里给它一条映射。
#
# 现状（已在本机 settings.json 加好）：
#   claude-fable-5[1m]  → deepseek-v4.1-flash      ← 主链（AI_MODEL=None，
#                                                    走 ANTHROPIC_MODEL 默认）
#   glm-5.3-flash       → glm-5.3-flash（直连该上游）← 备用链
# 两条链**上游不同**，这正是备用存在的理由：主链 deepseek 侧挂起/限流时
# 备用还在。网关侧 glm-5.3-flash 有 16 条渠道（多条 status=1 启用）。
#
# `[实测]` 2026-10-19 长 prompt（真实形态）对比：glm-5.3-flash 32.2s 产出
# 合格两段中文；默认链 31.7s 同样合格。二者耗时相当，质量均可用。
#
# ⚠️ 之前写 "glm-5.3-flash" 之所以**一次性失效**，是因为当时 settings.json
# 的 modelOverrides 里没有它 → 每次都 unrecognized → 主卡 600s 后备用再卡
# 600s，整轮白等 20 分钟才退兜底（当天 7 条推送 5 条成模板）。
# **改模型名时必须同步确认 settings.json 有对应映射**，
# `[AI 通道]` 的备用模型名守卫会在缺失时报 ❌。
# 备用模型 id —— **已删除**（2026-10-22，用户要求「取消备用模型机制」）。
#
# 历史正是取消它的依据（摘要）：
#   · 网关对 `glm-5.3-flash` 回 unrecognized_model 且**照常按默认模型干活**
#     → "切备用"= 原地重跑同一个模型，一次都没真正救过场；
#   · 主链卡死 600s + 备用再卡 600s = 整轮白等 ~20 分钟，
#     每次吞掉 ~3 个 launchd 触发（10-19 单轮最长 1200s）；
#   · 10-21 全天 7 轮 AI 产出 0/7，全部退化成兜底模板。
#
# 取消后的失败路径：主链零产出 → 归因日志（`_single_fail_verdict`）
# → AI 通道熔断 AI_COOLDOWN，期间直接用兜底模板，到期自动恢复尝试。


def ai_model_args(model=None):
    """拼进 claude CLI 命令行的模型参数。

    不带参（或 None）→ 按 AI_MODEL 拼：None 时空列表（CLI 默认模型），
    具体 id 时 ["--model", id]。显式传 id（备用链用）→ ["--model", id]。
    """
    m = AI_MODEL if model is None else model
    return ["--model", m] if m else []


# claude CLI 的 `-p` 输出模式，与它对应的取正文函数。
#
# ⚠️⚠️ 为什么生产要从 text 改成 stream-json（2026-10-23 实测定案）：
#   text 模式（CLI 默认）的 stdout 是**整段缓冲、进程退出时一次性 flush**。
#   `[实测]` 同一条 42KB prompt：text 模式首字节 +114.8s、总 115.7s ——
#   两步之间只差 0.9s，read1 全程只触发**一次**。
#   stream-json 模式则**边生成边输出**：3.5~10s 出首个 JSONL 事件，此后
#   每秒级持续吐 delta；同一条 prompt 首字节 8.4s、总 109.1s。
#   于是 text 模式下的 `AI_FIRST_BYTE_LIMIT`（首字节闸门）测的根本不是
#   「上游快不快」，而是「CLI 什么时候把整体缓冲 flush 出来」——
#   闸门触发的时机 ≈ 输出总长，与调用死活几乎无关。这解释了
#   `market-watch-ai-firstbyte-margin-exhausted` 记的现象（成功轮 150s、
#   致死轮 152s 挤在一起，余量是假的）：那个"余量"从来就是产物时长。
#
#   代价：stream-json 的 stdout 是事件流，调用方必须解析 JSONL 才能拿到
#   正文 —— 由 `ai_extract_text()` 统一收口，不让每个调用点各写一套。
AI_OUTPUT_MODE = "stream-json"
# stream-json 事件流里"真的开始产出正文"的标志。
#
# ⚠️ 为什么闸门必须认这个、而不是认"任意字节"（2026-10-23 实测）：
#   改成 stream-json 后 stdout **一启动就有字节** —— 实测 `system/hook_started`
#   0.5s、`system/init` 2.7s。若继续用「stdout+stderr 全空」当挂起判据，
#   它**永远不会成立**，挂起轮只能等 idle 600s 才被杀，等于把首字节闸门
#   彻底废掉（比改之前更糟：改之前 text 模式至少能靠"零字节"抓到挂起）。
#   实测挂起形态（模型名被网关拒）：0.5s 起有字节、3.3s 出 `api_retry`，
#   此后 **179 秒零内容**，assistant 事件直到 183s 才来。
#   所以判据改为盯**正文事件**，两者任一出现即算"活着"：
#     · `"type":"assistant"` —— 模型真的开始吐字（实测 20.3s）；
#     · `content_block_delta` —— 带 partial 时的逐 token 增量（实测 13.0s，
#       比 assistant 事件更早，慢链上留出更多余量）。
#   ⚠️ `"type":"result"` **不再算正文**（2026-10-28 修，P2）：
#   它是**收尾事件**，正常调用里它必然排在 assistant 之后，所以不放宽
#   任何真实场景；而异常形态下 CLI 可能先吐一个 `is_error` 的 result
#   再挂住 —— 把它当"正文已到"会让首字节闸门**永久解除**，一次真挂起
#   被记成"跑得慢"，只能等 total_limit 240s 才掐，归因指向错误方向。
#   实测首正文时刻：小 prompt 20.3s、42KB prompt 8.4s —— 全部落在 150s
#   窗口内，余量真实（改之前那个"首字节 114.8s"其实是产物全生成完的时刻，
#   见 AI_FIRST_BYTE_LIMIT 的注释）。
_AI_CONTENT_MARKS = (b'"type":"assistant"', b"content_block_delta")

# ⚠️ 2026-11-05（P1）：纯 thinking 流的**内容类型**判据。
# 上面两个 mark 只问"有没有 content 事件"，而 `content_block_delta` 也用于
# thinking 增量（`delta.type="thinking_delta"`）—— 模型可以一直吐思考、
# 零正文，看门狗误判成"活着"，idle 计时被每次思考刷新，于是在
# AI_ROUND_BUDGET=None（无总时长上限）后**永不终止**。
# 这里只用于**升格**警戒：正文事件出现才把警戒解除，纯思考流最多再给
# 一个 first_byte_limit 窗口，超时即按"零正文挂起"终止。
# 判据按**行**扫（stream-json 每行一个 JSON 事件），两种写法都认：
#   ① content_block_delta 事件里的 `"type":"text_delta"`（正常正文流）；
#   ② assistant 事件里的 `"type":"text"` 内容块（不带 partial 时）。
# 故意不认 `"type":"result"`（收尾事件，见 _AI_CONTENT_MARKS 注释）。
_AI_TEXT_MARKS = (b'"type":"text_delta"', b'"type":"text"')
_AI_THINK_MARKS = (b'"type":"thinking_delta"', b'"thinking"')
# 「有 content 事件但一直没正文」最多再等多久（秒）。设计取舍：
#   · 下限不能太小：2026-10-26 的合法慢调用在 thinking 里逐字数字数、
#     988s 才出正文（prompt 已修，62s 恢复）；291s 的生产长调也曾整段
#     在窗口内正常完成。600s 给这两类真实形态留足余量。
#   · 上限不能没有：AI_ROUND_BUDGET=None 后总时长闸门关闭，若模型进入
#     "无限思考"形态，idle 会被每个 thinking 增量刷新、永不触发 ——
#     只能靠这条"无正文"闸门兜底。超时按挂起终止，缓冲原样返回。
AI_TEXT_WAIT_LIMIT = 600


# 正文载荷的位置标记。stream-json 每行是一个完整 JSON 事件，`text` 字段
# 与类型标记同行，所以按行取载荷是安全的。
_AI_TEXT_PAYLOAD = b'"text":"'


def _ai_stream_kind(buf: bytes) -> str:
    """扫已累积的 stdout 事件流：'text'（**有非空正文**）/ 'thinking' / 'none'。

    ⚠️⚠️ 2026-11-06（全量复核 P1-11）：判据必须是「**非空**正文」。
    原先只要某行出现 `"type":"text"` / `"type":"text_delta"` 就返回 text，
    于是 **空 text 增量**（`"text":""`）也会把正文闸门解除：

        {"type":"content_block_delta","delta":{"type":"text_delta","text":""}}

    这一类事件同时做三件事：把 `_content_ts` 置上（首字节闸门解除）、
    把 `_text_ts` 置上（无正文闸门解除）、刷新 idle（卡死闸门解除）——
    而 `AI_ROUND_BUDGET=None` 后总时长闸门也关着。**四道闸门同时失效**，
    一个持续吐空增量的流可以永远占着调度周期，把后续每个 300s 槽位吃掉。
    这正是本函数下游那道 `AI_TEXT_WAIT_LIMIT` 闸门存在的理由，它不该被
    空载荷绕过。

    注意「类型标记存在但载荷为空」**不能**直接判成 thinking —— 那会把
    `content_block_start`（正文块刚开、内容还没来的合法形态）误判成
    空转。这里只是**不认定已有正文**，继续往后扫；真有正文的增量一行
    就能命中。
    """
    _saw_think = False
    for _ln in buf.split(b"\n"):
        if any(_m in _ln for _m in _AI_TEXT_MARKS):
            _i = _ln.find(_AI_TEXT_PAYLOAD)
            if _i >= 0:
                _rest = _ln[_i + len(_AI_TEXT_PAYLOAD):]
                _j = _rest.find(b'"')
                # 没找到收尾引号 = 这一行被 read1 截断了，本轮先不认；
                # 下一轮扫的是完整缓冲，自然会命中。
                if _j >= 0 and _rest[:_j].strip():
                    return "text"
        if any(_m in _ln for _m in _AI_THINK_MARKS):
            _saw_think = True
    return "thinking" if _saw_think else "none"


def ai_output_args() -> list[str]:
    """拼进 claude CLI 命令行的输出模式参数（ai_model_args 的同款套路）。

    text 模式返回空列表（CLI 默认就是 text，不传冗余 flag —— 保持与
    2026-10-23 之前的行为完全一致，回退只需改 AI_OUTPUT_MODE 一个值）；
    stream-json 模式返回 ["--output-format", "stream-json", "--verbose",
    "--include-partial-messages"]。

    ⚠️ 缺 `--verbose` 时 CLI 直接拒收 stream-json（报错退出）。
    ⚠️ `--include-partial-messages`（2026-10-23 实测后加）：不带它时
    **首个 `assistant` 事件要等整条消息生成完才出现**（实测 20.3s；慢链上
    可能逼近首字节窗口而被误杀），带它后 `content_block_delta` 从模型吐出
    第一个 token 就来（实测 13.0s）。看门狗的"正文已到"判据因此能真实贴住
    "模型在产出"这一语义。extractor 只收 assistant 的 text 块，
    partial 事件不影响正文内容。
    """
    if AI_OUTPUT_MODE == "text":
        return []
    return ["--output-format", AI_OUTPUT_MODE, "--verbose",
            "--include-partial-messages"]


def ai_tools_args() -> list[str]:
    """禁用全部内置工具的 CLI 参数（`--tools ""`）。

    ⚠️ 生产 AI 调用是**纯文本生成**（给定 brief + plan，产出一段推送正文），
    不需要任何工具。而 CLI 默认把整套工具（Bash/Write/Read/WebFetch…）
    暴露给模型，模型**真的会用**：
      · `[实测]` 2026-10-23 09:21 那轮：AI 为了核对「450 字上限」，
        连续 9 次调 Bash/Write —— 在 `/tmp/push_check/` 建目录、
        往 `~/tmp_push_msg.txt` 与 `~/tmp_count.py` 写文件（**真落了盘**，
        两个文件到 10-26 还在）。它是拿工具当草稿纸。
      · `[实测]` 2026-10-26 09:36 那轮：AI 又试图 `python3 - <<PY` 数
        中文字数 → `-p` 非交互模式下 Bash 需要审批 → 工具返回
        `This command requires approval` → 模型**白等一轮**（16s 发出调用 +
        154s 重新组织，占掉这轮 1312s 里的一大截），最后还是手写估算。
    两次都是"为了一个本来就该靠 prompt 约束的字数上限"去动工具，
    收益为零、代价是延迟与落盘副作用。

    ⚠️ 不能用 `--allowedTools ""` 代替：那个只收窄**白名单**，模型仍会
    尝试调用并拿到拒绝结果（同上面那次审批拒绝，白等一轮）。`--tools ""`
    是**从工具清单里移除**，模型看不到这些工具，不会产生调用意图。
    `[实测]` 2026-10-26 用**同一份真实生产 prompt**（18428 字符，从那次
    慢调用的会话转录里导出）跑 `--tools ""`：全程零 `tool_use`，
    产出 752 字结构合格的盘前简报，耗时 173.5s。
    ⚠️ 这 173.5s 与那轮 1312s 的差**不能全记在禁用工具头上** —— 那轮
    的大头是一个 45444 字符的 thinking 块（988s 才出第一条 assistant，
    见 `AI_IDLE_LIMIT` 的"慢而不死"），禁用工具只解释其中被审批拒绝
    白等的那 170s。两者是**独立的两笔开销**，本函数只管工具这一笔。
    """
    return ["--tools", ""]


def ai_extract_text(raw: str) -> str:
    """从 claude CLI stdout 取出**正文**文本。

    AI_OUTPUT_MODE 两态各自的收口（口径只在这一处维护，两个调用点
    analyze._invoke / selfcheck AI 冒烟都走这里）：

      · text 模式：CLI 退化 / 部分输出时 stdout 可能混入
        `[claude-code:unrecognized_model]…` 之类的自报行，**只剥 stderr
        同款标记行**，其余原样返回 —— 注意不能在这里先跑 fence/think
        两层剥离，那会打乱 analyze._invoke 既定的 ①fence→②think→③meta→
        ④preamble 顺序（顺序是带实测依据的硬约定，见那边的注释）。
      · stream-json：逐行 JSONL，拼接 `type=assistant` 事件里
        `message.content[].type=="text"` 的文本。[实测] 与 text 模式的
        同 prompt 产物对齐（assistant 分段拼接 == text 单块）；
        错误形态走 `result` 事件：`is_error=true` 时取 `result` 字段
        （形如 `API Error: 503 …`），让上层 safe()/熔断按正文判。
        解析失败/非 JSON 行一律跳过 —— 宁可少剥一层，绝不把 JSONL
        事件流当正文推给闸门。
      · ⚠️ **流式中断的兜底**（2026-10-27 加，见下）：没有任何完整
        `assistant` 正文块时，退回累积 `text_delta` 的分片文本。

    ⚠️⚠️ **为什么需要 delta 兜底**（2026-10-27 实测坐实）：
    带 `--include-partial-messages` 时正文是**逐 delta 流出来的**，完整
    `assistant` 事件要等该文本块 `content_block_stop` 之后才发。调用被
    中途终止（总时长闸门 / idle 闸门 / SIGKILL）时，正文全在 `text_delta`
    里、`assistant` 事件从未出现 —— 原先只认 `assistant`，抽取结果恒空。
    `[实测]` 2026-10-27 生产 10:14 那轮：进程跑到 241s（一直在正常产出，
    否则 150s 的首字节闸门先开）被总时长闸门掐停，日志报「已保留部分产出」
    却紧接着报「无任何产出」并把归因指向网关 —— 两条自相矛盾，真因是
    **正文被抽取链丢了**，害得 `ai_analyze` 误写 10 分钟 AI 熔断。
    delta 兜底只在**没有完整块**时生效（有完整块就用完整块，否则两者
    叠加会把正文翻倍）。

    ⚠️ 非 str 照原样返回（与 _strip_meta/_strip_think 的旧实现对齐：
    这是纯函数，自检会直接拿 123 之类当参数打探）。
    """
    if not isinstance(raw, str):
        return raw
    if AI_OUTPUT_MODE != "stream-json":
        _out = []
        for _ln in raw.split("\n"):
            if _ln.startswith("[claude-code:"):
                continue
            _out.append(_ln)
        return "\n".join(_out)
    _chunks: list[str] = []
    # ⚠⚠ 2026-11-07（复核 F12）：`_deltas` 要记下**它们落在哪一块**。
    # 原实现把所有 delta 合成一个平铺序列，只要 `_chunks` 非空就
    # **整体忽略** delta（`if _chunks: return ...`）。于是：
    #     assistant 块 0 完整输出「第一句」
    #     content_block_start(index=1) + text_delta「第二句」
    #     （进程此时被杀 / 输出被截，没有 assistant 块 1）
    #   -> 只得到「第一句」，第二句已经流出来了却**被丢掉**。
    # 而「截断时保留已产出正文」正是这个函数的契约。
    # 修法：按 `index` 分组；对应完整块已到位的组去重（它已在 `_chunks`
    # 里），尚未完成的组按原顺序补在尾部。缺 `index` 的旧式事件归组 0，与改前行为一致。
    _deltas: list[str] = []
    _deltas_by_idx: dict[int, list[str]] = {}
    _res_err: str | None = None
    for _ln in raw.split("\n"):
        _ln = _ln.strip()
        if not _ln.startswith("{"):
            continue
        try:
            _d = json.loads(_ln)
        except Exception:
            continue
        if not isinstance(_d, dict):
            continue
        _ty = _d.get("type")
        if _ty == "assistant":
            # ⚠️ `message` 必须是 dict 才取 content（2026-10-25 审查实测）：
            # 原先写 `(_d.get("message") or {}).get("content")`，当 message
            # 是字符串（畸形/降级事件）时 `.get` 不存在 → AttributeError
            # 抛出，与 docstring「解析失败一律跳过」矛盾，且会被 analyze
            # 外层 except 吞成"AI 异常"，整轮产出丢弃。
            _msg = _d.get("message")
            _content = _msg.get("content") if isinstance(_msg, dict) else None
            for _b in (_content or []):
                if (isinstance(_b, dict) and _b.get("type") == "text"
                        and isinstance(_b.get("text"), str)
                        and _b["text"].strip()):
                    _chunks.append(_b["text"])
            # ⚠⚠ 2026-11-07（F12 修正）：**完整块到达时丢弃之前累积的 delta**。
            # JSONL 是时序流：落在完整块**之前**的 delta，就是同一块的
            # 早期分片，它的最终形态已由该完整块给出 —— 不该再补，
            # 否则正文翻倍（`[\u5b9e测]` 自检探针：delta「甲」+完整块「乙」 → 旧逻辑得「乙甲」）。
            # 落在完整块**之后**的 delta 才是后续尾部块，那才是 F12 要救的那部分。
            # 用时序判据而不是"内容是否被包含"：后者对“字面相同但确实是新块”
            # 的情形会误判，而时序是硬事实。
            _deltas_by_idx.clear()
        elif _ty == "stream_event":
            # 流式分片兜底（2026-10-27）：累积 text_delta 的正文。
            # 只在最后没有完整 assistant 块时才用它（见 docstring）。
            # 只认 `text_delta` —— `thinking_delta` 是推理过程，绝不能
            # 混进正文（否则会把模型的思考推给读者）。
            _ev = _d.get("event")
            if isinstance(_ev, dict) and _ev.get("type") == "content_block_delta":
                _dl = _ev.get("delta")
                if (isinstance(_dl, dict) and _dl.get("type") == "text_delta"
                        and isinstance(_dl.get("text"), str)):
                    _deltas.append(_dl["text"])
                    _di = _ev.get("index")
                    if not isinstance(_di, int) or isinstance(_di, bool):
                        _di = 0
                    _deltas_by_idx.setdefault(_di, []).append(_dl["text"])
        elif _ty == "result" and _d.get("is_error"):
            _r = _d.get("result")
            if isinstance(_r, str) and _r.strip():
                _res_err = _r
    # ⚠️ 已产出的正文**优先于**错误文本（2026-10-25 审查实测）：
    # 原先 `if _res_err: return _res_err` 会丢掉 assistant 事件里已经拿到的
    # 完整正文 —— 实测 `{"type":"assistant",…"正常正文内容"}` +
    # `{"type":"result","is_error":true,"result":"API Error: 503"}` 只返回
    # 错误串。那正是"模型已写完、收尾时报错"的常见形态（CLI 侧网络抖动），
    # 丢掉等于白跑一整轮 AI 调用。
    if _chunks:
        # ⚠️ 完整块优先，但**尾部尚未完成的块要接上**（F12）。
        # 一个块到底完成了没有，只靠序号是测不准的（块 0 有完整块、
        # 块 1 只有 delta 是最常见的形态）。原实现直接用 `_chunks`
        # 的**数量**去对应，而两边都还没有比块更可靠的耦合信息。
        # 因此取**安全边**：只要某块有 delta 且**它的正文尾部
        # 不是已收完块的尾部**，就把它补上。实现上用一个简单判据：
        # 已完成块的尾部必然是各块文本的**连接**；若连接结果以某组 delta
        # 作为后缀，说明该组已在其中，不重复追加。
        _done = "".join(_chunks)
        # ⚠⚠ 2026-11-07（复核）：**不要再用「内容是否被包含」去重**。
        # 时序判据（上面完整块到达时 `_deltas_by_idx.clear()`）已经处理了
        # 「同一块的早期分片」 —— 剩下的组**必然落在最后一个完整块之后**，
        # 那就是真实的尾部块，应该无条件补上。
        # `[实测]` 却保留了 `in`/`endswith` 两条「安全边」，把真实内容当重复丢掉：
        #   assist("半导体走强，建议观望") + delta("观望", 1)  -> 丢「观望」
        #   assist("指数收红 3%")          + delta("3%", 1)    -> 丢「3%」
        # 尾部块是短数字/单位时命中率很高（正是 F12 要救的那类）。
        _tail_parts: list[str] = []
        for _i5 in _deltas_by_idx:          # 按**到达顺序**，不按 index 排序
            _grp = "".join(_deltas_by_idx[_i5])
            if not _grp or not _grp.strip():
                continue
            # 唯一保留的去重判据：**该组与已完成部分完全相等**。
            # 这种情形意味着 delta 把整段正文又流了一遍，几乎不可能是
            # 真实新内容；而 `_grp in _done` / `endswith` 两条会把**真实的短尾部块**
            # 当重复丢掉（`[实测]` delta("观望") / delta("3%") / delta("走弱")）。
            # 两边都盖不到的情形取**保留** —— 与 F12 的契约一致（截断时
            # 保留已产出正文优先于防重复）。
            if _grp == _done:
                continue
            _tail_parts.append(_grp)
        if _tail_parts:
            return _done + "".join(_tail_parts)
        return _done
    # ⚠️ 无完整块 → 用 delta 分片（2026-10-27）。顺序在 `_res_err` **之前**：
    # 被中途终止的轮次若同时有 is_error 的 result（SIGTERM 后 CLI 可能补一个
    # 错误 result），正文分片仍比那句错误串有用得多 —— 与上面"正文优先于
    # 错误文本"同一条原则。
    if _deltas:
        _joined = "".join(_deltas)
        if _joined.strip():
            return _joined
    return _res_err or ""


# `result.is_error` 的错误正文**形如** `API Error: 503 …`（CLI 固定前缀）。
# 供调用方区分"真产出"与"错误透传"：`ai_extract_text` 在**没有任何正文块**
# 时会把该错误串原样返回（保诊断信息），调用方若不区分就会
# `if out: ai_fail_clear()` 把熔断清掉 —— 上游持续 503 时熔断永远不触发，
# 每轮照烧 AI 调用（2026-10-25 审查实测该路径）。
AI_ERROR_PREFIX = "API Error"


def ai_is_error_text(t) -> bool:
    """`ai_extract_text` 的返回值是否是 is_error 的错误透传（而非真正文）。"""
    return isinstance(t, str) and t.lstrip().startswith(AI_ERROR_PREFIX)


def run_with_idle_watchdog(args, idle_limit=None, first_byte_limit=None,
                           total_limit=None, stdin_text=None, **kw):
    """跑子进程，**不设总时长上限**；只在连续 idle_limit 秒无输出时终止。

    与 `subprocess.run(timeout=N)` 的区别：后者卡的是**总时长**，
    一个跑了 901 秒、期间一直有输出的正常进程会被杀掉并丢弃全部结果。
    这里卡的是**空闲时长** —— 有输出就重置计时器。

    ⚠️ `total_limit`（2026-10-26 加）：可选的**总时长**硬闸门，与 idle 闸门
    并存。语义与上面那段"不要固定超时"**不冲突**，因为二者后果不同：
      · 旧 `subprocess.run(timeout=)` 被杀时**缓冲区连同结果一起丢**；
      · 本函数被杀时 stdout/stderr 的已读内容**完整保留**并返回
        （见末尾 `_dec(b"".join(bufs[1]))`），调用方 `ai_analyze` 的
        超时分支会把它当"部分产出"继续走闸门 —— 内容不白跑。
    所以 total_limit 的代价从"白跑 15 分钟"降到"可能拿到半截正文"，
    而收益是**单轮耗时上界**：一轮超过调度间隔（300s）就会吞掉后续触发
    （launchd 不并发同一 job），2026-10-26 实测一天吞掉 11 次。
    默认 None = 维持原语义（无限等待），只有显式传入才生效。

    实现：用 Popen + 两条线程分别读 stdout/stderr 到内存，主线程轮询
    "最后一次收到输出的时刻"。stdout/stderr 都读到内存（不是 PIPE 直读），
    避免管道写满导致子进程阻塞（AI 输出可达数百 KB）。

    ⚠️ `stdin_text`（2026-11-05 加，P1 安全）：非 None 时把该文本写进子进程
    stdin（**后台线程**写，prompt 可能超过管道缓冲，主线程阻塞写会卡住
    看门狗轮询），用于让 claude CLI 从 stdin 取 prompt 而不是 argv
    （argv 对同机其他用户 `ps` 可见）。传了就**覆盖** `kw["stdin"]`。

    返回 (returncode, stdout_text, stderr_text, timed_out)。
    任何异常都不抛 —— 调用方按"AI 不可用"处理即可。
    """
    import threading
    if idle_limit is None:
        idle_limit = AI_IDLE_LIMIT
    if first_byte_limit is None:
        first_byte_limit = AI_FIRST_BYTE_LIMIT
    if stdin_text is not None:
        kw["stdin"] = subprocess.PIPE
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, **kw)
    except Exception as e:
        return (None, "", f"Popen 失败: {e}", False)
    if stdin_text is not None:
        def _feed():
            try:
                proc.stdin.write(stdin_text.encode("utf-8"))
                proc.stdin.close()
            except Exception:
                pass            # 子进程早退/管道已关：由正常返回路径暴露
        threading.Thread(target=_feed, daemon=True).start()
    bufs = {1: [], 2: []}
    # ⚠️ 这条调用是不是 stream-json 形态 —— 首字节闸门的判据按它分流
    # （字节 vs 正文事件，见 _AI_CONTENT_MARK）。按 **argv 逐 token** 判
    # `--output-format stream-json`，不看 AI_OUTPUT_MODE 常量：prompt 本身
    # 也是 argv 的一员，整串模糊匹配会被 prompt 正文骗到；精确 token 对
    # 才与 CLI 的真实解析口径一致。
    _args_s = [str(a) for a in (args or [])]
    _want_stream = any(_args_s[i] == "--output-format"
                       and i + 1 < len(_args_s)
                       and _args_s[i + 1] == "stream-json"
                       for i in range(len(_args_s)))
    # 正文事件首次到达的时刻（stream-json 形态专用）；None = 还没出正文。
    # ⚠️ 必须显式标注 list[float | None]（同 _reject_ts：裸 [None] 推断成
    # list[None]，写 float 时 basedpyright 报 reportCallIssue）。
    _content_ts: list[float | None] = [None]
    # **真正文**（text_delta / text block）首次到达；与 _content_ts 分开记：
    # thinking 流会让 _content_ts 有值（首字节闸门解除），但不能解除下面的
    # 「零正文」硬闸门。见 `_ai_stream_kind` 的说明。
    _text_ts: list[float | None] = [None]
    _saw_think = [False]
    # ⚠️ 必须用单调钟（2026-10-15 组合场景复核）：macOS 睡眠期间
    # time.time() 照走而子进程被一起挂起 —— 死机/合盖恢复后墙钟 idle
    # 一瞬间超过 AI_IDLE_LIMIT，把"睡眠"误判成"AI 卡死"，杀掉本来
    # 正常的调用退回兜底模板。monotonic() 不含睡眠时间，恢复后 idle
    # 从挂起前的时刻继续计。
    last = [time.monotonic()]
    # 模型名被拒的时刻（见 `_pump` 与主循环的"首字节参考点"）；None = 未出现。
    # ⚠️ 必须显式标注 list[float | None]：裸 `[None]` 会被推断成 list[None]，
    # 写 float 时 basedpyright 报 reportCallIssue（2026-10-21 实测）。
    _reject_ts: list[float | None] = [None]

    def _pump(stream, idx):
        # ⚠️ 必须用 **read1** 而不是 read(4096)。
        # `read(n)` 在管道上会**阻塞到读满 n 字节或 EOF** —— 子进程每秒
        # 输出一行（十几字节）时，read(4096) 一直凑不满，`last[0]` 永不更新，
        # 看门狗把"持续在产出"误判成"卡死"并杀掉它。
        # 实测：子进程每秒 print 一行、共 8 行，idle_limit=3 ——
        #   read(4096)  -> 3.0s 被杀，只拿到 3 行（❌ 误杀正常进程）
        #   read1(...)  -> 8.0s 正常完成（✅）
        # read1 语义是"至少返回一次 read 系统调用的结果"，有数据就返回。
        try:
            while True:
                chunk = stream.read1(65536) if hasattr(stream, "read1") \
                        else stream.read(4096)
                if not chunk:
                    break
                bufs[idx].append(chunk)
                last[0] = time.monotonic()
                # 模型名被拒标记（2026-10-21）：stderr 里出现 CLI 自报的
                # unrecognized_model 时记下**时刻** —— 主循环据此收回
                # stderr 的"活跃"特权、改按 stdout 计首字节（见主循环）。
                # ⚠️ 判**累积缓冲**而不是单块：标记可能被 read1 拆在两块里
                # （`...unrecogn` + `ized_model...`），单块判永远命不中
                # —— 实测拆包进程白等满 idle 才死。缓冲里旧块重复扫的
                # 代价可忽略（stderr 全程通常 < 数 KB）。
                if idx == 2 and _reject_ts[0] is None:
                    if b"unrecognized_model" in b"".join(bufs[2]):
                        _reject_ts[0] = time.monotonic()
                # 正文事件首次到达（stream-json 专用，与上面的 reject 标记
                # 同款：判**累积缓冲**而不是单块 —— JSONL 行会被 read1 拆在
                # 两块里，单块判会漏。每轮拼接的开销与 stderr 扫描同量级）。
                if idx == 1 and _want_stream and _content_ts[0] is None:
                    _all = b"".join(bufs[1])
                    if any(_m in _all for _m in _AI_CONTENT_MARKS):
                        _content_ts[0] = time.monotonic()
                if idx == 1 and _want_stream:
                    _kind = _ai_stream_kind(b"".join(bufs[1]))
                    if _kind == "text" and _text_ts[0] is None:
                        _text_ts[0] = time.monotonic()
                    elif _kind == "thinking":
                        _saw_think[0] = True
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    ts = [threading.Thread(target=_pump, args=(proc.stdout, 1), daemon=True),
          threading.Thread(target=_pump, args=(proc.stderr, 2), daemon=True)]
    for t in ts:
        t.start()
    timed_out = False
    # ⚠️ 显式初始化：`_fb_only` 在 kill 分支里赋值，循环因进程自行退出而
    # break 时从未赋过 —— 裸引用在 `timed_out` 为假时靠短路侥幸不炸，
    # 但 basedpyright 报 reportPossiblyUnboundVariable（2026-10-21）。
    _fb_only = False
    # 总时长闸门触发标记（2026-10-26）。同样显式初始化：进程自行退出时
    # 从未赋值，裸引用会 reportPossiblyUnboundVariable。
    _total_only = False
    # 无正文闸门触发标记（2026-11-05）。同上。
    _text_only = False
    _t0 = time.monotonic()
    while True:
        if proc.poll() is not None:
            break
        _idle = time.monotonic() - last[0]
        # 双闸门：产出中途停了按 AI_IDLE_LIMIT；一个字节都没来按
        # AI_FIRST_BYTE_LIMIT（更短 —— 挂起时第一字节永远不来，等满
        # idle_limit 只是让「吞掉后续轮次」的窗口多 4 分钟）。
        #
        # ⚠️ 模型名被拒后的**首字节参考点切换**（2026-10-21）：
        # `[实测]` 2026-10-20 13:55 / 10-21 09:20：备用链 glm-5.3-flash
        # 的 CLI 把 `[claude-code:unrecognized_model]` 写进 stderr 后，
        # 要么很快按默认模型产出（09:04 实测 30.7s 成功），要么**一直挂**
        # 到 idle 600s（605s / 360s 两例）。stderr 有字节 → 首字节闸门
        # 天然不触发 → 挂起形态白等 10 分钟。
        # 修法不是"见标记就杀"（会误杀回退后能成功的调用，实测 30.7s 那例
        # 就是带标记成功的），而是：**标记一出现，stderr 不再算"活跃"**，
        # 首字节闸门改按 stdout、从标记时刻起计时。即"再给你一个
        # first_byte_limit 窗口把 stdout 吐出来；吐不出来就判死"。
        # 实测形态对照：回退成功者 stdout 在 ~30s 内到达（120s 窗口有 4 倍
        # 余量）；挂起形态零 stdout（窗口一到立即终止，605s → ~120s）。
        _rt = _reject_ts[0]
        if _want_stream:
            # stream-json 形态：stdout 一启动就有字节（system/init 2.7s 起，
            # 见 _AI_CONTENT_MARKS 注释），"零字节"判据永不成立。改盯**正文
            # 事件**：没出过 assistant/delta/result 且超过 first_byte_limit
            # → 挂起（实测挂起形态 179s 零内容就在这条闸门下被杀，
            # 不再等 idle 600s）。
            # reject 参考点同 text 形态：stderr 被拒后按标记时刻重新起算，
            # 给 CLI 回退留一个完整窗口。
            _fb_ref = max(_t0, _rt) if _rt is not None else _t0
            _first_byte_dead = bool(
                first_byte_limit
                and _content_ts[0] is None
                and time.monotonic() - _fb_ref > first_byte_limit)
        elif _rt is not None:
            _fb_ref = max(_t0, _rt)
            _first_byte_dead = (first_byte_limit
                                and not any(bufs[1])
                                and time.monotonic() - _fb_ref > first_byte_limit)
        else:
            _first_byte_dead = (first_byte_limit
                                and not any(bufs[1]) and not any(bufs[2])
                                and _t0
                                and time.monotonic() - _t0 > first_byte_limit)
        # 无正文闸门（2026-11-05，P1）：首字节（content）闸门解除后，若只有
        # thinking、迟迟没有正文，最多再等 AI_TEXT_WAIT_LIMIT 秒 —— 否则
        # "流一直在动"的纯思考调用在无总时长上限时永不结束（见常量注释）。
        _text_dead = bool(
            _want_stream and _content_ts[0] is not None
            and _text_ts[0] is None
            and time.monotonic() - _content_ts[0] > AI_TEXT_WAIT_LIMIT)
        if _idle > idle_limit or _first_byte_dead or _text_dead:
            timed_out = True
            # 触发来源（2026-10-14 审查 P2）：首字节闸门触发的终止，
            # 此前在 ai_analyze 日志里被硬编码报成
            # 「连续 {AI_IDLE_LIMIT}s=600s 无输出」—— 排查者按 600s 推算
            # 被吞轮数会差 4 分钟。在 stderr 前加来源标记，调用方日志透传。
            _fb_only = (_first_byte_dead and not (_idle > idle_limit))
            _text_only = (_text_dead and not (_idle > idle_limit)
                          and not _first_byte_dead)
            try:
                proc.terminate()
                proc.wait(timeout=AI_KILL_GRACE)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            break
        # 总时长闸门（2026-10-26）：与 idle/首字节闸门并列的第三条终止来源。
        # 判据用**总耗时**而不是 idle —— 这条闸门要治的是"跑得慢但在产出"：
        # 那种进程永远不会触发前两条闸门，却能吃掉后面好几个调度周期。
        # 放在 idle 判据**之后**：同样到点时优先归因给 idle/首字节（信息更具体）。
        if total_limit and time.monotonic() - _t0 > total_limit:
            timed_out = True
            _total_only = True
            try:
                proc.terminate()
                proc.wait(timeout=AI_KILL_GRACE)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            break
        time.sleep(0.5)
    for t in ts:
        t.join(timeout=AI_KILL_GRACE)
    try:
        rc = proc.wait(timeout=AI_KILL_GRACE)
    except Exception:
        rc = None
    _dec = lambda b: b.decode("utf-8", "replace")
    _se = _dec(b"".join(bufs[2]))
    if timed_out and _fb_only and _reject_ts[0] is not None:
        # 模型名被拒后仍未吐出 stdout —— 与普通首字节超时区分开：
        # 排查方向不同（前者查模型配置/网关，后者查通道挂起）。
        _se = (f"[first-byte-gate-after-model-reject {first_byte_limit}s 触发] "
               + _se)
    elif timed_out and _fb_only:
        _se = f"[first-byte-gate {first_byte_limit}s 触发，非 idle 闸门] " + _se
    elif timed_out and _text_only:
        # 无正文闸门（2026-11-05）：模型一直在 thinking、从不吐正文。
        # 与"卡死"（idle）和"慢但产出"（total-limit）都不同：排查方向是
        # prompt 是否诱导了过度推理 / 模型是否进入空转。
        _se = (f"[text-gate {AI_TEXT_WAIT_LIMIT}s 无正文（仅 thinking）"
               f"触发，非 idle 闸门] " + _se)
    elif timed_out and _total_only:
        # 总时长闸门（2026-10-26）：与上面两种区分开。它的语义是"跑得慢但
        # 一直在产出"，不是卡死 —— 排查方向完全不同（查 AI 自身耗时 /
        # prompt 让它反复思考）。
        # ⚠️ 标记里**不能**断言"已保留部分产出"（2026-10-27 修）：本函数
        # 只保证"读到的缓冲区原样返回"，缓冲区是否为空由调用方判断；
        # 原先写死这句，零产出时日志自相矛盾（生产实测「…已保留部分产出，
        # 耗时 241s，且无任何产出」）。
        _se = (f"[total-limit-gate {total_limit}s 触发，非 idle 闸门；"
               f"缓冲区原样返回] " + _se)
    return (rc, _dec(b"".join(bufs[1])), _se, timed_out)


def isnum(v: object) -> TypeIs[int | float]:
    """是否为可用数值。排除 nan/inf/bool。

    全库统一的数值判据 —— 原先各处裸写 isinstance(v,(int,float))，
    会放行 nan（比较永远 False -> 阈值静默漏报）和 bool（True 被当 1）。

    ⚠️ 返回类型标注成 TypeIs 而非 bool，**不是**装饰性改动：
    这个函数是全库唯一的数值闸门（约 200 个调用点），而它的典型用法是
        if not M.isnum(f62) or not M.isnum(f184): continue
        ...
        abs(f62) < threshold(...)          # pyright 认为 f62 可能是 None
    标注成裸 bool 时类型检查器**不会**在通过闸门后收窄类型，于是下游每一个
    `abs(f62)` / `f62 / 1e6` / `f62 >= 0` 都被报成"可能对 None 做运算"
    （实测 reportOptionalOperand 11 条 + reportOperatorIssue 5 条，全部是
    这一处标注缺失造成的假警报，与真实缺陷混在一起，把信号淹掉）。
    TypeIs（PEP 742，Python 3.13+）在**两个分支**都做收窄，语义与运行时
    行为完全一致：isnum 返回 False 时确实不是 int/float。
    """

    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return v == v and v not in (float("inf"), float("-inf"))


# ---------- 原子文件读写 ----------
def _atomic_write_json(path, obj, indent=None):
    """原子写 JSON：唯一临时名 -> fsync -> os.replace。

    唯一名必须由 mkstemp 生成 —— 曾经用 pid+毫秒，同毫秒并发会撞名，
    先完成的 replace 掉临时文件、后完成的 replace 报 ENOENT（静默丢状态）。
    返回 True/False，调用方按需决定是否记日志。
    """
    d = os.path.dirname(path) or "."
    fd = tmp = None
    try:
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + ".tmp.")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None                      # 已交给 with 管理
            json.dump(obj, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())           # 断电时不留空文件
        os.replace(tmp, path)
        # ⚠️ 必须再 fsync **目录**，rename 本身才是持久的。
        # 上面那次 fsync 只保证"临时文件的内容"落盘；os.replace 改变的是
        # 目录项，而目录项的持久化要单独 fsync 目录。实测（拦截 os.fsync
        # 看 fd 类型）：本函数全程只 fsync 过 FILE，从未 fsync 过 DIR。
        # 后果：断电后 rename 可能未落盘 —— state.json 回退到上一版，
        # fired[] 丢掉最近一轮，下一轮把同一条消息**再推一遍**
        # （与 _commit 落盘失败是同一个失败模式）。
        # best-effort：部分文件系统不支持对目录 fsync，失败不影响写入结果。
        try:
            _dfd = os.open(d, os.O_RDONLY)
            try: os.fsync(_dfd)
            finally: os.close(_dfd)
        except Exception:
            pass
        return True
    except Exception:
        if fd is not None:
            try: os.close(fd)
            except Exception: pass
        if tmp:
            try: os.unlink(tmp)
            except Exception: pass
        return False


def _load_text(path, default=""):
    """读文本；任何异常返回 default。避免 open(...).read() 泄漏句柄。"""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return default


def _load_json(path, default=None):
    """读 JSON；任何异常返回 default（文件不存在、半截、编码错）。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _open_lockfile(p):
    """以 0o600 打开（必要时创建）锁文件，返回文本文件对象。

    ⚠️ **不能**写 `open(p, "a")` —— 文件不存在时它按 **umask** 创建，
    launchd 下 umask 022 → 文件是 **644**。锁文件虽然为空、不含任何内容，
    但 `-rw-r--r--` 意味着**同机其他本地用户也能 open 它、并 flock 它**
    （flock 只要求一个可用的 fd），足以把 `_reserve` / `_locked_update`
    一起卡住 —— 又一个本地可及的拒绝服务面。
    `os.open` 的 mode 参数**只在创建那一刻**生效，所以必须走它。
    `[实测]` 2026-10-09 权限审计：`cache/{daily,host_stat,}.
    {json,}` 那批 `.lock` 六个里有四个是 644。
    """
    _d = os.path.dirname(p)
    if _d:
        os.makedirs(_d, exist_ok=True)
    return os.fdopen(
        os.open(p, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600),
        "a", encoding="utf-8")


def _locked_update(path, mutate, indent=None):
    """跨进程安全的「读-改-写 JSON」：排他 flock 内**重新读盘** -> mutate -> 原子写。

    `_atomic_write_json` 只保证**单次写**不撕裂，不保证**读-改-写**不丢更新：
    两个进程各自"读全量 -> 内存改 -> 整份写回"时，后写的会覆盖先写的增量
    （lost-update）。`dcache_set`/`RateLimiter._reserve` 都用 flock + 锁内重读
    解决，这里抽成通用 helper 供 dead_hosts / host_stat / host_fail / 日志去重共用。

    `mutate(d)` 就地把改动写进 d；返回 False 表示本次**不写**（如"今天已记过"）。
    返回 True 表示执行了写入。**任何异常都吞掉** —— 这些都是统计/状态文件，
    绝不能拖垮取数主链路。
    """
    try:
        with _open_lockfile(path + ".lock") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                d = _load_json(path, None)
                if not isinstance(d, dict):
                    d = {}
                if mutate(d) is False:
                    return False
                # ⚠ 2026-11-11（审查，P2）：**返回真实落盘结果**。
                # `_atomic_write_json` 失败返 False，而这里丢弃返回值、
                # 无条件 `return True` —— 磁盘满 / 权限错误时，调用方
                # 不仅不知道失败，还会把“已持久化”记进内存。
                return bool(_atomic_write_json(path, d, indent=indent))
            finally:
                try:
                    fcntl.flock(lk, fcntl.LOCK_UN)
                except Exception:
                    pass
    except Exception:
        return False


DEAD_HOST_FILE = p_cache("dead_hosts.json")
# 秒。**判死冷却时长**。
#
# ⚠️⚠️ 2026-11-17 从 1800(30min) 改到 300(5min)。原注释写"必须显著大于
# cron 间隔(300s)"，那个取舍在今天被证伪了：
#
# `[实测]` 2026-11-17 上午，东财主机在 09:35~10:05 期间接连被判死，
# 而 **10:15 再测 push2test/push2 全部返回 200**（连续 5 次，0.22s~2.3s），
# 且板块 `90.BK0465`、`f62`/`f184`/`f104`/`f105` 全都正常。
# 也就是说：**不是被风控，是瞬时抖动 + 30 分钟冷却**。
# 代价是那 30 分钟里 12 个关注板块全部 0 数据，播报退化成
# 「板块接口本轮不可用」+ 8 个板块全是昨收 —— 而源其实早就恢复了。
# 一次抖动丢 6 轮，比"每轮重探一次"昂贵得多。
#
# 现在改成 TTL = 5 分钟（= 一个采集周期），并配合**半开探测**
# （见 `_host_dead` 的 half_open 参数）—— 到期后第一轮**只探一次**，
# 成功即整轮恢复，失败则继续等下一轮。这样既不会"每轮都重探已死主机"
# （原注释担心的白烧 curl），又不会把一次抖动放大成 30 分钟盲区。
DEAD_TTL = 300

# ⚠️⚠️ 2026-11-18（按本机实测数据加的指数退避）：
#
# 固定 300s 的代价，在**长期不可用**的主机上会变成纯噪音。本机实测：
#
#     主机            半开探测次数   探测后真的恢复
#     push2test(em_test)    30            0*      ← 一整天都在空探
#     push2                 32            8
#
# (*) em_test 在 11-17 11:01 之后**再没顶上过**，但每 5 分钟仍探一次，
#     于是 watch.log 里整天是「半开探测：push2test … 冷却期已过」。
#     这些行对诊断毫无价值 —— 它说的只是"又试了一次，又失败了"。
#
# 更糟的是**它掩盖了真信号**：11-17 那次事故（TTL 30 分钟导致 6 轮数据盲区）
# 之所以难查，就是因为日志里混着大量同形的空探行。
#
# 修法：**连续失败次数越多，冷却越久**（指数退避，封顶 1 小时）：
#
#     第 1 次判死 → 300s     （5 分钟，与原来一致：瞬时抖动要能快速恢复）
#     第 2 次      → 600s    （10 分钟）
#     第 3 次      → 1200s   （20 分钟）
#     第 4 次及以后 → 3600s   （1 小时，封顶）
#
# 为什么第 1 次不变：绝大多数"死"是瞬时抖动（本机 push2 探测 32 次恢复 8 次
# = 25%），第一次必须保持灵敏。退避只惩罚**反复失败**的主机。
#
# 上限 1 小时而不是更长：站点解封后最多等 1 小时就能被重新启用，
# 不会像 PERMANENTLY_DEAD 那样要人工干预。
_COOL_BASE = DEAD_TTL
_COOL_CAP = 3600

# **永久**封锁的主机：直连与所有兜底都不再尝试。
#
# 与 DEAD_TTL（临时判死、到期重试）的区别：这些主机是**按主机名**被目标
# 站点永久封锁（`curl exit 52`），不是临时故障 —— 重试永远不会成功。
#
# 为什么必须单独列出来：降级链（代理 → firecrawl）加进来之后，一台
# **必然失败**的主机每轮仍会付出「代理请求 + firecrawl 请求」的代价 ——
# `[实测]` 2026-09-29 13:08 那轮为 push2delay 白付 2 次 firecrawl
# （各约 2.3s，且消耗额度），最后必然 502。一小时里 4 次这样的空转。
# 临时判死（DEAD_TTL 30 分钟）挡不住它 —— 到期后又会重来一遍。
#
# ⚠️ 这是**运维开关**，不是代码逻辑：站点解封后需要人工把它从这里删掉。
# 正因为如此，跳过时**仍然记一条日志**（低频率，只记第一次），
# 免得"永远静默"让人以为它一直在正常工作。
PERMANENTLY_DEAD = {"push2delay.eastmoney.com"}

# 主机级熔断阈值：**跨进程滚动窗口**内的失败次数（不是单进程累计）。
# 单进程内 cj() 最多重试 3 次，所以阈值必须按"跨轮累计"来定 ——
# 原值 10 配合进程内字典 = 永不触发。取 6：约两轮（10 分钟）连续失败即熔断。
HOST_FAIL_THRESHOLD = 6

# 配额耗尽哨兵。cj() 用它区分"本地限流"与"主机失败" —— 前者与主机可用性
# 完全无关，绝不能写进主机健康档案（见 _try_hosts）。
#
# ⚠️ 必须是**假值**（falsy）且不能被当 dict 用。
# 原先用裸 `object()`：它是 truthy，于是全库所有 `if d and d.get(...)`
# 的写法都会**穿透**它，直接 AttributeError: 'object' object has no attribute 'get'
# （实测 selfcheck 的快讯/板块列表两项就这样炸掉）。
# 做成 falsy + 任何属性访问都返回哨兵自身，使 `d.get("data")` 这类链式调用
# 静默降级为"无数据"，与原有的 `if d and ...` 守卫天然兼容。
class _RateLimited:
    _instance = None
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    def __bool__(self): return False
    def __repr__(self): return "<RATE_LIMITED>"
    def __getattr__(self, name):
        # 链式取值一律返回自身，最终因 falsy 而短路
        return self
    def __call__(self, *a, **k):
        # 使 `d.get("data")` 这类"取属性后立即调用"的链式写法也能短路
        return self
    def __getitem__(self, k): return self
    def __len__(self): return 0
    def __iter__(self): return iter(())

RATE_LIMITED = _RateLimited()


class _HostCircuit(_RateLimited):
    """**主机熔断**哨兵（2026-11-05 加，P1）——必须与"日配额耗尽"可区分。

    `cj()` 原先两个原因都返回 `RATE_LIMITED`，而 `_try_hosts()` 收到它就
    **放弃全部候选主机**（`return got`）并打「日配额耗尽」——只要有一台
    主机进了 5 分钟熔断冷却，本轮连健康的备用主机都不再尝试，港股/板块
    数据整批缺失。这里给出独立哨兵：`_try_hosts` 只是**跳过这一台、继续
    下一台**。语义与 `RateLimiter.host_open()` 同源（纯查询、无副作用）。
    """
    _instance = None      # ⚠️ 必须自己置 None：否则继承父类的单例实例
    def __repr__(self): return "<HOST_CIRCUIT>"


HOST_CIRCUIT = _HostCircuit()

# 半开探测的进程内去重：同一轮里同一台主机只允许一次"试探"，
# 避免一次取数（21 个标的）对同一台刚到期的主机连发 21 个探针包。
_HALF_OPEN_TRIED = set()


def _host_dead(host, half_open=True):
    """该主机最近是否连续失败到不可用（跨进程共享）。

    `half_open=True`（默认）时启用**半开探测**语义：
      · 冷却期内（`age < DEAD_TTL`）→ 判死，不请求；
      · 刚过冷却期且本轮**还没探过** → 判活（放它试一次），并记下本轮已探；
      · 试失败 → 调用方 `_mark_dead` 重新计时；试成功 → 不即会再判死。
    这就是熔断器的 half-open 状态。它解决的是 2026-11-17 那次事故：
    30 分钟 TTL 让一次瞬时抖动变成 6 轮数据盲区，而源 10 分钟就恢复了。
    探针成本已被 `_HALF_OPEN_TRIED` 控制在"每轮每主机最多一次"。
    """
    try:
        d = _load_json(DEAD_HOST_FILE, {})
        ts = d.get(host, 0)
        # 必须守 isnum：坏数据（手工写字符串 ts）会让相减抛 TypeError，
        # 被下面的 except 吞成 False = "主机健康"，于是又去打死主机 3 次。
        # 同一类守卫在 hot.py 的 HOT_CACHE.ts 已加，这里曾经漏掉
        # （feeds.FALLBACK_CACHE.ts 也加过，但该死链已于 2026-10-09 删除）。
        if not isnum(ts):
            ts = 0
        # 指数退避：按该主机的**连续失败次数**决定冷却期（见 _COOL_BASE 的注释）。
        # 次数存在 `主机#n` 键里，与时间戳分开 —— 这样 DEAD_HOST_FILE 的
        # 既有读者（自检、webui）看到的仍然是 `{host: ts}` 的形状。
        _n = d.get(host + "#n", 1)
        if not isnum(_n) or _n < 1:
            _n = 1
        _cool = min(_COOL_CAP, _COOL_BASE * (2 ** (min(int(_n), 8) - 1)))
        if time.time() - ts < _cool:
            return True
        # ---- 冷却期已过：半开探测 ----
        # 只对"真的死过"的主机走这条路（ts>0）；从未失败过的主机不该被
        # 记进这个集合，否则它在冷却期外的第一次失败会被误当成"已探过"。
        if ts and half_open:
            if host in _HALF_OPEN_TRIED:
                # 本轮已经探过一次仍失败（调用方又 mark_dead 了）→ 继续判死，
                # 不再重复发包。下一轮进程重启时集合清空，自然再探一次。
                return True
            _HALF_OPEN_TRIED.add(host)
            # 带上连续失败次数：只有第 1 次值得关注（可能是瞬时抖动），
            # 第 N 次（N≥3）说明这台已经长期不可用 —— 直接调大 DEAD_TTL
            # 或把它加进 PERMANENTLY_DEAD 比反复探测更实际。
            _tag = f"（连续失败 {int(_n)} 次，冷却 {_cool//60} 分钟）" if _n > 1 else ""
            log(f"半开探测：{host} 冷却期已过，本轮放行一次试探{_tag}", "FEED")
            return False
    except Exception:
        pass
    return False

# 本轮已计过失败的主机（见 `_mark_dead` 的 P1 说明）。
# 一轮 = 一次顶层 `quotes()` 的生命周期，由 `_round_begin()` 清空。
# 为什么必须是进程级可变集合而不是参数传递：`_mark_dead()` 的调用点
# 分散在 `cj()` / `_try_hosts()` / 港股/QDII 备用源里，逐层传参要改
# 5 个函数签名；而这个集合的语义就是"本进程本轮"，与 `_HALF_OPEN_TRIED`
# 完全同族（那个也是进程级）。
_MARK_ROUND = set()

def _round_begin():
    """开始新一轮取数：重置「本轮已计过失败的主机」。

    ⚠️ 只该由**顶层取数入口**调用（`quotes()`）。
    ⚠️⚠️ **不要在这里清 `_HALF_OPEN_TRIED`** —— 那会打破半开探测的
    「每轮每主机最多探一次」保证。

    `[自查]` 我第一版写了 `_HALF_OPEN_TRIED.clear()`，随后实测：
        同进程调 2 次 quotes() → _round_begin 被调 2 次
        → 探测令牌被重置 2 次 → 死主机一轮内可被探 2 次
    而 `_HALF_OPEN_TRIED` 的**原始语义就是进程级**（见 `_host_dead` 的
    注释：「下一轮进程重启时集合清空，自然再探一次」）。launchd 每轮起
    新进程，所以"进程级"恰好等于"轮级"，不需要也不该在这里重清。

    对比：`_MARK_ROUND` 必须每轮清，否则长驻进程（webui）里计数永远
    不涨、退避永不生效；而清一次的代价只是"同进程多调几次 quotes()
    可能多计一两次"，比原来的 9 次低一个数量级。
    """
    _MARK_ROUND.clear()

def _mark_dead(host):
    # ⚠️⚠️ **手工验证这道状态机时，先把状态重定向到 /tmp。**
    # `[实测]` 2026-11-17：为验证半开探测，直接在脚本里调
    # `_mark_dead('https://test.invalid.example')` —— 它写的是**生产**
    # `state/cache/dead_hosts.json`，并往 `logs/watch.log` 留了两行
    # 假日志。数据本身会自愈（`_host_dead` 过 TTL 就不再判死），
    # 但一行假日志会长期污染诊断线索。
    # 正确做法：
    #     import marketlib as M
    #     M.DEAD_HOST_FILE = "/tmp/dead_hosts.test.json"
    #     M.log = lambda *a, **k: None        # 别写生产日志
    # 项目里有 `[自检对生产状态的副作用]` 这道守卫，但它只守
    # `selfcheck.py` **自己**的 11 类探针 —— 守不住外部手工脚本。
    # 也就是说这类污染**没有任何自动闸门**，只能靠这条注释提醒。
    #
    # ⚠️ 读-改-写必须整体进 flock（_locked_update）：原实现是无锁的
    # `_load_json -> 改 -> _atomic_write_json`，两个进程同时操作不同主机时
    # 后写者会用自己读到的旧快照覆盖先写者的标记（lost-update）——
    # 已死主机被提前重新请求（白扣配额）或健康主机被误标为死。
    #
    # ⚠️ 判死必须**留痕**（2026-10-21 补）：此前这里是**静默**的 ——
    # `cj()` 重试耗尽后直接调它，一条日志都不打。
    # `[实测]` 2026-10-21 15:36:47 两台 EM 主机同时被写进 dead_hosts，
    # 而同一时段 watch.log 里**没有任何一条**主机失败记录（只有 firecrawl
    # 超时和「quotes 降级」）。事后要查"为什么 15:40 自检报 eastmoney ❌"
    # 只能靠 dead_hosts.json 的 mtime 反推 —— 与 v1 之前"埋点失败静默"
    # 同型。判死是**改变后续 30 分钟取数行为**的状态变更，必须可回放。
    #
    # 日志用 FEED 级 + 按日去重：一台主机一天判死多次是常态（TTL 30 分钟，
    # 反复失败会反复标），裸 log 会把 watch.log 刷满（同 _daily_log_once
    # 的理由）。留一行就够定位"什么时候死的、哪台"。
    now = time.time()

    def _mu(d):
        # 顺手剪掉早已过期的条目，否则文件只增不减。
        # 非数值 ts（手工编辑/半截写入）一并清掉，否则 now - t 抛 TypeError。
        # 注意跳过 `#n` 键：它存的是计数不是时间戳，按时间判会被误删。
        for h in [h for h, t in d.items()
                  if not h.endswith("#n")
                  and (not isnum(t) or now - t >= _COOL_CAP)]:
            # 用 _COOL_CAP（1 小时）而不是 DEAD_TTL 清理：退避后条目可能
            # 仍然有效，按 DEAD_TTL 清会把计数和短冷却的条目一起抹掉。
            del d[h]
            # ⚠️⚠️ 2026-11-19：**必须连计数一起删**，否则留下孤儿键。
            #
            # 原先只 `del d[h]`，`h#n` 永远不被清理 —— `[实测]` 沙箱复现：
            #     造 {old.host: 2小时前, old.host#n: 8} → 触发一次清理后
            #     剩 {new.host, new.host#n, old.host#n: 8}
            # 孤儿键的后果不是"文件大了一点"，而是**退避从最坏档开始**：
            # 这台主机下次再抖一次，`_mark_dead` 会把 8 加到 9，
            # 一上来就是 60 分钟冷却 —— 而不是设计意图的 5 分钟。
            # 也就是说：**历史上的失败会永久累积并拖累将来的恢复速度**，
            # 这正好是"指数退避应当按'连续'失败计"的反面。
            _nk = h + "#n"
            if _nk in d:
                try:
                    del d[_nk]
                except KeyError:
                    pass
        d[host] = now
        # ── 累加跨轮连续失败次数（供上面的指数退避用）──
        #
        # ⚠️⚠️ 2026-11-19（P1，退避被放大 3~5 倍）：**同一轮内不重复计数**。
        #
        # `#n` 的语义是「跨**轮**连续失败的轮次数」（见 1575 行的设计注释：
        # 第 1 轮 300s、第 2 轮 600s…），但原实现每调一次 `_mark_dead()`
        # 就 +1。而一轮 `quotes()` 里同一台主机会被判死**多次** ——
        # 9 个指数、12 个板块逐个取，每个标的失败都 `_mark_dead()` 一次。
        #
        # `[实测]` 沙箱（直连/代理/firecrawl 全置为必失败）：
        #     一轮 quotes() → _mark_dead 共 **9 次**
        #     push2(https) 计 3 次 → n 从 0 直接跳到 3（冷却 1200s，跳过前两档）
        # 两轮就封顶 3600s。这正是生产 13:06（手工清零后）到 13:20
        # 又爬回 n=8 的全过程 —— 主机只抖了一轮，却被当成"连续 3~5 轮失败"。
        #
        # 反过来也说明另一件事：**原设计里那个"连续"是假的**。它数的是
        # "失败请求数"，与"连续失败轮数"在标的数量多时相差一个数量级。
        _prev = d.get(host + "#n", 0)
        if not isnum(_prev) or _prev < 0:
            _prev = 0
        if host in _MARK_ROUND:
            # 本轮已计过：只把时间戳刷新到最新（冷却从**最后一次**失败算起，
            # 这是对的 —— 否则一轮内先失败的主机会提前解锁）。
            d[host + "#n"] = int(_prev)
        else:
            _MARK_ROUND.add(host)
            d[host + "#n"] = int(_prev) + 1
        return True
    if _locked_update(DEAD_HOST_FILE, _mu):
        pass
    try:
        _daily_log_once(
            host, "markdead",
            f"主机判死 {host}（{DEAD_TTL//60} 分钟内直连不再尝试，只走兜底）"
            f" —— 本日首次；若站点明明健康，查是否被瞬时超时误判")
    except Exception:
        pass

def _clear_dead(host):
    def _mu(d):
        _hit = False
        # 恢复后必须**连次数一起清零** —— 否则这台下次再挂，
        # 会直接从"第 N 次"的 1 小时冷却开始，白白错过快速恢复。
        for _k in (host, host + "#n"):
            if _k in d:
                del d[_k]
                _hit = True
        return _hit
    _locked_update(DEAD_HOST_FILE, _mu)

# ---------- 网络异常备用方案：firecrawl 云端抓取 ----------
#
# 直连全部失败（本机网络/DNS/目标站点对本地 IP 封禁）时，用 firecrawl 的
# 云端抓取作为**最后一道兜底** —— 请求从 firecrawl 的服务器发出，
# 完全绕开本机网络。
#
# `[实测]` 2026-09-29 11:1x：东财两个主机（push2delay / push2test）直连
# **同时返回空**，clist 也不可用，collect 连续 8 轮报 `no_data`；
# 而经 firecrawl 抓 `push2test` 拿到完整数据（`{"rc":0,...,"f2":3826.51}`）。
# 同一时刻腾讯源直连正常 —— 所以是**东财对本地网络的封禁**，不是本机断网。
#
# 网络异常时的**多级降级**（做成通用能力，不针对任何特定站点）：
#
#     直连  →  代理(PROXY)  →  firecrawl 云端
#
# 顺序按"代价从低到高"：
#   · 直连      —— 零额外代价
#   · 代理      —— 本机局域网内，实测 **0.19s**，不消耗外部额度
#   · firecrawl —— 云服务，实测 **2.3s**（慢 12 倍）、消耗额度、有并发限制
#
# 代理：走 `MW_PROXY` 指定的地址（作者当年用的是局域网内的一台，
# 具体地址不写进仓库 —— 那是环境信息，对别人没有意义）。
# ⚠️ 它**曾经不可用**（2026-09-29 上午 `curl -x` 报 `Connection refused`），
# 当时代理与 firecrawl 都试过，只有 firecrawl 能用，故先只做了 firecrawl。
# 后经用户告知代理已恢复，复测确认可用且稳定：
#     `curl -x <proxy> https://example.com/` 连续 6 次全 200；
#     `push2test` 经代理连续 3 次全 200（0.19s）—— 而直连当时全空。
# 所以两级都保留：代理优先（快、免费），代理不行再走 firecrawl。
#
# 设计要点：
#   · 任何走 `cj()` 的 JSON 取数、以及 `feeds._curl()` 的文本取数
#     都自动获得这两层兜底，调用方零改动
#   · **只在直连全部失败后触发**
#   · 结果过与直连**同一套**校验，保证契约一致
#   · 任何异常都吞掉 —— 兜底失败不能让调用方多一种失败模式
#
# launchd 环境注意：firecrawl 是 `#!/usr/bin/env node` 脚本，而 plist 的
# PATH 只有 `/usr/bin:/bin:/usr/sbin:/sbin` —— **必须用 node 的绝对路径**
# 调用（实测 `env -i PATH=/usr/bin:/bin` 下直接跑 firecrawl 报
# `env: node: No such file or directory`）。凭证在
# `~/Library/Application Support/firecrawl-cli`，HOME 下可读到。
# 代理地址。**默认为空 = 不启用代理兜底。**
#
# ⚠️ 2026-11-17（开源通用化）：原默认值是某个内网地址
# 那是作者内网的一台代理，是本机环境信息，对别人毫无意义。
# 改成空串默认，需要代理的人自己设
# `MW_PROXY`（例如 `export MW_PROXY=http://127.0.0.1:7890`）。
# 设成空串与不设等效，都是禁用（`proxy_get` 见到空 PROXY 直接返回 None）。
PROXY = os.environ.get("MW_PROXY", "")


def proxy_get(url, ref=None, timeout=12, enc=None):
    """经代理抓 URL，返回文本；未配置代理/失败返回 None。

    通用能力：任何 HTTP 抓取都能用，不限于 JSON 接口。
    与 firecrawl_get 的区别：本机局域网内，快（实测 0.19s）且不耗额度。
    """
    if not PROXY:
        return None
    _hk = _proxy_host_key(url)
    # 失败冷却（黑洞型故障的记忆）：冷却期内跳过，防每轮重付 20s×N。
    # ⚠ 按**目标主机**判，不是全局 —— 一台站点挂掉不该停掉所有源（见 _proxy_fail_get）。
    # ⚠⚠ 冷却期内也要**计一次失败**：否则冷却到期时计数已经归零，
    # 黑洞主机每 600s 又能重付一轮（永远不会被彻底隔离）。
    if time.time() < _proxy_fail_get(_hk):
        _proxy_fail_bump(_hk)
        return None
    args = ["curl", "-s", "--compressed", "--max-time", str(timeout),
            "-A", UA, "-x", PROXY]
    if ref:
        args += ["-H", f"Referer: {ref}"]
    args.append(url)
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout + 5)
    except Exception as e:
        log(f"代理请求异常: {type(e).__name__}: {e}", "FEED")
        _proxy_fail_bump(_hk)
        return None
    raw = r.stdout
    if not raw:
        # 空响应 = 拒连或黑洞超时的主要产物。
        # ⚠ 2026-11-10：**不能单次就冷却**（目标站点上游挂了也会产生空响应），
        # 且**必须按 host 分账**（否则单站点故障会株连全部源）。
        _proxy_fail_bump(_hk)
        return None
    if enc == "auto":
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            raw = raw.decode("gbk", "ignore").encode("utf-8")
    _proxy_fail_reset(_hk)       # 成功一次就清该主机的计数，它立即恢复可用
    return raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw


def proxy_json(url, ref=None, timeout=12):
    """经代理取 JSON 并解析；失败或**形状不是 dict** 时返回 None。

    ⚠️ 必须校验 `isinstance(d, dict)`，不能只 `json.loads` 成功就算拿到。
    `[实测]` 网关错误页的 body 常常**恰好是合法 JSON 标量**：
    502 页正文就是 `502`，`json.loads("502")` 返回 int 502 而不抛异常。
    于是"兜底拿到错误页"会被当成"兜底成功"，返回一个 int 给调用方
    （`_fallback_get` 的契约写的是 dict 或 None）——
    调用方的 `d.get(...)` 抛 AttributeError，或被 isinstance 守卫
    静默吃掉，表现为**数据凭空消失且日志说成功**。
    本项目所有取数接口的形状都是 JSON 对象，标量一律视为失败。
    """
    txt = proxy_get(url, ref=ref, timeout=timeout)
    if not txt:
        return None
    try:
        _d = json.loads(txt)
    except Exception:
        return None
    return _d if isinstance(_d, dict) else None


# firecrawl CLI 的 node 运行时 —— **不能写死 nvm 版本号**。
#
# ⚠️ 2026-10-23 实测：原值写死 `~/.nvm/versions/node/v24.19.0/bin`，而本机
# nvm 已升到 v24.22.1，那个目录**整个不存在** → `_FIRECRAWL_NODE` /
# `_FIRECRAWL_CLI` 两个路径永远 isfile=False → firecrawl 兜底层**全废**，
# 且自检 [firecrawl] 段报「CLI 缺失」。这与 agent 崩溃是同一个病的两处
# （agent 那边硬编码 PATH 导致 SDK 起不来、静默两天）。
# 版本用 `re.findall(r"\d+")` 排序而非字典序（"v24.9.0" 字典序 > "v24.22.1"），
# 与 claude_bin 的 nvm 探测同款判据。目录整个不在时退回空串（isfile 判假，
# 走既有的"CLI 缺失"降级路径，不会伪造成功）。
def _nvm_bin(sub: str = "", root: str | None = None) -> str:
    """返回 nvm 下**版本号最大**那套 node 的 bin 路径（可再拼 sub 子命令）。

    找不到任何版本时返回空串 —— 调用方（`_FIRECRAWL_NODE` 等）拿它拼路径，
    空串拼出来是相对名，随后的 os.path.exists 判假，自然落进"不可用"分支。

    ⚠️ `root` 参数只为自检/变异体而设：让「版本选择」这件事在**任意宿主**
    上都能被判定（建一个含 v24.9.0 / v24.22.1 / v24.100.3 的临时目录，
    断言取到 100.3）。不传时走真实 nvm 根，行为与原先完全一致。
    """
    _root = root or os.path.expanduser("~/.nvm/versions/node")
    try:
        _vs = [v for v in os.listdir(_root) if re.search(r"\d", v)]
    except Exception:
        return ""
    if not _vs:
        return ""
    try:
        # ⚠️ 数值序，不是字典序：字典序下 "v24.9.0" > "v24.22.1"（'9'>'2'），
        # 会选中更旧的版本 —— 与 claude_bin 的 nvm 探测同款判据。
        _vs.sort(key=lambda s: [int(x) for x in re.findall(r"\d+", s)])
        # ⚠️ 必须**逐个回退验证目标确实存在**（2026-10-25 审查实测）：
        # 原先直接取 `_vs[-1]`，而 nvm 半装态下会有"版本目录在、bin 是空壳"
        # 的情况（实测临时根含 v24.22.1 有 node + v25.0.0 空 bin → 返回
        # v25.0.0，`_FIRECRAWL_NODE` 指向不存在路径），firecrawl 兜底整层
        # 静默报废 —— 正是本函数 2026-10-23 要修的那个病换了个入口。
        # 现在从最大版本往下找**第一个真的存在目标文件**的版本；
        # `sub` 为空时验证 `<ver>/bin` 目录存在。
        # 对照 `claude_bin` 的 nvm 候选：那里本来就是逐个 isfile 跳过。
        for _v in reversed(_vs):
            _bin = os.path.join(_root, _v, "bin")
            if not os.path.isdir(_bin):
                continue
            if sub and not os.path.exists(os.path.join(_bin, sub)):
                continue
            return os.path.join(_bin, sub) if sub else _bin
        return ""
    except Exception:
        return ""


# ⚠️ 两个路径各自按**自己的目标文件**去挑版本，而不是先取 bin 目录再拼名
# （2026-10-25 审查）：nvm 半装态下某个版本可能只有 node 没有 firecrawl，
# 统一取"最大版本的 bin"会让缺 firecrawl 的那版把整层兜底带垮。
# 传 `sub` 时 `_nvm_bin` 会逐个版本验证该文件确实存在。
_FIRECRAWL_NODE = _nvm_bin("node")
_FIRECRAWL_CLI = _nvm_bin("firecrawl")
FIRECRAWL_TIMEOUT = 90          # 单次抓取上限（云端抓取通常 5~20s）
# 连续失败后的冷却截止（避免每轮都白等 90s）。
#
# ⚠️ 必须**落盘**，不能用进程内变量。注释原先写"避免每轮都白等 90s"，
# 但 analyze/collect **每轮各起一个新进程**，进程内变量每轮归零 ——
# 实际是"每轮都白等"：冷却只在本轮内有效，跨轮完全失效。
# `[实测]` 2026-09-29：两个连续进程各自都真的发起了 firecrawl 调用
# （subprocess.run 各被调用 1 次），第 2 个进程启动时 `_fc_fail_until == [0.0]`。
# 单次失败往返 3.24s，当日 18 次失败全部未跨轮冷却。
# 落盘范式与本文件 `_HOLIDAY_WARN_FILE` / alert 的 `none_ts.json` 一致。
_FC_FAIL_FILE = p_cache("firecrawl_fail.json")


def _fc_fail_get():
    """读冷却截止时刻（跨进程）。失败/损坏一律返回 0.0（不冷却）。"""
    try:
        _d = _load_json(_FC_FAIL_FILE, {}) or {}
        return float(_d.get("until") or 0.0)
    except Exception:
        return 0.0


def _fc_fail_set(until):
    """写冷却截止时刻。写失败不影响取数（退化成进程内冷却）。"""
    try:
        _atomic_write_json(_FC_FAIL_FILE,
                           {"until": until,
                            "t": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    except Exception:
        pass


def _proxy_host_key(url):
    """从 URL 取目标主机名（冷却分账键）。取不到时回退成全局键 `""`。"""
    try:
        return (url.split("//", 1)[1].split("/", 1)[0].split("?")[0].lower()
                if "//" in url else "")
    except Exception:
        return ""


def _proxy_fail_get(host=""):
    """读某主机的代理冷却截止时刻（跨进程）。失败/损坏返回 0.0（不冷却）。

    ⚠ 2026-11-10（修不彻）：原来只有**一个全局冷却**。
    `[实测]` 对同一台挂掉的 push2test 连打 3 个端点就凑满
    阈值，于是连**健康的腾讯**也 600s 内不再走代理 —— 而注释写的是
    「单个目标站点的故障不会牵连其他源」。现改为 `{host: {"until","n"}}`。
    """
    try:
        _d = _load_json(_PROXY_FAIL_FILE, {}) or {}
        if not isinstance(_d, dict):
            return 0.0
        _e = _d.get(host)
        if isinstance(_e, dict):
            return float(_e.get("until") or 0.0)
        # 旧格式（单层全局记录）：只对空 host 兼容读，不污染各主机
        if host == "" and "until" in _d:
            return float(_d.get("until") or 0.0)
        return 0.0
    except Exception:
        return 0.0


def _proxy_fail_bump(host=""):
    """记一次代理失败（**按 host 分账**）；达阈值才真冷却。

    计数落盘在 `_PROXY_FAIL_FILE` 的 `{host: {"n", "until"}}`。
    """
    _now = time.time()
    _cool = 0.0
    _n_new = 1
    try:
        with _open_lockfile(_PROXY_FAIL_FILE + ".lock") as _lk:
            try:
                fcntl.flock(_lk, fcntl.LOCK_EX)
            except Exception:
                pass
            _d = _load_json(_PROXY_FAIL_FILE, {}) or {}
            if not isinstance(_d, dict):
                _d = {}
            _e = _d.get(host)
            if not isinstance(_e, dict):
                _e = {}
            _n = _e.get("n")
            _n_new = int(_n) + 1 if isnum(_n) else 1
            _prev_cool = _e.get("until")
            _prev_cool = float(_prev_cool) if isnum(_prev_cool) else 0.0
            if _n_new >= PROXY_FAIL_THRESHOLD:
                # ⚠ 冷却期内再失败时**不能把 until 抹成 0**：
                # 原写法"达阈值就把 n 归零"，结果第四次失败时
                # `until` 被重置为 0 —— 冷却被**自己提前解除**。
                # 正确行为：冷却只能往后延，不能变短。
                _cool = max(_prev_cool, _now + PROXY_COOLDOWN)
            _d[host] = {"n": _n_new, "until": _cool,
                        "t": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            _atomic_write_json(_PROXY_FAIL_FILE, _d)
    except Exception:
        pass
    return _cool


def _proxy_fail_reset(host=""):
    """该主机代理一次成功 → 清掉它的连续失败计数与冷却。

    ⚠ 只清自己那一项 —— 否则 A 成功会把 B 的冷却一并抹掉，
    分账形同虚设。
    """
    try:
        with _open_lockfile(_PROXY_FAIL_FILE + ".lock") as _lk:
            try:
                fcntl.flock(_lk, fcntl.LOCK_EX)
            except Exception:
                pass
            _d = _load_json(_PROXY_FAIL_FILE, {}) or {}
            if isinstance(_d, dict) and host in _d:
                _d.pop(host, None)
                _atomic_write_json(_PROXY_FAIL_FILE, _d)
    except Exception:
        pass


# ---------- AI 通道熔断（2026-10-21）----------
# 主备**双双**零产出时，analyze 每轮仍要重付「主链首字节闸门 150s +
# 备用首字节闸门 120s」= 270s 才退兜底模板，而通道故障是**持续性**的。
# `[实测]` 2026-10-21：网关 `stream disconnected before completion`
# 全天不断，7 轮 AI 产出 0/7，光等 AI 烧掉约 2000s。
#
# 与 PROXY_COOLDOWN / FIRECRAWL_COOLDOWN 同族：**落盘**冷却（跨进程 ——
# analyze 每轮新起进程，进程内状态每轮重置等于没冷却）。
# 冷却期 600s = 2 个调度周期：短于它一轮慢轮就耗光；长于它通道恢复后
# 要多发几分钟模板。到期后第一轮**照常尝试**，不做"再确认一次"。
AI_COOLDOWN = 600
_AI_FAIL_FILE = p_cache("ai_fail.json")


def ai_fail_get():
    """读 AI 熔断截止时刻（跨进程）。失败/损坏一律返回 0.0（不熔断）。

    ⚠️ 读失败必须 **fail-open**（不熔断）：这个文件只用于"省时间"，
    把它当成放行条件会让文件损坏变成"AI 永久关闭"—— 那比白等 270s
    严重得多。与 `_fc_fail_get` / `_proxy_fail_get` 同款取舍。
    """
    try:
        _d = _load_json(_AI_FAIL_FILE, {}) or {}
        return float(_d.get("until") or 0.0)
    except Exception:
        return 0.0


def ai_fail_set(until):
    """写 AI 熔断截止时刻。写失败不影响主链路（退化成无熔断）。"""
    try:
        _atomic_write_json(_AI_FAIL_FILE,
                           {"until": until,
                            "t": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    except Exception:
        pass


def ai_fail_clear():
    """清除熔断（主链或备用任一条产出可用内容时调用）。

    ⚠️ 必须有清除路径：只写不清的熔断会在通道恢复后继续压 10 分钟模板。
    调用点在 analyze.ai_analyze 的两条成功 return 上。
    """
    try:
        _atomic_write_json(_AI_FAIL_FILE, {"until": 0.0, "t": ""})
    except Exception:
        pass
# 永久封锁主机的日志去重文件（落盘、按日）。与 _HOLIDAY_WARN_FILE 同构：
# 跨进程有效，同一天只报一次。
# 「每天只报一次」类日志的去重状态（跨进程）。文件名保留历史名，内容已
# 泛化为 {日期: {种类|主机: 日期}} —— 除永久封锁主机外，还用于代理兜底、
# 备用源熔断等**持续状态**型日志。
_PERM_DEAD_LOG_FILE = p_cache("perm_dead_warn.json")


def _daily_log_once(host, key, msg):
    """同一 host 的同一类日志，**每天只报一次**（跨进程）。

    用 `key` 区分日志种类：同一台主机可能既"被永久封锁"又"走了代理兜底"，
    两条都需要各自的每日一次。

    ⚠️ 必须是**落盘**去重，不能用进程内集合 —— `[实测]` analyze/collect
    **每轮各起一个新进程**，进程内集合每轮重置，等于每轮都打一行
    （collect 每 2 分钟一轮 → 30 行/小时），而 watch.log 是 2MB 单代轮转，
    真实诊断日志会被自身噪音刷掉。
    """
    try:
        _today = datetime.date.today().isoformat()
        _wrote = []
        def _mu(d):
            _h = d.get("hosts")
            if not isinstance(_h, dict):
                _h = {}
            _k = f"{key}|{host}"
            if _h.get(_k) == _today:
                return False                # 今天已经报过
            _h[_k] = _today
            d["date"] = _today
            d["hosts"] = _h
            _wrote.append(1)
            return True
        # 读-改-写整体进 flock：两个进程同时首报不同主机时，无锁版本会
        # 互相覆盖（后写者用旧快照整份写回），丢一次去重记录 -> 多打一行日志。
        _locked_update(_PERM_DEAD_LOG_FILE, _mu)
        if _wrote:
            log(msg, "FEED")
    except Exception:
        pass                            # 日志失败绝不影响取数


def _perm_dead_log_once(host):
    """永久封锁主机的跳过日志，**每天只报一次**（跨进程）。"""
    _daily_log_once(
        host, "permdead",
        f"跳过已永久封锁的主机（直连与兜底均不尝试）: {host}"
        f" —— 站点若已解封，请从 marketlib.PERMANENTLY_DEAD 移除")


def _proxy_fallback_log_once(host):
    """直连失败、走代理兜底成功的日志，**每天只报一次**（跨进程）。

    ⚠️ 原先这里是**裸 log()**，而紧邻的 PERMANENTLY_DEAD 分支已经用了
    按日去重（理由写在上面 `_daily_log_once` 的 docstring 里）——
    同一个函数内两套标准。
    `[实测]` 2026-09-30：`push2test.eastmoney.com` 直连全天不可用，
    代理每轮都兜成功，于是 13 点那一个小时写了 **187 行**同一句话
    （全天 294 行，占当日 FEED 的 98%）。
    它说的是"兜底成功了"—— 一个**正常运行**的事实，不是故障信号，
    却把 watch.log 这条唯一的排查证据源刷掉了。
    改为按日一次：既保留"这台主机今天直连是坏的"这个诊断事实，
    又不重复。"""

    _daily_log_once(
        host, "proxyfallback",
        f"直连失败，经代理兜底成功: {host}（本日首次，同主机后续不再重复）")
FIRECRAWL_COOLDOWN = 900        # 冷却 15 分钟；期间不再尝试

# 代理兜底的失败冷却（2026-10-14 九维度审查 P1）：firecrawl 有冷却而代理
# 没有 —— 不对称。代理的故障形态分两种：拒连（curl rc=5，0.0s 立即失败，
# 无害）；**黑洞**（代理活着但丢包，curl 挂满 max-time=20s）—— 后者一轮
# 4~8 个 cj 调用点串行最坏 +160s，且零记忆每轮重付，是「慢轮吞槽位」的
# 放大器。给代理级加与 firecrawl 同款的落盘冷却（600s = 2 个调度周期），
# 冷却期内直接跳过代理级（直连与 firecrawl 不受影响）。
PROXY_COOLDOWN = 600
_PROXY_FAIL_FILE = p_cache("proxy_fail.json")

# ⚠⚠ 2026-11-10（生产实测）：**目标站点的故障不能治连代理**。
# 原先一次空响应就写冷却 600s，而空响应在下面这种情形下照样发生：
#   `push2test` 上游挂了 → TLS 握手被**对端**断开（curl SSL_ERROR_SYSCALL）→ 空响应
# `[2026-11-10 10:50:27]` 就是这样把**完入健康的代理**封了 10 分钟
# （同期 `curl -x proxy https://example.com` 连续 200）。而代理冷却是**全局**的：
# 期间所有 `cj()` 的代理兜底、腾讯日K 的代理绕行全部失效，只剩 firecrawl
# （慢 12 倍且耗额度）。
# 现在改为：**连续** PROXY_FAIL_THRESHOLD 次失败才冷却，任一次成功即清零。
# 这样“代理真挂”仍会被隔离，而单个目标站点的故障不会牵连其他源。
PROXY_FAIL_THRESHOLD = 3


def firecrawl_get(url, timeout=None):
    """经 firecrawl 云端抓取 URL，返回**原始文本**；不可用/失败返回 None。

    通用能力：任何 HTTP 抓取都能用，不限于 JSON 接口。
    调用方负责解析与校验。
    """
    if time.time() < _fc_fail_get():
        return None                                  # 冷却期内
    if not (os.path.exists(_FIRECRAWL_NODE) and os.path.exists(_FIRECRAWL_CLI)):
        return None
    _to = timeout or FIRECRAWL_TIMEOUT
    try:
        r = subprocess.run(
            [_FIRECRAWL_NODE, _FIRECRAWL_CLI, "scrape", "--format", "rawHtml", url],
            capture_output=True, text=True, timeout=_to)
    except Exception as e:
        log(f"firecrawl 调用异常: {type(e).__name__}: {e}", "FEED")
        _fc_fail_set(time.time() + FIRECRAWL_COOLDOWN)
        return None
    out = (r.stdout or "").strip()
    if not out:
        log(f"firecrawl 无输出 rc={r.returncode} "
            f"stderr={(r.stderr or '')[:120]!r}", "FEED")
        _fc_fail_set(time.time() + FIRECRAWL_COOLDOWN)
        return None
    # ⚠️ CLI 会先打印一行 `Scrape ID: xxx`，必须剥掉，否则调用方的
    # json.loads / 正则解析全部失败 —— 而失败会被当成"兜底也拿不到"，
    # 于是这层兜底**看起来永远无效**。
    lines = out.splitlines()
    if lines and lines[0].startswith("Scrape ID:"):
        out = "\n".join(lines[1:]).strip()
    # ⚠️ 还要剥 **HTML 外壳**。firecrawl 把非 HTML 的响应也包一层文档骨架：
    #     <html><head><meta ... charset=GBK></head><body>v_sh000001="1~上证指数~…
    # 而 `feeds._curl` 的调用方（from_tencent / from_sina / from_baidu）
    # 期望的是**裸文本**，按行首 `v_` / `var hq_str_` 匹配。
    # `[实测]` 带外壳喂给 from_tencent -> **返回 {}**，
    # 即"兜底拿得到数据却用不上"，比不兜底更隐蔽（日志说成功，数据是空的）。
    # 只剥**包裹整段的** `<html>…<body>` / `</body></html>`，
    # 中间内容原样保留（正则不用 DOTALL 贪婪，避免吃掉正文里的标签）。
    _m = re.match(r"\s*<html[^>]*>.*?<body[^>]*>(.*)</body>\s*</html>\s*$",
                  out, re.S | re.I)
    if _m:
        out = _m.group(1).strip()
    return out or None


def firecrawl_json(url, timeout=None):
    """经 firecrawl 取 JSON 并解析；失败返回 None。供 cj() 的兜底使用。"""
    txt = firecrawl_get(url, timeout=timeout)
    if not txt:
        return None
    try:
        d = json.loads(txt)
    except Exception:
        log(f"firecrawl 返回非 JSON: {txt[:120]!r}", "FEED")
        # ⚠️ 解析失败同样要**武装冷却**。原先只记日志就 return，
        # 于是网关持续故障时每一轮、每一次调用都重新付一次
        # firecrawl 往返（实测 3.24s）。`[实测]` 2026-09-29：
        # 单日 18 次失败全部未武装冷却。
        _fc_fail_set(time.time() + FIRECRAWL_COOLDOWN)
        return None
    # ⚠️ 形状校验同 proxy_json：502 页正文是 `502`，json.loads 成功返回 int。
    # 不校验的话"错误页"会被当成"兜底成功"返回给调用方。
    if not isinstance(d, dict):
        log(f"firecrawl 返回非对象 JSON（{type(d).__name__}）: {txt[:120]!r}", "FEED")
        _fc_fail_set(time.time() + FIRECRAWL_COOLDOWN)
        return None
    return d


def cj(url, ref=REF_EM, retries=3, timeout=20, limit=True):
    """curl 取 JSON（python urllib 会被东财封，必须用 curl 子进程）。
    带限流、指数退避、熔断。直连全败时经 firecrawl 云端兜底。"""
    # ⚠⚠ 2026-11-17（Codex 审查 + 本地复核）：**判死键必须与 `_host_key()`
    # 同源**。原实现是 `url.split("/")[2]`（**裸主机名，不含协议**），
    # 而候选排序/健康度用的是 `_host_key()`（协议 + 主机名）。
    #
    # 后果：`EM_HTTPS` 与 `EM_HTTP` 是同一台主机的两种协议（本文件
    # `_host_key` 的长注释就是为区分它们而写的，2026-11-10 已修过排序侧），
    # 但 `cj()` 这一侧一直没跟上 —— HTTPS 失败一次就把裸主机名写进
    # dead_hosts.json，于是 **HTTP 直连也一起被跳过整个冷却期**，
    # 与"两种协议分别计健康度"的设计直接矛盾。
    # 今天（东财 HTTPS 抖动）正好踩中：一台协议失败，两个协议一起哑。
    #
    # 用 `_host_key()` 之后两侧键一致：HTTPS 死了不影响 HTTP 候选，
    # 反之亦然。`PERMANENTLY_DEAD` 里存的是**裸主机名**（push2delay），
    # 所以下面那道永久封锁闸用 `_host_of()` 单独判，不受影响。
    host = _host_key(url) if "//" in url else None
    # 被封锁的主机短时间内不再重试：主源 push2delay 已永久 exit 52，
    # 每轮 3 次重试纯属浪费（3 函数 × 3 重试 × 66 轮 = 594 次/天白费请求）
    #
    # ⚠️ 但**不能直接 return None** —— 那样会把下面那道 firecrawl 兜底
    # 一起短路掉，兜底代码变成死代码（"闸门在 ≠ 闸门生效"的老毛病）。
    # `[实测]` 2026-09-29：两台东财主机都被标死后，`cj()` 耗时 0.0s 直接返回
    # None，兜底从未被调用 —— 而这恰恰是**最需要兜底**的场景
    # （主机被判死 = 直连持续不通）。
    # 改为：跳过重试循环，但仍走兜底。
    # ⚠️ `PERMANENTLY_DEAD` 必须在这里也判一次 —— 原先只判 `_host_dead`，
    # 而 `_host_dead` 有 **30 分钟 TTL**（DEAD_TTL，会自愈），
    # `PERMANENTLY_DEAD` 只在下方的 `_fallback_get` 里生效。
    # 于是每过 30 分钟 TTL 一过期，`_host_dead` 变 False，本函数就**重新直连
    # 那台按主机名被永久封锁的主机**，跑满 3 次重试 + 指数退避（0.5/1/2s），
    # 再记一次主机失败、再一次熔断日志。
    # `[实测]` 2026-09-30：当天 2 次 `主机 push2delay 1 小时内失败 6 次，熔断`
    # （10:01:57 / 11:07:24），而它**本就不该被请求**。
    # 这正是本文件上方注释批评过的"闸门在 ≠ 闸门生效"——
    # 兜底路径挡住了，直连路径没挡。
    # ⚠️ 2026-11-17：`host` 现在是 `_host_key(url)`（协议+主机名），
    # 而 `PERMANENTLY_DEAD` 里存的是**裸主机名** —— 必须用 `_host_of()`
    # 单独取出来比对，否则永久封锁闸会静默失效（`"https://push2delay…"`
    # 永远不等于 `"push2delay.eastmoney.com"`）。
    _bare_host = _host_of(url) if "//" in url else None
    # ⚠️ 传给 `_fallback_get` 的必须是**裸主机名**：它内部用
    # `if host in PERMANENTLY_DEAD` 做永久封锁闸，而该集合存的是裸名。
    # 传含协议的 `_host_key()` 过去，那道闸会永远不成立（集合里没有
    # "https://…" 这样的键）→ 被封的主机照样白付一次代理+firecrawl。
    # 自检当场抓到了这一点：「cj() 对永久封锁主机仍发起了 1 次 curl
    # —— PERMANENTLY_DEAD 未在直连路径生效」（2026-11-17）。
    #
    # ⚠️ 说明必须放在 if **之前**：自检用 AST 断言这个早退分支的
    #     第一条语句就是 `return _fallback_get(...)`（防"兜底链断了"），
    #     把注释塞进 body 里会改变 body[0] 的行号关系而被判违规。
    if limit and host and (_bare_host in PERMANENTLY_DEAD or _host_dead(host)):
        return _fallback_get(url, ref=ref, host=_bare_host, timeout=timeout)
    # ⚠️ 配额闸门在**重试循环之内** —— 这是刻意的，不要"优化"到循环外。
    #
    # 每次重试都会**真实发出一个 HTTP 包**，而 daily_cap=2000 的设立目的
    # 正是"防止外发请求量过大导致 IP 被封"（历史教训：约 8400 次/天被封）。
    # 所以配额必须按**包**计，而不是按"逻辑取数次数"计 —— 移出循环会让
    # 一次失败请求只扣 1 次配额却发 3 个包，配额低估真实流量、闸门失效。
    #
    # 减少配额消耗的正确做法是**减少无效发包**（见下面 except 分支对
    # 网关错误页的早退），不是改记账口径。
    for i in range(retries):
        if limit and not RL.wait(host):
            # ⚠️⚠️ 2026-11-05（P1）：必须区分**主机熔断**与**日配额耗尽**。
            # 两者原先同返回 RATE_LIMITED，而 `_try_hosts()` 收到它就直接
            # 放弃全部候选主机 —— 一台主机进 5 分钟冷却会让本轮连健康的
            # 备用主机都不再尝试（港股/板块整批缺失），日志还误报"配额耗尽"。
            # 判据用 `RL.host_open()`（纯查询、无副作用，feeds.py 同款）：
            #   · 该主机在熔断冷却中 → HOST_CIRCUIT：跳过这台、换下一台；
            #   · 否则就是日配额耗尽 → RATE_LIMITED：整轮放弃（零发包）。
            # `wait()` 自身有副作用（扣配额/sleep），不能为了判原因再调一次。
            if host and not RL.host_open(host):
                return HOST_CIRCUIT
            # ⚠️ 必须返回**可区分**的哨兵，不能返回 None。
            # None 与"主机失败"不可区分，_try_hosts() 会对它执行
            # _mark_dead + _host_stat_bump —— 实测：配额耗尽时**零网络请求**，
            # 却把两台主机都盖上 30 分钟的"死亡"戳并各记一次失败。
            # 后果不只是日志噪音：_ordered_em_hosts() 的排序键第一项就是
            # _host_dead，配额耗尽期间每轮都把主机重新标死，主机恢复后
            # 还要再等 30 分钟才会被优先选用；真实故障信号也被假失败稀释。
            # （生产实证：dead_hosts.json mtime 19:49:25，同秒 watch.log
            #   全是"备用源配额/熔断限制，跳过"，无一条真实网络失败。）
            #
            # ⚠⚠ 2026-11-10（Codex P2）：配额耗尽也要**先试代理/firecrawl**。
            # `daily_cap` 的设立理由是“防本机 IP 被封”（见上方注释），
            # 而代理出口与 firecrawl 云服务**都不占本机 IP**、也不经 `RL`。
            # 原写法直接 `return RATE_LIMITED`，把不耗 IP 的兜底通道一并短路 ——
            # 生产 2026-11-10 13:15 配额耗尽后板块/港股整段失明，
            # 而那些数据本可由代理兜住。
            _rl_fb = _fallback_get(url, ref=ref, host=_bare_host, timeout=timeout)
            if _rl_fb is not None:
                _daily_log_once(f"quota-fb-{host or ''}", "quota-fb",
                                f"直连配额已耗尽，改走代理/firecrawl 兜底: {url[:60]}"
                                f"（本日首次）")
                return _rl_fb
            return RATE_LIMITED
        try:
            # 加进程级超时：--max-time 覆盖不了 DNS 卡死，子进程挂住会让
            # 整轮 cron 永不退出（上一轮未结束，acquire_lock 又会一直跳过）
            r = subprocess.run(["curl","-s","--compressed","-A",UA,
                "-H",f"Referer: {ref}",url,"--max-time",str(timeout)],
                capture_output=True, text=True, timeout=timeout + 5)
            if not r.stdout.strip():
                raise ValueError("empty")
            d = json.loads(r.stdout)
            # ⚠️ 形状校验，与 proxy_json / firecrawl_json 同一判据。
            # `[实测]` 2026-09-29：curl 拿到网关错误页时 body 常常**恰好是
            # 合法 JSON 标量**（502 页正文就是 `502`），json.loads 不抛异常，
            # 于是错误页被当成"取数成功"返回 int 给调用方。
            # 这一处比兜底路径更危险：直连路径接着会执行
            # `RL.ok()` + `_clear_dead(host)` —— 等于**把网关故障当成
            # 主机健康**，把刚判死的主机重新救活，下一轮继续白重试。
            if not isinstance(d, dict):
                raise ValueError(f"非对象 JSON: {type(d).__name__}")
            RL.ok()
            if host: _clear_dead(host)
            return d
        except Exception as _e:
            RL.fail(host)
            # ⚠️ 失败后**立即停止重试**，不等 retries 耗尽。
            #
            # 原先无论什么原因失败都要把 retries(3) 走满、每轮退避
            # 0.5/1/2s。但失败形态是**同质**的：curl 空响应 / 非对象 JSON
            # （网关错误页）都不可能在 0.5 秒后自愈 —— 那不是抖动，是这台
            # 主机此刻就是不可用。实测 2026-09-30：push2test 整段失败时
            # 一轮里 5 个 cj 调用点各打 3 个包，**15 个外发请求里 10 个是
            # 注定失败的重复**，还把配额从同轮健康的腾讯取数里挤掉。
            #
            # 只保留**一次**重试，且仅针对"可能是瞬时抖动"的形态 ——
            # 空响应最常见于限流窗口，隔一下确有恢复的先例（生产里
            # 代理兜底 0.19s 就成功的正是这种）。非对象 JSON 是网关错误页，
            # 重试无意义，直接放弃。
            #
            # ⚠️ 节流不受影响：下面仍 sleep 一次，且 RL.wait 的
            # min_interval 也照常生效 —— 防封 IP 靠的是**请求速率**，
            # 而这里减少的是**无效请求总量**，两者同向。
            if isinstance(_e, ValueError) and "非对象 JSON" in str(_e):
                break                        # 网关错误页：重试无意义
            if i < retries - 1:
                time.sleep(0.5 * (2 ** i))   # 指数退避 0.5/1/2
    if host: _mark_dead(host)
    # ---- 最后一道兜底：直连全部失败时，改从 firecrawl 云端抓 ----
    # 走到这里说明重试已耗尽（curl 空响应 / 解析失败 / 主机被判死）。
    # 请求由 firecrawl 的服务器发出，绕开本机网络 —— 用于
    # "目标站点封禁本机 IP / 本机 DNS 异常" 这类直连无法自愈的场景。
    # `[实测]` 2026-09-29：东财 push2delay/push2test 直连同时返回空，
    # 经 firecrawl 抓 push2test 拿到完整数据。
    #
    # ⚠️ 位置必须在 `_mark_dead` **之后** —— 那一步是"直连路径"的结论，
    # 兜底成功了也不该改变"直连确实不通"这个事实（否则主机不会被标死，
    # 下一轮又白白重试 3 次）。两者互不干扰。
    if limit:
        # 同样必须传裸主机名（见上方 `_bare_host` 的说明）
        return _fallback_get(url, ref=ref, host=_bare_host, timeout=timeout)
    return None


def _fallback_get(url, ref=None, host=None, timeout=20):
    """直连失败后的两级兜底：**先代理，再 firecrawl**。

    顺序按代价：代理在本机局域网内（实测 0.19s、不耗外部额度），
    firecrawl 是云服务（实测 2.3s、消耗额度）。
    返回与 `cj()` 同构的 dict，或 None。
    """
    # 永久封锁的主机：连兜底也不试 —— 见 PERMANENTLY_DEAD 的说明。
    # 这是本函数**唯一**的提前返回，且它省下的是最贵的那部分
    # （两次外部请求）。只记一次日志，避免每轮刷屏。
    if host in PERMANENTLY_DEAD:
        # ⚠️ 日志必须**落盘按日去重**，不能用进程内集合。
        # `[实测]` analyze/collect **每轮各起一个新进程**，进程内集合每轮重置 ——
        # 那样等于每轮都打一行（collect 每 2 分钟一轮 → 30 行/小时），
        # 而 watch.log 是 2MB 单代轮转，真实诊断日志会被自身噪音刷掉。
        # 这与本文件 `_HOLIDAY_WARN_FILE` 用的是**同一套**机制与理由。
        _perm_dead_log_once(host)
        return None
    _d = proxy_json(url, ref=ref, timeout=timeout)
    if _d is not None:
        _proxy_fallback_log_once(host)
        return _d
    # ⚠⚠ 2026-11-11（审查，P2）：**firecrawl 不能硬编码自己的 90s**。
    # 原写 `timeout=FIRECRAWL_TIMEOUT`（固定 90s），而 `cj()` 的 `timeout` 参数
    # 已经贯穿了直连重试与代理两档 —— 只有末端这档不认账。
    # 一条请求链最坏是「直连 3 次重试 + 代理 + firecrawl 90s」，
    # 而 `hot.kline_stats` 的 `VR_BUDGET`（量比 80s）只在**板块之间**检查
    # —— 它管不住已经在跑的那一条。`[实测]` 一条链 191.5s。
    # 取两者较小值：调用方给了更短的预算就尊重它，
    # 没给（默认 20s）也不会因此把 firecrawl 压成过短。
    _d = firecrawl_json(url, timeout=min(FIRECRAWL_TIMEOUT,
                                        max(5, int(timeout or 0) * 2)))
    if _d is not None:
        # 与紧邻的 _proxy_fallback_log_once / _perm_dead_log_once 同标准：
        # **持续状态**型日志按日去重（2026-10-08 审查：实测 09-29 有 9 秒内
        # 3 行的记录，生产累计 58 行，而代理/永久封锁两个兄弟分支早都改成
        # 每日一次 —— 同一函数三套标准，watch.log 是 2MB 单代轮转）。
        _daily_log_once(host, "fcfallback",
                        f"直连与代理均失败，经 firecrawl 兜底成功: {host}"
                        f"（本日首次，同主机后续不再重复）")
        return _d
    return None

# ---------- 日K 与关键位 ----------
# 关键位（20/60 日高低）需要**日K历史**，而实测三个源的可信度按标的分化：
#
#   A股指数   EM_TEST 的日K**损坏** —— 上证 61 根里 14 根单日跳变 >15%，
#             高点在 3828~9282 之间乱跳（沪深300 13 根、科创50 18 根）。
#             用它算"20日高"会得出"距高 -45%"这种荒谬结论。腾讯完全干净。
#   板块 90.BKxxxx  腾讯无此接口；EM_TEST 干净（0 跳变）。
#   港股      EM_TEST 口径错误（恒指 25737 vs 真值 24700，与此前发现的
#             f2/f3 问题同源）；腾讯干净且是权威源。
#
# 故按类型分流：A股指数/港股 → 腾讯，板块 → EM_TEST。
# 所有取回的序列都过一遍跳变校验，异常即判定该源不可信并丢弃。
_TX_KLINE_URL = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                 "?param={code},day,,,{n},qfq")
_TX_INDEX = {
    "1.000001": "sh000001", "1.000300": "sh000300",
    "0.399006": "sz399006", "1.000688": "sh000688",
    # ⚠️ 日K接口**不认** r_ 前缀（实测 param error），必须用 hkHSI 形式；
    # 日K每天只变一次且剔除今日未完成K线，延迟 15 分钟对它无影响。
    "100.HSI":  "hkHSI",    "124.HSTECH": "hkHSTECH",
}
_TX_STOCK = {                     # 港股个股：腾讯用 hk00700 形式（不带 r_ 前缀）
    "116.00700": "hk00700", "116.09988": "hk09988", "116.00981": "hk00981",
}
# 缓存走 dcache（按日期键，跨进程落盘）：日K每天只变一次，且本函数
# 刻意剔除今日未完成的K线，故同一交易日内结果恒定 —— 一天取一次即可，
# 14 个标的约占日配额 2000 的 0.7%，远低于按轮重取的 1122 次。
_SANE_JUMP = 0.15                 # 单日 >15% 视为源损坏


def _tx_kline_fetch(url, code, direct=None, proxy=None):
    """腾讯日K：直连 → 代理 兜底，返回 `[[日期,开,收,高,低,量], ...]` 或 None。

    ⚠️ 存在的理由（2026-11-03 修，P3）：`_kline_ohlc` 的腾讯分支此前是
    **裸 curl**，是全库唯一不走「直连→代理→firecrawl」降级链的取数路径
    （`cj()` / `proxy_get()` 都有）。`[实测]` 2026-11-02：本机 IP 被腾讯
    风控后 `web.ifzq.gtimg.cn` 全 501（WAF 页），6 个指数（上证/科创50/
    沪深300/创业板指/恒指/恒科）的关键位段整段退化成"上一交易日收盘"，
    持续约 40 分钟自愈。走 `MW_PROXY` 指定的代理能立即
    绕开**本机 IP** 的风控 —— 风控针对来源 IP，不针对目标站点。

    判据与取值分离（纯函数，喂构造的 direct/proxy 即可测，见
    `tests/test_kline_proxy_fallback.py`）：
      · 直连返回**合法 JSON** 才采用（WAF 页是 HTML，`json.loads` 抛异常）；
      · 直连空 / 非 JSON / 抛异常 → 走代理兜底；
      · 两路都失败 → None（与原先裸 curl 失败同路，上层 return None）。

    `direct`/`proxy` 是取值函数，签名 `(url) -> str|None`；默认走真网络
    （`direct` = 裸 curl；`proxy` = `proxy_get`）。注入它们是为了可测。
    """
    if direct is None:
        def direct(_u):
            try:
                return subprocess.run(
                    ["curl", "-s", "-m", "15", _u],
                    capture_output=True, timeout=20).stdout.decode(
                        "utf-8", "ignore")
            except subprocess.TimeoutExpired:
                return None
    if proxy is None:
        def proxy(_u):
            return proxy_get(_u, timeout=12)

    def _parse(raw):
        if not raw:
            return None
        try:
            j = json.loads(raw)
            node = j["data"][code]
            kl = node.get("qfqday") or node.get("day") or []
        except Exception:
            return None           # 含 WAF 页（HTML）与形状不符的 JSON
        return kl or None

    def _safe(fn, _u):
        # ⚠️ 取值函数**必须**包起来：直连抛异常（DNS 卡死后被杀、网络层
        # 异常）时若不吞，异常冒泡到 `_kline_ohlc` 的 `except Exception`
        # 直接 `return None` —— 代理兜底就永远不会被尝试，本函数存在的
        # 意义（直连坏了还能靠代理救）当场失效。
        try:
            return fn(_u)
        except Exception:
            return None

    kl = _parse(_safe(direct, url))
    if kl is None:
        # 直连不可用（空/WAF/异常）→ 代理兜底。风控针对本机 IP，
        # 代理出口能把这条链路救回来，不必等它自愈。
        kl = _parse(_safe(proxy, url))
    return kl


def _kline_ohlc(secid, n=61):
    """取日K [(日期, 高, 低, 量, 收)]。源不可信/取不到返回 None。

    第 5 项（收盘价）是 levels() 的 cur 兜底来源 —— 行情源全挂时用它代表"昨收"。

    第 4 项是成交量（腾讯给股数、EM 给手数，口径不同，故只用于**同源对比**，
    不做跨源换算）。

    分流规则见上方注释：A股指数与港股走腾讯，其余（板块）走 EM_TEST。
    绝不走 push2his —— 该主机已被按主机名永久封锁。
    """
    # 缓存策略：**只缓存"今日之前"的那部分**（历史日K恒定不变），
    # 今日那根每次实时取 —— 因为盘中成交量持续增长，整段缓存会把
    # 「今日成交量」冻结在当天首次调用的值（09:25），导致【量能对比】
    # 的「已达上日 X%」在之后每一轮都是错的（实测过这类静默错数据）。
    #
    # 为什么需要：levels() 有自己的 `levels:{secid}` dcache，但它存的是
    # **加工后的结果**；任何直接调 _kline_ohlc 的地方（如 build_brief 的
    # 【量能对比】）都会重新走网络。实测 2026-09-24：analyze 每天约 70 轮，
    # 量能对比涉及 4 个指数 → 全天约 280 次**重复**的日K请求。
    # 这些请求走腾讯 curl，不经 cj() 限流器（不吃日配额），但纯属浪费，
    # 且放大了被腾讯风控的概率。
    _ck = f"kline_hist:{secid}:{n}"
    _today_s = datetime.date.today().isoformat()
    _hist = dcache_get(_ck)
    tx = _TX_INDEX.get(secid) or _TX_STOCK.get(secid)
    rows = []
    try:
        if tx:
            # 有历史缓存时只取最近 2 根（覆盖今日），否则取满 n 根
            _n = 2 if _hist else n
            # ⚠️ Python 层超时**必须显式给**（2026-10-21 补）：`curl -m 15`
            # 覆盖的是"整个传输"，但**盖不住 DNS 解析卡死**（macOS 上
            # resolver 挂起时 curl 可能停在 connect 之前，-m 不计时）。
            # 全库另外两处外部调用（cj / proxy_get）都写了
            # `timeout=timeout + 5` 兜底，唯独这里没有 —— 一旦卡住，
            # `_kline_ohlc` 会永久阻塞，而它被 build_brief 的关键位段
            # **裸调用**，整轮就再也退不出来了（launchd 不并发同一 job，
            # 后续槽位全丢，且日志里一个字都没有）。
            # 这一批共 9 个标的走这里：6 个指数 + 3 只港股。
            #
            # ⚠️⚠️ 取数走 `_tx_kline_fetch`（2026-11-03，P3）：直连失败时
            # 经代理兜底 —— 此前这里是**裸 curl**，本机 IP 被腾讯风控
            # （501 WAF）时 6 个指数关键位段整段消失且只能等自愈。
            kl = _tx_kline_fetch(_TX_KLINE_URL.format(code=tx, n=_n), tx)
            if kl is None:
                # 直连与代理都拿不到：本标的这次作废，交给上层 return None
                # 走"源不可信"分支（与解析失败同路）。
                log(f"日K 取数失败（腾讯 {tx}，直连+代理均无有效响应）"
                    f"—— 本标的本次跳过", "FEED")
                return None
            # 腾讯列序: [日期, 开, 收, 高, 低, 量]
            # 第 6 列（成交量）也带上：它已经在响应里，多解析一个字段零成本，
            # 而量能对比（今日 vs 昨日）是「盘面」段最有价值的判断依据之一。
            # 收盘价（x[2]）必须一并解析 —— levels() 的 cur 兜底要用它。
            # 原先只取 (日期,高,低,量)，导致 levels() 里 `cur = hist[-1][2]`
            # 拿到的是**当日最低价**，注释却写"用最后一根收盘兜底"。
            _fresh = [(x[0], float(x[3]), float(x[4]),
                       float(x[5]) if len(x) > 5 else 0.0,
                       float(x[2]) if len(x) > 2 else 0.0) for x in kl]
            # 合并：历史缓存（不含今日）+ 刚取的新数据
            if _hist:
                _new = [r for r in _fresh if r[0] > _hist[-1][0]]
                rows = _hist + _new
            else:
                rows = _fresh
        else:
            _n2 = 2 if _hist else n
            # ⚠️ 2026-10-22：**不要**给这段加明文 HTTP 的 push2his 兜底。
            # 试过，已撤回 —— 理由是本项目最忌讳的那类"看着正常的错数"：
            #   · `levels()` 返回的 h20/h60 与 cur 都来自**同一条**序列，
            #     内部自洽；但 `analyze.build_brief` 盘中是
            #     `cur = x["f2"] if _live_px else lv["cur"]` —— 现价来自
            #     quotes()（push2test 基点），高低点来自这条K线。
            #   · HTTP 主机与 push2test 的板块序列是**不同指数基点**
            #     （实测 90.BK1030 近 5 日恒定 ratio=0.986336），
            #     一旦兜底生效，`(cur/h20-1)*100` 会带上约 1.4 个百分点的
            #     系统性偏差，而那个数字是"距 60 日高还有多少空间"的
            #     唯一依据 —— 偏差不会报警，只会让结论偏乐观。
            #   · 现状（只有 EM_TEST）失败时 levels() 返回 None，关键位段
            #     **显式消失**，读者知道"这项没有"，比给一个偏 1.4pp 的数好。
            # 若将来要加兜底，必须同时把 cur 也取自同一条序列（或把两边
            # 基点对齐），否则不要加。
            d = cj(f"{EM_TEST}/stock/kline/get?secid={secid}"
                   f"&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56"
                   f"&klt=101&fqt=1&end=20500101&lmt={_n2}&ut={UT}")
            kl = ((d or {}).get("data") or {}).get("klines") or []
            # EM 列序: 日期,开,收,高,低,量,成交额
            # EM 列序: 日期,开,收,高,低,量,成交额
            _fresh2 = [(x.split(",")[0], float(x.split(",")[3]),
                        float(x.split(",")[4]),
                        float(x.split(",")[5]) if len(x.split(",")) > 5 else 0.0,
                        float(x.split(",")[2]) if len(x.split(",")) > 2 else 0.0)
                       for x in kl]
            if _hist:
                _new2 = [r for r in _fresh2 if r[0] > _hist[-1][0]]
                rows = _hist + _new2
            else:
                rows = _fresh2
    except Exception:
        return None
    if len(rows) < 20:
        return None
    # 损坏校验：任一根高点相对前一根跳变 >15% 即判源不可信
    #
    # ⚠️ 循环从 i=1 开始，**首根从未被校验**；而下面 `rows[i-1][1] > 0`
    # 这个守卫本身就说明"高点为 0"是预期会出现的情况（源偶发返回占位 0）。
    # 首根为 0 时能整条穿过去，落到 levels() 里：
    #   h20 = max(r[1] for r in hist[-20:])   # 含 0 -> 0
    #   d20h = (cur/h20 - 1) * 100            # ZeroDivisionError
    # 实测复现（20 根 K 线 + 首根 高/低 = 0）：levels() 抛 ZeroDivisionError，
    # 而它在 build_brief 的关键位段里是裸调用 —— 整段「关键位」消失。
    # 非正值的高/低没有任何行情含义，直接判源损坏（与下方注释"宁可缺数据"一致）。
    if any(not isnum(r[1]) or not isnum(r[2]) or r[1] <= 0 or r[2] <= 0
           for r in rows):
        log(f"K线源异常({secid} 含非正高/低价)，弃用", "FEED")
        return None
    # ⚠️ 单根跳变**只剔这一根**，不整条弃用（2026-11-01 修，P3）。
    # 原实现对任意一根 >15% 的跳变都 `return None`，理由是"源损坏"。
    # 但**指数单日真的可以涨跌 >15%**（`[实测]` 恒生科技 2022-03-16
    # 单日 +22.8%），而那根 K 线在窗口里要停留 61 个交易日（≈3 个月）——
    # 期间该标的的【关键位】整段恒为「日K暂不可取」，读者看不到任何
    # 20 日高低。判据应该是"这根可疑"而不是"整条可疑"：剔掉它，
    # 其余 60 根照常可用（h20/l20 少一根的影响远小于整段消失）。
    # 若异常根数过多（>3，源真的坏了），才整条弃用。
    _bad_bars = []
    for i in range(1, len(rows)):
        if rows[i-1][1] > 0 and abs(rows[i][1]/rows[i-1][1] - 1) > _SANE_JUMP:
            _bad_bars.append(i)
    if len(_bad_bars) > 3:
        log(f"K线源异常({secid} {len(_bad_bars)} 根跳变过大)，整条弃用", "FEED")
        return None
    if _bad_bars:
        _drop = set(_bad_bars)
        log(f"K线源异常({secid} 第 {'/'.join(str(i) for i in _bad_bars)} 根"
            f"跳变过大)，仅剔除该根（指数单日确有 >15% 的行情）", "FEED")
        rows = [r for _i, r in enumerate(rows) if _i not in _drop]
    # 只缓存"今日之前"的部分 —— 今日那根盘中还在变，缓存它会让
    # 【量能对比】的「已达上日 X%」冻结在首次调用的值。
    if not _hist:
        _past = [r for r in rows if r[0] != _today_s]
        if len(_past) >= 20:
            dcache_set(_ck, _past)
    # 统一成 tuple：缓存经 JSON 往返后是 list，而实时取的是 tuple。
    # 两者索引取值等价，但混用会让 `==` 比较意外为 False（实测踩过：
    # 逐根比对时 61/61 全"不同"，实际数据一致）。
    #
    # ⚠️ 同时补齐第 5 项（收盘价）：第 5 项是后加的，盘上已有的缓存是 4 元组。
    # 旧缓存没有收盘价，用 low 顶替（与加这一列之前的行为一致，不劣化），
    # 下次缓存轮换后自然变成 5 元组。不补齐会让 r[4] 抛 IndexError，
    # 整条 K 线链路挂掉 —— 比"收盘价暂时不准"严重得多。
    out = []
    for r in rows:
        t = tuple(r)
        if len(t) < 5:
            t = t + (t[2] if len(t) > 2 else 0.0,)
        out.append(t)
    return out


# 取数失败的重试间隔（秒）。与 alert.NONE_TTL 同值同语义：
# 失败后短期记住避免每轮白拉，过期则允许重试一次。
LEVELS_NONE_TTL = 1800
# 路径直接构造，**不引用 CACHE_DIR** —— 那个常量定义在文件靠后处（缓存一节），
# 在这里引用会 NameError（模块级语句按出现顺序执行）。
_LEVELS_NONE_FILE = p_cache("none_ts.json")

def _levels_none_expired(secid):
    """levels 对该 secid 的失败记忆是否已过期（过期则允许重试）。

    与 alert._none_expired 共用 cache/none_ts.json —— 键加 "levels:" 前缀，
    两个模块各写各的键，不会互相覆盖。
    """
    try:
        d = _load_json(_LEVELS_NONE_FILE, {})
        ts = d.get(f"levels:{secid}", 0)
        return (time.time() - ts) > LEVELS_NONE_TTL
    except Exception:
        return True

def _levels_stamp_none(secid):
    """记下 levels() 对该 secid 的失败时刻。

    ⚠️ 写入时**顺带剔除已过期的条目**。过期语义是"允许重试一次",
    与"键不存在"完全等价 —— 删掉不改变任何行为;不删则文件永久留着
    陈旧时间戳。`[实测]` 2026-10-08:文件里躺着两条 2026-09-23 的记录
    (15.5 天前),而这个文件的语义是"只活 30 分钟",正常情况**早该是空的**。
    留着它会让"写入路径是不是还在跑"这件事**无法从文件本身判断**
    —— 一个永远非空的 TTL 缓存,等于把可观测性也一起删了。
    (同型教训见 [[market-watch-log-cache-hygiene]]:TTL 缓存要问"过期那份谁删"。)

    ⚠️ 本文件与 `alert._stamp_none` **共用**,alert 的键前缀是 `alert:`。
    两个模块的 TTL **必须相等**,否则这里按自己的 TTL 清理会把对方
    仍然有效的记忆删掉 —— 自检的 [失败记忆缓存] 段对这条不变量做了断言。
    """
    try:
        # ⚠️ 必须走 `_locked_update`（2026-10-11 审查）：本文件与
        # `alert._stamp_none` **共用** `cache/none_ts.json`（见上面的注释），
        # 两处原先都是「读全量 -> 内存改 -> 整份写回」且**无锁** ——
        # 典型的 lost-update：后写的把先写的增量整段覆盖掉。
        # 后果：被覆盖的那一侧以为"这个 secid 刚失败过"，在 TTL 内不再
        # 重试 —— 某个指数/板块静默缺一段时间数据，日志里零痕迹。
        # 可达性：analyze.run 同一轮里先调本函数（1313 行的 levels）再调
        # `ALERT.should_alert()`，两者各写一次同一文件。
        # `_locked_update` 在**排他 flock 内重新读盘**再改再原子写 ——
        # 与 dead_hosts / host_stat 同一把锁模式（见其 docstring）。
        def _mu(d):
            _now = time.time()
            for _k in [k for k, v in list(d.items())
                       if not isinstance(v, (int, float))
                       or (_now - v) > LEVELS_NONE_TTL]:
                d.pop(_k, None)
            d[f"levels:{secid}"] = _now
        _locked_update(_LEVELS_NONE_FILE, _mu)
    except Exception:
        pass

def levels(secid):
    """关键位：20/60 日高低与当前位置。取不到返回 None。

    结构: {"cur": 现价, "h20","l20","h60","l60", "d20h","d20l"（距高/低 %）}

    **剔除今日未完成的K线** —— 盘中最后一根的高低价是动态的，
    直接参与 max/min 会让"20日高"随分时抖动，同一交易日反复变化。

    ⚠️ 失败记忆必须**会过期**。原先这里写死 dcache_set(key, "__none__")，
    而 dcache 的生存期是"到当天结束"—— 于是 09:25 首轮一次网络抖动，
    就让当天 14 个标的全部显示「（日K暂不可用）」，之后每轮都直接命中
    __none__ 不再重试，直到次日才恢复。对照 alert.ewma_sigma 有
    NONE_TTL=1800s 的重试窗口，这里补齐同样的语义。
    """
    key = f"levels:{secid}"
    hit = dcache_get(key)
    if hit is not None:
        if hit != "__none__":
            return hit
        # 上次取数失败：30 分钟内不重试（避免每轮白拉），过期后放行
        if not _levels_none_expired(secid):
            return None
        # 过期 —— 继续往下走，重试一次
    rows = _kline_ohlc(secid)
    if not rows:
        dcache_set(key, "__none__")
        _levels_stamp_none(secid)
        return None
    today = datetime.date.today().isoformat()
    hist = [r for r in rows if r[0] != today] or rows
    # ⚠️ 长度校验：h60/l60 用的是**全部** hist，若 hist 不足 60 根，
    # "60日高/低"会静默变成"59日高/低"—— 数字看着正常，含义却变了。
    # 实测 2026-09-24：板块（90.BKxxxx）恰好返回 60 根、剔除今日后正好 60，
    # 已在边界上；腾讯少给一根就会退化。这里记一条日志让它可见，
    # 而不是静默给出偏小/偏大的极值。
    # 不直接 return None：20 日部分仍然可用，缺的是 60 日精度 ——
    # 整条丢弃反而让"关键位"整段消失，损失更大。
    if len(hist) < 60:
        log(f"K线仅 {len(hist)} 根（<60），60日高低降级为 {len(hist)} 日口径（{secid}）", "FEED")
    # 无实时价时用最后一根**收盘价**兜底。
    # ⚠️ 原先写 hist[-1][2]，而元组是 (日期,高,低,量) —— 取到的是当日**最低价**，
    # 注释与实现不符。行情源全挂时（quotes() 返回空）lv["cur"] 是唯一来源，
    # 会把"昨日最低"当"昨收"写进推送，chg = (cur/lv["cur"]-1)*100 跟着全错。
    # 正常盘中 x["f2"] 存在，所以这个错误只在故障时暴露 —— 最难发现的一类。
    cur = hist[-1][4] if len(hist[-1]) > 4 else hist[-1][2]
    h20 = max(r[1] for r in hist[-20:]); l20 = min(r[2] for r in hist[-20:])
    h60 = max(r[1] for r in hist);       l60 = min(r[2] for r in hist)
    # 兜底：即便上游放行了非正极值，也绝不在**这里**除零。
    # levels() 的调用点在 build_brief 的关键位段（裸调用），
    # 一个异常就让整段「关键位」消失 —— 宁可这一项不显示。
    if h20 <= 0 or l20 <= 0 or h60 <= 0 or l60 <= 0:
        log(f"关键位极值非正（{secid}: h20={h20} l20={l20} "
            f"h60={h60} l60={l60}），弃用", "FEED")
        return None
    out = {"cur": cur, "h20": h20, "l20": l20, "h60": h60, "l60": l60,
           "d20h": (cur/h20-1)*100, "d20l": (cur/l20-1)*100}
    dcache_set(key, out)
    return out


# ---------- 限流与熔断 ----------
class RateLimiter:
    """令牌桶限流 + 指数退避 + 日配额"""
    def __init__(self, min_interval=0.15, daily_cap=2000):
        self.min_interval = min_interval   # 两次请求最小间隔（秒）
        self.daily_cap = daily_cap
        self.last = 0.0
        self.count = 0
        self.day = None
        self.consec_fail = 0
        self.circuit_until = 0.0
        self._cap_warned = False
        self._synced = 0              # self.count 中已写入磁盘的部分
        self._host_circuit = {}       # host -> 熔断截止时间
        # host -> {"n": 窗口内失败次数, "ts": 最后一次失败时刻}
        # 从盘上加载，跨进程累计（单进程内永远到不了阈值，见 fail() 的说明）
        self._host_fail = self._load_host_fail()

    def _roll(self):
        today = datetime.date.today().isoformat()
        if self.day != today:
            self.day = today
            # 日配额跨进程共享：launchd 每 5 分钟起新进程，若 count 只存在
            # 进程内则日配额永不生效（历史教训：约 8400 次/天导致 IP 被封）
            self.count = self._load_count(today)
            self._synced = self.count      # 与盘上一致的基准（_reserve 会同步更新）
            self._cap_warned = False       # 跨天重置，否则第二天不再告警配额耗尽
            self.consec_fail = 0
            self.circuit_until = 0.0
            # ⚠️ 原先这里注释写「主机熔断状态**不跨天重置**」，但代码紧接着
            # 就是 `self._host_circuit = {}` —— 注释与实现**正好相反**。
            # 实测：制造 6 次失败触发熔断后把 self.day 改成昨天再 _roll()，
            # _host_circuit 被清空、wait(host) 立刻放行。
            #
            # 实际行为是**重置**，且这个重置是可达的：_roll() 只在 wait()
            # 开头调用，而熔断检查紧跟在它之后 —— 跨天后第一个 wait() 先
            # 清空、再检查，熔断必丢。
            #
            # 影响很小（进程寿命 < 5 分钟，跨天只在"某轮恰好跨午夜"时发生；
            # 且 _host_dead 落盘 TTL 1800s 才是主要保护，它跨天不清），
            # 所以**保留重置行为**、只把注释改成与实现一致 —— 一个与代码
            # 相反的注释比没有注释更糟：维护者会据此推断出错误结论。
            self._host_circuit = {}
            self._host_fail = self._load_host_fail()

    RATE_FILE = p_state("rate.json")

    def _load_count(self, today):
        """读当日已用配额。

        ⚠️ 损坏时必须留痕。原实现静默返回 0 = **fail-open**：rate.json 被写坏
        时配额从 0 重算，等于当天再放 2000 次请求 —— 而这正是"IP 被封"
        的历史故障模式。至少要让它在日志里可见。
        """
        try:
            d = _load_json(self.RATE_FILE, {})
            if isinstance(d, dict) and d.get("date") == today:
                return int(d.get("count", 0))
        except Exception as e:
            log(f"rate.json 读取失败({e})，配额从 0 重算 —— 请检查该文件", "ERR")
            return 0
        # 文件存在且非空，却读不出当天的记录 —— 分两种情形，不能一视同仁：
        #   · 日期是**别的某天**（通常是昨天）：这是**正常的跨日滚动** ——
        #     每天第一轮必然如此，配额本就该从 0 重新起算。原实现也报
        #     `[ERR] 内容异常`，于是每天开盘第一轮都留一条假 ERR
        #     （实测 2026-10-09 09:19:20 生产日志），真错误反而被淹掉。
        #     这一支**静默返回 0** —— 完全预期的事不需要留痕（留 INFO 的话
        #     每进程一次、全天几十条，只是把假 ERR 换成假噪音）。
        #   · 不是 dict / 没有 date 字段：那才是**真损坏**，必须报 ERR ——
        #     这正是上面那条 fail-open 要留痕的对象。
        try:
            if os.path.exists(self.RATE_FILE) and os.path.getsize(self.RATE_FILE) > 0:
                d = _load_json(self.RATE_FILE, None)
                if not isinstance(d, dict):
                    log("rate.json 结构损坏（非对象），配额从 0 重算 —— 请检查该文件", "ERR")
                elif not d.get("date"):
                    log("rate.json 缺 date 字段，配额从 0 重算 —— 请检查该文件", "ERR")
                # else：date 是别的日期 → 正常跨日滚动，静默。
        except Exception:
            pass
        return 0

    def _reserve(self):
        """在锁内原子地"查配额 + 占一次额度"。返回 False 表示配额已耗尽。

        必须整体在锁内：早先是"先看本地 count 再自增再落盘"，检查在锁外，
        N 个进程会同时看到 count<cap 而一起放行（实测 cap=10 放了 12 次）。
        锁内读-判-写后，跨进程合计恰好等于 cap。

        异常时放行：拿不到锁或磁盘故障不该阻断取数 —— 宁可多发几个请求，
        也不要整个时段没有数据。
        """
        try:
            with _open_lockfile(self.RATE_FILE + ".lock") as lk:
                fcntl.flock(lk, fcntl.LOCK_EX)
                cur = _load_json(self.RATE_FILE, {})
                if not isinstance(cur, dict):
                    # ⚠️ 2026-11-05（P2）：合法 JSON 但**非对象**（数组/字符串）
                    # 时 `cur.get` 抛 AttributeError → 冒到外层 except →
                    # 这次请求放行且不计数；下一次读的还是坏文件 ——
                    # **fail-open 变成永久态，日配额形同虚设**，而日志只有
                    # 一行"配额锁异常"（措辞指向锁，真因是文件形状）。
                    # 按空计数重建（写回会得到正常形状，自愈）。
                    log(f"rate.json 顶层不是对象（{type(cur).__name__}），"
                        f"按 0 计数重建", "ERR")
                    cur = {}
                try:
                    base = (int(cur.get("count", 0))
                            if cur.get("date") == self.day else 0)
                except (TypeError, ValueError):
                    # count 被写成 "abc"/null 等同款问题：坏值按 0 计，
                    # 不能让一次 ValueError 让配额永久失效。
                    log("rate.json 的 count 不是数字，按 0 计数", "ERR")
                    base = 0
                if base >= self.daily_cap:
                    self.count = self._synced = base
                    fcntl.flock(lk, fcntl.LOCK_UN)
                    return False
                base += 1
                # 保留 warned_tier（跨进程去重用），不能被本次计数覆盖掉
                _keep_tier = cur.get("warned_tier")
                _rec = {"date": self.day, "count": base}
                if isnum(_keep_tier):
                    _rec["warned_tier"] = int(_keep_tier)
                # ⚠⚠ 2026-11-11（审查，P2）：**预警与计数必须在同一把锁内、同一次落盘**。
                # 原写法先 `LOCK_UN` 解锁，再在预警块里 `_load_json` + `_atomic_write_json`
                # 写回 —— 而那个块的注释还写着「这里处在 RATE 文件锁内」，**与代码相反**。
                # 后果是丢更新：A 预留到 1600 → 解锁 → 读到 1600；B 预留到 1601 并落盘；
                # A 再把自己那份 1600（+warned_tier）写回去 → 计数退回 1600。
                # 两个请求只计一个，配额限制与预警同时失真。
                # 改法：界内完成全部计算，**只写一次**，落盘后才解锁、才记日志。
                _pct = int(base * 100 / self.daily_cap) if self.daily_cap else 0
                _warn = False
                if _pct >= 80:
                    _tier = _rec.get("warned_tier")
                    _tier = int(_tier) if isnum(_tier) else 0
                    if _pct // 10 > _tier:
                        _rec["warned_tier"] = _pct // 10
                        _warn = True
                _wok = _atomic_write_json(self.RATE_FILE, _rec)
                self.count = base
                if _wok:
                    self._synced = base
                fcntl.flock(lk, fcntl.LOCK_UN)
                if _warn:
                    log(f"日配额已用 {base}/{self.daily_cap}"
                        f"（{_pct}%）—— 接近上限，"
                        f"到达后本轮起全部取数将被拦下", "RATE")
                return True
        except Exception as e:
            # 异常时放行（宁可多发几个请求，也不要整个时段没数据），
            # 但必须留痕 —— 否则"配额锁失败"会表现为请求量悄悄超标。
            log(f"配额锁异常({e})，本轮放行一次请求", "ERR")
            self.count += 1
            return True

    def wait(self, host=None):
        """请求前调用。返回 False 表示应放弃本次请求。

        host 非空时按主机独立熔断 —— 否则主源被封锁(EM 永久 exit 52)会把
        健康的备用主机(EM_TEST)一起拖入熔断，白白丢掉 5 分钟的数据。
        """
        self._roll()
        now = time.time()
        if host is not None:
            if now < self._host_circuit_until(host):
                return False
        elif now < self.circuit_until:
            return False
        if not self._reserve():
            if not self._cap_warned:
                log(f"日配额已耗尽({self.count}/{self.daily_cap})，暂停请求", "RATE")
                self._cap_warned = True
            return False
        # ⚠⚠ 2026-11-10（风控）：节流必须**跨进程**。
        # 原先 `self.last` 是进程内变量 —— 每个进程/线程各记各的，合并后频率翻倍。
        # `[实测]` 两个并发工作流各 6 次请求，**全局最小间隔 2ms**（150ms 下限）。
        # 现实对应：`marketwatch` 与 `marketcollect` 都是 300s 一轮、几乎同时启动；
        # 而**风控看的是同一 IP 的总请求速率**，不分进程。
        # 改用与 `_reserve` 同一份文件锁保护的共享时间戳（`_PROXY_*` 同族的落盘做法）。
        return self._throttle_cross()

    THROTTLE_FILE = p_state("throttle.json")

    def _throttle_cross(self):
        """锁内读-改-写全局上次请求时刻，保证**跨进程**间隔 ≥ min_interval。

        失败（写不进、锁不到）时降级为进程内节流 —— 不能因为它而丢请求。
        """
        try:
            with _open_lockfile(self.THROTTLE_FILE + ".lock") as _lk:
                try:
                    fcntl.flock(_lk, fcntl.LOCK_EX)
                except Exception:
                    pass
                _d = _load_json(self.THROTTLE_FILE, {}) or {}
                _last = _d.get("ts")
                _last = float(_last) if isnum(_last) else 0.0
                _w = time.time()
                if _w - _last < self.min_interval:
                    time.sleep(self.min_interval - (_w - _last))
                    _w = time.time()
                try:
                    _atomic_write_json(self.THROTTLE_FILE, {"ts": _w})
                except Exception:
                    pass
        except Exception:
            _w = time.time()
            if _w - self.last < self.min_interval:
                time.sleep(self.min_interval - (_w - self.last))
        self.last = time.time()
        return True

    def _host_circuit_until(self, host):
        """该主机熔断的截止时刻：内存与**落盘**取较大值。

        ⚠ 为什么要磁盘：本进程每 5 分钟就被 launchd 重新拉起，
        而熔断窗口也是 5 分钟 —— 纯内存的熔断在下一轮开始时
        恰好过期，等于**永远没生效**。
        """
        _mem = self._host_circuit.get(host, 0.0)
        try:
            _rec = (self._host_fail or {}).get(host) or {}
            _disk = float(_rec.get("circuit_until") or 0.0) \
                if isinstance(_rec, dict) and isnum(_rec.get("circuit_until")) else 0.0
        except Exception:
            _disk = 0.0
        return max(_mem, _disk)

    def host_open(self, host):
        """该主机是否**当前可请求**（未处于熔断冷却中）。纯查询，无副作用。

        ⚠️ 存在的理由：`wait()` 返回 False 混着两种原因（主机熔断 /
        日配额耗尽），而调用方需要**区分它们**才能给出正确的日志措辞
        —— 两者的排查方向相反。原先调用方无从区分，只能写一句含糊的
        「配额/熔断限制」。不能用 `wait()` 自己返回原因：它有副作用
        （`_reserve()` 会扣配额、`sleep` 会阻塞），不能为了日志再调一次。
        """
        if host is None:
            return time.time() >= self.circuit_until
        return time.time() >= self._host_circuit_until(host)

    def exhausted(self):
        """日配额是否已耗尽。纯查询、无副作用（与 host_open 同型）。

        ⚠️ 存在的理由：配额耗尽时 `cj()` 与 `feeds._curl()` 都在**发出请求
        之前**就返回空（前者 RATE_LIMITED，后者 ""），于是"源真的坏了"与
        "我们没配额去探测"在返回值上**完全同形**。诊断路径据此会得出相反
        的结论：`feeds.health()` 全 False -> selfcheck 判"所有内置源不可用"
        -> 唤起 Claude 找源。
        `[实测]` 2026-10-01：配额耗尽（2000/2000）时按上述链条跑，
        `ensure_source()`（当时仍在；2026-10-09 确认零调用并删除，本段
        实测记录保留）真实唤起 Claude CLI（`--allowedTools
        WebSearch,WebFetch`，空闲看门狗 600s、**无总时长上限**），
        挂起 >400s —— 自检超时，且全程占着 `selfcheck.lock`，
        下一次调度只能直接退出。而配额耗尽是**常态**（09-22、09-30 都
        出现过 2000/2000），自检每 86400s 触发一次，落在耗尽之后完全可能。
        """
        self._roll()
        return self.count >= self.daily_cap

    def ok(self):
        self.consec_fail = 0

    # 主机熔断状态落盘（跨进程共享）。
    #
    # ⚠️ 原实现里 `_host_fail`/`_host_circuit` 是**纯进程内字典**，而 launchd
    # 每 300s 起一个新进程 -> 每轮从 0 开始。单进程最多累计 retries=3 次，
    # 阈值 10 **永远不可达**（实测 5 轮全部 `_host_circuit={}`）。
    # 叠加 `cj()` 开头的 `_host_dead` 早退门（被判死的主机直接 return None，
    # 连重试都不进），主机级熔断实际从未生效 —— 净效果只有"30 分钟 dead
    # 记忆 + 排序降权"，没有熔断收敛：真正坏掉的主机会被以 300s 间隔
    # 持续打，直到 DEAD_TTL 到期再重试，如此循环。
    #
    # 全局熔断分支（host=None）是**死代码**：全仓库调用点都传 host
    # （marketlib.py 的 cj、feeds.py 的 _curl）。保留但不再假装它有效。
    _HOST_FAIL_FILE = p_cache("host_fail.json")

    def _load_host_fail(self):
        # ⚠️ 必须**逐条**校验，不能整份丢弃。
        # 原实现把过滤写在字典推导里、外面只包一层 `except Exception: return {}`：
        # 任何一个条目的 ts 不是数值（手工编辑 / 旧版本格式 / 半截写入）都会让
        # `now - v.get("ts", 0)` 抛 TypeError，被外层吞成**空字典** ——
        # 于是**所有**主机的失败计数一起归零。
        # 实测（2026-09-27）：3 条记录里把 1 条的 ts 写成字符串，
        # 载入结果是 `{}`（另外两条完好的也丢了）。
        # 后果正是本类上方那段长注释描述的失败模式：主机级熔断
        # （HOST_FAIL_THRESHOLD=6，跨进程累计）**永远收敛不了**，
        # 坏掉的主机会被以 300s 间隔持续打，直到 DEAD_TTL 到期再重试，如此循环。
        # 同类的 isnum 守卫在 _host_dead / hot.HOT_CACHE.ts 已加，这里漏了
        # （feeds.FALLBACK_CACHE.ts 也加过，但该死链已于 2026-10-09 删除）。
        # 坏条目只影响它自己那台主机，其余主机不该跟着一起失去熔断保护。
        out: dict[str, Any] = {}
        try:
            d = _load_json(self._HOST_FAIL_FILE, {}) or {}
            now = time.time()
            for k, v in d.items():
                if not isinstance(v, dict):
                    continue
                _ts = v.get("ts")
                if not isnum(_ts) or now - _ts >= 3600:
                    continue
                out[k] = v
        except Exception:
            pass
        return out

    def fail(self, host=None):
        self.consec_fail += 1
        if host is not None:
            now = time.time()
            # ⚠️ 2026-10-08 并发审查：原实现是"__init__ 加载一次 -> 内存累加
            # -> 整份写回"，collect 与 analyze 并发时互相覆盖失败计数
            # （lost-update）—— 熔断被推迟、坏主机继续被请求，正是本类
            # 上方长注释要解决的"熔断永远收敛不了"。改为 flock 内重新读盘累加。
            _hit = [0]
            def _mu(d):
                # 顺手剪掉窗口外/坏条目，避免文件只增不减
                for k in [k for k, v in d.items()
                          if not isinstance(v, dict) or not isnum(v.get("ts"))
                          or now - v.get("ts", 0) >= 3600]:
                    del d[k]
                rec = d.get(host)
                if not isinstance(rec, dict):
                    rec = {"n": 0, "ts": now}
                # 距上次失败超过 1 小时则重新计数（滚动窗口，不是"累计总数"）
                if now - rec.get("ts", 0) > 3600:
                    rec = {"n": 0, "ts": now}
                try:
                    rec["n"] = int(rec.get("n", 0)) + 1
                except (TypeError, ValueError):
                    rec["n"] = 1
                rec["ts"] = now
                if rec["n"] >= HOST_FAIL_THRESHOLD:
                    _hit[0] = rec["n"]
                    rec["n"] = 0
                    # ⚠ 2026-11-10（Codex P2）：熔断截止时刻**必须落盘**。
                    # 原来只存 `self._host_circuit`（进程内字典），而 analyze/collect
                    # 每轮都是**新进程**、寿命 < 300s 熔断窗口 ——
                    # 于是熔断从来只在单轮内生效，
                    # `HOST_FAIL_THRESHOLD=6` 的跨轮累计意义被这一层抵消。
                    rec["circuit_until"] = now + 300
                d[host] = rec
                return True
            _locked_update(self._HOST_FAIL_FILE, _mu)
            self._host_fail = self._load_host_fail()
            if _hit[0]:
                self._host_circuit[host] = now + 300
                log(f"主机 {host} 1 小时内失败 {_hit[0]} 次，熔断 5 分钟", "RATE")
            return
        if self.consec_fail >= 10:
            self.circuit_until = time.time() + 300   # 熔断5分钟
            log(f"限流器熔断：连续失败{self.consec_fail}次，暂停5分钟", "RATE")
            self.consec_fail = 0

    def stats(self):
        self._roll()
        return {"count": self.count, "cap": self.daily_cap,
                "circuit": max(0, int(self.circuit_until - time.time()))}

RL = RateLimiter()

def _is_hk(i):
    """港股/港股指数：100.HSI、124.HSTECH、116.00700"""
    return i.split(".")[0] in ("100", "116", "124")

# ---------- 行情主机候选与健康度 ----------
# EM 与 EM_TEST 是两台**等价**的东财行情主机（同一套 ulist.np 接口、
# 同一套 secid 规范），区别只在域名。原先硬编码 EM 为主、EM_TEST 为备，
# 但 push2delay 已被按主机名永久封锁 —— 于是每轮都白打一次死主机。
# 实测：09-22 白打 119 次、09-23 白打 143 次。
# 故改为「等价候选 + 健康度排序」，先试活着的那台。
#
# ⚠️ 2026-10-22：候选池扩到**四台**，含两台明文 HTTP 主机
# （EM_HTTP / EM_HIS_HTTP）。依据是当天的逐主机 × 逐端点 × 逐协议
# 实测（见文件头 EM_HTTP 上方那张表）：HTTPS 下只有 push2test 通，
# 而 push2 / push2his 换 http:// 明文后 **ulist.np 与个股字段逐位一致**。
# 把候选从 1 台恢复到 3 台，是「降低被单点限流拖垮」的正解 ——
# 排序层会按实测成功率自动把健康的那台排前面。
#
# ⚠️ 但 clist（板块列表）**不得**加这两台：HTTP 下 clist 在
# push2/push2his 全被拒（rc=52，静默 300s/600s 后重测依旧），
# 加进去只会每轮白打两个必然失败的包。clist 的候选仍是
# `_CLIST_HOSTS`（只有 push2test）。
# ⚠ 2026-11-10：加入 `EM_HTTPS`。它与 `EM_HTTP` 是**同一台主机**的两种协议，
# 可用性独立计算（`_host_of()` 不含协议，故健康度共享）。它让
# `_https_hosts()` 在 `push2test` 挂掉时有第二个能取板块的候选。
EM_HOSTS = [EM, EM_TEST, EM_HTTPS, EM_HTTP, EM_HIS_HTTP]

# **只能走 HTTPS** 的端点用这组候选（clist / trends2 / clist_available）。
# 三个理由，缺一不可：
#   ① clist 在 HTTP 主机上是端点级封锁（rc=52，静默 300s/600s 后仍在）
#   ② trends2 / clist 的板块序列在 HTTP 主机上是**另一个指数基点**
#      （见 _codes_for_host），与 push2test 的日K不可混算
#   ③ 这两条路都不做"按标的类型分流"，加进来就是无条件混用
# 若将来 HTTP 侧解封且基点对齐，改这一行即可，不必动 EM_HOSTS。
#
# ⚠️ 2026-10-23：**必须过滤掉 PERMANENTLY_DEAD**，不能直接写 [EM, EM_TEST]。
# 原先的写法让每个 clist / trend 调用都先撞一次 push2delay —— `cj()` 认得
# 永久封锁，零发包直接返回 None，但**调用方会照常走到下一次循环**，
# 于是每次都要多跑一层封锁判定 + 打一条按日去重日志。
# `[实测]` 2026-10-23 09:20:10 生产日志就是这条：
#   「跳过已永久封锁的主机…push2delay…」—— 而当天 clist 其实一次就成功了
#   （push2test 第一次就返回 496 行）。这条日志读起来像"东财被封了"，
#   实际只是配置里写着别碰它。
# 同一个 bug 昨天在 `feeds.health()` 修过一次（那里改成 _ordered_em_hosts），
# 但 clist / trend / clist_available 三处是**另一份硬编码副本**，漏改了 ——
# 典型的「同一真值抄两份，改一处漏两处」。
# 现在从 `_ordered_em_hosts()` 派生：它已把永久封锁的排末位，
# 再过滤一次即可；同时保留"HTTPS-only"这个语义（HTTP 主机不在 EM_HOSTS
# 的 HTTPS 子集里 —— 见下面的显式过滤条件）。
# ⚠️ 用**函数**而不是模块级常量，因为 `_host_of()` 与 `_ordered_em_hosts()`
# 都定义在本行**之后**（2263 / 2292 行）—— 模块级直接求值会 NameError。
# 惰性求值还有个好处：每轮都按**当时的** dead/健康度重排，而不是
# 在 import 时冻结一份顺序（那份顺序会随首个 clist 调用前的探测而变）。
def _https_hosts():
    """只能走 HTTPS 的端点用的候选：HTTPS 且未被永久封锁，按健康度排序。

    `[实测]` 2026-10-23 09:20:10 生产日志「跳过已永久封锁的主机…push2delay」
    就是这个 bug —— 原实现硬编码 `[EM, EM_TEST]`，每次 clist/trend 都先
    撞一次 push2delay。**同一个 bug 昨天在 feeds.health() 修过**，但那三处
    是另一份硬编码副本，漏改了（「同一真值抄两份，改一处漏两处」）。
    """
    return [h for h in _ordered_em_hosts()
            if h.startswith("https://") and _host_of(h) not in PERMANENTLY_DEAD]

# 明文 HTTP 行情主机集合（供 _codes_for_host 判断）。
_EM_HTTP_HOSTS = {EM_HTTP, EM_HIS_HTTP}


def _codes_for_host(host, codes):
    """该主机本轮该请求哪些 secid —— **板块（90.BKxxxx）只走 HTTPS 主机**。

    ⚠️ 这不是保守，是实测硬约束（2026-10-22）：两台 HTTP 主机与 push2test
    对板块给出的是**不同指数基点**的序列。实测 90.BK1030 电机Ⅱ：
      · 2026-08-20 及之前，两边逐日**完全相同**（ratio=1.000000）
      · 2026-08-21 起 ratio 变为 0.9639，2026-10-16 起恒定 0.986336
      · 最近 5 日 ratio 稳定到小数点后 6 位 —— 不是陈旧，是**整条序列
        被按另一个基点重标定**
    而 `levels()` 的关键位（20/60 日高低）取自 **push2test** 的日K
    （见 `_kline_ohlc` 里 `f"{EM_TEST}/stock/kline/get"` 那一支）。
    两边 f2 混用 → `analyze.build_brief` 的 `(th_h/cur-1)*100` 会带上
    整整 1.4 个百分点的系统性偏差，而那个数字是"距 60 日高还有多少空间"
    的唯一依据。
    比率型字段（f3/f6/f62/f184）两边**逐位一致**，但 f2 会进
    `collect` 的快照（`row["px"] = x["f2"]`）并被跨源相减，
    所以整块板块标的都排除，不做"只取部分字段"的精细切分 ——
    那会让同一行的 f2 与 f3 来自不同基点，更难排查。
    """
    if host in _EM_HTTP_HOSTS:
        return [c for c in codes if not c.startswith("90.")]
    return codes

# ---------- 主源选择：实测可用性驱动 ----------
# 历史上硬编码 EM(push2delay) 为主源。但主机可用性会变（曾整台被按
# 主机名封锁），硬编码意味着"主源死了也不知道"—— 实测 09-22 白打 119 次、
# 09-23 白打 143 次，每轮都先撞一次死主机再回退。
#
# 现在改为：**按实测可用性排序**。可用性来自两个信号叠加：
#   ① _host_dead() —— 近期硬失败（TTL 30 分钟，会自愈）
#   ② _HOST_STAT  —— 滚动成功率（成功/失败计数，跨进程持久化到 cache/）
# 排序键 = (是否已死, 滚动成功率, 上次成功时间)，全部优于"硬编码"。
_HOST_STAT_FILE = p_cache("host_stat.json")
_HOST_STAT: dict[str, Any] = {"loaded": False}

def _host_stat_load():
    """加载滚动统计（跨进程：launchd 每 5 分钟起新进程，进程内计数无意义）。"""
    if _HOST_STAT["loaded"]:
        return
    _HOST_STAT["loaded"] = True
    # ⚠️ 必须**逐条** try。原实现把 int()/float() 转换放在整个 for 循环里、
    # 外面只包一层 `except Exception: pass` —— 任一条目的字段是字符串
    # （手工编辑 / 旧版本格式 / 半截写入）就抛 ValueError，**整个循环中断**，
    # 排在它后面的主机**全部静默丢失**（实测：3 条里第 2 条 ok="abc"，
    # 第 3 条完好的主机没被载入）。
    # 后果：_host_rate() 对丢失的主机返回中性 0.5、last_ok 为 0，
    # _ordered_em_hosts() 的排序键失真 —— 明明健康的主机被排到后面，
    # 每轮先撞一次坏主机。与 _load_host_fail 是同一类缺陷（已修）。
    d = _load_json(_HOST_STAT_FILE, {}) or {}
    # ⚠️ 2026-11-05（P1）：顶层必须是 dict。`_load_json` 对半截写入之外的
    # 合法但错形状 JSON（数组/标量）也原样返回 —— `d.items()` 直接
    # AttributeError，整个 `_host_rate` 排序链崩。逐条 try 保护不了这一层。
    if not isinstance(d, dict):
        return
    for k, v in d.items():
        if not isinstance(v, dict):
            continue
        try:
            _HOST_STAT[str(k)] = {
                "ok": int(v.get("ok", 0)), "fail": int(v.get("fail", 0)),
                "last_ok": float(v.get("last_ok", 0)),
            }
        except (TypeError, ValueError):
            # 坏条目只影响它自己那台主机，其余照常载入
            continue

def _host_stat_bump(host, ok):
    """记录一次请求结果。只记最近 200 次，避免历史包袱让主机永远翻不了身。

    ⚠️ 2026-10-08 并发审查：原实现是"进程启动时加载一次 -> 内存累加 ->
    每次整份写回"。collect 与 analyze 并发时，后写者用自己启动时的旧快照
    覆盖先写者的增量（lost-update）——成功率统计回退、熔断被推迟、坏主机
    继续被请求白扣配额。改为 flock 内**重新读盘**再累加（与 dcache_set 同模式）。
    """
    now = time.time()
    def _mu(d):
        st = d.get(host)
        if not isinstance(st, dict):
            st = {"ok": 0, "fail": 0, "last_ok": 0.0}
        try:
            ok_n = int(st.get("ok", 0)); fail_n = int(st.get("fail", 0))
        except (TypeError, ValueError):
            ok_n = fail_n = 0
        last_ok = st.get("last_ok", 0.0)
        if not isnum(last_ok):
            last_ok = 0.0
        if ok:
            ok_n += 1
            last_ok = now
            # 成功时衰减失败计数：主机恢复了就该被重新信任
            fail_n = max(0, int(fail_n * 0.5))
        else:
            fail_n += 1
        if ok_n + fail_n > 200:
            ok_n = int(ok_n * 0.5)
            fail_n = int(fail_n * 0.5)
        d[host] = {"ok": ok_n, "fail": fail_n, "last_ok": last_ok}
        return True
    _locked_update(_HOST_STAT_FILE, _mu, indent=1)
    # 刷新进程内视图（clear 后重载 = 与盘上合并后的真值一致），
    # 供本进程后续 _host_rate()/排序键读取。
    _HOST_STAT.clear()
    _HOST_STAT["loaded"] = False
    _host_stat_load()

def _host_rate(host):
    """滚动成功率 0.0~1.0。样本不足时给中性值 0.5（不做过度判断）。"""
    _host_stat_load()
    st = _HOST_STAT.get(host)
    if not isinstance(st, dict):
        return 0.5, 0.0
    ok, fail = st.get("ok", 0), st.get("fail", 0)
    n = ok + fail
    if n < 3:
        return 0.5, st.get("last_ok", 0.0)
    return ok / n, st.get("last_ok", 0.0)

def _host_of(url):
    """从 URL 取主机名（用于 _host_dead 缓存键）。"""
    try:
        return url.split("//", 1)[1].split("/", 1)[0]
    except Exception:
        return url

def _host_key(url):
    """健康度/判死的键：**协议 + 主机名**。与 `_host_of()` 分工。

    ⚠⚠ 2026-11-10（Codex P2）：原来全部用 `_host_of()`（**不含协议**）。
    而 `EM_HTTPS` 与 `EM_HTTP` 是**同一台主机的两种协议**、帮助度却完全不同
    （本仓库自己的 `_host_name` 就为此专门区分 `em_push2` / `em_push2_http`）。
    用同一个键的后果：一批 HTTP 请求失败会让 **HTTPS 候选一起被判死
    30 分钟**，之后 `cj()` 跳过直连只走兜底 —— 而 HTTPS 可能全好。
    反之，两个活探测也会互相污染健康分。

    ⚠ 但 `PERMANENTLY_DEAD` 必须继续用 `_host_of()`——那是“这台主机人工确认封锁”，
    与协议无关。两个语义不能合并。
    """
    h = _host_of(url)
    return ("http://" if url.startswith("http://") else "https://") + h


def _host_name(url):
    """主机短名，写进 row['_src'] 与日志，便于回查数据来自哪台。

    ⚠️ 2026-10-22：必须带上**协议**。候选池里现在同时有
    `https://push2his…`（全端点被封）与 `http://push2his…`（可用），
    两者主机名相同 —— 只按主机名归一会让日志与 `_src` 完全无法区分
    "这轮的数据是走通的那台还是被封的那台"，上面那张封锁矩阵也就
    没法在事后回放里核对。
    """
    h = _host_of(url)
    _http = url.startswith("http://")
    if h.startswith("push2delay"):
        return "em"
    if h.startswith("push2test"):
        return "em_test"
    if "his" in h:
        return "em_his_http" if _http else "em_his"
    if h.startswith("push2."):
        # ⚠ 2026-11-10：原注释说“push2.eastmoney.com 只有明文可用
        # （HTTPS 全端点 rc=52）”—— 那个封地**已解除**。
        # `[2026-11-10 实测]` HTTPS 三个端点全 200，板块 f2 与明文逐位一致。
        # 协议仍要分开标注（与 push2his 同理）：日志里要能区分
        # “这轮走的是 HTTPS 还是明文”，否则断开后无法回放。
        return "em_push2_http" if _http else "em_push2"
    return h

def _ordered_em_hosts():
    """按**实测可用性**排序返回行情主机，最优在前。

    排序键 (永久封锁, 已死, -成功率, -上次成功时间)：
      · 未永久封锁 > 永久封锁  —— `PERMANENTLY_DEAD` 是**人工确认**的硬事实
      · 未死 > 已死            —— _host_dead 是硬证据（TTL 30 分钟会自愈）
      · 成功率高 > 成功率低    —— 滚动统计，样本 <3 次时中性 0.5
      · 最近成功 > 久未成功    —— 同一成功率下偏好新鲜的

    ⚠️ 必须把 `PERMANENTLY_DEAD` 排在**第一位**。原先只判 `_host_dead`，
    而它有 30 分钟 TTL —— 过期后 `push2delay` 就重新参与排序。
    `[实测]` 2026-09-30：TTL 过期后它成功率仅 **3.4%**（ok=1 / fail=28），
    却因为"两台主机里总得排一个在前"而仍可能被先试到。
    `cj()` 内部已能拦住它（见那里的 `PERMANENTLY_DEAD` 判定），
    但那是**第二层**兜底 —— 排序层是"该不该把它当候选"的第一道判断，
    两层都要判，否则第一层失效时没有任何提示。

    不剔除任何主机：_host_dead 有 TTL、`PERMANENTLY_DEAD` 也可能被解封，
    排在后面仍会被试到（且被 `cj()` 拦下）。
    """
    _host_stat_load()
    def key(h):
        perm = 1 if _host_of(h) in PERMANENTLY_DEAD else 0
        # ⚠ 用 _host_key（含协议）：否则 HTTP 失败会把 HTTPS 候选一起排到末位
        # ⚠️⚠️ 2026-11-19（P0，生产事故）：排序**只读**判死状态，绝不触发半开探测。
        #
        # `_host_dead()` 有个隐藏副作用：冷却期过后它会**消费掉本轮唯一的
        # 探测令牌**（`_HALF_OPEN_TRIED.add(host)`），返回 False 表示"放行"。
        # 而排序函数每轮要调它 **5 次**（每台主机一次）—— 于是令牌在
        # **排序阶段就被吃光**，等 `cj()` 真正要发包时那次判定已变成
        # "本轮探过了，继续判死"，直连被跳过、直接砸到 firecrawl 兜底。
        #
        # `[实测]` 沙箱复现（n=7、冷却已过）：
        #     不先排序 → _host_dead() = False（放行直连）
        #     先排序后 → _host_dead() = True （直连被跳过，走兜底）
        # 同一时刻、同一份状态，结论相反。
        #
        # 生产后果就是 2026-11-19 10:00 那条推送：板块 0/12 全灭，
        # 「数据本轮不可用」。判死状态显示的 n 只增不减（7→8），
        # 因为探测令牌每轮都被排序吃掉，直连永远没机会成功、`_clear_dead()`
        # 永远不会被调用 —— 指数退避于是从 5 分钟一路爬到 60 分钟。
        #
        # 修法：排序用 `half_open=False`（纯读，不消费令牌）。
        dead = 1 if _host_dead(_host_key(h), half_open=False) else 0
        rate, last_ok = _host_rate(_host_key(h))
        return (perm, dead, -rate, -last_ok)
    return sorted(EM_HOSTS, key=key)

# 港股 secid -> 腾讯代码。腾讯是港股的实际权威源（qt.gtimg.cn）。
_TX_HK = {
    # ⚠️ 港股指数必须用 **r_ 前缀（实时）**：不带前缀的 hkHSI/hkHSTECH 是
    # **延迟 15 分钟**的行情（实测 2026-10-08 实时探针：hkHSI 时间戳
    # 15:44:34 vs r_hkHSI 15:59:39）。后果是每天 13:00–13:15 恒生/恒生科技
    # 被新鲜度闸门丢弃 3 轮（午休豁免 13:00 整结束，而延迟源的最新报价
    # 还停在 12:00，年龄 45–75 分钟 > 20 分钟阈值）—— hist 缺行、
    # hstech 三态静默不可判。个股本来就用的 r_ 前缀，指数是唯一漏网。
    "100.HSI":    "r_hkHSI",
    "124.HSTECH": "r_hkHSTECH",
    "116.00700":  "r_hk00700",
    "116.09988":  "r_hk09988",
    "116.00981":  "r_hk00981",
    "116.01810":  "r_hk01810",
    "116.03690":  "r_hk03690",
    "116.09618":  "r_hk09618",
}

def _hk_quotes_via_tencent(secids):
    """港股行情走腾讯，返回与 quotes() 兼容的 {secid: row}。

    为什么不复用 feeds.backup_quotes：它按**短码**建键（00700），而调用方
    （build_brief / collect）用的是完整 secid（116.00700）。原先靠下游补别名
    兜住，既绕又脆。这里直接按请求的 secid 建键，一次到位。
    """
    import feeds
    pairs = [(i, _TX_HK[i]) for i in secids if i in _TX_HK]
    if not pairs:
        return {}
    t = feeds.from_tencent([tk for _, tk in pairs])
    out = {}
    for secid, tk in pairs:
        row = t.get(tk)
        if not row:
            continue
        px, chg = row.get("px"), row.get("chg")
        if not (isnum(px) and px > 0):
            continue
        out[secid] = {"f12": secid.split(".")[-1], "f14": row.get("name"),
                      "f2": px, "f3": chg, "_src": "tencent",
                      "_cross_ok": True,    # 已在取数处做过正数校验
                      # ⚠️ _ts 必须透传。港股**只走这条路径**（见 quotes() 里
                      # "不要改成 _try_hosts" 那段），而 quotes() 末尾的新鲜度
                      # 闸门只对带 _ts 的行走 —— 原先这里把 _ts 丢了，于是
                      # **整个港股的新鲜度闸门从未生效过**：
                      # feeds.from_tencent 明明解析并保留了 _ts（它的注释还
                      # 专门说明"备用源原先完全不输出时间戳，无法区分今天的
                      # 收盘价与上周五的收盘价"），却被本函数在这一步扔掉。
                      # 后果与 _is_ts_stale 那个 NameError 是同一个失败模式：
                      # 腾讯在休市/限流时返回上一交易日的港股价格，会被当成
                      # 现价写进推送，日志里零痕迹。实测（2026-09-27）：
                      # 构造昨日时间戳，quotes(["124.HSTECH"]) 照原样返回。
                      # A股路径没这个问题 —— 它走 _fetch → _valid_quotes，
                      # 行里的 _ts 被保留。
                      "_ts": row.get("_ts", "")}
    return out or {}

def _valid_quotes(o, min_ratio=0.5):
    """数据合理性校验：返回过滤后的 dict，或 None 表示不可用。

    判据：
      · o 必须是 dict 且非空
      · 至少 min_ratio 比例的条目含**有限正数**的 f2（现价）
    剔除 f2 <= 0 / 非数（停牌占位、字段错位返回 0）。

    为什么需要：EM_TEST(push2test) 对港股会返回**字段错位**的数据 ——
    实测腾讯控股 f2=661.5（真值 441.6，差 49.8%）、涨幅 f3=+60.64%。
    这类"请求成功但数据是错的"最危险：不报错、不入日志、直接进推送。
    所以不能只判"有没有返回"，必须判"返回的是不是合理数字"。
    """
    if not isinstance(o, dict) or not o:
        return None
    # ⚠️ 必须**真的剔除**坏项，不能只统计后原样返回。
    # 文档一直写"返回过滤后的 dict…剔除 f2<=0/非数"，但原实现只数了个比例
    # 就 `return o` —— 坏行（停牌占位 f2=0、字段错位返回 "-"）**原样进结果**。
    # 后果不只是"多一行垃圾"：
    #   quotes() 末尾的备用源补齐是 `miss = [i for i in ids if i not in out]`
    #   —— 坏行**键已存在**，于是该标的永远不会被备用源补上，下游拿到
    #   f2=0 后在 push 里渲染成「科创50 +0.00%」这种看着正常、其实是没数据的
    #   数字（analyze 的 isnum 守卫只挡 "-"，挡不住 0）。
    # 剔除后键消失，备用源补齐自然接管。
    bad = [k for k, v in o.items()
           if not (isinstance(v, dict) and isnum(v.get("f2")) and v["f2"] > 0)]
    for k in bad:
        del o[k]
    if not o or len(bad) / (len(o) + len(bad)) > (1 - min_ratio):
        return None
    return o

def _inject_ts(o):
    """给 `_fetch` 产出的原始 EM 行注入 `_ts`（2026-10-28）。

    抽成独立函数的理由是**可测**：原先它是 `_fetch` 里的一段内联
    循环，测试只能拿源码子串去断言 —— 而子串在注释里也存在，
    断言恒真。现在直接喂行进来看标记。

    ⚠️ 不覆盖已带 `_ts` 的行（快照复用/备用源注入的时间戳必须原样保留）。

    ⚠⚠ 2026-11-07（复核 F5）：f124 不可用的行原来**什么标记都不留**。
    下方 `quotes()` 的新鲜度闸门对「无 `_ts`」的行直接放行 —— 两个
    缺口叠在一起，这类行就**绕过了新鲜度闸门且不留痕迹**。
    同一语义在本文件里有三种处理：`_ts_unknown` → 丢行；`_ts` 缺失 → 放行；
    解析失败 → 放行且记一行日志。f124 缺失是第四条路径，也是唯一完全静默的一条。

    为什么这一步只标记、不直接判「丢」：这是**主路径**，而 f124 在
    集合竞价/停牌时本就可能缺失 —— 直接 fail-closed 会让整个市场在那些
    时段集体消失（跑一下午才能发现）。先把它变成**可观测**：标 `_ts_missing`，
    并在闸门里记一行 FEED 日志。真实占比出来后再决定是否锁死。
    """
    for _x in o.values():
        if not isinstance(_x, dict) or _x.get("_ts"):
            continue
        _e = _x.get("f124")
        if isinstance(_e, (int, float)) and not isinstance(_e, bool) and _e > 0:
            try:
                _x["_ts"] = datetime.datetime.fromtimestamp(
                    float(_e)).strftime("%Y%m%d%H%M%S")
                # 这条链路只做「跨日」判据（见 `_is_ts_stale` 的
                # `cross_day_only` 参数）。集合竞价期间 f124 可能仍是昨日
                # 收盘时刻，20 分钟阈值会误杀盘前数据。
                _x["_ts_crossday"] = True
            except (OverflowError, OSError, ValueError):
                _x["_ts_missing"] = True
        else:
            _x["_ts_missing"] = True
    return o

def _fetch(host, ids, fields):
    """请求行情。返回 {代码: row}，代码取自请求的 secid。

    字段里会强制带上 f12（见下一行的 f 拼接），因为回填**依赖 f12**。
    调用方只传 f2,f3 时若不自补，f12 缺失会让整批结果对不上号。

    回填策略（两种，按数量是否吻合分流）：
      · len(rows) == len(ids) —— 按位置回填。东财 diff 顺序与请求 secids 一致。
      · 否则 —— 按 f12 与 secid 后缀**精确匹配**，匹配不上就不建键。
        （东财会静默跳过无效 secid，此时按位置回填会张冠李戴，详见下方注释。）

    ⚠️ 本函数**不会**过滤 f2<=0 的坏行 —— 那是 _valid_quotes 的职责。

    ⚠️ 会强制补 `f124`（行情更新时间戳，unix 秒）并注入 `_ts`（2026-10-28 修，
    P2）：原先主路径**从不带 _ts**，而 `quotes()` 末尾的新鲜度闸门只对
    `v.get("_ts")` 为真的行生效 —— 于是**闸门对 A 股主链路结构性失效**，
    实际只保护港股（腾讯路径）与备用源。一旦 EM 限流期回上一交易日点位，
    `_valid_quotes` 只校验"有限正数"，拦不住。
    `[实测]` 2026-10-28 盘中：f124 与 `time.time()` 相差 16s（指数/板块/
    个股全部如此），是可信的行情时刻。
    ⚠️ 但**判据必须放宽到"跨日"**（见 `_is_ts_stale` 的 `em=True` 分支）：
    A 股 09:15–09:25 集合竞价期间 f124 可能仍是**昨日**收盘时刻（撮合未出
    开盘价），用 20 分钟阈值会把整个盘前简报的 A 股数据全部丢掉。
    """
    f = fields if "f12" in fields.split(",") else fields + ",f12"
    if "f124" not in f.split(","):
        f += ",f124"
    d = cj(f"{host}/ulist.np/get?secids={','.join(ids)}"
           f"&fields={f}&ut={UT}&fltt=2&invt=2")
    # 配额耗尽哨兵必须**原样穿透**给 _try_hosts —— 它据此避免把本地限流
    # 误记为主机故障。返回空 dict 会被当成"主机失败"而标记死亡。
    if d is RATE_LIMITED or d is HOST_CIRCUIT:
        return d          # 两种「本地未发包」哨兵都原样穿透给 _try_hosts
    o = {}
    try:
        diff = d["data"]["diff"]
        rows = diff if isinstance(diff, list) else list(diff.values())
    except (TypeError, KeyError, AttributeError):
        return o
    # 按顺序回填：东财 diff 顺序与请求 secids 一致，但无效 secid 会被静默跳过，
    # 一旦错位就会张冠李戴。故仅在数量吻合时才按位置回填，否则丢弃。
    if len(rows) == len(ids):
        for i, x in enumerate(rows):
            if not isinstance(x, dict): continue
            # 键必须用**请求时的原始 secid**，不能用 f12。
            # 板块的 f12 是短码 "BK1036"，而调用方（build_brief / sectors.json）
            # 用的是完整 secid "90.BK1036" —— 键一短一长，q.get("90.BK1036")
            # 永远返回 None，导致 sectors.json 的 watch/push 板块**全部静默丢失**。
            # 这正是「固定看的板块」从来没出现在推送里的原因。
            o[ids[i]] = x
    else:
        # ⚠️ 数量不吻合（东财静默跳过无效 secid）时**绝不能按位置回填**。
        # 原实现 `o.setdefault(ids[i], x)` 把第 i 行配给第 i 个请求 secid ——
        # 一旦跳过，其后每个标的都拿到**前一个标的**的行情。且这种错值拦不住：
        #   · _valid_quotes 只校验"有限正数"，错位后的价格仍是真实价格 → 通过
        #   · quotes() 末尾的别名补齐是 `if i not in out` → 错值已存在，
        #     正确值永远不会覆盖它
        #   · _pq()/pick() 都是"完整 secid 优先" → 错值胜出
        # 结果：sectors.json 里任一 code 失效，该批其后所有标的在推送里
        # 全部是别人的数字。这正是 _valid_quotes 注释自己点名最危险的那类
        # ——「请求成功但数据是错的」。
        #
        # 改为**按 f12 与 secid 后缀精确匹配**：匹配不上就不建键。
        # 宁可缺数据（下游有 if not x 守卫，会降级/跳过），绝不错数据。
        by_short = {}
        for x in rows:
            if not isinstance(x, dict): continue
            k = x.get("f12")
            if k is not None and str(k) != "": by_short[str(k)] = x
        for sid in ids:
            x = by_short.get(str(sid).split(".")[-1])
            if x is not None: o[sid] = x
        # 短码键保留一份：备用源与部分调用方用短码取值
        for k, x in by_short.items():
            o.setdefault(k, x)
    # 注入 `_ts`（抽成函数以便单独测：现场逻辑在 `_fetch` 里不可命中）。
    _inject_ts(o)
    return o

def quotes(ids, fields="f2,f3,f6,f12,f14,f24,f62,f104,f105,f184",
           allow_backup=True):
    """行情快照。多主机故障转移 + 备用源降级。

    fields 里的 f104/f105 是**涨家数/跌家数**（仅 A 股指数返回，港股无）。
    加它们是为了让播报的「盘面」段有市场宽度可写 —— 原先只有指数涨跌幅，
    读者看不出"是普跌还是权重股拖累"（实测 2026-09-24 用户反馈盘面不够丰富）。
    多取两个字段不增加请求数（同一个 ulist.np 调用）。

    港股与A股分流：备用行情主机(EM_TEST)的港股字段陈旧（f18昨收对不上），
    实测 HSI/个股偏差可达 46%，所以港股只用主源，失败即走腾讯。
    A股与板块：主源 → EM_TEST → 腾讯/新浪等。
    返回 {代码: row}
    """
    # ⚠️ 新一轮开始：重置"本轮已计过失败的主机"与半开探测令牌。
    # 必须在这里（而不是 `_mark_dead` 里）—— 本函数是**顶层取数入口**，
    # collect/analyze 每轮各调它若干次。放在这里意味着"一轮 = 一次
    # quotes() 调用"：同一次调用里 12 个板块逐个失败只算 **1 次**，
    # 而不是 12 次（那会把指数退避放大一个数量级，见 `_mark_dead` 的注释）。
    _round_begin()
    ids = list(ids)
    hk  = [i for i in ids if _is_hk(i)]
    a   = [i for i in ids if not _is_hk(i)]
    out = {}

    # ---------- 行情主机顺序：动态、可探活 ----------
    # 原先硬编码 EM 为"主源"，只在它**已经失败之后**才用 EM_TEST。而
    # push2delay 已被按主机名永久封锁（curl exit 52 / Empty reply），
    # _host_dead() 早就把它标死了，但 quotes() 从不查这个标记 ——
    # 结果每轮都白打一次死主机、记一条 FEED 日志，再走 EM_TEST。
    # 实测 09-22 一天 119 次、09-23 143 次无谓失败。
    #
    # 改为：把两台主机视为**等价候选**，按 _host_dead() 健康度排序，
    # 先试健康的。健康的那台失败时才回退到另一台，并立刻标记死亡。
    em_hosts = _ordered_em_hosts()

    def _try_hosts(codes, tag):
        """按可用性顺序尝试各行情主机，**带回数据校验**。

        校验两层：
          ① 完整性 —— 该主机是否真的返回了数据（空/部分返回都算失败）
          ② 合理性 —— 返回的价格是否为有限正数（isnum 已排除 nan/inf/bool，
                       这里再排除 <=0 的占位值，如停牌时的 0）

        成功/失败都写进滚动统计，下一次 _ordered_em_hosts() 会据此重排。
        这让"哪台是主源"由**实测**决定，而不是靠硬编码。
        """
        got = {}
        for host in em_hosts:
            hn = _host_of(host)          # 裸主机名：用于 PERMANENTLY_DEAD（与协议无关）
            hk = _host_key(host)         # 协议+主机：用于健康度/判死（两种协议必须分开）
            # ⚠️ 永久封锁的主机**不参与尝试**（2026-10-22）。
            # `cj()` 认得 `PERMANENTLY_DEAD`，零发包直接返回 None —— 但这里
            # 的调用方会把它当成「这一轮它又失败了」：`_host_stat_bump(hn, False)`
            # 记一次假失败、`_mark_dead(hn)` 盖 30 分钟死亡戳并打一条
            # 「主机判死 … 若站点明明健康，查是否被瞬时超时误判」。
            # 零发包不可能是超时，这条日志把排查方向指向「误判」。
            # `[实测]` 2026-10-22 11:39:34 那条就是这个形态；
            # 统计侧 `push2delay: ok=1 / fail=35` 里相当一部分是假失败，
            # 真实故障信号被稀释。
            if hn in PERMANENTLY_DEAD:
                continue
            # ⚠️ 2026-10-22：候选池里有两台**只能服务非板块标的**的明文
            # HTTP 主机（见 _codes_for_host）。故每台主机只请求「还没取到
            # 且它有能力服务」的那部分。
            pending = [c for c in codes if c not in got]
            if not pending:
                return got
            sub = _codes_for_host(host, pending)
            if not sub:
                # 该主机不具备服务这批标的的能力（板块只在 HTTPS 主机上
                # 有可信序列）—— **不是失败**，不计入统计、不判死。
                continue
            # 这台能不能覆盖**全部**待取项？能，则它成功后就收工
            # （与旧行为一致：旧代码只有一台可用，成功即 return）。
            # 不能（HTTP 主机跳过板块），成功后还要继续往后找板块。
            # ⚠⚠ 2026-11-11（审查，P2）：**"被请求全部" ≠ "返回全部"。
            # `_covers_all` 只说明这台主机**有能力服务**这批标的（所以被要了全部），
            # 而它的回包可能**只含其中一部分**。
            # 原写在 `got.update(good)` 后 `if _covers_all: return got` ——
            # 于是首台只回上证、不回板块时就提前收工，后面能提供板块的
            # 主机**永远不被调用**；若备用源也不覆盖板块，盘面就少一块。
            # 正确判据：**pending 全部拿到才收工**（`got` 以 secid 为键）。
            o = _fetch(host, sub, fields)
            if o is HOST_CIRCUIT:
                # 主机熔断冷却中（会自愈）：**换下一台**，不判死、
                # 不记失败、更不放弃整轮候选（2026-11-05 修，P1）。
                _daily_log_once(hn, 'hostcircuit',
                                f'行情主机 {hn} 熔断冷却中，本轮换下一台')
                continue
            if o is RATE_LIMITED:
                # 配额耗尽：本地限流，与主机可用性无关 —— 直接放弃本轮，
                # **不标记主机死亡、不计失败**。
                log("日配额耗尽，本轮放弃取数（不记为主机故障）", "RATE")
                return got
            good = _valid_quotes(o)
            _host_stat_bump(hk, bool(good))
            if good:
                name = _host_name(host)
                for x in good.values():
                    x["_src"] = name
                # ⚠️ 判据是「**发生了降级**」，不是「不是 em」。
                # 原写 `if name != "em"` —— 那是"主源固定是 push2delay"
                # 时代的写法。而 push2delay 已进 `PERMANENTLY_DEAD`，
                # `_ordered_em_hosts()` 现在把 `em_test` 排在**第一**，
                # 于是它是**当前的首选主机**，每轮成功都被记成"顶上"。
                # `[实测]` 2026-09-30：当天 99 行「行情主机 em_test 顶上」，
                # 占全天 FEED 日志的 **98%** —— 把真实信号彻底淹没
                # （watch.log 是 2MB 单代轮转，噪音直接挤掉诊断信息）。
                #
                # 正确判据：成功的主机**不是排序首选**时才算降级。
                # 首选成功 = 正常路径，不记；次选顶上 = 首选失败，必须记。
                # 只在**还没取到任何东西**时判 —— 后续补缺口的那台不算
                # 顶上（它补的是首选**结构上服务不了**的标的，不是降级）。
                if not got and host is not em_hosts[0]:
                    log(f"行情主机 {name} 顶上（首选失败，{tag} {len(good)} 项）",
                        "FEED")
                got.update(good)
                # ⚠ 收工条件看**实际拿到了多少**，不看请求了多少。
                if not [c for c in codes if c not in got]:
                    return got
            else:
                # 该主机失败：标记死亡，冷却期内不再试
                _mark_dead(hk)
        if len(got) < len(codes):
            # 板块（90.*）缺了要单独说 —— 这是"唯一可信源是 HTTPS 主机、
            # 而它这轮失败了"的形态（HTTP 主机结构上不接板块），
            # 与"备用源没覆盖"完全不同，排查方向也不一样。
            _miss_blk = [c for c in codes if c not in got and c.startswith("90.")]
            if _miss_blk:
                log(f"{tag} 有 {len(_miss_blk)} 个板块标的未取到 —— "
                    f"板块序列只在 HTTPS 主机上有可信数据（HTTP 主机指数"
                    f"基点不同，见 _codes_for_host），本轮该源未成功", "FEED")
        return got

    if a:
        out.update(_try_hosts(a, "A股/板块"))
    if hk:
        # ---------- 港股：唯一可信路径是腾讯 ----------
        # 不要改成 _try_hosts()！实测 EM_TEST(push2test) 的港股 f2/f3
        # 字段口径与 A 股完全不同，返回的是**有限正数但错误的数**：
        #   腾讯控股 661.5（真值 441.0，差 +50.0%）、f3=+60.64%
        #   恒生指数 25737.6（真值 24824，差 +3.7%）
        #   阿里 119.0（真值 109.8，+8.4%）/ 中芯 65.2（真值 63.8，+2.2%）
        # 只有恒生科技恰好接近（+0.0%）—— 所以**不能按标的挑**，只能整类排除。
        #
        # 关键：这类错误 _valid_quotes() 拦不住（值都是有限正数），
        # 自检永远发现不了，必须靠**交叉源比对**。故港股直接走腾讯，
        # 从源头绕开这个坑；EM 仅在腾讯失效时作为最后手段。
        hk_key = {}
        try:
            hk_key = _hk_quotes_via_tencent(hk)
        except Exception as e:
            log(f"港股腾讯源异常: {e}", "FEED")
        if hk_key:
            out.update(hk_key)
        else:
            # 腾讯挂了才退到 EM（此时宁可要可能有偏差的数据，也不要空）
            # ⚠️ EM 若已在 PERMANENTLY_DEAD 里，**根本不该去试**：
            # `cj()` 会零发包直接返回 None，而下面 `elif not o:` 分支会
            # `_mark_dead(EM)` —— 把一台「人工确认已封锁」的主机再盖一次
            # 30 分钟死亡戳，并打一条「港股腾讯与EM均不可用」的日志。
            # 那条日志指向一个从未发生的网络失败（实测 2026-10-22 11:39:
            # 11:39:34 那条「主机判死 push2delay…查是否被瞬时超时误判」
            # 就是这么来的 —— 零发包不可能是超时）。
            # 这里显式跳过，并把日志改成指向真正的缺口：**没有可用港股源**。
            # ⚠⚠ 2026-11-10（审查发现，结构性不可达）：原来写的是 `if _host_of(EM) in
            # PERMANENTLY_DEAD ... else: _fetch(EM, ...)`，而 `EM` 本身就是
            # `push2delay.eastmoney.com`、**就在** PERMANENTLY_DEAD 里 —— 那个 else 永远
            # 走不到，港股的“最后手段”从未存在过（腾讯一挂就整段失明）。
            # 不能改用 `EM_TEST`（push2test）—— 它的港股 f2/f3 口径不同，
            # 实测腾讯控股偏差达 +50%（见上方长注释）。
            # 改用 `_ordered_em_hosts()` 的头一台（实测 2026-11-10 就是 push2），
            # 并**先核对口径**：同时拿它与腾讯的港股点位比对，偏离超 5%
            # 就宁可不要（不能把 +50% 的错数当成真数推给用户）。
            _hk_fb = ""
            try:
                _cand = [h for h in _ordered_em_hosts()
                         if _host_of(h) not in PERMANENTLY_DEAD
                         and _host_of(h) != _host_of(EM_TEST)]
                _hk_fb = _cand[0] if _cand else ""
            except Exception:
                _hk_fb = ""
            if not _hk_fb:
                log("港股腾讯源不可用，且无可用的 EM 备用主机 —— "
                    "本轮无港股行情", "FEED")
            else:
                o = _fetch(_hk_fb, hk, fields)
                # ⚠️ 用 o is None 而非 `if o:` 区分"取数失败"与"配额耗尽"。
                # 原写法把 RATE_LIMITED 置成 None 后再落到 else 分支，
                # 于是**配额耗尽照样 _mark_dead** —— 与 _try_hosts 里那段
                # "不标记主机死亡、不计失败"的注释直接矛盾（那处是对的，
                # 这处漏了）。实测（沙箱、count=daily_cap）：
                #   quotes(["1.000001"])          → dead_hosts 为空（A股路径已修）
                #   quotes(["124.HSTECH"])        → push2delay 被盖上 30 分钟死亡戳
                # 生产实证：09-24 19:49:25 dead_hosts.json 两个主机被标死，
                # 同秒 watch.log 全是"备用源配额/熔断限制，跳过"、零真实网络失败。
                # 后果：配额耗尽期间每轮把主机重新标死，主机恢复后还要再等
                # DEAD_TTL(30 分钟) 才会被 _ordered_em_hosts() 优先选用 ——
                # 而这恰好覆盖次日 09:25 的首轮。真实故障信号同时被假失败稀释。
                if o is RATE_LIMITED:
                    log("日配额耗尽，港股 EM 回退跳过（不记为主机故障）", "RATE")
                elif o is HOST_CIRCUIT:
                    # 熔断冷却中：不是故障、也不是并发用尽 —— 不判死。
                    log("港股 EM 主机熔断冷却中，本轮不回退", "FEED")
                elif not o:
                    # 空 dict / None 都算"主机给了数据但没一项可用" —— 仍标记死亡。
                    # 只有上面的 RATE_LIMITED（本地限流、零网络请求）才豁免。
                    _mark_dead(_host_key(_hk_fb))
                    log("港股腾讯与EM均不可用", "FEED")
                else:
                    # ⚠⚠ 口径核对（必须）：这条回退路径历史上被一台只返回
                    # "有限正数但错误" 的主机坑过（push2test 偏差 +50%），
                    # 而 `_valid_quotes()` 拦不住这类错。所以不能“有数就用”：
                    # 用 `_hk_fb` 与金融/指数的另一份参考核对，偏离超 5% 就丢弃。
                    _hk_ok = True
                    try:
                        _ref = _hk_quotes_via_tencent(list(o.keys())[:3]) or {}
                        for _k, _v in list(o.items())[:3]:
                            _r = _ref.get(_k) or {}
                            _a, _b = _v.get("px"), _r.get("px")
                            if isnum(_a) and isnum(_b) and float(_b):
                                if abs(float(_a) / float(_b) - 1) > 0.05:
                                    _hk_ok = False
                                    log(f"港股 EM 回退口径异常：{_k} 得{_a} 而腾讯{_b}"
                                        f"（偏离 >5%）—— 本轮不采用", "FEED")
                                    break
                    except Exception:
                        pass
                    if _hk_ok:
                        # ⚠⚠ 2026-11-11（审查，P3）：**核对没做成要留痕**。
                        # 上面那段的意图是「用腾讯参考核对 EM 回退口径」，
                        # 但 `_ref` 拿不到时∈循环里 `_b` 是 None → `isnum(None)` 为 False
                        # → 不会 break → `_hk_ok` 仍然 True → **静默放行**。
                        # 也就是「闸门在 ≠ 闸门生效」的老毛病：代码看着核对过了，
                        # 实际一次比较都没做，日志里零痕迹。
                        # 为什么**不改成 fail-closed**：这条回退路径本就是「腾讯失败」
                        # 才走的 —— 参考源不可用是**常态而非罕见**，
                        # 硬要求必须核对等于把整条兜底废掉。
                        # 只补一行求证：把「本轮未核对」写进 FEED 日志，
                        # 排查时能分辨「核对通过」与「根本没核对」。
                        if not _ref:
                            log(f"港股 EM 回退本轮**未核对**（参考源腾讯不可用）"
                                f"，直接采用 {len(o)} 项 —— 偏差风险未验证", "FEED")
                        for x in o.values():
                            x["_src"] = "em"
                        out.update(o)

    if not allow_backup:
        return out

    # 统一键：_fetch 现在按原始 secid 建键，但备用源（feeds）用的是短码，
    # 这里把短码键补一份完整 secid 别名，保证调用方用哪种都能取到
    for i in list(ids):
        if i not in out and i.split(".")[-1] in out:
            out[i] = out[i.split(".")[-1]]

    # 备用源：补齐仍未取到的（港股必走这里，板块备用源无覆盖）
    miss = [i for i in ids if i not in out and i.split(".")[-1] not in out]
    if miss:
        try:
            import feeds
            bk = feeds.backup_quotes([i.split(".")[-1] for i in miss])
            if bk:
                log(f"quotes 降级到备用源，补到 {len(bk)}/{len(miss)} 项", "FEED")
            out.update(bk)
            # 备用源返回短码键，补齐完整 secid 别名供调用方使用
            for i in miss:
                if i not in out and i.split(".")[-1] in bk:
                    out[i] = bk[i.split(".")[-1]]
        except Exception as e:
            log(f"备用源也失败: {e}", "FEED")
    # 新鲜度闸门：备用源的数据带 _ts（行情时间戳），盘中若明显滞后则丢弃。
    # 原先**没有任何时效判断** —— 腾讯/新浪在休市或限流时会返回上一交易日的
    # 收盘价，_valid_quotes 只校验"有限正数"，于是会被当成现价写进推送
    # （"上证 3700.00 +33.00%"这种用昨天价格配今天涨跌幅的消息），
    # 且日志里无任何痕迹。
    # 只在交易时段内检查（收盘后/盘前的旧数据是正常的，不该丢）。
    try:
        # ⚠️ 这里曾写 `_ts_is_stale`（无此函数）—— NameError 被下面的
        # `except Exception: pass` 吞掉，于是**新鲜度闸门整体失效**，
        # 陈旧的上一交易日收盘价照样进推送。真名是 `_is_ts_stale`。
        # 顺手把"逐项判断 + 逐项删除"合并成一次遍历，语义不变。
        _now = datetime.datetime.now()
        # ⚠️ 必须叠加**交易日**判据（2026-11-01 修，P1）：
        # `in_session` 只看时刻不看日历，周末/节假日 09:30–15:05 之间恒为
        # True。而非交易日各源返回的必然是**上一交易日收盘价**（滞后
        # 数小时～数天），20 分钟阈值把它们**全部**判为陈旧丢弃 ——
        # quotes() 返回空。`[实测]` 2026-11-01（周日）11:28：
        # selfcheck 的 [行情取数] 报 `❌ 取到 0 项，源=set()`，
        # 把周末的自检基线打红；而 10-24/10-25（同为周末）自检恰好跑在
        # 10:39/10:41 也不该有实时数据却报 ✅ —— 差异只来自源站当时是否
        # 返回了 em_test 行。周末盘中触发 analyze 时同样会让整轮行情为空。
        # 修法：**两市场当天都不开市**时整段闸门跳过（周末、全休市日）；
        # 任一市场开市时行为与原先完全一致（A 开港休 10-19 那类场景的
        # 既有行为不在本次改动范围）。
        if (in_session(_now)
                and (is_trading_day(_now.date(), warn=False)
                     or is_hk_trading_day(_now.date(), warn=False))):
            # ⚠️ 逐行声明市场（2026-10-28）：港股半日市下午的豁免**只对港股行**
            # 生效。`out` 的键可能是完整 secid（100.HSI）也可能是短码（HSI，
            # 备用源用短码），故两种形态都要认。
            _hk_short = {i.split(".")[-1] for i in hk}
            # ⚠️⚠️ 2026-11-06（P1-1 修）：原判据 `v.get("_ts") and ...` 对
            # **没有 `_ts` 的行整条跳过检查**。而三条产出路径结构上就不带
            # 时间戳：百度行（feeds.py 的 from_baidu）、新浪简版兜底
            # （`len(p)<30`）、腾讯 `p[30]` 为空。于是「上一交易日收盘价」
            # 被原样当现价写进快照与推送 —— 正是本闸门自称要防的事。
            # 现在 feeds 侧给这类行打了 `_ts_unknown`：**明确标记"时效未知"**。
            # 盘中对这类行按 **fail-closed** 处理（丢）：无法验证新鲜度时，
            # 宁可报「数据缺失」，也不能把陈旧价格当实时价推给读者。
            # 非交易时段（收盘/周末/休市）照旧放行 —— 那时"最近一次收盘价"
            # 本来就是正确语义。
            # ⚠️ 2026-11-06（P1-13）：判据改成**逐行**求值，任一行抛异常
            # 只丢那一行，而不是让整段闸门被外层 except 吞掉。
            def _row_stale(_k):
                _v = out.get(_k)
                if not isinstance(_v, dict):
                    return False
                if _v.get("_ts_unknown"):
                    return True
                if not _v.get("_ts"):
                    # ⚠️ 2026-11-07（复核 F5）：无时间戳的行**仍然放行**（方向不变），
                    # 但不再静默 —— 记一行日志。这是一个真实的深度缺口：
                    # `_ts_unknown` 会被丢，而「连标记都没有」反而放行。
                    # 之所以不直接改成丢，见 `_fetch` 里 `_ts_missing` 的注释：
                    # 它是主路径，且 f124 在集合竞价/停牌时本就可能缺失。
                    if _v.get("_ts_missing"):
                        log(f"行情 {_k} 无时间戳（主路径 f124 缺失/非数值）"
                            f"—— 新鲜度闸门无法判定，**放行**", "FEED")
                    return False
                return bool(_is_ts_stale(
                    _v["_ts"], _now,
                    market=("hk" if (_is_hk(_k) or _k in _hk_short) else None),
                    # 东财主路径（`_fetch` 注入的 f124）只做跨日判据
                    cross_day_only=bool(_v.get("_ts_crossday"))))
            _stale = []
            for _k in list(out.keys()):
                try:
                    if _row_stale(_k):
                        _stale.append(_k)
                except Exception as _e1:
                    # 单行判不出来 → 丢这一行（无法证明新鲜 = 不新鲜）
                    log(f"行情 {_k} 新鲜度无法判定（{type(_e1).__name__}: "
                        f"{_e1}）—— 丢弃该行", "FEED")
                    _stale.append(_k)
            # 措辞要准：被丢的行可能来自主路径腾讯（_hk_quotes_via_tencent），
            # 原先写「备用源」会把排查方向带偏（2026-10-08 审查实测）。
            # 必须在 pop **之前**取 _src，否则全是默认值。
            _srcs = sorted({(out.get(_k) or {}).get("_src", "?")
                            for _k in _stale}) if _stale else []
            for _k in _stale:
                out.pop(_k, None)
            if _stale:
                log(f"丢弃 {len(_stale)} 项陈旧行情（时间戳滞后或无法判定，"
                    f"来源 {'/'.join(_srcs)}）", "FEED")
    except Exception as _e:
        # ⚠️⚠️ 2026-11-06（全量复核 P1-13）：原先是 `except Exception: pass`。
        # 这是**唯一的新鲜度防线**，一旦内部抛异常（历史上真的发生过：
        # 曾把 `_is_ts_stale` 写成不存在的 `_ts_is_stale`，NameError 被
        # 这行吞掉，闸门整体失效而日志零痕迹，陈旧收盘价照推）。
        # 静默失效 = 防线消失且无人知道。逐行判据已把异常收敛到单行；
        # 走到这里说明是外层逻辑本身坏了（例如 `_now`/`hk` 未定义）——
        # 那时整段结论不可信，返回空 dict（fail-closed）并**大声记日志**。
        log(f"新鲜度闸门整体异常（{type(_e).__name__}: {_e}）—— "
            f"本轮行情整体作废（fail-closed），不交出未验证的数据", "FEED")
        return {}
    return out

# 午休区间（分钟数）：A股 11:30~13:00 与港股 12:00~13:00。
# 取并集 [690, 780) = 11:30~13:00 —— 两个市场在午休期间都没有新成交，
# 行情时间戳必然停在午休前那一刻，用"距 now 多少分钟"判陈旧会把
# **正常数据误判为陈旧**。实测：12:30 时 A股时间戳 11:29，滞后 60 分钟
# -> 被判 stale；13:30 时滞后 120 分钟 -> 同样被判 stale，而 13:30
# 恰好是一个推送槽位（HOURLY 含 13:30），那一轮的行情会被整体丢弃。
_LUNCH = (11 * 60 + 30, 13 * 60)

def _ts_parse(ts):
    """解析备用源的行情时间戳。腾讯两种格式都返回，必须都认：

      A股 sh000001 -> "20260924161401"      （YYYYMMDDHHMMSS）
      港股 hkHSI   -> "2026/09/25 09:27:02" （带斜杠与空格）

    ⚠️ 原实现只按 "%Y%m%d%H%M%S" 解析。港股时间戳带斜杠，strptime 直接
    抛异常 -> except 返回 False（"不陈旧"）-> **港股的新鲜度闸门完全失效**：
    实测把时间戳换成 90 分钟前，_is_ts_stale 仍返回 False，那条陈旧价格
    会被当成现价写进推送 —— 而这正是该闸门存在的唯一理由。
    feeds.MAP 里 5 个港股键（HSI/HSTECH/00700/00981/09988）全部受影响。
    """
    if not ts: return None
    t = str(ts).strip()
    # ⚠⚠ 2026-11-11（Codex 审查，P2）：补两种常见变体。
    # `[实测]` 原只认三种格式，带 `T` 分隔符或带时区的 ISO-8601
    # （`2026-11-10T15:00:00+08:00`）**一律返回 None**，而 `_is_ts_stale` 对 None
    # 的处理是「宁可放行」→ **陈旧行被当新鲜使用**。
    # 当前三个内部源都自建无时区字符串，所以现网不可达；
    # 但 `_ts` 是**供给外部行的接口字段**，降级链任一环换成其他实现
    # （如一个直接转发接口 JSON 时间戳的代理）就会碰上。加两个格式成本为零。
    # 时区说明：仅**静默忽略**尾部时区后按本机墙钟计算 —— 本机即
    # Asia/Shanghai，与数据源同域；不做真正的时区换算（那需要额外依赖，且当前
    # 没有跨时区数据源。
    for fmt in ("%Y%m%d%H%M%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.datetime.strptime(t[:19], fmt)
        except Exception:
            continue
    return None

def _is_ts_stale(ts, now, max_min=20, market=None, cross_day_only=False):
    """行情时间戳是否比 now 滞后超过 max_min 分钟。

    解析失败返回 False（宁可放行，不误杀）—— 但**必须记日志**：
    原先静默返回 False，格式不匹配时闸门整体失效却无任何痕迹。

    `market="hk"` 时额外套用港股日历（半日市下午不判陈旧，见下方注释）。
    ⚠️ 默认 `None` = 不套用任何市场日历 —— 调用方必须**显式**声明是港股，
    否则半日市豁免会连带放过 A 股的陈旧行。

    `cross_day_only=True`（2026-10-28 加）：只判「**跨日**」——时间戳的
    日期与 now 不同才算陈旧，不看 20 分钟阈值。给东财主路径（`_fetch`
    注入的 f124）专用：那条链路的数据本身是连续的，但**集合竞价期间**
    （09:15–09:25）f124 可能仍是**昨日收盘时刻**（撮合未出开盘价），
    用 20 分钟阈值会把盘前那一轮的 A 股行情整批误杀。
    它要防的真实故障是「EM 限流期回**上一交易日**点位」—— 跨日判据正好
    覆盖，且对盘中任何正常抖动零误伤。
    """
    t = _ts_parse(ts)
    if t is None:
        if ts:
            log(f"行情时间戳无法解析，新鲜度闸门跳过该项: {ts!r}", "FEED")
        return False
    if cross_day_only:
        # ⚠️ 开盘头 10 分钟不判（2026-10-28）：09:25 集合竞价刚撮合，
        # 东财的 f124 在**第一笔连续竞价成交前**可能仍指向昨日收盘 ——
        # 那正是 09:25 盘前简报那一轮，误杀会让整条简报的 A 股数据消失。
        # 用 09:35 作缓冲（连续竞价已跑 5 分钟，f124 必然已是今日）。
        # 代价：09:25–09:35 之间"回上一交易日点位"的故障抓不到 ——
        # 但那个窗口本来就在集合竞价刚结束、数据源刷新中，误判风险更高。
        if now.hour * 60 + now.minute < 9 * 60 + 35:
            return False
        return t.date() != now.date()
    # 午休期间时间戳停在午休前是正常的，不判陈旧。
    # ⚠️ 窗口按**市场**分开（2026-10-28）：原先一律用并集 [11:30,13:00)，
    # 而港股 11:30–12:00 **仍在交易** —— 那半小时里港股若返回冻结时间戳，
    # 会被当现价放行（方向与注释想防的相反）。A 股午休确实是 11:30–13:00。
    # ⚠⚠ 2026-11-11（审查）：**必须限定「时间戳就是当天」**。
    # 原写法只看「现在是不是午休」，不看 `t` 的日期 ——
    # 于是午休时段新鲜度闸门**整体失效**，包括它存在的唯一理由：
    # 源回退到**上一交易日收盘价**。
    # `[实测 2026-11-11]` 午休时 `_is_ts_stale("20261110150000")`（昨日收盘）返回 **False**：
    #   A股 11:20 -> True | 11:45 -> **False** | 12:30 -> **False** | 13:05 -> True
    #   港股 11:45 -> True | 12:30 -> **False**（昨日 16:00 收盘）| 13:05 -> True
    # 而 11:30 槽位的窗口是 11:30~11:50（TTL 20 分钟）—— **正落在盲区里**：
    # 那一带数据源回退到昨日收盘价，会被当成现价写进「午间小结」。
    # 与下方半日市分支同型：那里 2026-10-28 已因**完全相同的理由**补上了
    # `t.date() == now.date()`，午休这一支当时漏了。两处必须同口径。
    _lunch = ((12 * 60, 13 * 60) if market == "hk" else _LUNCH)
    if (_lunch[0] <= now.hour * 60 + now.minute < _lunch[1]
            and t.date() == now.date()):
        return False
    # ⚠️ 港股半日市（2026-10-28 加，P1）：12:00 收市，13:00 之后时间戳
    # 必然冻结 60~125 分钟 > 20 分钟阈值，闸门会把**正常**的当日数据
    # 整批丢弃（读者看到"数据缺失"而非"港股下午不开市"）。
    # `[实测]` 2026-12-24 13:30 对 12:00 的时间戳判 stale=True 且
    # in_session/is_hk_trading_day 全为 True —— 三个判据全成立，必误杀。
    # 豁免**只对港股行**生效（`market="hk"`）：A 股当天照常交易到 15:00。
    # ⚠️⚠️ 还必须限定「时间戳就是**当天**」（2026-10-28 补）：原判据只看
    # 「今天是半日市 + 现在 ≥12:00」，完全不看 `t` 的日期 —— 于是半日市
    # 整个下午（12:00–15:05）的港股新鲜度闸门**整体失效**，包括它存在的
    # 唯一理由：源回退到**上一交易日收盘价**。`[实测]` 沙箱：
    #   ts=2026/12/18 16:00（上一交易日收盘）
    #     · 普通周一 12-21 13:30 → stale=True（正确丢弃）
    #     · 半日市   12-24 13:30 → stale=False（❌ 昨日收盘当现价放行）
    # 半日市下午「12:00 收的价」确实该一直有效到收盘，所以判据是
    # 「当天 + 已过 12:00」，而不是「今天是不是半日市」。
    if (market == "hk" and now.date().isoformat() in HK_HALF_DAYS
            and now.hour * 60 + now.minute >= 12 * 60
            and t.date() == now.date()):
        return False
    return (now - t).total_seconds() > max_min * 60

def trend(sid):
    """当日分时。收盘后或所有行情主机都不可用时返回 None（备用源无分时数据）。

    ⚠️ 候选是 `_HTTPS_HOSTS` 而不是 `EM_HOSTS`（2026-10-22）：trends2 的
    返回里 `high/low/open/last` 是**绝对值**，调用方（analyze.detect 与
    collect）拿它们算 `(last/high-1)*100` 之类的**跨源**比率 ——
    板块在 HTTP 主机上是另一个指数基点（实测 90.BK1030 差 1.36%），
    与 quotes() 里 push2test 的 f2 混算会得出错误百分比。
    个股/指数三源逐位一致，但 trend() 不区分标的类型，只能整条走 HTTPS。
    """
    d = None
    for host in _https_hosts():
        d = cj(f"{host}/stock/trends2/get?secid={sid}"
               f"&fields1=f1,f2,f3,f7&fields2=f51,f53,f56,f58&iscr=0&ndays=1&ut={UT}")
        if _has_data(d, "trends"):
            break
    _inner = _has_data(d, "trends")
    if not _inner:
        return None
    try:
        # 用 _inner 而不是再写 d["data"]：后者在类型检查器眼里仍是
        # "可能为 None 的下标"（reportOptionalSubscript），而 _has_data
        # 已经证明过它是 dict 了 —— 重取一次等于把刚建立的保证丢掉。
        # ⚠️ 元素**必须先判字符串**（2026-10-11 审查）：`x.split(",")` 对
        # 非字符串元素抛 **AttributeError**，而下面只捕 (ValueError,
        # IndexError) —— 异常直接冒到调用方。可达性：trends2 的 `trends`
        # 是 list，但**元素类型不受 _has_data 约束**（它只证明 data 是 dict、
        # key 存在）。实测 `{"data":{"trends":[123,456]}}` -> AttributeError
        # 穿透；而 trend() 的两个调用点（collect._main:462、analyze.run:2735）
        # **都不在 try 内**（AST 核实），异常一路冒到 main() 的兜底 except ——
        # 整轮快照/简报全丢，日志里只有一行 traceback。
        # 这与本函数注释里"不守卫会一路抛到调用方顶层"是同一个失败模式，
        # 只是漏了 AttributeError 这一类。
        t = [x.split(",") for x in _inner["trends"] if isinstance(x, str)]
        px = [float(x[1]) for x in t]
        vol = [int(x[2]) for x in t]
    except (ValueError, IndexError):
        # trends2 偶发返回列数不足/非数值的行；不守卫会一路抛到调用方顶层，
        # 让整轮报告或整轮快照静默丢失
        return None
    hi = max(px) if px else 0
    if not px or hi <= 0:
        # 停牌/集合竞价前，trends2 会返回全 0 的价格序列。
        # 此时 high==0，而调用方**三处**都在算 (last/high-1)*100：
        #   collect.py:58、analyze.py:230、analyze.py:343 —— 全部 ZeroDivisionError。
        # 与其在三处各加守卫，不如在这里判定"这份分时不可用"直接返回 None
        # （调用方已有 if sg: 判空），语义也更准确：价格为 0 的分时没有分析价值。
        return None
    am = sum(v for v, x in zip(vol, t) if x[0][-5:] <= "11:30")
    return {"last": px[-1], "high": hi, "low": min(px), "open": px[0],
            "vol": sum(vol), "am": am, "pm": sum(vol) - am}

# ==================== 取数阶段的**全局**时间上限（2026-11-20，P1）==========
# 为什么需要：取数阶段原先只有**各数据源自己的**预算，没有总数。
# `[实测]` 三个子预算可以叠加到 180s：
#     hot.fetch_sectors 的 clist  60s
#     hot.MEMBERS_BUDGET          40s
#     hot.VR_BUDGET                80s
# 而 `AI_ROUND_BUDGET`(240s) 是**减去取数耗时**再给 AI 的（见
# analyze.ai_analyze 的 `_used` 计算）—— 取数吃到 172s 时 AI 只剩 68s，
# 而成功轮 AI 耗时的 P90 是 140s、最长 210s ⇒ 结构性必死。
# `[实测]` 2026-11-18 14:54：取数 172s → AI 上限 68s → 退模板，
# 日志里「量比预算 80s 用尽」与「members 预算用尽」同时出现。
#
# 实测分布（近 20 天 986 轮 `data_ms`）：P50=0.1s P90=13.6s P99=75s
# 最大 171.8s；>60s 只占 1.6%，>90s 占 0.6%。故取 120s 作硬上界 ——
# 截掉长尾，正常轮次（P99=75s）完全不受影响。
#
# ⚠️ 语义是**软上限**：到期不是杀进程，而是"各数据源不再开新活"，
# 已拿到的部分照常使用（与 clist 的 budget_s 同款取舍 —— 半份数据比
# 没有更糟，但"再花 80s 去补最后 5% 板块"更糟）。
# ⚠️ 只约束 `clist()` 与 hot 的三个循环，**不约束 `cj()` 单次超时**：
# 把 20s 的 timeout 压到 3s 会让本来能成功的请求失败，是另一类错误。
DATA_BUDGET = 120

# 单元素可变容器（同 `_lock_fd` 的写法）：模块级 None 无法被别的模块回写。
_DATA_DEADLINE = [None]


# 本轮哪些数据源被全局上限**真的**钳过（用于每轮只记一次日志）。
_DATA_CAPPED: set = set()


def set_data_deadline(ts=None):
    """设置本轮取数的硬截止时刻（墙钟秒）。`None` = 不限制（默认）。"""
    _DATA_DEADLINE[0] = ts
    _DATA_CAPPED.clear()


def data_left():
    """距取数截止还剩多少秒；未设截止返回 None（调用方据此跳过钳制）。

    ⚠️ 返回值**可能是负数**（已超时）—— 刻意不钳到 0：调用方用
    `max(0.5, ...)` 之类的下限保证单次请求仍有最小预算，钳成 0 会让
    `budget_s=0` 变成"一页都不翻"，反而拿不到已到手的第一页。
    """
    _d = _DATA_DEADLINE[0]
    if not isinstance(_d, (int, float)):
        return None
    return _d - time.time()


def data_cap(own_s, what=""):
    """把某个数据源自己的预算 `own_s` 钳到"全局剩余"以内（取较小者）。

    任一为 None 时取另一个；都为 None 返回 None（= 不限制）。

    ⚠️ 钳到了就**记一条日志**（每个 `what` 每轮只记一次）。
    理由是本仓库反复的那类缺陷：**降级必须留痕**。全局上限一旦生效，
    该数据源本轮就拿不全（量比全按 1.0 → 排序失真；成分股缺失 →
    热点榜变子集），而这在推送里看不出来。不记日志的话，事后只能靠
    "这一轮热点榜怪怪的"去猜。`what` 为空时按"未命名"去重。
    """
    _left = data_left()
    if _left is None:
        return own_s
    if own_s is None:
        _cap = _left
    else:
        _cap = min(own_s, _left)
    if own_s is not None and _cap < own_s:
        _k = what or "未命名"
        if _k not in _DATA_CAPPED:
            _DATA_CAPPED.add(_k)
            log(f"取数全局上限生效：{_k} 自身预算 {own_s:.0f}s 被压到 "
                f"{_cap:.0f}s（全局 {DATA_BUDGET}s，本轮已用 "
                f"{DATA_BUDGET - _left:.0f}s）—— 该源本轮取不全，"
                f"结果按已有部分使用", "FEED")
    return _cap


def clist(fs, fields, pz=100, max_pn=12, fid="f3", po=1, budget_s=None,
          with_total=False):
    """板块列表（clist）。该接口收盘后常被限流，失败返回空列表。
    调用方需自行处理空结果。
    注：备用源（腾讯/新浪）无板块列表接口，故此处无降级。

    ⚠️ `budget_s`（2026-10-21）：本函数的**总耗时上限**，超了就不再翻下一页。

    为什么必须有：单次 `cj()` 的最坏耗时是 **175s**（直连 3 次重试 60s +
    代理 25s + firecrawl 90s），而本函数每页要对 2 台主机各调一次、最多
    翻 `max_pn` 页 —— 理论上 `12 × 2 × 175 = 70 分钟`。实际有缓冲垫
    （`hot.py` 只用 max_pn=3；`_mark_dead` 会让后续主机被跳过），但**没有
    任何硬上界**：一旦两台 EM 主机同时出现"能连但慢"的黑洞型故障，
    一个函数就能吃掉整轮，而 launchd 不并发同一 job —— 后续槽位全被吞。
    `[实测]` 2026-10-21 14:18–14:21：curl 超时 17s → 代理 TimeoutExpired
    → firecrawl TimeoutExpired ×2，一条链就烧掉 3 分钟。

    判据取**已用时间**而不是"还剩几页"：慢的是每一次 cj，与页数无关。
    到期即**返回已经拿到的部分**（不是空列表）—— 前几页的数据是好的，
    丢掉它们等于让一次慢故障连带毁掉已取到的数据。
    """
    out = []
    _t0 = time.monotonic()
    # 2026-11-20（P1）：先按**全局**取数上限钳一次自己的预算。
    # 各数据源自己的 budget_s 是"我是这段活的上限"，而全局 deadline 是
    # "整轮取数还剩多少" —— 后者更紧时必须听后者，否则三个子预算又能
    # 叠加到 180s（见 DATA_BUDGET 的注释）。
    budget_s = data_cap(budget_s, what=f"clist({fs})")
    _total = None
    _seen = set()          # 已取到的 f12，用于跨页去重与防死循环
    for pn in range(1, max_pn+1):
        # 每页开工前判一次（而不是每次 cj 前）：一页最多 2 次 cj，
        # 判在页边界已足够阻止 70 分钟的累积，且不改变单页的完整语义
        # （半页数据比没有更糟 —— 下游会把它当成"这页就这么多"）。
        if budget_s is not None and time.monotonic() - _t0 > budget_s:
            log(f"clist 总耗时超预算 {budget_s}s（已取 {len(out)} 条，"
                f"{pn-1} 页）—— 停止翻页，用已拿到的部分", "FEED")
            break
        d = None
        for host in _https_hosts():
            d = cj(f"{host}/clist/get?pn={pn}&pz={pz}&po={po}&np=1&fltt=2&invt=2"
                   f"&fid={fid}&fs={fs}&fields={fields}&ut={UT}")
            if _has_data(d, "diff"):
                break
        _inner = _has_data(d, "diff")
        if not _inner:
            break
        # ⚠️ diff 有**两种形状**：list，或 dict（键为序号字符串）。
        # 同一份代码库里 alert.py:235 与 marketlib.py:982 都为此写了
        # `isinstance(diff, list) else list(diff.values())` 的分支，
        # 说明这是已知的真实形状；而这里直接 `out += got`，
        # dict 时 `list += dict` 会**把键字符串逐个加进去** ——
        # 实测返回 ['0', '1']，下游 x.get("f14") 立刻 AttributeError，
        # 整个热点榜 / 板块列表静默变空。
        got = _inner["diff"]
        if not isinstance(got, list):
            got = list(got.values())
        # ⚠️ **终止判据必须用服务端的 total，不能用 `len(got) < pz`**
        # （2026-10-22 实测）。原写法有一个静默截断的致命形态：
        # 服务端可能把 pz **截到自己的上限**再返回 —— 实测明文 HTTP 主机
        # 对 pz=200/500 一律只返 100 行，且翻页偏移也按 100 算
        # （`pn=2&pz=500` 与 `pn=2&pz=100` 返回同一批）。
        # 于是一次 `clist(pz=500)` 拿到 100 行 → `100 < 500` 成立 →
        # **立刻 break** → 496 个行业板块只返回 100 个，而且
        # 返回的是"看起来正常的一页"，调用方无从察觉少了 80%。
        # `hot.fetch_sectors` / `alert._sector_rows` 都传 pz=500，
        # 一旦哪天候选池里出现会截断的主机，热点榜与板块资金扫描
        # 会**同时**静默丢掉 4/5 的板块 —— 且日志里一个字都没有。
        #
        # 现在：total 已知就用 total 判；未知则退化为"本页为空才停"，
        # 并靠 _seen 去重 + max_pn 兜底，绝不因为"比 pz 少"而提前停。
        if _total is None:
            _t = _inner.get("total")
            _total = _t if isinstance(_t, int) and _t > 0 else None
        _new = 0
        for x in got:
            k = x.get("f12") if isinstance(x, dict) else None
            if isinstance(k, str) and k:
                if k in _seen:
                    continue
                _seen.add(k)
            out.append(x)
            _new += 1
        if not got:
            break
        if _total is not None and len(out) >= _total:
            break
        # 服务端把 pz 截断了：本页拿到的是"它允许的最大页大小"，
        # 说明还有后续页 —— **继续翻**，不要按 `len(got) < pz` 停。
        # 只有"本页一条新增都没有"才停（防止服务端忽略 pn、永远返第一页
        # 时打满 max_pn 次无用请求）。
        if _new == 0:
            break
    # ⚠ 2026-11-10（Codex P2）：可选回传服务端声明的 `total`。
    # 原来 `_total` 只在函数内部用于判终止，调用方**无法知道
    # 自己拿到的是完整名单还是截断的部分** ——
    # `hot.members()` 于是把残缺名单当完整缓存 7 天。
    return (out, _total) if with_total else out

def clist_available():
    """探测 clist 是否可用。与 clist() 一致，含主机故障转移。"""
    for host in _https_hosts():
        d = cj(f"{host}/clist/get?pn=1&pz=1&po=1&np=1&fltt=2&invt=2"
               f"&fid=f3&fs=m:90+t:2&fields=f12&ut={UT}")
        if _has_data(d, "diff"):
            return True
    return False

def news_sina(limit=40, within_min=180):
    """新浪财经直播快讯（备用源）"""
    d = cj("https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=40"
           "&zhibo_id=152&tag_id=0&dire=f&dpc=1", REF_NEWS)
    out = []
    # 形状校验同 news()：result/data/feed/list 任一层不是预期的 dict/list，
    # 或 list 里混进非 dict 条目，都会在 x.get() 处 AttributeError
    # （实测 2026-09-26）。这是 news() 的**降级源** —— 它炸掉意味着
    # "东财不可用 + 新浪也炸"，快讯整段消失且没有第三层可退。
    _lst = None
    if isinstance(d, dict):
        _r = d.get("result")
        if isinstance(_r, dict):
            _da = _r.get("data")
            if isinstance(_da, dict):
                _fd = _da.get("feed")
                if isinstance(_fd, dict):
                    _lst = _fd.get("list")
    if isinstance(_lst, list):
        now = datetime.datetime.now()
        _skip = 0
        for x in _lst:
            # ⚠️ 2026-10-08 数据契约审查：降级源必须与主路径（news.py）同标准 ——
            # **逐条隔离** + 时间格式容错。原实现任一条目出问题（rich_text 是
            # 容器、create_time 是 int、上游改 ISO 格式）要么抛异常被外层
            # except 吞掉、要么整条 continue 静默丢，最坏 **fetch_news 返回 []**
            # → alert.should_alert 的 `elif not ns:` fail-open ——
            # **异动不经新闻佐证直接推**。
            try:
                if not isinstance(x, dict):
                    _skip += 1
                    continue
                t = x.get("create_time")
                if not isinstance(t, str):
                    _skip += 1
                    continue
                dt = None
                for _fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S",
                             "%Y-%m-%dT%H:%M:%S"):
                    try:
                        dt = datetime.datetime.strptime(t[:19], _fmt)
                        break
                    except Exception:
                        continue
                if dt is None:
                    _skip += 1
                    continue
                if (now - dt).total_seconds() <= within_min * 60:
                    # 容器/非字符串的正文一律转成字符串，不整条丢
                    _rich = x.get("rich_text")
                    txt = (_rich if isinstance(_rich, str)
                           else json.dumps(_rich, ensure_ascii=False)
                           if _rich is not None else "").strip()
                    out.append({"t": t[11:16] if len(t) >= 16 else "",
                                "time": dt, "title": txt[:70]})
            except Exception:
                _skip += 1
                continue
        if _skip:
            _daily_log_once("news", "sina-skip",
                            f"新浪快讯跳过 {_skip} 条"
                            f"（字段形状/时间格式不可用；本日首次，同因后续不再记）")
    return out[:limit]

def news(limit=60, within_min=60, allow_backup=True):
    """东财 7x24 快讯，只返回 within_min 分钟内的。失败时降级到新浪。"""
    d = cj("https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
           f"?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize={limit}"
           "&req_trace=1&fields=code,showTime,title,summary,url&callback=", REF_NEWS)
    out = []
    # ⚠️ data / fastNewsList 的形状必须校验，不能只判"真值"。
    # 实测三种形状都会抛 AttributeError（2026-09-26）：
    #   data 是 list          -> d["data"].get(...) 炸
    #   fastNewsList 是 dict  -> 迭代出键字符串，x.get(...) 炸
    #   条目不是 dict         -> x.get(...) 炸
    # 同一类形状校验 news.py:fetch_news 早已加上（那里还留了注释），
    # 但**这个函数漏了** —— 而 selfcheck 的 [快讯] 检查直接调它，
    # 形状异常时那项会显示 ❌ 却看不出是"源返回了怪形状"还是"网络不通"。
    # 这里统一收敛成"无快讯"，让下面的降级分支接手（与 news.py 同策略）。
    if isinstance(d, dict) and isinstance(d.get("data"), dict):
        _lst = d["data"].get("fastNewsList")
        if isinstance(_lst, dict):
            _lst = list(_lst.values())          # 键为序号的 dict 形态
        if not isinstance(_lst, list):
            _lst = []
        now = datetime.datetime.now()
        for x in _lst:
            if not isinstance(x, dict):
                continue
            # ⚠️ 值也必须判类型（2026-10-11 审查）：`(v or "")[:19]` 对
            # **非字符串**值抛 TypeError（int/float/list 都不可下标）。
            # 上面那三层 isinstance 只保证了**容器**形状（data 是 dict、
            # fastNewsList 是 list、条目是 dict），**没有**保证字段值的类型
            # —— 而 showTime 在某些东财变体里是 unix 秒（int），title 偶尔
            # 是数字。实测：`{"showTime": 1730000000}` -> TypeError 穿透。
            # 本函数的注释里写的正是"形状异常时让降级分支接手"，但值级
            # 异常会直接冒到调用方（collect 的 _news() 调用点、selfcheck 的
            # [快讯] 段），整轮快照与事件表一起丢。
            # 非字符串一律当"取不到"，continue 跳过这一条 —— 与上面
            # "条目不是 dict"同策略，不改变任何正常路径的行为。
            _raw_t = x.get("showTime")
            if not isinstance(_raw_t, str):
                continue
            t = _raw_t[:19]
            try:
                dt = datetime.datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if (now - dt).total_seconds() <= within_min * 60:
                _raw_ti = x.get("title")
                out.append({"t": t[11:16], "time": dt,
                            "title": (_raw_ti if isinstance(_raw_ti, str)
                                      else "")[:70]})
    if not out and allow_backup:
        try:
            b = news_sina(limit, max(within_min, 180))
            if b:
                log(f"news 降级到新浪，{len(b)} 条", "FEED")
                return b
        except Exception as e:
            log(f"news 备用源失败: {e}", "FEED")
    return out

# ---------- 主链路停摆告警（2026-11-10）----------
# ⚠⚠ 存在理由：analyze 停摆时**它自己告警不了**，而自检一天只跑一次。
# `[事故]` 2026-11-08 20:23~21:08：import 写坏 analyze，45 分钟零推送零通知。
# 观察者必须是**另一个活着的进程** —— collect 每 5 分钟一轮、
# 与 analyze 完全独立（今日实测跑了 183 次）。
STALL_ALERT_MIN_AGE = 900          # 心跳超过 15 分钟即判停摆

def analyze_lock_held():
    """analyze 是否**正在运行**（持有 flock 排他锁）。

    ⚠⚠ 2026-11-11（审查，P2）：停摆告警需要这个判据。
    原判据只看心跳年龄（`STALL_ALERT_MIN_AGE=900`），而
    `runlog.beat()` 只在 `run()` 的首尾各写一次——
    **一轮跑得久，中间就没心跳**。
    `[实测真实运行记录 state/history/runs.jsonl]` analyze 轮次时长：
        24.3 分钟（2026-10-09 10:01）、21.9 分钟（10-26）、20.0 / 18.8 分钟（10-19）、17.6 分钟（10-16）
    —— 这些轮次跑到第 16 分钟时，collect（每 300s）看到心跳超 900s，
    会往企业微信发「主链路疑似停摆」—— **而 analyze 正在勤奉地跑**。
    更坏的是 `_maybe_stall_alert` 有按日去重：这条假告警发出后，
    **当天真出现停摆就再也告不了了**。

    判据选择：flock 由内核维护，进程活着=持有、进程死了=立即释放（见
    `analyze.acquire_lock` 的长注释）。所以“锁被持有”比“心跳新”更接近
    “它还活着”这个本意。探测用 LOCK_NB，**不会阻塞、不会抢锁**。
    任何异常都返回 False（宁可多告一次，不能因为探测失败而永不告警）。
    """
    try:
        import fcntl
        _lk = p_log("watch.lock")
        _fd = os.open(_lk, os.O_RDWR)
    except Exception:
        return False
    try:
        try:
            fcntl.flock(_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True          # 锁被占：正在跑
        else:
            fcntl.flock(_fd, fcntl.LOCK_UN)
            return False
    finally:
        try:
            os.close(_fd)
        except Exception:
            pass


def stall_alert_should_fire(now, age_s, trading, running=None):
    """是否应发停摆告警。**纯函数**，方便测。

    ⚠ 盘外不告警：analyze 在盘外本来就不该频繁心跳，
    在那些时段判“停摆”只会制造假警告（与今早 09:20
    自检误报同因）。

    `running`: analyze 是否正在运行（默认自行探锁）。
    ⚠⚠ 2026-11-11：**正在跑就不叫停摆**。心跳旧只能证明“没开新轮”，
    不能证明“没在跑”—— 长轮次中间心跳必然是旧的。
    """
    if not trading:
        return False
    if running is None:
        running = analyze_lock_held()
    if running:
        return False
    try:
        return float(age_s) > STALL_ALERT_MIN_AGE
    except (TypeError, ValueError):
        return False


def is_trading_day(d=None, warn=True):
    d = d or datetime.date.today()
    # 表过期告警：**按日**去重（落盘），跨进程有效。
    # 原先用进程内标志，而每轮都是新进程 -> 每天约 430 条重复告警刷掉日志。
    # ⚠️ `warn=False` 供**只读调用方**（webui 面板）使用：否则 2027-12-31 之后
    # 打开一次面板就会写 cache/holiday_warn.json + 往 watch.log 插一行，
    # 破坏"面板只读"契约（2026-10-08 审查）。
    if warn and d.isoformat() > HOLIDAYS_COVERED_THROUGH:
        try:
            _today = datetime.date.today().isoformat()
            _w = _load_json(_HOLIDAY_WARN_FILE, {}) or {}
            if _w.get("last") != _today:
                _w["last"] = _today
                _atomic_write_json(_HOLIDAY_WARN_FILE, _w)
                log(f"节假日表已过期（仅覆盖至 {HOLIDAYS_COVERED_THROUGH}），"
                    f"请更新 marketlib.HOLIDAYS —— 否则休市日会被当作交易日", "CFG")
        except Exception:
            pass
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS

def is_hk_trading_day(d=None, warn=True):
    """港股今天是否开市。周末或香港公众假期返回 False。

    与 `is_trading_day`（A 股口径）**并列而非复用**：两张假期表只有
    国庆/中秋/春节重叠，其余完全不同（见 HK_HOLIDAYS 的注释）。
    本轮事故（2026-10-19 重阳节翌日）的直接教训就是"拿 A 股日历当唯一
    真值"——那天 `is_trading_day` 返回 True，系统于是把港股"整日无成交"
    当成了"取数失败"。

    ⚠️ 覆盖期告警（2026-10-29 加，P2）：原先这里**刻意不落盘告警**，
    理由是"本函数被 build_brief / 兜底模板逐项循环调用（每轮 10+ 次）"。
    那个理由只对**无去重**的裸 log 成立 —— 与 `is_trading_day` 同款的
    **按日落盘去重**（每天最多写一次 `cache/holiday_warn.json` + 一行
    日志）代价是常数级，而漏掉它的代价是：**2028 起圣诞被当交易日**，
    系统照常采集「港股无成交」并写「数据缺失」，把人引向查系统 ——
    正是 2026-10-19 那次事故的同型复发，且**没有任何信号**。
    `warn=False` 供只读调用方（面板/探针）使用，保持"只读不落盘"契约。
    """
    d = d or datetime.date.today()
    if warn:
        _ds = d.isoformat()
        # 两张表各自判过期（来源不同、可能不同年补齐，见
        # HK_HALF_DAYS_COVERED_THROUGH 的注释）。同一个落盘文件、不同键，
        # 避免为半日市再开一个 cache 文件。
        _why = []
        if _ds > HK_HOLIDAYS_COVERED_THROUGH:
            _why.append(f"HK_HOLIDAYS 仅覆盖至 {HK_HOLIDAYS_COVERED_THROUGH}")
        if _ds > HK_HALF_DAYS_COVERED_THROUGH:
            _why.append(f"HK_HALF_DAYS 仅覆盖至 {HK_HALF_DAYS_COVERED_THROUGH}")
        if _why:
            # ⚠️ 进程内也短路一次：本函数每轮被 build_brief / 兜底模板
            # 逐项调用 10+ 次，每次都读一遍 JSON 是纯浪费（跨进程的
            # 按日去重仍由下面那个落盘判断保证）。
            if not _HK_WARN_DONE[0]:
                _HK_WARN_DONE[0] = True
                try:
                    _today = datetime.date.today().isoformat()
                    _w = _load_json(_HOLIDAY_WARN_FILE, {}) or {}
                    # 键用 `hk`：A 股那条用 `last`，互不覆盖。
                    if _w.get("hk") != _today:
                        _w["hk"] = _today
                        _atomic_write_json(_HOLIDAY_WARN_FILE, _w)
                        log("港股节假日表已过期（" + "；".join(_why) + "）"
                            "—— 休市日会被当作交易日、半日市下午会重新误报"
                            "「数据缺失」，请更新 marketlib.HK_HOLIDAYS / "
                            "HK_HALF_DAYS", "CFG")
                except Exception:
                    pass
    return d.weekday() < 5 and d.isoformat() not in HK_HOLIDAYS


# A 股**连续竞价**起点（09:30）。09:15–09:25 是开盘集合竞价、09:25 撮合出
# 开盘价，而 collect 的采集窗口从 09:25 就开始 —— 两者之间有一个 **5 分钟**
# 的窗口，此时 f2 是刚撮合出的开盘价、f3 却可能还是**上一交易日**的涨跌幅。
# `[实测]` 2026-11-06（全量复核 P1-2）：`collect.evaluate(q, [], 09:26)` 用
# 昨天冻结的 f3 产出 `idx_weak 上证指数 -3.20%`，写进 pending.json 并**占住
# idx_weak 这个去重键** —— 开盘后真的跌破 1% 也不会再记一条，面板上永远是
# 昨天那个数。与 2026-09-30 港股 09:26 那次（见 collect.evaluate 长注释）
# 是**同一个病**的 A 股分支，当时只补了港股。
_ASHARE_OPEN_MIN = 9 * 60 + 30


def a_session_open(now=None):
    """A 股**此刻**的行情可否当作当日盘中数据（09:30 且当天开市）。

    ⚠️ 函数名里的 "session" 指**行情可用窗**，不是字面的"连续竞价"：
    上界用的是 `TH["session"][1]`（15:05，与 collect 的采集窗口同源），
    比 A 股真实收盘 15:00 晚 5 分钟。这是**刻意**的 —— 15:00~15:05
    那一轮 f3 已经是**今日收盘涨跌幅**，拿它判 `idx_weak` 是事实正确的
    （"今天确实跌了 3%"）；如果把上界收到 15:00，那 5 分钟拿不到数据，
    15:00 槽位（收盘汇总）会空手而归，反而更糟。

    与 `in_session()` 的区别：`in_session` 的区间是 09:25–15:05，含开盘集合
    竞价那 5 分钟。判「这个价格能不能当作今日行情」必须用 09:30 起算，
    否则会把集合竞价刚撮出来的开盘价配上昨天的涨跌幅当成异动。

    下午收盘后（>15:05）返回 False —— 收盘价定格，同样不能当「盘中异动」。
    """
    now = now or datetime.datetime.now()
    hm = now.hour * 60 + now.minute
    if not (_ASHARE_OPEN_MIN <= hm <= TH["session"][1]):
        return False
    return is_trading_day(now.date(), warn=False)


def hk_session_open(now=None):
    """港股**此刻**是否处于交易时段（09:30 开盘 ~ 当日收盘之前）。

    这是"能否拿当前行情判**盘中**异动"的唯一判据，与"今天开不开市"
    （`is_hk_trading_day`）是两件事：

    · 半日市（平安夜/除夕，`HK_HALF_DAYS`）**12:00 就收盘** ——
      `is_hk_trading_day` 仍返回 True，但 12:00 之后行情定格在收盘价。
      拿它判 hstech 三态 / 指数 3σ，就是把 12:00 的事实盖上下午的
      触发时刻推给读者（与盘前闸门 [[market-watch-hk-preopen-gate]]
      同型，只是另一端）。`[读码]` 2026-11-05：detect / collect.evaluate
      两处 / alert.check_index 原先只判"今天开不开市"，半日市下午
      三个闸门全开。
    · 正常交易日 16:00 收盘。analyze/collect 的窗口 15:05 前就停了，
      这一支只是把语义写全 —— 正常日行为零变化。
    · 午休（正常日 12:00~13:00）**仍算开启**：价格只是暂停更新，
      13:00 恢复交易，事件判定不打断；与既有行为一致。

    ⚠️ 展示侧**不受**本判据影响：行情照常保留（`_is_ts_stale` 对半日市
    下午有时间戳豁免），只由 `hk_half_day_closed()` 换口径标注。
    """
    now = now or datetime.datetime.now()
    d = now.date()
    if not is_hk_trading_day(d):
        return False
    hm = now.hour * 60 + now.minute
    _close = 12 * 60 if d.isoformat() in HK_HALF_DAYS else 16 * 60
    return 9 * 60 + 30 <= hm < _close


def hk_half_day_closed(now=None):
    """今天是港股半日市且已到 12:00 收盘（之后行情定格为当日收盘价）。

    只用于**展示口径**（推送/兜底模板给港股段加「已收盘」标注）；
    "能不能判盘中异动"请用 `hk_session_open()`。
    """
    now = now or datetime.datetime.now()
    return (now.date().isoformat() in HK_HALF_DAYS
            and now.hour * 60 + now.minute >= 12 * 60)


def in_session(now=None):
    now = now or datetime.datetime.now()
    hm = now.hour*60 + now.minute
    a, b = TH["session"]
    return a <= hm <= b

def in_guard(now=None):
    """是否处于不告警的保护时段"""
    now = now or datetime.datetime.now()
    hm = now.hour*60 + now.minute
    for k in ("guard_open","guard_mid","guard_close"):
        a, b = TH[k]
        if a <= hm < b: return True
    return False

def hstech_up_threshold(d=None):
    """恒生科技上方触发线。优先读 sectors.json（改配置即时生效），
    再按日期取当日专用值，最后回退 TH。"""
    d = d or datetime.date.today()
    c = _thresholds_cfg()
    # 日期专用覆盖：`hstech_up_MMDD`（如 hstech_up_0922）。
    #
    # ⚠️ 原先这里硬编码 `if d.isoformat() == "2026-09-22"` —— 只有一个日期
    # 能命中，而 sectors.json 的注释却写「将来照此格式加键即可，无需改代码」。
    # 实测：加 hstech_up_0925 后调用（模拟 9/25）返回的仍是通用值 4450，
    # **新键根本不会被读到** —— 注释与实现不符，会误导使用者以为改配置就行。
    # 改为按当前日期动态拼键，让那套机制真正通用。
    key = f"hstech_up_{d.strftime('%m%d')}"
    v = c.get(key)
    if isnum(v): return v
    v = c.get("hstech_up")
    if isnum(v): return v
    # ⚠️ 必须**保证返回数值**。原先末行写 `TH.get(key, TH.get("hstech_up"))`
    # —— 两个键都缺时返回 None，而调用方是**裸比较**：
    #     collect.evaluate:  if v["f2"] >= hstech_up_threshold(now.date())
    #     analyze.detect:    v2 >= th
    # None 参与比较直接 TypeError，冒到 main() 的兜底 except ->
    # **整轮采集记 error、pending.json 完全不更新**。
    # 对照同函数的 hstech_down 走的是 threshold(k, 3781)（有字面量兜底），
    # 两条相邻判定用了两套标准 —— 这里补齐同样的兜底。
    # ⚠️ 末行兜底不在这里写死数值，改引模块级常量 `_HSTECH_UP_LAST_RESORT`
    # （原先硬编码 `return 4300.0`，与 TH / 配置各自为政、必然漂移）。
    _fallback = TH.get(key)
    if isnum(_fallback): return _fallback
    _fallback = TH.get("hstech_up")
    if isnum(_fallback): return _fallback
    log(f"恒生科技上方触发线配置缺失（{key} / hstech_up 均无），"
        f"暂用默认 {_HSTECH_UP_LAST_RESORT:g}", "CFG")
    return _HSTECH_UP_LAST_RESORT

# ---------- 板块配置 ----------
_CFG_PATH = p_conf("sectors.json")
_CFG_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}

def config(reload=False):
    """读取 sectors.json（带缓存，文件变更自动重载）"""
    try:
        mt = os.path.getmtime(_CFG_PATH)
        if reload or _CFG_CACHE["data"] is None or mt > _CFG_CACHE["ts"]:
            _c = _load_json(_CFG_PATH, {})
            # ⚠️ 2026-11-05（P1）：顶层形状容错。sectors.json 顶层被误写成
            # 数组/标量（整份文件替换事故）时，原先 config() 原样返回 list，
            # 下游每一处 `.get()` 都 AttributeError —— collect.evaluate()
            # 裸调用链每轮记 error、pending.json 全天停更，而 `_thresholds_cfg`
            # 的**段级**容错只在顶层是 dict 时才可达。与那条同源，这里补顶层：
            # 非法形状记一条 CFG 日志（按日去重），退回空表（全部走默认值）。
            if not isinstance(_c, dict):
                _daily_log_once(
                    "cfg", "sectors-top-shape",
                    f"sectors.json 顶层不是字典（{type(_c).__name__}），"
                    f"已忽略，全部回退默认值")
                _c = {}
            _CFG_CACHE["data"] = _c
            _CFG_CACHE["ts"] = mt
    except Exception as e:
        log(f"config 读取失败: {e}", "CFG")
        _CFG_CACHE["data"] = _CFG_CACHE["data"] or {}
    return _CFG_CACHE["data"]

def _has_data(d, key=None):
    """东财响应是否"有货"：d 是 dict 且 d["data"] 是 dict（且可选的 key 存在）。

    ⚠️ 返回类型标注成 `dict | None` 而不是 bool，是**为了让调用方不再重复
    下标**。原先写 `if _has_data(d, "diff"): ... d["data"]["diff"] ...` ——
    那个 `d["data"]` 在类型检查器眼里仍是"可能是 None 的下标"
    （实测 6 条 reportOptionalSubscript），而它恰恰是**运行时真正会炸的那一步**：
    `_has_data` 证明了 d 是 dict、d["data"] 是 dict，但调用方重取时又退回到
    未收窄的状态，将来谁把 _has_data 改成别的语义，这 6 处会一起失效。
    返回内层 dict 后，调用方拿到的就是**已经收窄过**的对象，天然不会漏。

    ⚠️ 有一处**行为变化**，已核实影响面：`{"data": {}}`（空的内层 dict）
    旧版判为"有货"（返回字面量 True），新版判为"无货"（返回空 dict，假值）。
    带 key 的调用点（trend/clist/clist_available/kline_stats 共 5 处）
    **无差异** —— 它们本来就要求 inner.get(key) 为真。不带 key 的只有
    feeds.health 一处，而它对 `data: {}` 判"不健康"比判"健康"更准确：
    空 data 就是没有取到数据。故这个变化是修正而非回归。

    ⚠️ 这个函数存在的唯一理由是 `d and d.get("data") and d["data"].get(...)`
    这个惯用法**本身不安全**：`d` 不是 dict 时（主机返回 JSON 数组 / 裸字符串 /
    数字，或 _curl 解析出意外类型）`d.get` 直接 AttributeError。
    实测（2026-09-26）8 种形状里 6 种抛异常，覆盖 trend / clist /
    clist_available / kline / news 五条取数路径 —— 全部是行情主链路。

    后果分两级：
      · 在 try 内的（如 hot.kline_stats）被吞成"这一项没有数据"；
      · 不在 try 内的（trend / clist / clist_available）异常直接冒到
        build_brief 或 analyze.run，**整轮简报或整轮推送判定失效**。
    而这类形状异常并非臆测：主机故障页、限流页、CDN 返回的 HTML 片段
    都会让 json 解析出非 dict 的顶层。

    注意 `not d` 的短路只挡住 None/空值，**挡不住类型错** —— 与
    state.json 那个 setdefault 的坑是同一类（只判"有没有"，不判"是不是"）。
    """
    if not isinstance(d, dict):
        return None
    inner = d.get("data")
    if not isinstance(inner, dict):
        return None
    if key is not None and not inner.get(key):
        return None
    return inner


def _sid(code):
    """补板块 secid 前缀，**幂等**。

    sectors.json 里两种写法历史上都出现过（裸短码 BK1036 / 完整 90.BK1036），
    无条件拼 90. 会得到 90.90.BK1036 —— 东财不认，整批取数静默失败。
    这里统一处理，两种写法都能正确工作。
    """
    return code if "." in code else f"90.{code}"

# ⚠️ sectors.json 是**用户可编辑**的文件（其 _说明 明写"改此文件即时生效"），
# 而下面这些访问器原先一律用裸下标 x["code"] / x["name"]。手工编辑时漏写一个
# 字段，KeyError 就会从**任意**调用点抛出 —— 实测：给 watch 加一项只写 code、
# 不写 name，当时的 names_of()（2026-10-09 查明零调用已删）立刻 KeyError。
# 而 sectors.json 的容错要求比代码高得多：
# 改错一个字段不该让整个盯盘系统瘫掉，应该跳过那一项、记一条日志、继续跑。
# 统一的取值口径：缺 code/key 的项直接跳过（没它无法定位标的），
# 缺 name 的项保留但名字退化成代码本身（仍能监控，只是显示得难看）。
def _cfg_items(*sections):
    """按顺序取配置里的若干段落，逐项过滤掉结构不完整的元素。"""
    c = config()
    out = []
    for _seg in sections:
        _lst = c.get(_seg)
        if not isinstance(_lst, list):
            if _lst is not None:
                log(f"配置段 {_seg!r} 不是列表（{type(_lst).__name__}），已忽略", "CFG")
            continue
        for _i in _lst:
            if not isinstance(_i, dict):
                log(f"配置段 {_seg!r} 中有非字典项（{type(_i).__name__}），已跳过", "CFG")
                continue
            out.append((_seg, _i))
    return out

def _cfg_id(item, *keys):
    """取标的标识（code/key 任一）。全缺则返回 None（调用方跳过）。"""
    for _k in keys:
        _v = item.get(_k)
        if isinstance(_v, str) and _v.strip():
            return _v.strip()
    return None

def watch_secids():
    """固定监控的板块 secid 列表（含 90. 前缀）"""
    return [_sid(_c) for _seg, x in _cfg_items("watch")
            if (_c := _cfg_id(x, "code"))]

def index_secids():
    return [_c for _seg, x in _cfg_items("index")
            if (_c := _cfg_id(x, "code", "key"))]

def hk_secids():
    return [_c for _seg, x in _cfg_items("hk_stocks")
            if (_c := _cfg_id(x, "code", "key"))]

def _thresholds_cfg() -> dict:
    """sectors.json 的 thresholds 段，**形状容错**。

    ⚠️ 2026-10-08 数据契约审查实测：`"thresholds": []`（一个笔误）时
    原先的 `config().get("thresholds", {}).get(key)` 抛
    `AttributeError: 'list' object has no attribute 'get'` —— 发生在
    `collect.evaluate()` 的裸调用链里 -> **每轮采集记 error、
    pending.json 全天停更**。与 sectors.json 的既有契约
    （`_cfg_items` 注释："改错一个字段不该让整个盯盘系统瘫掉"）直接冲突。
    非法形状记一条 CFG 日志（每次进程首次读到才记，避免刷屏），退回空表。
    """
    _c = config().get("thresholds")
    if isinstance(_c, dict):
        return _c
    if _c is not None:
        _daily_log_once("cfg", "thresholds-shape",
                        f"sectors.json 的 thresholds 不是字典"
                        f"（{type(_c).__name__}），已忽略，全部回退默认值")
    return {}


def threshold(key: str, default: float | None = None) -> float:
    """读阈值（优先 sectors.json，其次 marketlib.TH）。

    ⚠️ 返回类型标注成 float 而非 `float | None`，依据是**调用约定**：
    实际调用点**全部**传了 default（已核：正则扫出的 16 处
    `threshold("k", <字面量>)` 覆盖了库里用到的全部 8 个键）。
    返回 None 只会在"键既不在 sectors.json 又不在 TH 且调用方没给默认值"
    时发生 —— 那种写法在库里不存在。

    ⚠️ 不要说"TH 里每个键都有字面默认值兜底" —— 那**不成立**。
    实测（2026-09-28）：`watch_fund_pct`、`market_fund_in`、
    `market_fund_out`、`market_fund_pct` 这 4 个键**不在 TH 里**，
    只在 sectors.json 里（market_* 那 3 个连 sectors.json 都没有）。
    它们的兜底**完全靠调用点传的 default**。好在：
      · selfcheck 的 [阈值默认值一致性] 按 `threshold("k", <字面量>)`
        正则扫出 16 处，**完整覆盖**库里用到的 8 个键，所以漂移会被发现；
      · 库内**没有**不带 default 的调用
        （`grep -rn 'threshold("[a-z_0-9]*")'` 在非 selfcheck 文件里为 0 条）。
    这条区别有实际意义：若哪天给这些键加一个不带 default 的调用点，
    threshold() 会静默返回 **0.0** —— 对 `market_fund_out`（缺省 -1000）
    这种"越负越触发"的阈值，0.0 会让判定**全部失效**。

    标注成 float 而不是 `float | None` 不是掩盖问题：`float | None` 会让
    下游每一个 `threshold(...)*1e8` / `abs(f62) < threshold(...)` 都被报成
    "可能对 None 做运算"（实测 3 条 reportOperatorIssue / reportOptionalOperand），
    把真信号淹在假警报里。真出现 None 的情况由 selfcheck 拦截。
    """
    v = _thresholds_cfg().get(key)
    if isnum(v): return v
    if v is not None:
        # 形状容错（2026-10-08 数据契约审查）：原先 `if v is not None: return v`
        # 会把带引号的 `"8.0"` 原样返回，下游 `abs(f62) < "8.0"` 直接 TypeError
        # -> 每轮采集记 error、pending.json 全天停更。非法值记痕后回退默认。
        log(f"阈值 {key!r} 的值不是数值（{v!r}），已忽略并回退默认", "CFG")
    _t = TH.get(key, default)
    if _t is None:
        # 兜底：真的两个来源都没有且调用方没给默认值。记一条痕并给 0，
        # 而不是把 None 漏出去让下游在**很远的运算符处**炸掉。
        log(f"阈值 {key!r} 既不在 sectors.json 也不在 TH，且未给默认值，暂用 0", "CFG")
        return 0.0
    return _t


# 指数 σ 表：secid -> (名称, 静态σ)。放在 marketlib 而非 alert，
# 因为**两条通道都要用**（见 idx_thresholds）。alert 与 collect 都 import
# marketlib，反过来不行 —— 下移才能共享同一份真值。
#
# 静态值取自近 250 日实测，用途是 EWMA 的**下限保护**。
# ⚠️ 覆盖面**必须与 sectors.json 的 index 一致**。
# alert.index_quotes() 按本表的 keys 拼请求，所以表里有、index 里没有的指数
# 会**被请求、被判异动、被推送到群**，而 collect 从不请求它 —— 快照通道
# 不记，面板上看不到这条告警（与"推送/快照阈值分叉"是同一类缺陷）。
# 原先这里有 "0.399001"(深证成指) 与 "1.000905"(中证500)，两项都不在
# index 里 —— 实测深证成指 -5% 会被 alert 命中并推送。已删除。
# 将来要启用：往 sectors.json 的 index 加一条，**同时**在此加回 σ 值，
# selfcheck 的 [指数阈值同源] 会检查两边一致。
STATIC_SIGMA = {
    "1.000001": ("上证指数", 0.90), "1.000300": ("沪深300", 1.11),
    "0.399006": ("创业板指", 2.14), "1.000688": ("科创50", 2.61),
    "100.HSI":  ("恒生指数", 1.18), "124.HSTECH": ("恒生科技", 1.77),
}
# 下限系数：thr = max(3σ_dyn, 3σ_static*FLOOR)。σ_dyn 很小时把门槛托住，
# 防止"平静期"阈值低到任何微小波动都触发。**它不是给回退路径打折** ——
# 回退时 sig 就是 σ_static，此时 FLOOR 项恒小于主项，从不生效。
SIGMA_FLOOR = 0.7


def idx_thresholds():
    """指数异动阈值：{secid: (名称, 阈值%)}。

    ⚠️ **唯一真值**。原先 collect.IDX3S（固定 3σ 表）与 alert.check_index
    （动态 EWMA 3σ）各算一套，两条通道对同一指数用**不同门槛**，且方向是反的：
      指数      推送门槛(alert)  快照门槛(collect)
      上证指数     2.25%          2.70%
      恒生科技     3.94%          5.30%
    于是 [2.25%, 2.70%) 这个区间里：**推送到群了，但快照不记** ——
    面板事件区看不到这条异动，读者无从回看"当时为什么推"。
    实测（生产 daily.json 的 σ）：上证 -2.40% 会推送到群，快照不记。
    语义上快照是免费记录、推送是昂贵打断，快照门槛理应 <= 推送门槛，
    让"推过的"必定留下记录。

    这里统一到 alert 的动态口径（EWMA σ，自适应波动变化），
    σ 直接读 dcache（alert 早已在算并落盘），**不增加任何请求**。
    """
    out = {}
    for sid, (nm, s_sig) in STATIC_SIGMA.items():
        es = dcache_get(f"sigma:{sid}")
        if not isnum(es) or not (0.3 < es < 6):
            es = s_sig
        out[sid] = (nm, max(3.0 * es, 3.0 * s_sig * SIGMA_FLOOR))
    return out

def algo_top():
    return config().get("algo_top", 5)




# ---------- 慢变量磁盘缓存 ----------
# launchd 每 5 分钟起新进程，进程内缓存全部失效。慢变量（波动率、成分股、
# K线基线等）按 (键, 日期) 落盘，跨进程复用，显著降低请求量与风控风险。
# CACHE_DIR 已由文件头部的 `from paths import ...` 提供，此处不再重复定义
DCACHE_FILE = os.path.join(CACHE_DIR, "daily.json")

def dcache_get(key, day=None):
    """取当日缓存值。未命中返回 None。"""
    day = day or datetime.date.today().isoformat()
    try:
        d = _load_json(DCACHE_FILE, {})
        if d.get("date") != day: return None
        return d.get("kv", {}).get(key)
    except Exception:
        return None

def dcache_set(key, val, day=None):
    """写当日缓存值（原子替换）。**整体在文件锁内**。

    ⚠️ 原实现是"读 -> 改 -> 写"三步无锁。`_atomic_write_json` 只保证
    **单次写入**不产生半截文件，不保证这三步之间没有别的进程插进来。
    daily.json 被 collect / analyze / hot **三个进程**共享（存 sigma、levels、
    日K、失败记忆），launchd 每 5 分钟同时拉起 collect 与 analyze，
    盘中最坏情况是同一毫秒都在写。实测（16 进程同步起跑，5 次试验）
    每次丢 2~8 个更新。
    对比 `RateLimiter._reserve()` 用的是 fcntl.flock + 锁内读-判-写 ——
    同一份代码里两套标准。

    另外：`_load_json` 解析失败返回 {}，`d.get("date")` 得 None != today，
    于是 kv = {} —— **整个当日的 sigma/levels/日K 缓存被一份新键覆盖掉**，
    且没有任何日志。实测：半截 daily.json -> 原有 kv 全部丢失。
    锁 + 损坏检测一起解决。
    """
    day = day or datetime.date.today().isoformat()
    try:
        with _open_lockfile(DCACHE_FILE + ".lock") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                # 损坏检测：文件存在且非空但解析失败 -> 明确告警，
                # 不要把"解析失败"和"首次创建"混为一谈（后者才是正常空值）。
                raw_ok = True
                if os.path.exists(DCACHE_FILE) and os.path.getsize(DCACHE_FILE) > 0:
                    try:
                        _probe = _load_json(DCACHE_FILE, None)
                        raw_ok = isinstance(_probe, dict)
                    except Exception:
                        raw_ok = False
                d = _load_json(DCACHE_FILE, {})
                if not isinstance(d, dict):
                    # ⚠️ 2026-11-05（P2）：文件内容是**合法 JSON 但非对象**
                    # （数组/字符串/数字）时，`d.get("date")` 直接
                    # AttributeError，被外层 except 静默吞掉 —— 本轮缓存
                    # 既不读也不写、没有任何日志，表现成"sigma 怎么算都是
                    # 静态值"。上层 raw_ok 只判有没有损坏，没管这里。
                    log(f"daily.json 顶层不是对象（{type(d).__name__}），"
                        f"按空缓存重建", "FEED")
                    d = {}
                if not raw_ok:
                    log("daily.json 解析失败（可能被写坏），本轮重建缓存", "FEED")
                kv = d.get("kv", {}) if d.get("date") == day else {}
                if not isinstance(kv, dict): kv = {}
                kv[key] = val
                _atomic_write_json(DCACHE_FILE, {"date": day, "kv": kv})
            finally:
                try: fcntl.flock(lk, fcntl.LOCK_UN)
                except Exception: pass
    except Exception:
        pass

# ---------- 盘中历史（由 collect.py 写入，供 analyze.py 看"变化"）----------
PENDING = p_state("pending.json")

# ⚠️🌗 hist 新鲜度上界（2026-10-30 补，P0）。见 read_history 头部长注释 ——
# hist 本身只判"当日有"，"还算不算现价"由渲染层用本常量判。
# 取 360s 不是拍脑袋：它与 `analyze._quotes_from_snapshot(max_age_s=360)`
# **必须是同一个数** —— 两者读的是同一份 `pending.json`、取自同一轮 collect，
# 若复用闸门认为"超过 6 分钟就不算当前价"，那从同一文件读出的 hist 更不
# 该被叫作「现」。生产实据（10-30）：14:49 配额耗尽 → hist 停在 14:44，
# 15:04「收盘定局」把 20 分钟前的 chg 当现价播。
HIST_MAX_AGE = 360.0


def _hist_stale_verdict(h, now=None, max_age_s=None):
    """判定 hist 的最后一帧对"当下"是否仍可称作实时。

    返回 (verdict, last_time, age_min)：
      verdict ∈ {"empty","live","stale"}；last_time 为末帧 "HH:MM" 或 None；
      age_min 为末帧距今分钟数（empty 时 0.0）。

    · 空序列 / 无可用 time → "empty"（该段本就静默消失，无错可标）
    · 末帧距今 <= max_age_s → "live"
    · 其余 → "stale"（含**未来时刻**：时钟回拨或手工编辑。把未发生的值
      当"现价"是比陈旧更严重的方向，一律按 stale —— 同
      [[market-watch-cross-scope-local-and-clock-mix-p0]] 的守时态）

    `now` / `max_age_s` 可注入（沙箱与构造值测试用），缺省为真实时钟与
    `HIST_MAX_AGE`。只判分钟粒度：hist 的 time 是 "HH:MM"，与 collect 写
    快照用的是同一个 `now`，精度足够支撑 6 分钟级阈值。
    """
    try:
        if max_age_s is None:
            max_age_s = float(HIST_MAX_AGE)
        if not h:
            return ("empty", None, 0.0)
        _times = [x.get("time") for x in h
                  if isinstance(x, dict) and isinstance(x.get("time"), str)
                  and ":" in x["time"]]
        if not _times:
            return ("empty", None, 0.0)
        _last_t = max(_times)              # "HH:MM" 字典序即时间序
        try:
            _h, _m = _last_t.split(":")[:2]
            _own_last = int(_h) * 60 + int(_m)
        except (ValueError, IndexError):
            return ("empty", None, 0.0)
        _now = now if isinstance(now, datetime.datetime) else datetime.datetime.now()
        _own_now = _now.hour * 60 + _now.minute
        _diff = _own_now - _own_last
        if _diff < 0:
            return ("stale", _last_t, 0.0)      # 未来时刻
        # ⚠⚠ 2026-11-10（Codex P3）：**不能丢掉当前秒**。
        # `HIST_MAX_AGE` 是“秒”级阈值，而上面只拿到分钟整数 ——
        # `[实测]` 10:00 的帧在 10:06:00 与 10:06:59 都算 (6.0 分, live)，
        # 但 6×60=360 <= 360 恰好卡在边界上：后者实际已经 419s、应判 stale。
        # 影响：面板把早已停更的快照响应成“实时”，差值小但方向是“把陈旧当新鲜”。
        _now_s = _now.hour * 3600 + _now.minute * 60 + _now.second
        _last_s = _own_last * 60
        _age_s = float(max(0, _now_s - _last_s))
        _age_min = _age_s / 60.0
        if _age_s <= max_age_s + 1:
            return ("live", _last_t, _age_min)
        return ("stale", _last_t, _age_min)
    except Exception:
        return ("empty", None, 0.0)


def read_history():
    """读取当日盘中快照序列。返回 [{"time":"HH:MM","idx":{...},"sec":{...}}, ...]

    collect.py 每 SCHED_INTERVAL（当前 5 分钟）追加一条，保留最近 24 条，
    即约 2 小时的窗口。用于让 AI 写"变化"
    （如"腾讯涨幅从 +5% 收窄到 +2.8%"），而不是每轮重复描述同一静态状态。

    ⚠️ 条数与 collect.SCHED_INTERVAL、collect 里 `hist[-N:]` 的 N 三处必须
    同步改 —— 窗口时长 = N × SCHED_INTERVAL，是下游【盘中变化】段参照点
    可追溯范围的**上界**。

    ⚠️🌗 本函数**故意不做新鲜度判定**（2026-10-30 定案）：它只回答"当日有
    哪些快照"，"这些快照还算不算现价"由**渲染层**判 —— `analyze.build_brief`
    的【盘中变化】段用 `_hist_stale_verdict()` 判，陈旧时把「现」改成
    「末帧」并显式警告。理由：在这里返回 [] 会让该段**静默消失**（读者
    分不清"今天没波动"和"数据停了"），而本文件已确立的约定是"缺了要说"
    （同 `levels()` 拿不到时逐行标「为上一交易日收盘，非现价」）。
    生产实证见 [[market-watch-close-round-stale-hist]]。
    """
    try:
        d = _load_json(PENDING, {})
        if d.get("date") != datetime.date.today().isoformat():
            return []
        h = d.get("hist", [])
        if not isinstance(h, list): return []
        # 只保留形状正确的快照（dict 且有 time）。外部写坏一条不该毁掉整轮报告
        return [x for x in h if isinstance(x, dict) and "time" in x]
    except Exception:
        return []

def changes(hist, codes):
    """把快照序列换算成"变化"。codes: {代码: 名称}。

    返回 [(名称, 最新值, 首值, 较开盘变化, 较上一快照变化), ...]
    按 |较开盘变化| 降序。
    """
    if not hist or len(hist) < 2: return []
    first, last = hist[0], hist[-1]
    prev = hist[-2]
    out = []
    for c, nm in codes.items():
        def _get(snap, field="chg"):
            # 逐层 isinstance 守卫：快照文件可能被外部写坏（半截、结构不对、
            # 手工编辑）。曾经这里直接 snap.get(...) 抛 AttributeError，
            # 一路炸到 run() 顶层，整轮报告全丢且 inflight 泄漏。
            if not isinstance(snap, dict): return None
            for grp in ("sec", "idx"):
                g = snap.get(grp)
                if not isinstance(g, dict): continue
                v = g.get(c)
                if isnum(v): return v                              # 旧格式：裸数值
                if isinstance(v, dict):                            # 新格式：{chg, f62}
                    x = v.get(field)
                    if isnum(x): return x
            return None
        v0, v1, vp = _get(first), _get(last), _get(prev)
        if v1 is None or v0 is None: continue
        out.append((nm, v1, v0, v1 - v0, (v1 - vp) if vp is not None else 0.0))
    out.sort(key=lambda x: -abs(x[3]))
    return out

# ---------- 隔夜外盘（盘前用）----------
# 注：东财的美股/港股字段实测有陈旧数据（HSI 报 25737.6 而真实 25087.75），
# 故美股一律走腾讯，用 现价/昨收 自算涨跌幅。
#
# 标的选取依据（2026-09-30 定）：
#   · usSMH 半导体 —— 四个 watch 板块（半导体/存储/CPO/印制电路板）的共同源头
#   · usEWJ/usEWY/usVGK —— 亚太与欧洲，A股开盘前最后一段外盘信息
#   · usGLD/usSLV/usUSO —— 商品，对应组合考虑过的黄金配置
# ⚠️ VIX **不在这里**（2026-11-03 移出）：腾讯的 `usVIX` 是**冻结占位值**
#    （时间戳恒停在 2026-09-15、涨跌幅恒 0.00%），陈旧闸门每天丢弃它，
#    等于白解析一遍。改用新浪 `znb_VIX`（活的现货，见 `_ov_sina_vix`）。
# ⚠️ 不要用 usCNH / usCNY 当汇率 —— 实测是 **Cnh Industrial** 与
#    **Market Vectors** 两只股票（13.06 / 44.25），推出去是完全错误的数字。
#    离岸人民币走 _fx_cnh()（新浪外汇）。
OVERSEAS = [
    ("usDJI",  "道琼斯"),
    ("usIXIC", "纳斯达克"),
    ("usINX",  "标普500"),
    ("usSMH",  "费城半导体"),
    ("usEWJ",  "日经(EWJ)"),
    ("usEWY",  "韩国(EWY)"),
    ("usVGK",  "欧洲(VGK)"),
    ("usKWEB", "中概互联(KWEB)"),
    ("usFXI",  "中国大盘(FXI)"),
    ("usGLD",  "黄金(GLD)"),
    ("usSLV",  "白银(SLV)"),
    ("usUSO",  "原油(USO)"),
]

# 隔夜段的单源超时（秒）。三个源并发取，整体耗时≈最慢的那个。
# 实测：腾讯 0.10s / 东财 0.12s / 新浪 0.38s（最慢一次 1.18s）。
# 取 8s 是给"网络抖动"留量 —— 这是盘前简报，宁可少一个数字也不能拖慢整轮。
_OV_TIMEOUT = 8
# 隔夜行情的**陈旧阈值**（小时）。见 `_ov_tencent` 的 docstring：
# 腾讯对**部分**代码会返回冻结占位值（`usVIX` 曾冻结在 2026-09-15，现已移出
# OVERSEAS 名单），不校验就每天推一样的数字。取 96h（4 天）：跨周末 + 一个
# 假期仍有余量，而任何"停在几周前"的源都会被拦下。`_sina_vix_row` 复用同
# 一思路（按**日期**判，非当天即丢）。
_OV_STALE_H = 96

def _ov_tencent():
    """腾讯行情：指数/半导体/亚太/中概/商品/波动率。返回 [(名称, 涨跌幅%)]。

    ⚠️⚠️ **必须校验行情时间戳**（2026-10-27 修，P2）：`usVIX` 这个代码在
    腾讯返回的是一个**冻结占位值** —— 活体两次探针均为
    `p[3]=21.67 p[4]=21.67 p[30]="2026-09-15 09:30:00"`，算出来
    `chg = 0.00`。于是盘前简报**每天**都写「VIX波动率 +0.00%」，读者会
    理解成"波动率没变"，实际是源根本没更新。
    `[实测]` history/pushes.jsonl 里 2026-10-09 / 10-21 / 10-27 三条
    盘前简报全是 `VIX波动率 +0.00%`。
    同文件 `quotes()` 末尾早有 `_is_ts_stale` 闸门，隔夜这条通道没有。
    判据：`p[30]` 是行情时间，超过 `_OV_STALE_H` 小时的条目**直接丢弃**
    （宁可少一行数字，也不能把三个月前的价格当今天的涨跌幅推给读者）。
    """
    codes = ",".join(c for c, _ in OVERSEAS)
    try:
        raw = subprocess.run(["curl","-s","-m",str(_OV_TIMEOUT),
                              f"https://qt.gtimg.cn/q={codes}"],
                             capture_output=True, timeout=_OV_TIMEOUT+2
                             ).stdout.decode("gbk","ignore")
    except Exception:
        return []
    got = {}
    for line in raw.split("\n"):
        if "=" not in line: continue
        key = line.split("=")[0].replace("v_", "").strip()   # usDJI / usKWEB
        p = line.split("=",1)[1].strip().rstrip(";").strip('"').split("~")
        if len(p) < 6: continue
        # 时间戳新鲜度（见 docstring）：解析失败**不**因此丢弃
        # （有些品种该字段可能为空，那种情况维持原行为），
        # 只有"解析成功且确实过旧"才丢。
        if len(p) > 30:
            try:
                _ts = datetime.datetime.strptime(p[30].strip(),
                                                 "%Y-%m-%d %H:%M:%S")
                _age_h = (datetime.datetime.now() - _ts).total_seconds() / 3600
                if _age_h > _OV_STALE_H or _age_h < -1:
                    # ⚠️ 2026-11-06（P1-3）：未来时间戳同样不能信
                    log(f"隔夜行情 {key} 时间戳过旧/未来（{p[30]}，"
                        f"{_age_h:.0f}h）—— 丢弃该条，"
                        f"不把陈旧或错位价格当今日涨跌幅", "FEED")
                    continue
            except (ValueError, IndexError):
                pass
        try:
            px = float(p[3]); prev = float(p[4])
            # ⚠️ 2026-11-06（P1-4）：float("nan")/float("inf") 解析成功，
            # 旧判据 `prev > 0` 对 inf 成立、对 nan 为 False 但 px 仍可能是
            # nan → 结果 nan 渲染成「+nan%」且过 safe()。统一走 isnum()。
            if not isnum(px) or not isnum(prev) or prev <= 0: continue
            _chg = (px/prev - 1) * 100
            if not isnum(_chg) or abs(_chg) > 100: continue
            got[key] = _chg
        except (ValueError, IndexError):
            continue
    out = []
    for code, name in OVERSEAS:
        if code in got: out.append((name, got[code]))
    return out

def _ov_treasury():
    """东财数据中心：美债 2/10/30Y + 中债 10Y。返回 [(名称, 收益率%)]。

    ⚠️ 这里返回的是**收益率水平**不是涨跌幅 —— 与其余各源语义不同，
    故名称里带「收益率」字样，由 _ov_line() 分别处理。
    数据源同 akshare 的 bond_zh_us_rate（实测东财与美财政部官方 CSV
    逐位一致：9/29 10Y=5.26 30Y=5.59 2Y=4.89），且额外白送中债。
    """
    u = ("https://datacenter.eastmoney.com/api/data/get"
         "?type=RPTA_WEB_TREASURYYIELD&sty=ALL&st=SOLAR_DATE&sr=-1"
         "&token=894050c76af8597a853f5b408b759f5d&p=1&ps=2&pageNo=1&pageNum=1")
    try:
        raw = subprocess.run(["curl","-s","-m",str(_OV_TIMEOUT),"-A",UA,
                              "-H","Referer: https://data.eastmoney.com/",u],
                             capture_output=True, timeout=_OV_TIMEOUT+2
                             ).stdout.decode("utf-8","ignore")
        rows = (json.loads(raw).get("result") or {}).get("data") or []
    except Exception:
        return []
    if not rows: return []
    # 取**最新一条有美债值的**（当日数据偶尔尚未落库，实测 9/25 中债为空）
    for r in rows:
        v10 = r.get("EMG00001310")
        if not isnum(v10): continue
        # ⚠⚠ 2026-11-10（Codex P2）：**新鲜度闸门**，与 `_ov_tencent` / `_sina_vix_row`
        # 对齐。原来只取“最新一条有值的行”，不看 `SOLAR_DATE` ——
        # 而收益率是**水平值**，冻结几周也“看着正常”（不像涨跌幅那样
        # 一眼能看出不对）。东财数据中心停更时，这一段会默默推旧数。
        _sd = r.get("SOLAR_DATE")
        if _sd:
            try:
                _st = _ts_parse(str(_sd)[:19])
                if _st is not None:
                    _ah = (datetime.datetime.now() - _st).total_seconds() / 3600.0
                    if _ah > _OV_STALE_H or _ah < -1:
                        log(f"美债收益率数据陈旧（{_sd}，"
                            f"{_ah:.0f}h）—— 本轮丢弃", "FEED")
                        continue
            except Exception:
                pass
        out = []
        # ⚠️ 标签**不能带「收益率」三字** —— 它在 BANNED 里（第 27 条，
        # 防的是"个人收益率"泄漏）。实测 `safe("美债10Y收益率 5.26%")`
        # 返回 (False,'收益率')，整段隔夜数据会被拦掉。
        # 改用「美债10年期」这种等价写法，实测 safe() 通过。
        for k, nm in (("EMG00001306","美债2年期"), ("EMG00001310","美债10年期"),
                      ("EMG00001312","美债30年期"), ("EMM00166466","中债10年期")):
            v = r.get(k)
            if isnum(v): out.append((f"{nm}利率", float(v)))
        return out
    return []

def _ov_fx():
    """新浪外汇：离岸人民币。返回 [(名称, 涨跌幅%)]。

    字段语义（实测锁定，18 列）：[8]=最新价、[3]=昨收、[11]=[8]-[3]（绝对变动）。
    故涨跌幅 = [11]/[3]*100。不能用 [1] 当昨收 —— 实测 [1] 与 [8] 恒等。
    """
    u = "https://hq.sinajs.cn/list=fx_susdcnh"
    try:
        raw = subprocess.run(["curl","-s","-m",str(_OV_TIMEOUT),"-A",UA,
                              "-H","Referer: https://finance.sina.com.cn/",u],
                             capture_output=True, timeout=_OV_TIMEOUT+2
                             ).stdout.decode("gbk","ignore")
        body = raw.split('="',1)[1].rstrip('";\n')
        f = body.split(",")
        if len(f) < 12: return []
        last = float(f[8]); prev = float(f[3])
        # ⚠️ 2026-11-06（P1-4）：只判 `prev <= 0` 不够 —— 源返回 "nan"/"inf"
        # 时 `float()` 照样解析成功，`last/prev` 得到 nan/inf，
        # 最终渲染成「离岸人民币 +nan%」并被 `safe()` 放行（实测）。
        # 解析边界统一用 `isnum()`（本库唯一的有限数值判据）。
        if not isnum(last) or not isnum(prev) or prev <= 0: return []
        # ⚠⚠ 2026-11-10（Codex P2）：必须校验**行情日期**。
        # `[实测]` 字段布局：[0]=时刻，[8]=最新价，[3]=昨收，**[末列]=2026-11-10**。
        # 原来只算 `last/prev-1`，不看日期 —— 源停更时会把
        # 几周前的汇率当成今天的涨跌幅推出去，而 `_chg` 形态完全正常。
        # 与 `_ov_tencent` / `_ov_treasury` 同一口径：超过 `_OV_STALE_H` 就丢。
        _dt_s = f[-1] if f else ""
        if _dt_s:
            try:
                _dtp = _ts_parse(str(_dt_s)[:19])
                # ⚠⚠ 2026-11-11（审查）：新浪汇率的末列是**纯日期**，
                # 而 `_ts_parse` 只接受三种带时分秒的格式 —— 纯日期返回 None。
                # 于是下面 `if _dtp is not None` 恒为假，**整段陈旧判定是死代码**。
                # `[实测 2026-11-11 08:12]` 真实返回体：
                #   var hq_str_fx_susdcnh="08:12:06,6.6761,...,,离岸人民币,..,2026-11-11"
                #                                                                              ^^^^^^^^^^ 末列
                # 本函数存在的唯一理由就是“防源冻结在几周前”，而判不了就等于没有。
                # 补：纯日期按当日 00:00 算（与 `_ov_treasury` 的 "00:00:00" 同语义）。
                if _dtp is None:
                    _d_only = str(_dt_s).strip()[:10]
                    try:
                        _dtp = datetime.datetime.combine(
                            datetime.date.fromisoformat(_d_only),
                            datetime.time.min)
                    except (ValueError, TypeError):
                        _dtp = None
                if _dtp is not None and "_" not in str(_dt_s):
                    _ah = (datetime.datetime.now() - _dtp).total_seconds() / 3600.0
                    if _ah > _OV_STALE_H or _ah < -1:
                        log(f"离岸人民币行情陈旧（{_dt_s}，"
                            f"{_ah:.0f}h）—— 本轮丢弃", "FEED")
                        return []
            except Exception:
                pass
        _chg = (last/prev - 1) * 100
        if not isnum(_chg) or abs(_chg) > 50: return []
        return [("离岸人民币", _chg)]
    except Exception:
        return []


def _sina_vix_row(body, today=None):
    """解析新浪 `znb_VIX` 响应 → ("VIX波动率", 涨跌幅%) 或 None。

    实测字段（13 列，逗号分隔）：
      [0]名称 [1]最新 [2]涨跌额 **[3]涨跌幅%** [4][5]空 [6]日期 [7]时间
      [8]开 [9]高 [10]低 [11]昨收 [12]标志
    `hq_str_znb_VIX="VIX恐慌指数,21.1400,1.73,8.91,,,2026-11-03,05:15:01,..."`

    ⚠️ **必须校验日期**（同 `_ov_tencent` 那条陈旧闸门的理由）：新浪这个源
    目前是活的（实测 2026-11-03 05:15 给出 21.14/+8.91%），但**不能假设它
    永远活** —— 腾讯的 `usVIX` 就是活了一阵后冻成占位值的先例。若新浪某天
    也冻结，不校验就会把几周前的 VIX 当今日涨跌幅推出去。

    ⚠️ 窗口是 **`_OV_STALE_H`（96h）而不是"必须是今天"**（2026-11-05 修，P1）：
    VIX 是**美股**品种，周五收盘后到周一开盘前、以及美国假期间，最新一条
    报价的日期**不是今天** —— "必须今天"会让每周末/每个美股假日都判 VIX
    缺失，而腾讯那条死源早已移除、新浪是唯一供应方（隔夜段长期没有 VIX
    这个"跌是普跌还是恐慌"的判断维度）。判据与 `_ov_tencent` 的陈旧闸门
    **同源同值**（96h）：既挡住"冻结几周"，又容得下跨周末的正常陈旧。

    纯函数（喂构造响应即可测，见 tests/test_vix_fallback.py）。
    """
    if not isinstance(body, str) or '="' not in body:
        return None
    try:
        inner = body.split('="', 1)[1].rstrip('";\n')
        f = inner.split(",")
        if len(f) < 8:
            return None
        # 日期新鲜度：超过 _OV_STALE_H 小时 → 丢（见 docstring）
        # ⚠️⚠️ 2026-11-06（P1-3 修）：只判「太旧」→ 未来日期 / NaN / 极端值全部放行
        #   · 未来日期（2099-01-01）→ 原判据永远不成立 → 放行 ❌
        #   · NaN / 9999 等 `float()` 能解析的非有限/非合理值 → 放行 ❌
        # 改：检查日期在合理范围内 + 数值是有限正数（VIX 恒 >0）。
        _d = f[6].strip()
        _today_str = today or datetime.date.today().isoformat()
        _dd = datetime.date.fromisoformat(_d)
        _td = datetime.date.fromisoformat(str(_today_str))
        _age = (_td - _dd).days
        if _age < 0 or _age > _OV_STALE_H // 24:
            # 未来日期或超过 4 天：一律丢弃
            return None
        v = float(f[3])
        if not isnum(v) or v <= 0 or v > 200:
            # 非有限值、非正数、或明显不合理（VIX 历史上限约 82.69）
            return None
    except (ValueError, IndexError):
        return None
    return ("VIX波动率", v)


def _ov_sina_vix():
    """新浪 `znb_VIX`：波动率现货指数 → [("VIX波动率", 涨跌幅%)] 或 []。

    ⚠️ 存在的理由（2026-11-03 加）：腾讯的 `usVIX` 是**冻结占位值**，陈旧
    闸门每天丢弃它 → 隔夜段**长期缺失 VIX 这个"跌是普跌还是恐慌"的判断
    维度**，而三源里只有腾讯供 VIX。`[实测]` 新浪 `znb_VIX` 给的是活的
    现货（`VIX恐慌指数,21.1400,1.73,8.91,,,2026-11-03,05:15:01,...`），
    与腾讯死值（恒 21.67/0.00%、时间戳 2026-09-15）形成对照。
    列语义与解析见 `_sina_vix_row`。
    """
    u = "https://hq.sinajs.cn/list=znb_VIX"
    try:
        raw = subprocess.run(["curl", "-s", "-m", str(_OV_TIMEOUT), "-A", UA,
                              "-H", "Referer: https://finance.sina.com.cn/", u],
                             capture_output=True,
                             timeout=_OV_TIMEOUT + 2).stdout.decode(
                                 "gbk", "ignore")
    except Exception:
        return []
    row = _sina_vix_row(raw)
    return [row] if row else []

def _ov_line(out):
    """把隔夜项压成**一行紧凑文本**，供 prompt 用。

    为什么要压：09:25 的 max 是 450 字，而实测该时段正文已到 467/916/529 字
    （历史三条均超限）。逐项 `名称: +x.xx%` 换行会占 13 行 ——
    压成一行可省约 12 行，把空间留给「要闻」「今日关注」。

    收益率项（美债/中债）与涨跌幅项语义不同，分两段输出：
    前者写水平值，后者写百分比变动。
    """
    if not out: return ""
    # ⚠️ 2026-11-06（P1-4，最后一道）：解析层已用 isnum() 拦非有限值，
    # 但这里是**渲染成正文的出口**，再兜一次 —— 上游任何新源忘了校验，
    # `f"{nan:+.2f}%"` 会直接产出「+nan%」并被 safe() 放行（实测）。
    # 丢弃而不是渲染占位符：宁可少一项数字，也不能把 nan 推给读者。
    out = [(n, v) for n, v in out if isnum(v)]
    if not out: return ""
    # 判据用「利率」后缀（见 _ov_treasury 的 BANNED 注释：不能用「收益率」）
    pcts = [(n, v) for n, v in out if not n.endswith("利率")]
    ylds = [(n, v) for n, v in out if n.endswith("利率")]
    segs = []
    if pcts:
        segs.append("、".join(f"{n} {v:+.2f}%" for n, v in pcts))
    if ylds:
        segs.append("、".join(f"{n} {v:.2f}%" for n, v in ylds))
    return "；".join(segs)

# 公开别名：模块内用私有名 _ov_line，但 analyze 按「公开 API 不带下划线」
# 的既有约定导入（同 overseas / quotes / trend）。给一个正式名，
# 免得调用方去 import 私有名。
ov_line = _ov_line

# 隔夜数据的**进程内**短缓存。analyze 一轮里 overseas() 会被调两次
# （build_brief 一次、AI 失败时的兜底模板一次），而它是三源并发网络请求，
# 实测 0.22~0.49s —— 第二次纯属白跑，且结果必然与第一次相同（同一轮内
# 隔夜数据不会变）。
#
# ⚠️ 只做**进程内**缓存，不落盘：analyze 每轮都是新进程（launchd 每 5 分钟
# 拉起一次），所以这个缓存的生命周期恰好就是"一轮"，正是想要的粒度。
# 落盘反而有害 —— 隔夜数据在开盘后会变（美股盘后、次日亚太开盘），
# 跨轮复用会拿到过期值。相关：[[market-watch-proc-local-cache-resets]]
_OV_MEM: list = []          # [时间戳, 结果]；空列表 = 未缓存
_OV_MEM_TTL = 120           # 秒：远大于单轮耗时，又远小于 300s 的轮间隔

def overseas():
    """隔夜外盘。返回 [(名称, 涨跌幅%|收益率%)]，全失败返回空列表。

    三源并发（腾讯/东财/新浪），任一失败不影响其余 —— 各自独立降级。
    实测单源 0.10~0.38s，并发后整体≈最慢的那个；串行最坏会是 3×8=24s，
    而 analyze 轮次 p90 仅 2.5s，故必须并发。
    """
    # 进程内缓存命中：同一轮的第二次调用直接复用（见 _OV_MEM 注释）
    if _OV_MEM and time.time() - _OV_MEM[0] < _OV_MEM_TTL:
        return _OV_MEM[1]

    import threading
    res = {}
    def _run(key, fn):
        try: res[key] = fn()
        except Exception: res[key] = []
    # ⚠️ `vix` 与 `fx` 同为新浪源，但**必须各起一个线程**而不是合并：
    # 合并成一次 curl 会把"VIX 拿不到"与"汇率拿不到"绑成一件事，任一失败
    # 两个维度一起没了 —— 而它们本来就是独立降级的（见 _ov_sina_vix 的注释）。
    ts = [threading.Thread(target=_run, args=(k, f), daemon=True)
          for k, f in (("tc", _ov_tencent), ("tr", _ov_treasury),
                       ("fx", _ov_fx), ("vix", _ov_sina_vix))]
    for t in ts: t.start()
    for t in ts: t.join(timeout=_OV_TIMEOUT + 3)
    out = []
    for k in ("tc", "tr", "fx", "vix"):
        out.extend(res.get(k) or [])
    # ⚠️ 只缓存**非空**结果。全源失败时缓存空列表，会让同一轮内的第二次
    # 调用拿不到"重试机会" —— 而网络抖动正是最需要重试的情形。
    # 空结果不缓存，代价是失败时多跑一次（本来也没数据可省）。
    if out:
        _OV_MEM[:] = [time.time(), out]
    return out


# ---------- 软降级层（财经常用语，误伤代价高于泄漏代价）----------
# 这些词在**市场描述**里是标准用语，却在**持仓描述**里是泄漏：
#   机构维持持有评级 / 北向资金持有该板块比例上升 / 股东持有比例上升
#   ETF 份额增加 / 基金重仓股走强 / 该板块已清库存
# 原先它们在 BANNED 里 -> 整条拦截 -> 实测 8 个正常快讯标题里 **7 个**
# 导致**整条紧急推送丢失**（build_urgent 的输出被 _safe 拦下，
# 而 _strip_fh 只剔除基金公司名，救不回来）。紧急推送是最不该漏的一类。
#
# 故下沉到"软降级"：命中则剔除**含该词的句子**再推，而不是整条丢弃。
# 若剔除后仍残留（说明该词出现在关键句里），才由调用方整条拦截。
SOFT = [
 "持有", "份额", "重仓", "已清", "仓位", "成本", "收益", "盈亏",
 "浮盈", "浮亏", "亏损", "本金", "投入", "落袋", "兑现", "底仓",
 "待提交", "账户",
]

def strip_soft(t):
    """剔除含 SOFT 词的句子（保留其余内容）。与 strip_fund_houses 同构。

    用途：紧急推送降级 —— 快讯标题里的"持有/份额"是财经标准用语，
    整条丢弃会导致真异动漏报（实测 7/8 个正常标题会触发丢失）。
    """
    if not t: return t
    out = []
    for seg in re.split(r"(?<=[。；;！!\n])", t):
        # 用 _hits 而非裸子串：加粗/空格拆断的软词同样要剔句（见 _mask 注释）
        # ⚠️ 判据看的是 `_yield_neutral(seg)`，**落盘的仍是原文 seg**
        # （2026-10-30）：中性化只用于「这一句算不算软词命中」，
        # 直接返中性化文本会把「美债收益率」悄悄改写成「美债利率」——
        # 那是内容篡改，比剔句更难察觉。
        if _hits(_yield_neutral(seg), SOFT): continue
        out.append(seg)
    r = "".join(out).strip()
    return r

def strip_fund_houses(t):
    """剔除含基金公司名的句子（保留其余内容）。

    用于紧急推送降级：快讯标题里出现"广发证券"这类机构名时，
    剔除该句而不是整条丢弃，避免真异动被误废（漏报）。
    """
    if not t: return t
    out = []
    for seg in re.split(r"(?<=[。；;！!\n])", t):
        # 用 _hits 而非裸子串：`"华夏" in "**华夏**全球科技"` 为假，
        # 该句会被漏剔 -> 降级链失效 -> 整条推送丢弃（见 _mask 注释）
        if _hits(seg, FUND_HOUSES): continue
        out.append(seg)
    r = "".join(out).strip()
    return r

# ---------- 群聊安全 ----------
# 基金公司名：这些是财经快讯里的高频机构名（如"广发证券研报"），
# 整条拦截会导致紧急推送被误废（漏报）。改为"剔除含该词的句子"。
FUND_HOUSES = [
 "汇添富","华夏","南方医药","富荣","富国","广发","永赢","东方阿尔法",
]

BANNED = [
 # 用户持仓的基金名（含简称，防绕过）—— 这些必须整条拦截
 "恒生科技ETF联接","华夏全球科技先锋","华夏全球科技","华夏科创",
 "南方医药保健",
 # 基金公司名：同时出现在 FUND_HOUSES 里供"剔除句子"降级。
 # 放在这里是为了兜底 —— 若降级后仍残留（如单独成句），必须拦截。
 "汇添富","富荣","富国","广发","永赢","东方阿尔法","南方基金","华夏基金",
 "港股通互联网","中证传媒","先锋半导体","科技智选","指数增强","全球精选",
 "沪深300指数增强","科创50指数增强","全球科技先锋",
 # 身份/账户
 # ⚠️ 「收益率」已从词表移出，改走 `YIELD_RE` 语境判据（2026-10-30 修）：
 # 债券口径的「美债10年期收益率」是**市场数据**，裸词表分不开两者 ——
 # `[实测]` 2026-10-23 09:31 整条 AI 正文因它被判「AI 输出不可用(收益率)」
 # 退兜底模板；10-08 09:20 另有一次无谓重写。见 YIELD_RE 处的判据说明。
 "我的","你的","本人","账户","成本价","净值","块钱",
 # 仓位状态
 "已无","没有仓","清仓","未持有","空仓","满仓","持仓",
 # ⚠️ 半仓（2026-10-30 补，实测放行）：它是**仓位状态**，与满仓/空仓同族。
 # 繁体形同理（AI 偶尔输出繁体，`減倉`/`倉位` 实测全部放行）。
 "半仓","半倉","清倉","空倉","滿倉","持倉","減倉","減仓","减倉","加倉","建倉",
 "調倉","補倉","平倉","倉位",
 # 计划内部表述
 "减约","计划目标","降至","敞口",
]
# 计划编号。prompt 规则 4 写的是「也不要写"第N周"这类计划内部编号」，
# 措辞是「第N周**这类**」—— 覆盖的是整个「第N<单位>」编号族，不是只有「周」。
# ⚠️ 原模式 `第\s*[0-9一二三]\s*周` 只认「周」（2026-10-11 审查）：
#   「第2天」「第 3 阶段」「第4项」「第2轮」「第2步」**全部漏网**，
#   而它们与「第2周」暴露的是同一件事（发送者有一份带编号的内部计划）。
#   实测：`safe('本轮第2天，注意节奏')` -> 放行。
# 改法：单位改成**枚举**而不是 `周` —— 用枚举而非 `\w+` 是为了不误伤
# 「第二季度」「第3名」这类正常表述（它们是市场语言，不是计划编号）。
# ⚠️ 数字位保留 `[0-9一二三四五六七八九十]`：原先只有 `[0-9一二三]`，
#   「第4周」「第 5 周」都漏 —— 而那正是最可能出现的编号。
# ⚠️ 2026-10-26：「日/天」从单位枚举里**拆出来单判**（修两条生产误拦）。
#   `[10-22 09:19:12] 重写：命中禁用词 '第四日'` ——「科创50 1330~1345 第四日
#   站稳」的「第四日」是市场语言（连续第 4 个交易日），不是计划编号。
#   历史 195 条推送里 `第N<单位>` 只命中过这 1 次且为假阳性、真泄漏 0 次，
#   plan.md 里也没有「第N日/天」的写法 —— 但「日/天」不能直接删：
#   「计划第3日」这类真泄漏仍要拦。
#   判据（实测 11/11 全对）：
#     · 阿拉伯数字「第3日/天」→ 编号感强，直接拦；
#     · 中文数字「第三日/天」→ 仅当**同子句**（前后 12 字内、不跨句读符）
#       出现计划语境词（计划|方案|安排|节奏|步调|本轮）才拦 ——
#       「第四日站稳」「连续第三日收在均线上」放行，「计划第三日减仓」
#       「本轮第二天注意节奏」拦截。
PLAN_DAY_CTX = r"(?:计划|方案|安排|节奏|步调|本轮)"
PLAN_RE = re.compile(
    r"第\s*[0-9０-９一二三四五六七八九十]{1,3}\s*"
    r"(?:周|阶段|项|步|轮|批|次|期|回合|部分|章节)")
PLAN_DAY_RE = re.compile(
    r"第\s*[0-9０-９]{1,3}\s*[日天]"
    r"|" + PLAN_DAY_CTX + r"[^，。；！？]{0,12}第\s*[一二三四五六七八九十]{1,3}\s*[日天]"
    r"|第\s*[一二三四五六七八九十]{1,3}\s*[日天][^，。；！？]{0,12}" + PLAN_DAY_CTX)

# ---------- 词表匹配的**规范化**（防"拆字"绕过）----------
# ⚠️ 为什么必须有这一层：safe() 与 strip_fund_houses()/strip_soft() 原先全是
# **裸子串匹配** `w in t`，而推送正文是 **Markdown**、且**逐字透传外部快讯标题**
# （news.py 的 title 字段直接来自东方财富 JSON）。于是只要在词里插一个字符，
# 匹配就断掉 —— 实测（2026-09-27，MW_DIR 沙箱，走完 analyze.push() 的整条降级链）：
#     '【异动】华夏全球科技 领涨 +3.2%'      -> BLOCKED       ✅
#     '【异动】**华夏**全球科技 领涨 +3.2%'   -> 放行原文 ❌  ← 加粗左半
#     '【异动】华夏**全球科技** 领涨 +3.2%'   -> 放行原文 ❌  ← 加粗右半
#     '【异动】华夏 全球科技 领涨 +3.2%'      -> 放行原文 ❌  ← 插空格
#     '【异动】华夏·全球科技 领涨 +3.2%'      -> 放行原文 ❌  ← 插间隔号
#     '【异动】恒生科技**ETF**联接 领涨'      -> 放行原文 ❌
# 注意降级链**救不回来**：strip_fund_houses 用的是同一套裸子串判据，
# `"华夏" in "**华夏**全球科技"` 为假 -> 该句不被剔除 -> 再次过闸门仍被拦 -> 无候选可用。
# 也就是说词表里最要紧的那批（用户真实持仓的基金名）**只需一个 `*` 就能上线**。
#
# 修法：把参与匹配的文本先剥掉 markdown 强调符与全部空白再比对。
# ⚠️ 剥空白**不会**把两个无关词粘成禁词：中文禁词本身不含空格，而
# "A B" 型拼接要求 A 恰好以某禁词结尾、B 恰好以某禁词开头，中文里不存在
# 这样的单字禁词。已用两批真实语料量化误伤（见下方 selfcheck 项）。
# ⚠️ 这是一份**手写白名单**，只覆盖了 11 个字符。而 Unicode 里
# 「视觉上不可见但能切断子串」的字符远不止这些 —— 仅 Cf（格式控制）类
# 就有 170 个。实测（MW_DIR 沙箱，穷举 Unicode 全码位）：
#
#   禁词「华夏全球科技先锋」，在**每字之间**插入分隔符：
#     '*'   U+002A  -> 拦（在名单里）
#     '\u3000' U+3000  -> 拦（在名单里）
#     '\u200b' U+200B  ZERO WIDTH SPACE      -> **放行** ❌
#     '\u200c' U+200C  ZWNJ                  -> **放行** ❌
#     '\u2060' U+2060  WORD JOINER           -> **放行** ❌
#     '\ufeff' U+FEFF  ZWNBSP/BOM            -> **放行** ❌
#     '\u00ad' U+00AD  SOFT HYPHEN           -> **放行** ❌
#     '\u00a0' U+00A0  NO-BREAK SPACE        -> **放行** ❌
#     '\u2028' U+2028  LINE SEPARATOR        -> **放行** ❌
#   共 **170/170** 个 Cf 字符全部可绕过。端到端走 analyze.push() 确认放行。
#
# 危害面：本系统是**群推送** —— 词表里最要紧的正是用户真实持仓的基金名
# （'华夏全球科技' / '恒生科技ETF联接' …）。一个零宽字符就让它显示在群里，
# 而闸门判断放行，读者视觉上看到的就是那个基金名。
#
# 为什么自检没能发现：那 22 条「拆字绕过」用例用到的分隔字符**全部落在
# 本名单之内** —— 用例是照着这份名单写的，等于在复述实现。
#
# 修法：不再枚举字符，改按 **Unicode 类别**判定。要剥的是三类：
#   · Cf  l **格式控制**（零宽、方向控制、WORD JOINER…）—— 不可见，纯绕过
#   · Zs  **空格分隔符**（含 U+00A0 / U+2009 / U+202F 等非 ASCII 空格）
#   · Zl/Zp **行/段分隔符**（U+2028/2029）
# 这三类是"视觉不可见或纯空白"的完整集合，按类别取值不会漏。
# 另需保留原有的 markdown 强调符与 ASCII 空白（它们不是 Cf/Zs 类）。
#
# ⚠️ 为什么不用 `str.isspace()`：它也返回 True 的 U+001C-U+001F 属 Cc 类，
# 剥掉它们没有额外收益却扩大了影响面；而 `isspace()` **不覆盖 Cf**
# —— 零宽空格 U+200B 的 isspace() 为 False，正是最要紧的那个漏网者。
_MASK_MARKDOWN = "*_~`"
# ⚠️ 原白名单里的 `\u00b7`(·) 与 `\u2022`(•) 都是 **Po** 类，不在下面三类里。
# 它们是中文里常见的"词内分隔号"（"华夏·全球科技"），必须保留。
# 实测：只按 Cf/Zs/Zl/Zp 判定会把这两个从"拦"退回"漏" —— 那是**回归**，
# 修安全漏洞时不能顺手削掉原有的覆盖。
_MASK_CHARS_EXTRA = ("\u00b7\u2022"        # \u00b7 \u2022 \u539f\u6709\u7684\u4e24\u4e2a\u8bcd\u5185\u5206\u9694\u53f7
                     # \u26a0\ufe0f Po \u7c7b\u300c\u8bcd\u5185\u5206\u9694\u53f7\u300d\u65cf\uff082026-10-11 \u5ba1\u67e5\u8865\uff09\u3002
                     # \u539f\u5148\u53ea\u624b\u5de5\u5217\u4e86\u4e0a\u9762\u4e24\u4e2a\uff0c\u800c\u540c\u8bed\u4e49\u7684\u8fd8\u6709\u4e00\u6279 \u2014\u2014 \u5b9e\u6d4b
                     # \u5168\u90e8\u80fd\u8ba9 safe() \u7684\u8bcd\u8868\u5339\u914d\u65ad\u88c2\uff08\u5728\u57fa\u91d1\u540d\u4e2d\u95f4\u63d2\u4e00\u4e2a
                     # \u5b57\u7b26\u5373\u7ed5\u8fc7\uff09\uff1a
                     #   U+30FB \u30fb \u300c\u534e\u590f\u30fb\u5168\u7403\u79d1\u6280\u300d -> \u653e\u884c \u274c
                     #   U+FF65 \uff65 \u300c\u534e\u590f\uff65\u5168\u7403\u79d1\u6280\u300d -> \u653e\u884c \u274c
                     #   U+2027 \u2027 \u300c\u534e\u590f\u2027\u5168\u7403\u79d1\u6280\u300d -> \u653e\u884c \u274c
                     #   U+FE52 \ufe52 \u300c\u534e\u590f\ufe52\u5168\u7403\u79d1\u6280\u300d -> \u653e\u884c \u274c
                     #   U+FF0E \uff0e \u300c\u534e\u590f\uff0e\u5168\u7403\u79d1\u6280\u300d -> \u653e\u884c \u274c
                     # \u6b63\u5219\u4fa7\uff08AMT_RE \u7b49\uff09\u4e0d\u8986\u76d6\u57fa\u91d1\u540d\uff0c\u6240\u4ee5\u8bcd\u8868\u4fa7\u4e00\u65ad\u5c31\u662f
                     # \u771f\u6cc4\u6f0f\u3002\u4e0e\u300c\u53ea\u5265\u624b\u5199\u767d\u540d\u5355\u3001170 \u4e2a Cf \u5168\u7ed5\u8fc7\u300d\u540c\u578b\u3002
                     #
                     # \u26a0\ufe0f \u4e0d\u80fd\u628a Po **\u6574\u7c7b**\u52a0\u8fdb _MASK_UNICODE_CATS\uff1aPo \u542b
                     # \u53e5\u53f7/\u9017\u53f7/\u95ee\u53f7/\u5f15\u53f7/\u4e66\u540d\u53f7\u7b49**\u771f\u6807\u70b9**\uff0c\u6574\u7c7b\u5265\u4f1a\u8ba9
                     # \u300c\u79d1\u6280\uff0c\u534a\u5bfc\u4f53\u300d\u5224\u6210\u300c\u79d1\u6280\u534a\u5bfc\u4f53\u300d\uff0c\u8de8\u8bcd\u8bef\u4f24\u9762\u8fc7\u5927\u3002
                     # \u6545\u6309 Unicode **\u540d\u5b57**\u9010\u4e2a\u5217 \u2014\u2014 \u540d\u5b57\u91cc\u542b FULL STOP /
                     # MIDDLE DOT / HYPHENATION POINT \u7684\u624d\u662f\u8fd9\u4e00\u65cf\u3002
                     #
                     # \u26a0\ufe0f \u5168\u89d2\u53e5\u53f7 U+3002\uff08\u3002\uff09\u4e0e\u534a\u89d2 U+FF61\uff08\uff61\uff09**\u4e0d\u5728\u6b64\u5217**\uff1a
                     # \u5b83\u4eec\u662f\u53e5\u672b\u70b9\u53f7\uff0c\u51fa\u73b0\u5728\u8bcd\u4e2d\u95f4\u672c\u8eab\u5c31\u662f\u75c5\u53e5\uff1b\u5265\u6389\u53cd\u800c\u4f1a\u8ba9
                     # \u300c\u79d1\u6280\u3002\u534a\u5bfc\u4f53\u300d\u4e0e\u300c\u79d1\u6280\u534a\u5bfc\u4f53\u300d\u540c\u4e32\u3002\u8fd9\u91cc\u8981\u5835\u7684\u662f
                     # "\u8bcd**\u5185\u90e8**\u7684\u5206\u9694\u53f7"\uff0c\u5373 CJK \u91cc\u5199\u5916\u6765\u540d/\u590d\u5408\u540d\u7684\u5199\u6cd5\u3002
                     "\u2024\u2025\u2027\u2056"   # \u70b9\u53f7\u65cf
                     "\u2058\u2059\u205a\u205b"
                     "\u30fb\ua4ff\ua60e\ua6f3"   # \u5404\u7c7b\u4e2d\u70b9
                     "\ufe12\ufe30\ufe52\uff0e"   # \u5168\u89d2\u53e5\u70b9\u65cf
                     "\uff65"
                     # 2026-10-14 \u5ba1\u67e5\u5b9e\u6d4b\uff1a\u5168\u89d2\u659c\u6760 U+FF0F\uff08FULLWIDTH SOLIDUS\uff09
                     # \u662f Po \u7c7b\u4f46\u4e0d\u5728\u4e0a\u9762\u4efb\u4f55\u4e00\u65cf \u2014\u2014
                     # safe('\u534e\u590f\uff0f\u5168\u7403\u79d1\u6280') \u653e\u884c \u274c\u3002\u6309\u540d\u5b57\u8865\u5165\u3002
                     "\uff0f"
                     # 2026-10-15 \u590d\u6838\u5b9e\u9524\uff1a\u534a\u89d2\u659c\u6760 '/' \u4e0e U+FF0F \u540c\u65cf\u6f0f\u8865 \u2014\u2014
                     # safe('\u534e\u590f/\u5168\u7403\u79d1\u6280')\u3001safe('\u6e05/\u4ed3') \u653e\u884c \u274c\u3002
                     # \u4e0d\u7834\u574f\u65e5\u671f/\u6bd4\u4f8b\uff1a'9/30'\u3001'\u51cf 1/3' \u7684\u5224\u5b9a\u8d70 STATE_RE/\u65e5\u671f
                     # \u8bed\u5883\u5728**\u539f\u6587**\u4e0a\u5148\u547d\u4e2d\uff0c\u539f\u6587\u5339\u914d\u4f18\u5148\u4e8e\u89c4\u8303\u5316\u5339\u914d\u3002
                     "/")
_MASK_UNICODE_CATS = ("Cf", "Zs", "Zl", "Zp", "Mn", "Mc", "Cc", "Me",
                      "Sk", "Lm", "Pd", "Sm",
                      # ⚠️ 2026-10-16 复核实测：Cs（代理码点）也必须剥 ——
                      # 它此前不在任何一份名单里，且 `_mask` 对它是**空操作**
                      # （不是漏剥某一类，是这个码位根本通不过编码）：
                      #   · `safe('持\ud800仓')` -> (True, None) 放行（BANNED 拆断）
                      #   · `json.dumps(..., ensure_ascii=False)` **成功**
                      #   · 但 `analyze.push()` 的 `_body.encode("utf-8")`
                      #     抛 `UnicodeEncodeError: surrogates not allowed`
                      #     -> 被 `_boot()` 捕成 [BOOT] 日志 -> `push()` 不返回
                      #     -> `_rl.push()` 从不调用（rate.json 无痕）-> 批次
                      #     回滚后**下一轮重试同一内容**（AI 复现同句则循环）。
                      # 剥掉后这些字符在词表匹配前消失；正文里若真出现，
                      # 也由 push() 的既有截断/编码路径处理。
                      "Cs")
# ⚠️ 2026-10-14 全维度审查实测（P0）：Sk/Lm/Pd/Sm 四类同样能拆断词表匹配 —
# `safe('清ˊ仓')`（U+02CA 修饰字母，Sk）、`safe('华夏×全球科技')`（×，Sm）、
# `safe('华夏—全球科技')`（—，Pd）、`safe('华夏／全球科技')`（／，Sm）全部
# 放行。这四类同为「附着/连接不承载语义」族：Sk/Lm 是修饰符与修饰字母
# （约 130+ 码位），Pd 是各类连字符，Sm 是数学符号。整类剥安全：
# 它们出现在中文词间只有拆字一种用途；对数字的影响与 '.' 同判 ——
# AMT_RE 等正则跑在**原文**上，原文匹配优先于规范化匹配，金额判定不受影响。
# 误伤面审查：'-'/'+'/'×' 在中文正文里的合法用法（"风险 -11.2%"、
# "AI+医疗"）只影响词表匹配的拼接，词表全是纯中文词，拼接不产生假命中。
# ⚠️ 2026-10-08 安全审查实测：只剥 Cf/Zs/Zl/Zp 时，在禁词中插一个 **Mn** 类
# 字符（U+FE0F 变体选择符 / U+034F 组合用连接符）就能让子串匹配断裂 ——
# `safe('恒生科技' + U+FE0F + 'ETF联接')`、`safe('5' + U+034F + '万')` 均放行，
# 金额正则 AMT_RE 同根因可绕过。Mn/Mc（组合字符）与 Cc（控制符，\t\r\n 已由
# 空白判据剥掉）都不承载语义，整类剥是安全的。
# ⚠️ 2026-10-14 九维度审查实测：**Me 类（Enclosing Mark）同样全绕**——
# 全码位穷举 13 个 Me 字符（U+0488/U+0489/U+1ABE/U+20DD~U+20E4/U+A670~A672）
# 插入「恒生科技҈ETF联接」内部，`safe()` 全部放行。Me 与 Mn/Mc 同为不承载
# 语义的附着类（包围前一字符的封装标记），整类剥安全（仅 ~40 码位，无误伤面）。
# ⚠️ **不能**把 Lo/So 整类加进来：Lo 包含**全部 CJK 汉字**（'中' 就是 Lo），
# So 包含 emoji。视觉不可见的"填充字符"只能按码点逐个列 —— 见 _MASK_INVISIBLE。
_MASK_INVISIBLE = "ᅟᅠㅤﾠ⠀"


# 繁→简（仅限金额/口径闸门需要的字）2026-11-09 新增。
_T2S = {
    '萬': '万','塊': '块','帳': '账','賬': '账','淨': '净','賺': '赚',
    '虧': '亏','財': '财','錢': '钱','儲': '储','種': '种','級': '级',
    '劃': '划','歷': '历','將': '将','對': '对','後': '后','於': '于',
    '畢': '毕',
}

def _mask(t):
    """剥掉 markdown 强调符、空白与不可见控制字符，用于**词表匹配**。

    仅用于"这个词在不在"的判断；任何面向用户的输出都必须用原文，
    否则会破坏 Markdown 排版（`**触发**` 被剥成 `触发`）。

    判据有两条，任一命中即剥：
      · 在 `_MASK_MARKDOWN` 里（`* _ ~ ` ` 四个 markdown 强调符）
      · 在 ASCII 空白里（空格 / \t / \r / \n）
      · 在 `_MASK_CHARS_EXTRA` 里（`·`/`•` + Po 类词内分隔号族，见其注释）
      · `unicodedata.category(ch)` ∈ (Cf, Zs, Zl, Zp, Mn, Mc, Cc)
        —— 覆盖全部 170 个零宽/格式控制字符与各类 Unicode 空格

    ⚠️ 不能用"剥掉一切非中文/非字母"的粗暴做法：那会把标点和数字一起
    剥掉，让「风险 -11.2%」与「风险11.2」被判为同一串，反而制造跨词误伤。
    按类别剥是精确的 —— Cf/Zs/Zl/Zp 都不承载语义。
    """
    if not t:
        return t
    # ⚠⚠ 2026-11-09（复核）：**繁→简归一**。
    # `BANNED` 当年只给「仓」族补了繁体，萬/塊/帳/淨/賺/虧 一个没补 ——
    # `[实测]` 「賺了 8 萬」「留 5000 塊」「帳上還有 12000」全部放行。
    # 只归一与金额/口径直接相关的字（`_T2S`），不做通用繁简转换：
    # 通用转换会把「台灣中央銀行」这类正常新闻标题也改掉。
    # ⚠️ 只用于词表判定（与原有的 markdown 剥离同一层语义），落盘内容始终用原文。
    if any(c in _T2S for c in t):
        t = "".join(_T2S.get(c, c) for c in t)
    return "".join(
        ch for ch in t
        if ch not in _MASK_MARKDOWN
        and ch not in _MASK_CHARS_EXTRA
        # ⚠️ ASCII 句点 '.' 也剥（2026-10-11 审查）：它是 Po 类里
        # 唯一**不在**上面按名字列出的那一族里的 —— 因为 '.' 的
        # Unicode 名是 FULL STOP，而全角句号 U+3002 也叫
        # IDEOGRAPHIC FULL STOP，按名字列会把两者混在一起。
        # 但 '.' 在中文正文里几乎只出现在**词内部**（小数点/版本号
        # 都有专门的数字语境），而它出现在两个中文词之间时正是
        # 「华夏.全球科技」这种拆字写法。实测：
        #   safe('减仓华夏.全球科技') -> 放行 ❌（修复前）
        # 剥掉 '.' 后 -> 拦下 ✅
        # ⚠️ 不误伤：'风险 -11.2%' 里的 '.' 两侧是数字，剥掉后
        # 变成 '风险 -112%' —— 但 AMT_RE 的 \d[\d,]*(?:\.\d+)?
        # 在**原文**上仍命中 '11.2%'，而原文匹配优先于规范化匹配，
        # 所以金额判定不受影响。
        and ch != '.'
        and ch not in _MASK_INVISIBLE
        and ch not in " \t\r\n"
        and unicodedata.category(ch) not in _MASK_UNICODE_CATS
    )


def _hits(t, words):
    """在原文**和**规范化文本里找命中词。返回命中的词，无则 None。

    两条判据都跑：原文匹配保证不误伤（原文命中即拦，行为与修复前一致），
    规范化匹配堵住拆字绕过。返回的命中词优先取原文里的那一个，
    便于日志排查"到底是哪个词"。
    """
    for w in words:
        if w in t:
            return w
    m = _mask(t)
    if m != t:
        for w in words:
            if w in m:
                return w
    return None


# 人称词。用户明确要求「不用你/您/我/本人等人称，用中性表述」——
# 这条面向多人微信群：出现「建议你减仓」会让每个读者以为在对自己说，
# 而系统并不认识任何人的仓位。
#
# ⚠️ 原先**只拦了「本人」和「你的」**，裸的「建议你减仓」「请您关注」
# 「我建议」**全部放行** —— 实测 2026-09-24 验证时才发现闸门是空的。
# 历史推送恰好零命中（AI 遵守得不错），但闸门的意义正是在 AI 不遵守时兜住。
#
# 为什么「我」「我们」也拦：市场播报是客观陈述，不代表任何群体，
# 没有正当用途。实测历史推送里 你/您/我/我们 出现次数均为 0，不会误伤。
# 「自我」是市场描述里的常见词（自我修复、自我强化），必须排除，
# 否则「市场自我修复」会被误判成人称句而触发一次无谓的 AI 重写。
# 「我武」是真实股票名（我武生物）。PRON_RE 原先只排除了「自我」，
# 于是「我武生物涨停」这类**正常财经快讯**会被判成人称句而整条拦下。
# ⚠️ 「我国/我司/我省/我市/我县/我区/我方/我军/我党/我校/我院/我厂/
#    我局/我部/我委」是**机构自称**，不是个人持仓表述（2026-10-30 修）。
# 原先只排除「们/武」两字，实测 `safe('我国央行降准')` 返回
# (False,'我') —— 一次假阳性触发一轮完整 AI 重写，重写还撞词就退兜底模板。
# 判据是"我 + 机构单位字"，与"我"单独使用（我建议/我已减仓）互不重叠。
PRON_RE = re.compile(
    r"[你您]|我们"
    r"|(?<![自])我(?![们武国司省市区县军党校院厂局部委方])")

_CN_NUM = "零一二三四五六七八九十百千万两壹贰叁肆伍陆柒捌玖拾佰仟"
# 只拦"个人金额"表述，不拦"亿" —— 亿是市场数据单位
# （成交额 2505 亿、主力净流入 28.31 亿），推送里必须能写。
#
# ⚠️ 原先只认「阿拉伯数字 + 元/块/万」，实测这些**全部放行**：
#   投入五万 / 投入三万 / 投入5w / 投入5W / 投入5k / 投入$5000 / 投入¥5000 / 投入六位数
# 中文数字、w/k 缩写、货币符号都是自然的持仓表述方式，只堵阿拉伯数字等于没堵。
# ⚠️ 数字部分必须是 `\d[\d,]*`（**至少一位数字**），不能写 `[\d,]+` ——
# 后者允许逗号**单独**成立，于是英文正文里的 `, while` / `, with` 会被
# 匹配成金额 `, w`（`, ` 当千分位、`w` 当"万"缩写）。
# `[实测]` 2026-10-08 14:03 生产轮：AI 正文被 `safe()` 判「命中禁用词 ', w'」
# → 触发一次**无谓重写** → 重写那次调用又撞上 CLI 符号链接瞬断（见 claude_bin）
# → 整条推送退化成兜底模板。**一个假阳性丢掉了前一次 384s 跑出来的可用正文。**
# 拆成两条：CJK 单位保留千分位逗号（"1,234 元"），w/k 缩写不允许 ——
# "5w" 这种写法永远不带千分位，而 "3, while" 里的逗号正是假阳性来源。
# ⚠️⚠️ 「元/块」后必须跟**金额后缀或非汉字**（2026-10-26 生产 P0 修）。
#
# 根因是 `_mask()` 的规范化拼接：它剥掉空格与 `**` 后，**相邻两行会拼在一起**，
# 于是「今日跌幅 TOP3」的 `3` 与下一行板块名「元件」拼成 `3元件`
# → 被 `\d+元` 分支判成金额泄漏。
#
# `[实测]` 2026-10-26 生产连续 5 轮推送失败，用户当天下午完全没收到播报：
#   [13:39:30] 重写：命中禁用词 '3元'
#   [13:41:19] BLOCKED hit='3元' :: '> **午后开局 · 整点播报 13:30**…'
#   [13:52:46] BLOCKED hit='3元'（第二轮）
#   [14:02:21] BLOCKED hit='3元'（第三轮，13:30 槽位当天彻底作废）
#   [14:25:46] BLOCKED hit='3元'（连**事件类**推送也被拦）
# 触发条件只是「跌幅榜里出现『元件』板块」—— A 股常见板块，**每天都会复发**。
#
# 为什么不能靠 `_AMT_CTX` 市场语境排除：它的判据是"命中处之前 12 字内有
# 成交/流入/市值…"，而 `3元件` 前面是「今日跌幅TOP」，一个语境词都没有
# —— 排除不了。必须在正则层面区分「金额的元」与「板块名的元件」。
#
# 判据：`元`/`块` 之后要么是**金额后缀**（以上/左右/多/余/整/内/外/前/后/起），
# 要么**不是汉字**（行尾/标点/空格/斜杠/数字）。「元件」「元宇宙」「元器件」
# 这类词后面跟的是汉字且不在后缀表里 → 不算金额。
# `[实测]` 该判据对 195 条历史推送零误拦（`成交 1.44 万亿` 走「万」分支不受影响）。
# ⚠️⚠️ 2026-11-06（全量复核 P0-1）：右边界判据从「不是汉字」改为
# 「不是**复合成词首字**」。
#
# 原判据 `(?![^\W\d_])` = 「后面不是字母/汉字」。它确实挡住了
# 「3元件」那条生产事故（10-26 连续 5 轮推送失败），但代价是把
# **金额 + 用途**这种中文里最自然的写法整体放过了：
#   safe('另留5000元备用。') -> (True, None)   ← 泄漏
#   safe('用5000元买入。')   -> (True, None)   ← 泄漏
#   safe('现金5000元放着。') -> (True, None)   ← 泄漏
# 加空格反而能拦住（'另留 5000 元 备用' 命中）—— 说明判据抓的是
# 排版巧合，不是语义。这是整个闸门里唯一一条「随便写一句话就漏」的路径。
#
# 修法反过来：只排除**以「元」开头且不是金额**的复合成词（元件/元旦/
# 元素/宇宙/元气/元首/元老/元凶/元宝/元宵/元帅/元组/元神/元字）。
# 这一族是封闭小集合，而「金额后接用途」是开放集合 —— 只能堵前者。
#
# `[实测]` 用 321 条历史推送做回归：新判据命中 0 条（旧判据同样 0 条），
# 零误拦；而上面三条例句全部拦下。见 /tmp/mw_corpus.txt 回归脚本。
_AMT_COMPOUND = "件旦素宇气首老凶宝宵帅组神字器勋谋"
# 上面这串是「以元开头但**不是金额单位**」的复合成词首字：
#   元件/元旦/元素/宇宙/元气/元首/元老/元凶/元宝/元宵/
#   元帅/元组/元神/元字/元器件/元勋
#
# ⚠⚠ 2026-11-06（复核后回退一次错改）：我曾经把这张表扩到「购/买/首/付/退」等字，
# 想用它去拦「5000元购买」—— 方向根本反了。这张表是**排除表**：
# 表里的字会让「X元+该字」**不再被当成金额**。把「备/购/首」加进去，
# 直接把「另留 5000 元备用」「5000 元购买」放行了 —— 实测基线一下子红了 8 条。
# 正确做法：这张表只收**真正的元字头复合词**；「金额+用途」由下面
# AMT_RE 里的「4 位以上数字 + 元/块」硬分支契约兜住（真金额不会是 3 元这种价位量级）。
#
# 取舍：**宁可多拦，不可漏拦**（多拦=多一次重写，漏拦=信息泄露）。
# 但纯误拦同样有代价（10-26 那次事故就是误拦把整个下午的播报废掉了），
# 所以这里只穷举真复合词，不是简单粗暴地「接汉字即不算」。
AMT_RE = re.compile(
    # ⚠⚠ 2026-11-07（复核）：**全角货币符号必须一起收**。
    # 中文输入法下拿到的就是全角：`￥5000`（U+FFE5）、`＄5000`（U+FF04）。
    # 原表只写了半角 `¥`/`$`，于是「￥5000」直接放行 —— 而它比半角
    # 形态**更常见**（输入法默认）。同样补上全角美元。
    # ⚠️ 2026-11-08（复核）：**文字货币代码同样拦**。`USD 5000` / `RMB 5000` /
    # `CNY 5000` 都是人写钱数的常见形态，而原表只收符号。
    # 只收这三个顶层代码：再多就要开始猜了（`HKD`/`JPY` 在推送里
    # 几乎不会代表个人仓位，且外汇报价本来就该走市场语境）。
    # 必须要求**紧跟数字**（可隔空格）：否则「USD 走强」这类
    # 没数字的正常市场表述也会被拦。
    r"(?:[¥$￥＄]\s*\d[\d,]*(?:\.\d+)?)"
    r"|(?:(?i:USD|RMB|CNY)\s*\d[\d,]*(?:\.\d+)?)"
    r"|(?:\d[\d,]*(?:\.\d+)?\s*(?:万元|万|千元|千))(?!亿)(?![" + _AMT_COMPOUND + r"])"
    r"|(?:\d[\d,]*(?:\.\d+)?\s*(?:元|块))(?![" + _AMT_COMPOUND + r"])"
    r"|(?:(?:\d{1,3}(?:,\d{3})+|\d{4,})(?:\.\d+)?\s*(?:元|块|万|千))(?!亿)"
    # ⚠️ 2026-11-08（复核）：**单位被括号/引号包住**的形态。
    # `[实测]` 「另留 5000【元】备用」「留 5000「元」备用」「留 5000(元)备用」
    # 三种全部放行——因为上面每一个分支都要求数字紧贴单位。
    # 这不是疯狂构造：AI 写得带着 markdown 装饰时就是这个形态。
    # ⚠️ 本分支**强制要求带括号**，不放宽上面那两条。只在真的有括号时才命中，
    # 所以市场口径的常见写法不受影响（下面用 323 条真实语料验过）。
    r"|(?:\d[\d,]*(?:\.\d+)?\s*[【\[（(「『]　*\s*(?:元|块|万|千)\s*[】\]）)」』])(?!亿)"
    # ⚠⚠ 2026-11-06（复核后补）：**真金额硬分支**。
    # 「金额 + 用途」（另留 5000 元备用 / 5000 元购买 / 5000 元首付）
    # 是开放集，用词表去枚举必定漏。改用量级契约：
    # **4 位以上的数字跟着 元/块/万/千，无条件当金额**。
    # 依据：股价/板块价位几乎都是 3 位以下（站上 4243.65 / 失守 500 元），
    # 而个人金额常见 4 位以上（另留 5000 元）。两者在量级上自然分开，
    # 不需要靠词表去猜后面跟的是什么字。
    # 市场口径依然可以走上方语境豁免（市值 3000 亿元 → 「市值」在 `_AMT_CTX`）。
    # ⚠️ 2026-11-06（P0-1）：`w/W/k/K`（金额俚语「1.5w」）去掉右边界判据。
    # 原判据要求 w 后不是汉字，于是「另留5w备用」整条漏检（已实测）。
    # 现在只要求 w/k **紧贴数字**（`\s*` 已去掉，故「第 8 w」「5 W 功率」
    # 这类带空格的非金额仍然不命中），后面接什么都不影响判定。
    # 代价：「8w条」这种数量口径也会被当金额 —— 方向是**宁可多拦**，
    # 与合规要求一致（多拦只是多一次重写，漏拦是信息泄漏）。
    r"|(?:\d+(?:\.\d+)?[wWkK])(?![功率瓦条件台个只名次股秒时])"
    # ⚠️ 上面这个字符类是**量词**（瓦=功率、条/件/台/个=数量、秒/时=时长）。
    # 没有它，「5W功率」「8w条」会被当金额拦下（实测确认过）。
    # 有它不影响「另留5w备用」「1.5w现金」—— 备/现都不在量词表里。
    # ⚠⚠ 2026-11-08（复核）：**约数助词会打断连续数字跑**。
    # 原写 `[_CN_NUM]{1,6}` 要求数字**连续**，而「三百**多**万」「二十**余**万」
    # 「五十**几**万」「百**把**万」都拿不到 —— 而它们是极自然的
    # 中文表达。`[实测]` '"账上还有三百多万没动"' 原样放行。
    # 修法：在数字与单位之间允许**一个**约数助词。
    # 只允许一个：两个以上的连续助词不成词，放宽只会增误拦。
    r"|(?:[" + _CN_NUM + r"]{1,6}[多来余几把数]?\s*(?:万元|万|千元|千))(?!亿)"
    r"|(?:[" + _CN_NUM + r"]{1,6}[多来余几把数]?\s*(?:元|块))(?![" + _AMT_COMPOUND + r"])"
)

# `safe()` 里「同子句」判据的句读符集合（2026-11-06，P0-2）。
# ⚠️ 必须含**换行**：prompt 要求「段内每句一行」，换行在真实输出里是常态，
# 不当作子句边界就会让上一行的宏观/报价语境豁免掉下一行的个人金额。
# 半角句点 `.` 与全角 `｡`(U+FF61) / `､`(U+FF64) 同理 —— AI 偶尔会用。
# 省略号 `…`、间隔号 `·` 也一并收（它们同样切句，且不收会串句）。
_AMT_BREAK = "。；;，,、！!？?：:.．\n\r｡､…·"
# 语境词与金额之间是否还夹着**另一笔金额**（P0-3）。出现数字即说明语境
# 属于更早那笔，「豁免」不能顺延到本笔。
_AMT_BETWEEN_RE = re.compile(r"\d|[" + _CN_NUM + r"]")
# ⚠️ 2026-11-06（复核）：只认 `\d` 对**中文数字金额是瞎的**。
# `AMT_RE` 自己有中文数字分支，判据却只按 ASCII 数字去数 ——
# 两条口径不同源。实测修前：`safe("成交额三万亿另留五万备用")`
# 中间挡着一笔「三万亿」却判「没有别的金额」→ 整句豁免。

# AMT_RE 命中后的**市场语境排除**（2026-10-14 审查 P1）：
# 「成交额1.5万亿」「净流入28.31亿」「市值三千亿」「成交480亿」是盘面播报
# 高频句式，全被 AMT_RE 当个人金额拦下 → 每条假阳性触发一次无谓 AI 重写
# （历史：`, w` 假阳性曾毁掉 384s 的正文）。它们与个人金额的分界不是数字
# 大小（「480 亿」个人语境不现实，但正则不该靠数量级猜），而是**前文语境**：
# 命中处之前若出现市场数据动词/名词（成交/流入/流出/市值/净买/净卖/换手/
# 营收/规模/净值/GDP/进出口/储备），该数字是市场数据，放行。
# 负向后视不可行（数字前字符不定长，如「主力净流入28.31亿」隔 4 字），
# 故做成命中后的二次扫描：在**命中起点之前**的 12 字窗口内找语境词。
# ⚠️⚠️ 2026-10-27 补：原表漏掉「融资余额 / 两融 / 全市场 / 缩量 / 放量」
# 这一类盘面播报高频句式，实测全部被当成个人金额拦下：
#   safe('融资余额 1.8 万亿')  -> (False, '1.8 万')
#   safe('两融余额 1.5 万亿')  -> (False, '1.5 万')
#   safe('全市场 1.2 万亿')    -> (False, '1.2 万')
#   safe('缩量至 1.2 万亿')    -> (False, '1.2 万')
# 每一条都白付一轮 AI 重写。
#
# ⚠️ 修法**不是**往这张表里加词 —— 那样会扩大「同子句有市场词就放行
# 个人金额」的既有缺口（实测：「放量后另留 5万」在加词方案下会被放行）。
# 改为在 AMT_RE 的 `万` 分支上加**负向前瞻 `(?!亿)`**：`万` 后紧跟 `亿`
# 就是「万亿」= 市场量级，个人金额不会有这个量级。判据更精确、且不碰
# 语境表。`万` 后不是 `亿` 时行为完全不变（「另留 5万」照拦）。
# 同一处也覆盖了中文数字分支（「一点二万亿」）。
_AMT_CTX = re.compile(
    r"(?:成交额?|流入|流出|市值|净买|净卖|换手|营收|规模|净值|GDP|出口|进口|储备|回购|分红|派息|派现|募资|融资|融券|余额"
    # ⚠️ 2026-11-05（P1，用户实测）：调研 Agent 的播报里「11/6 21:30 美国
    # 10 月非农（预期 6.3 万、前值 2.9 万）」整句被 `_safe_summary` 剔掉
    # —— 群里的报告因此少了明日最重要的经济事件，而这是**宏观数据**，
    # 与"发送者自己的金额"是两回事。补宏观口径语境词（仍然是**同子句**才
    # 豁免，跨句读符照样拦：`非农公布后，另留 5万 备用` 仍会被拦下）。
    # 刻意**不加**裸「预期」—— 它太泛（「预期到手 6.3 万」是个人口径），
    # 而本批真正需要的两处：「非农（预期 …」由「非农」覆盖、「前值 2.9 万」
    # 由「前值」覆盖。
    r"|非农|新增就业|就业人数|失业率|CPI|PPI|社融|赤字|财政|人口|前值|公布值|目标价|EPS|每股收益|每股|股息|股息率|市盈率|市净率|北向|沪股通|深股通|南向|港资|两融|锂价|铜价|铝价|铁矿石|螺纹钢|动力煤|焦炭|原油|商品价|期货|现货|报盘)")

# **逐股/逐指数报价**语境（2026-11-06 加，与 `_AMT_CTX` 分开维护）：
# 「中际旭创 -9% 失守 500 元」「回落至 500 元下方」是公开行情，不是发送者
# 金额 —— 但 `AMT_RE` 的 `元` 分支会命中，`_safe_summary` 于是整句剔除，
# 读者在播报里看不到这只票的关键价位。
# ⚠️ 只收**报价专用动词**：刻意**不收**「跌破/降至/减至/回到」这类
# 与个人账户语义重叠的写法（「账户跌破 5万」就是真泄漏）。个人口径的
# 句子通常还带 BANNED 词（账户/净值/浮亏…），那一道在 AMT_RE **之前**
# 就拦下了，两层叠加才安全。
_AMT_QUOTE_CTX = re.compile(
    r"(?:失守|站上|涨至|跌至|回落至|报收|收报|报于|收于|现报|股价|收盘价)")

# ⚠⚠ 2026-11-06（复核后补）：**报价豁免必须看主语**。
#
# 「失守|跌至|涨至|收于」这批动词在**股价**和**账户余额**上是同一批词：
#   失守 500 元      → 公开行情（应放）
#   余额跌至 5万      → 个人金额（必须拦）
# 原判据只看「12 字内有没有报价动词 + 中间没有别的数字」，不看主语 ——
# `[A/B 实测修前]` `safe("余额跌至 5万")` = (True, None)，同族的
# 总资产/存款/积蓄/工资/年终奖/到手 全部新增放行。
# 而 BANNED 词表里**没有**这批词，所以上方注释说的「两层叠加才安全」
# 在这条路径上第二层压根不存在。
#
# 修法：子句里出现**个人资产名词**就取消报价豁免。
# 市场限定语（融资/两融/北向…）在前 6 字内时视为市场口径——
# 「融资余额 …」是公开数据，不能被这条误杀。
_AMT_PRIV = ("余额", "存款", "积蓄", "工资", "年终奖", "总资产",
             "到手", "零钱", "红包", "年薪", "月薪", "奖金",
             "家底", "现金", "余钱", "攒下", "存下", "剩下",
             # ⚠️ 2026-11-09（复核）：**「融资买入」是个人仓位动作，不是市场口径**。
             # 上方 `_AMT_PRIV_OK` 里有「融资」（为了放行「融资余额 1200 万元」），
             # 于是「融资买入 5万」也吃到了豁免 —— 而那是用自己的钱买股票。
             # `[实测]` 加了裸词「融资」之后，安全闸门基线直接报
             # 「拦截[金额(融资买入 是个人口径)]: 融资买入 5万 <\- 漏网!」
             # ⚠️ 2026-11-09（复核）：**只用复合词，不用裸词**。
             # 第一版写了裸「买入/卖出」，`[A/B 实测]` 立即误拦：
             #   「北向买入 5万元」被当成个人金额（它是公开资金数据）。
             # 原因：「北向」在 `_AMT_PRIV_OK` 里（判“是不是个人钱”用），
             # 不在 `_AMT_CTX`（金额豁免用）—— 两张表各管一件事，混用就误伤。
             "融资买入", "融券卖出", "加仓", "减仓")
_AMT_PRIV_OK = ("融资", "融券", "两融", "北向", "南向",
                "沪股通", "深股通", "市场", "港资", "瓶颈")


def _priv_in_clause(pre: str) -> bool:
    """子句里是否出现了**个人资产名词**（市场限定语除外）。

    纯判据，不改文本。为真＝这句话说的是“我自己的钱”，
    报价语应豁免不得生效。
    """
    # ⚠⚠ 2026-11-08（复核）：必须检**所有**出现位置。
    # 原写 `pre.find(_w)` 只查第一次出现 —— 于是这句话里同时有两个「余额」时：
    #   '融资余额大增，规模创纪录，余额只剩 5000 元'
    # 第一个带「融资」限定语 → `continue` 跳过整个词 → **第二个根本没看**，
    # 返回 False（“不是个人钱”）。目前被三道联合约束兜住（`[实测]`：
    # 12 字窗口把「规模」切出、切句符把句子断开），但那是
    # **守住了**而非**判对了** —— 判据本身没找到第二个词。
    # `_AMT_PRIV_OK` 里的「市场」一旡对否，这里改成逐个出现位置判。
    for _w in _AMT_PRIV:
        _start = 0
        while True:
            _i = pre.find(_w, _start)
            if _i < 0:
                break
            _head = pre[max(0, _i - 6):_i]
            if not any(_q in _head for _q in _AMT_PRIV_OK):
                return True        # 有一处不带市场限定语 → 就是个人钱
            _start = _i + 1
    return False

# 计划窗口用语。prompt 的 deadline 规则里有一段**逐字**的禁令：
#   「措辞必须是市场语言的「观察期/时间节点」，**绝不能出现「本轮窗口」
#     「本窗口」「窗口最后一日」「计划窗口」这类字样** —— 它们会向群里的
#     每个读者暴露「发送者有一份带截止日的内部操作计划」」
# 但代码侧原先**没有对应的闸门** —— 这四种写法里只有「窗口最后一日」
# 恰好被 PLAN_TENSE_RE 的分支命中，另外三种全部漏网。
# 实测（2026-09-27）：
#   "**触发** 今天本轮窗口，注意节奏"  -> ✅放行（漏网）
#   "**触发** 今天本窗口，注意节奏"    -> ✅放行（漏网）
#   "**触发** 今天计划窗口，注意节奏"  -> ✅放行（漏网）
#   "**触发** 今天窗口最后一日…"       -> ❌拦截（唯一被拦下的）
# prompt 是"请求"，闸门是"执行" —— 只有 prompt 禁止而闸门放行时，
# 规则的实际合规率完全取决于 AI 的自觉（历史事故：09-24 13:42 那条
# 「今日本轮窗口最后一日」**已真实投递到群**，见 history/pushes.jsonl）。
#
# ⚠️ 「本窗口」必须排除「日本窗口」—— "日本窗口期" 是正常财经措辞，
# 不能因为含"本窗口"三个字就整条拦下。用负向后视断言排除。
#
# ⚠️ 2026-10-25 全维度审查实测出两个漏网面，已修：
#   ① 数量词只写 `[一]?`，阿拉伯数字可穿：`窗口最后 1 日` / `窗口最后1天`
#      全部放行。改为 `[0-9０-９一二三四五六七八九十]?`。
#   ② 「本周期」完全不在正则里，而 analyze.py 的 deadline 注释却写着
#      「现在 safe() 的 WINDOW_RE 会把这类措辞拦下来（含"本周期"）」——
#      注释声称的防线不存在。`今天本周期最后一天` 实测放行。
#      「本周期」与「本窗口」同性质（暴露发送方有内部周期计划），必须拦。
#      ⚠️ 必须排除「本周期**货**」——「本周期货市场走强」是正常财经措辞
#      （本周 + 期货），整串含"本周期"却不是本义。用负向前瞻 `(?!货)` 排除
#      （实测：加之前 '本周期货市场走强' 被误拦）。
#   实测确认**不会误伤系统自身输出**：`观察期最后一日`（detect 的事件标签、
#   brief 的日期提示）不含上述任一形态，加这两条后仍放行。
WINDOW_RE = re.compile(
    r"本轮窗口|计划窗口|窗口\s*最后\s*[0-9０-９一二三四五六七八九十]?\s*[日天]"
    r"|(?<![日])本窗口|本周期(?!货)"
)

# 敞口/仓位：不限方向（"敞口 61%"、"敞口约 61%"、"敞口偏高" 都要拦）
#
# ⚠️ 原先只认「敞口|仓位|(仍|还|已)(持有|重仓|满仓|空仓)」，实测**成数表述
# 全部放行**：占比约六成 / 占六成 / 减到三成 / 减到四成 / 剩余三成 /
# 减 1/3 / 减三分之一 / 降到四成。「降到四成」被拦纯属巧合 ——
# 命中的是 BANNED 里的「降至」，换成「降到」/「减到」即绕过。
# 成数（几成、几分之几）与百分比一样是仓位表述的核心形式，必须覆盖。
#
# ⚠️⚠️ **`a/b` 的日期误伤**（2026-10-10 修）：四个分支里只有 `a/b` 是
# **歧义**的 —— `成`/`分之`/`位数` 都不出现在日期里，而 `M/D` 形式的日期
# 是正文高频写法，尤其「至 10/7」「回落至 10/7」这类区间表述。
# `[实测]`：
#   「上证自 9/30 至 10/7 累计下跌 2.1%」 -> ❌拦截（命中 '至 10/7'）
#   「恒生科技较 9/29 回落至 10/7 的 4194」-> ❌拦截
# 后果不是"少一条推送"这么轻：`produce_text` 命中后**会再发起一次完整
# AI 调用**去重写（prompt 里还明说"上一版因出现禁用词「至 10/7」被拦截"，
# 把日期当成敏感词喂给模型），重写仍不过就**退化成兜底模板**
# —— 一条正常行情播报因写了日期而丢掉 AI 分析。
#
# 修法：把 `a/b` 单独拆出来，只认**无歧义的仓位限定词**。
# 原先把 `至|到|近|超` 与 `减|降|占|约` 混在同一组里，而 `至`/`到` 恰恰是
# 日期区间（"自 X 至 Y"）最常用的介词 —— 歧义全由它们引入。
# 拆开后：`减/降/占/约/升/增/加/留/剩/投/为/余` 仍带 `a/b`
# （「减 1/3」「占比约 2/3」），而 `至/到` 只带**无歧义**的
# `成|分之|位数`（「减到四成」「降至三分之一」）—— 那三支本就不可能是日期。
# 代价：「加仓至 1/2」这类"`至` + 纯分数"不再被拦。**这个代价是有意选的**：
# 漏一个仓位数只是少拦一句，误拦一个日期会毁掉整条推送（见上）。
# 且实务上 `至/到` 引出的比例几乎总写成「成」或「分之」。
_ST_AMBIG = r"占|约|近|超|减|降|升|增|加|留|剩|用|投|为|余"
_ST_SAFE = r"占|约|近|超|减|降|升|增|加|留|剩|用|投|到|至|为|余"
# ⚠️ 数字类必须含**全角** ０-９（2026-10-30 修，实测绕过）。
# `_STATE_AB_RE` 那支早就有全角（2026-10-14 踩过 `减１/３`），但
# 成/分之/位数三支与「占比+约数」支用的是本常量，仍是半角 ——
# `[实测]` `safe('占比约６成')` 返回 (True, None)，全角一位数字
# 就把"仓位占比"整句放行。中文输入法与 LLM 输出里全角数字都常见，
# 与那次的 `减１/３` 是同一类绕过面，只是当时只修了 a/b 那一支。
_ST_NUM = r"[0-9０-９" + _CN_NUM + r"]"
# `a/b` 分数两侧允许夹 markdown 强调符 / 词内分隔符 / 空白（2026-10-25 修）。
# ⚠️ 这是一处**真实绕过面**，不是洁癖：
#   `_mask()` 把 `/`（U+002F）当"词内分隔号"剥掉（见 _MASK_CHARS_EXTRA），
#   于是 `减 1/3` 规范化后变成 `减13` —— **`a/b` 分支在规范化文本里恒不可达**；
#   而原文侧 `\s*` 只吃空白、不吃 `*`，`减**1/3**` 里斜杠紧邻 `*` 就匹配不上。
#   两个变体都不命中 → 「减**1/3**」直接放行（实测，优化前的备份版同样放行，
#   属存量缺陷）。AI 正文用 `**` 强调是常态，等于一键绕过仓位闸门。
# 修法：把 `\s*` 换成"空白 + markdown 强调符 + 词内分隔符"的字符类，
# 并显式吃全角斜杠 `／`（U+FF0F，原先 `减 1／3` 也放行）。
# ⚠️ 全角斜杠要从分隔符集合里**排除**，否则它会被 SEP 吞掉、`[/／]` 反而匹配不到。
_ST_SEP_CHARS = (" \t\r\n" + _MASK_MARKDOWN
                 + "".join(_MASK_CHARS_EXTRA)
                   .replace("／", "").replace("/", ""))
_ST_SEP = "[" + re.escape(_ST_SEP_CHARS) + "]*"
# 「动作仓词 + 分数」的限定词（2026-10-26 补，实测 4 个漏网形态）：
#   `减仓 1/3`、`建仓 1/4`、`加仓 1/2`、`港股科技减仓 1/3` 全部放行 ——
#   单字「减」被「仓」隔开，`(?:减)\s*1` 接不上。这些词后面直接跟 a/b
#   分数时**必然**是仓位表述（「加仓 1/2」不可能是日期），纯收紧、零歧义。
# ⚠️ 日期形态「近 10/8」不经过这支（近 不在表里，靠数值过滤放行）。
_ST_ACTION = r"减仓|加仓|建仓|清仓|调仓|补仓|平仓|空仓|满仓|重仓"
# 「仓位动词 + 至/到 + 分数」（2026-10-26 补，`调仓至 1/2` 实测放行）：
# 当年（2026-10-10）把 至/到 从限定词里整体摘掉，理由是"误拦日期会毁掉
# 整条推送"——那时没有数值过滤，`至 10/7` 与 `至 1/2` 同拦。现在过滤在：
# 分子≥分母或分母>12 的日期（10/8、10/23、12/31）天然放行，歧义只剩
# M<D≤12 的「月/日」形态。而本支的动词表收窄到**仓位动词**（减/降/留/
# 剩/用/投/加/建/清/调/补/平），「涨至 1/5」「收在 1/5」不在表里照常
# 放行；「减至 1/5」读成"减到1月5日"语义不通（"减"不带日期宾语），
# 拦是对的。故当年"有意选的代价"在动词收窄 + 数值过滤之后不再成立。
_ST_VERB_TO = (r"(?:减|降|留|剩|用|投|加|建|清|调|补|平)仓?"
               r"\s*[至到]\s*")
# 「成/分之/位数」三支的**仓语境**要求（2026-10-14，09:19 实测）：
# `约\s*数字成` 会命中「涨跌家数**约两成**」「成交额**约八成**来自科技」
# 这类纯市场约数 —— 每次假阳性触发一次完整 AI 重写（重写又撞词就丢整轮
# AI 退兜底），09:25 盘前简报实测多付 30s。修法：三支加**子句级前瞻**，
# 要求同子句（按 ，。；！？切分）内出现 仓|敞口 才拦 —— 仓位分数几乎
# 总与仓位词同句（「仓位约三成」「敞口约六成」「底仓约五成」），
# 而市场约数不带仓词。真泄漏里唯一无仓字的形态是「占比 + 数字成」
# （memory 实录「科技方向占比约六成，计划降到四成」），单列一支：
# 「占比」两字连写后跟约数 → 拦；「占 X 成 权重」形态不同（占比不连写）
# → 不命中，指数构成可以正常写（「恒指约占七成权重」放行）。
_STATE_CTX = r"(?=[^，。；！？]*[仓敞])"
_STATE_PREFIX = r"[^，。；！？]*?"
STATE_RE = re.compile(
    r"敞口|仓位"
    r"|(?:仍|还|已|未|继续|计划|准备|打算)\s*(?:持有|重仓|满仓|空仓|清仓)"
    # ① 有歧义的 `a/b`：只认无歧义限定词（**不含** 至/到）。
    #    ⚠️ 数字类含全角 ０-９（2026-10-14 实测 `减１/３` 全角放行）——
    #    显式 [0-9] 不吃 Nd 全角，中文输入/LLM 输出全角数字常见。
    #    ⚠️ `_ST_SEP` 而非 `\s*`（2026-10-25）：见其定义处的绕过说明。
    #    ⚠️ 2026-10-26：这一支**拆出去**成 `_STATE_AB_RE` 单独判 ——
    #    日期（`近 10/8`、`为 10/23`，生产各真实误拦过 2/1 次）与仓位
    #    分数（`减 1/3`）在字面上区分不了，必须在命中后按
    #    「分子 < 分母 且 分母 ≤ 12」过滤，正则做不了数值比较。
    # ② 成/分之/位数：必须仓语境（见 _STATE_CTX；_STATE_PREFIX 允许仓词
    #    与约数之间隔任意非标点文字，如「底仓**约**五成」）
    r"|" + _STATE_CTX + _STATE_PREFIX + r"(?:" + _ST_SAFE + r")\s*"
    r"(?:" + _ST_NUM + r"{1,4}\s*成"
    r"|" + _ST_NUM + r"{1,4}\s*分之[" + _CN_NUM + r"0-9０-９]{1,4}"
    r"|" + _ST_NUM + r"{1,4}\s*位数)"
    # ③ 「占比」连写 + 约数：仓位专用词，无歧义。
    #    ⚠️ % 分支要求**原文**即「占比 NN%」相邻且带小数点/符号？都不行 ——
    #    2026-10-15 实测（watch.log 当天 9 次「重写：命中禁用词 '占比NN%'」）：
    #    系统**自己的数据段**就在教 AI 写「主力占比 +7.7%」（analyze.py
    #    资金段/热点段自产「占比 +7.7%」表述），AI 照写后被 ③ 的 % 分支拦下
    #    → 每次触发一次无谓重写。且 _mask 剥掉空格+符号后规范化文本拼出
    #    「占比77%」命中 —— 原文侧反而不匹配（% 后置），是规范化拼接
    #    制造的假命中。修法：% 分支整体删除 —— 百分数是市场数据的高频
    #    正确表述（占成交额 -4.2%）；仓位泄漏的中文口径（占比约六成/
    #    占比六成）仍由 成/分之 分支拦截，另有 ② 支仓语境 + BANNED 词表兜底。
    r"|占比\s*(?:约|近|超)?\s*" + _ST_NUM + r"{1,4}\s*(?:成|分之)"
    # ④ 「仓位词 + 一半/减半」（2026-10-30 补，实测放行）：
    #    `减仓一半`、`仓位减半`、`底仓砍半` 全部漏网 —— ③ 支只认
    #    成/分之，② 支只认数字+成，「一半」不在任何一支里。
    #    ⚠️ 锚必须是**显式仓位词**，不能复用 ② 支的 `_STATE_CTX` 宽判据：
    #    那支只要求同子句出现「仓」字，而「仓库库存减半」也满足它 ——
    #    库存是**市场数据**，误拦一次就要白跑一轮 AI 重写。
    #    显式词表（库存/仓储 不在其中）天然不会命中这类句子。
    r"|(?:仓位|底仓|持仓|空仓|满仓|半仓|重仓|清仓|减仓|加仓|建仓|调仓|"
    r"补仓|平仓|敞口)\s*(?:砍|减|降|剩|留|用|投)?\s*"
    r"(?:一半|半数|减半|砍半|大半|小半)"
    # ⑤ 仓位动作 + 至/到 + 百分数（2026-11-05 补，P1 合规）：
    #    `减仓至 30%`、`仓位降到 60%` 完整漏网 —— 2026-10-15 为救
    #    「主力占比 -7.7%」（系统自产数据段的高频表述）删掉了 ② 的 % 分支，
    #    动作型百分数（真正的仓位指令）随之一起放行。
    #    二者区别在**动作 + 至/到**：「占比 ±7.7%」「成交额增加至 15%」
    #    是市场数据（无仓位动作），放行；「减至 30%」「降到 60%」是指令。
    #    沿用 ② 的仓语境要求（同子句有 仓|敞 才拦）：「减仓至 30%」的仓字
    #    在动词里天然满足；「占比增加至 15%」不带仓词，照常放行。
    r"|" + _STATE_CTX + _STATE_PREFIX + _ST_VERB_TO
    + r"[0-9０-９]{1,3}(?:\.[0-9]+)?\s*(?:%|％)"
)

# `a/b` 仓位分数（2026-10-26 从 STATE_RE ① 支拆出）：
# 日期与分数字面同形，命中后必须按「分子 < 分母 且 分母 ≤ 12」过滤 ——
# `近 10/8`（10>8）、`为 10/23`（分母 23>12）、`更新为 10/1`（10>1）
# 是日期，放行；`减 1/3`、`约 2/3`、`用 1/4`、`约 3/10` 是仓位，拦截。
# 生产实据：`[10-14 13:59:53]`/`[10-15 09:27:46]` 两次 `重写：命中禁用词
# '近 10/8'`、agent 推送一次 `为 10/23` —— 每次白付一轮 AI 重写。
# 理论漏点：分母 >12 的真仓位分数（「减 13/20」）—— 实务上仓位分数
# 几乎总用 ≤12 的分母（成/半/三成，或 1/3、1/4、3/10）。
# `_ST_SEP` 的由来（防加粗绕过）见其定义处注释。
# 两个分支（各有自己的捕获组对）：
#   ① `_ST_AMBIG`（含单字 减/加/用…）或 `_ST_ACTION`（动作仓词 减仓/建仓…）
#      直接接分数 —— `减仓 1/3` 里「仓」字隔开单字「减」，靠动作仓词兜住；
#   ② `_ST_VERB_TO`（仓位动词 + 至/到）接分数 —— `调仓至 1/2`。
#      「涨至 1/5」「收在 1/5」不在动词表里，照常放行。
_STATE_AB_RE = re.compile(
    r"(?:(?:" + _ST_AMBIG + r"|" + _ST_ACTION + r")" + _ST_SEP
    + r"([0-9０-９]{1,2})" + _ST_SEP + r"[/／]" + _ST_SEP
    + r"([0-9０-９]{1,2})"
    r"|" + _ST_VERB_TO + _ST_SEP + r"([0-9０-９]{1,2})" + _ST_SEP
    + r"[/／]" + _ST_SEP + r"([0-9０-９]{1,2}))")
_AB_FW = str.maketrans("０１２３４５６７８９", "0123456789")

def _state_ab_hit(v):
    """在 v 里找**真仓位分数**形态的 a/b；命中返回原串，否则 None。"""
    for _m in _STATE_AB_RE.finditer(v):
        # 两支各带一对捕获组，取命中的那一对做数值过滤
        _a, _b = ((_m.group(1), _m.group(2)) if _m.group(1) is not None
                  else (_m.group(3), _m.group(4)))
        _n = int(_a.translate(_AB_FW))
        _d = int(_b.translate(_AB_FW))
        if _n < _d and _d <= 12:
            return _m.group(0)
    return None

# ---------- 语义级结构约束（补词表的根本局限）----------
# 词表只能拦"恰好用了那个字"的表述。以下这类句子不含任何被拦词、
# 不匹配任何正则，却完整描述了持仓状态 —— 实测全部放行：
#   「存储方向已于 9/21 处理完毕，目前科技方向占比约六成，计划降到四成。」
#   「浮盈部分逢反弹减仓」「底仓不动」「已落袋」「待提交」「按计划执行减仓」
# 根因是约束本身是**语义级**的（"不得透露发送者的持仓与操作安排"），
# 而实现是字面词黑名单。补词表会永远滞后于措辞变化，故加一层结构约束：
# 拦截**「计划时态 + 操作动词」**的组合 —— 市场播报可以写「建议逢反弹减仓」
# （面向读者的建议），但不该出现「已/计划/本轮 + 减仓」这类发送者自己的安排。
PLAN_TENSE_RE = re.compile(
    # 计划窗口的时态本身：不含操作动词，却是最典型的计划内部表述。
    # 实测 2026-09-24 13:42 那条**已真实投递到群**的推送正文就是
    # 「今日本轮窗口最后一日」—— prompt 明令 AI 开头第一句就点明它，
    # 而词表里没有任何一项能拦住。它暴露的是"发送者有一份带截止日的
    # 内部操作计划"，同样不该出现在面向全体群成员的消息里。
    r"(?:本轮|本窗口|本期|本阶段|这轮)\s*(?:窗口)?\s*(?:最后|截止|末|结束)"
    r"|(?:窗口|计划)\s*(?:最后一日|最后一天|截止日|结束日|到期日)"
    r"|(?:已|已经|计划|准备|打算|将要|本轮|本窗口|本期|这轮|今日内|明天|次日)"
    r"[^。；，、！？\n]{0,12}"     # ⚠️ 2026-10-27 修：不跨句读符 —— 防止「已破 20 日低，逢反弹分批减仓」这类市场判断被误拦（2026-10-27 生产三次误熔断根因之一）
    r"(?:处理|完成|执行|落实|减仓|加仓|建仓|清仓|赎回|申购|调仓|落袋|兑现|提交)"
    r"|(?:待|需|须|应当|要)\s*(?:提交|确认|执行|处理|完成)"
    r"|(?:按|依)\s*计划\s*(?:执行|操作|进行|减|加|清)"
)

# 「收益率」的**语境**判据（2026-10-30 从 BANNED 词表移出）。
#
# ⚠️ 为什么不能留在裸词表：债券口径的「美债 10 年期收益率」「国债收益率」
# 是**市场数据**，而这张表防的是「我的收益率 / 组合收益率」这类个人口径。
# 两者字面完全相同，词表分不开 —— `[实测]` 2026-10-23 09:31 整条 AI 正文
# 因它被判「AI 输出不可用(收益率)」退兜底模板；10-08 09:20 另有一次
# 命中后重写。而 `_ov_treasury` 刻意改用「利率」标签绕开，恰恰说明
# 把市场口径堵死是**误伤**，绕开只是权宜。
#
# 判据：命中处**前 8 字**出现债券语境词 → 放行；紧跟「曲线」也放行
# （收益率曲线是宏观术语）。个人口径（我的/组合/年化/今年以来 + 收益率）
# 前 8 字里没有任何债券词，照拦。
_YIELD_RE = re.compile(r"收益率")
_YIELD_CTX = re.compile(r"债|到期|票面|期限|国债|美债|中债|城投")

def _yield_ok(v, m):
    """该处「收益率」是不是**债券/利差口径**（市场数据）？"""
    return (v[m.end():m.end() + 2] == "曲线"
            or bool(_YIELD_CTX.search(v[max(0, m.start() - 8):m.start()])))

def _yield_hit(v):
    """在 v 里找**个人口径**的「收益率」；命中返回原串，否则 None。

    与 `_state_ab_hit` 同构：正则只能定位，语境只能逐命中判 ——
    把语境写进正则（如 `(?<!债)收益率`）挡不住「美债10年期收益率」
    （紧邻的前一个字是「期」不是「债」）。
    """
    for _m in _YIELD_RE.finditer(v):
        if not _yield_ok(v, _m):
            return _m.group(0)
    return None

def _yield_neutral(t):
    """把**债券口径**的「收益率」换成「利率」，专供 SOFT 词表匹配。

    ⚠️ 为什么 SOFT 也要修（2026-10-30 同步修）：SOFT 里有「收益」这个软词，
    而 `_hits(t, SOFT)` 是**子串**匹配 —— 「美债10年期收益率」照样命中
    「收益」，于是 `safe()` 判 False、走重写，重写仍写它就退兜底模板。
    只把「收益率」从 BANNED 移出**不够**：整条链上两处都要放行债券口径。
    SOFT 的「收益」本意是拦「收益 +12%」这类个人口径，与债券收益率无关。
    """
    out, last = [], 0
    for _m in _YIELD_RE.finditer(t):
        if not _yield_ok(t, _m):
            continue
        out.append(t[last:_m.start()])
        out.append("利率")            # 与 _ov_treasury 的标签口径一致
        last = _m.end()
    out.append(t[last:])
    # ⚠⚠ 2026-11-09（复核）：**研报口径的「每股收益」同理**。
    # SOFT 里有「收益」，子串匹配会把「每股收益 2.5 元」
    # 当成个人收益口径 —— 而它是研报里最常见的指标之一。
    # 只在这四个**整词**上做归一，不动裸「收益」本身（个人口径仍拦）。
    _r = "".join(out)
    for _w in ("每股收益", "每股流收益", "收益预测", "收益预期"):
        _r = _r.replace(_w, "指标")
    return _r

# ⚠⚠ 2026-11-08（复核）：**无单位裸数字是金额闸门的一个整类漏拦**。
# `AMT_RE` 每一个分支都要求紧跟单位/货币符号，而人写钱数时
# **常常不带单位**：`账上还剩 12000`。`[实测]` 七个结构正则全部 None，直接放行。
# （`账户还剩 12000` 能拦，只是因为「账户」恰好在 `BANNED` 里；`账上`不在。）
#
# 判据：**个人口径词 + 16 字内的 4 位以上裸数字**（同一子句）。
#   · 为什么要 4 位：指数点位与板块价位几乎都在 3 位以下
#     （站上 4200 / 跌破 380 / 涨幅 3.5），与个人金额在量级上自然分开。
#   · 后接「亿/万」的排除：那是市场口径（成交额 1.2 万亿。
#   · 市场限定语排除（融资余额/两融）：那是市场数据，不是我们的钱。
# 为什么不写成正则：市场限定语有 2 字也有 3 字，
# 变长 lookbehind 不可用；放进正则只能缩水。而且这里要逐命中判定，
# 与 `AMT_RE` 那段「不能只看第一个命中」是同一个理由。
_BARE_PRIV = ("账上", "账户", "卡里", "手里", "手头", "余额", "存款",
              "积蓄", "家底", "余钱", "现金", "还剩", "只剩", "攒下")
_BARE_QUAL = ("融资", "融券", "两融", "北向", "南向", "沪股通",
              "深股通", "市场", "港资", "规模", "存款准备金")
# ⚠️ `(?![\d.])` 不可省：没它的话 `\d{4,}` 会**回溯到更短的前缀**。
# `[实测]` 「余额 12000 亿元」：贪婪匹配「12000」→ 后接「亿」被
# `(?!\s*[亿万])` 否掉→ 回渀到「1200」→ 后一位是「0」，**不是** 亿/万 → 通过。
# 于是一条正常的市场表述被当成个人金额拦下。先管住数字尾巴。
_BARE_NUM_RE = re.compile(r"\d{4,}(?:\.\d+)?(?![\d.])(?!\s*[亿万])")
_BARE_SEP = "。；;，,、！!？?\n"


def _bare_amt_hit(t: str):
    """个人口径词 + 近距离的裸数字 → 返回命中串，否则 None。

    纯判据，不改文本。与 `_priv_in_clause` 分工不同：
    那个是「取消市场语境豁免」，这个是「直接判定为金额」。
    """
    for _w in _BARE_PRIV:
        for _m in re.finditer(re.escape(_w), t):
            # 市场限定语：「融资余额」「存款准备金」不是我们的钱
            _pre = t[max(0, _m.start() - 5):_m.start()]
            if any(_q in _pre for _q in _BARE_QUAL):
                continue
            _seg = t[_m.end():_m.end() + 16]
            # 只在**同一子句**里找（遇句读就停）——
            # 否则「余额不足。成交额 12500亿」会被误判。
            for _sep in _BARE_SEP:
                _i = _seg.find(_sep)
                if _i >= 0:
                    _seg = _seg[:_i]
            _mm = _BARE_NUM_RE.search(_seg)
            if _mm:
                return t[_m.start():_m.end() + _mm.end()]
    return None


def safe(t):
    """群聊内容闸门。返回 (是否安全, 命中的词)。

    ⚠️ 这是**字面词表 + 结构约束**，不是语义闸门。词表必然滞后于措辞变化，
    所以它只是最后一道兜底；prompt 里的禁止清单与这里的词表**必须同源维护**
    （两处独立维护必然漂移 —— 实测 prompt 明令禁止「收益/盈亏/成本价」，
    而 safe() 对它们零检查，唯一执行者是 AI 的自觉）。
    """
    if not t: return False, "empty"
    # 词表匹配走 _hits（原文 + 规范化），防 markdown/空白拆字绕过
    _w = _hits(t, BANNED)
    if _w: return False, _w
    # ⚠️ 正则闸门也必须跑**规范化文本**。
    # 原先只有 BANNED/SOFT 两个**词表**走了 _hits 的双匹配，五个正则
    # 一律 `RE.search(t)` 只测原文 —— 于是「markdown/空白拆字绕过」
    # 这一类缺陷在正则侧**原样存在**，而 memory 里那次修复只覆盖了词表。
    # 实测（2026-09-28，六种拆字 × 四个逐字禁词）：
    #   中间插空格 / 加粗 / 换行 / 全角空格 / 制表符
    #     -> WINDOW_RE 的四个禁词**全部漏网**
    #     -> PLAN_TENSE_RE 同样全部漏网
    #   例：「今天本 窗口，注意节奏」「今天**本窗口**，注意节奏」均放行
    # （首尾加粗不受影响 —— 那只改边界，不改词内部）
    # 危害与词表那次相同：prompt 逐字禁令形同虚设，合规率取决于 AI 自觉。
    # 修法：原文匹配**保持在前**（不误伤、行为与修复前一致），
    # 规范化文本再跑一遍，两者任一命中即拦 —— 与 _hits 的判据同构。
    # 返回的命中串优先取原文里的那个（便于日志排查）。
    _m = _mask(t)
    _variants = (t,) if _m == t else (t, _m)
    for _re_ in (AMT_RE, STATE_RE, PLAN_RE, PLAN_DAY_RE,
                 PLAN_TENSE_RE, WINDOW_RE, PRON_RE):
        for _v in _variants:
            if _re_ is AMT_RE:
                # ⚠️ AMT_RE 专属的市场语境排除必须**逐命中**判定，不能
                # `search()` 只看第一个命中就整条跳过。
                # `[实测]` 2026-10-16：`search()` 命中「成交额1.5万亿」→
                # 语境词命中 → `continue` **跳过整个 AMT_RE** → 同句里
                # 真正的个人金额「另留 5万 备用」「备好 ¥5000」全部放行：
                #   safe('成交额1.5万亿。另需备好5万。')  -> (True, None) ❌
                #   safe('市值 3 万亿，另留 ¥5000 备用。') -> (True, None) ❌
                # 而单独出现时（无市场金额垫背）能正常拦下。
                # 改为对**每一个**命中逐个判：命中处前 12 字内有市场语境词
                # 才豁免，否则拦下。任一未豁免的命中即整条拦。
                for _mm in _re_.finditer(_v):
                    _pre = _v[max(0, _mm.start() - 12):_mm.start()]
                    # ⚠️ 语境词必须与金额**同子句**才豁免：隔了句读符
                    # （。；，、！？）就是另一件事了。
                    # `[实测]` 2026-10-16：`净流出 28 亿，另留 5万 备用` ——
                    # 命中「5万」前 12 字含语境词「流出」，若只看窗口会把
                    # 个人金额当市场数据放行。按最后一个句读符切分后再找。
                    #
                    # ⚠️⚠️ 2026-11-06（全量复核 P0-2）：切句符集合原先只有
                    # 全角+半角的中文句读，**漏了换行与半角句点**，于是：
                    #   safe('净流出 28 亿\n另留 5万 备用') -> (True, None) ❌
                    #   safe('非农就业. 另留 5万 备用')     -> (True, None) ❌
                    #   safe('非农｡ 另留 5万 备用')        -> (True, None) ❌
                    # 而 prompt 明确要求「段内每句一行」—— **换行是常态
                    # 而非异常**，这条在真实输出形态下命中率很高。
                    # 更阴的是 `_mask()` 会把 `\n\r\t` 剥掉，所以规范化
                    # 那一路即使原文有换行也照样串句（两路都漏）。
                    # 补全：换行 + 半角句点 + 全角｡､ + 省略号 + 间隔号。
                    _cut = max(_pre.rfind(c) for c in _AMT_BREAK)
                    if _cut >= 0:
                        _pre = _pre[_cut + 1:]
                    _ctx = None
                    # ⚠️ 必须取**最近一个**语境词，不是第一个。
                    # `[实测]` 2026-11-06：取第一个时「失守 500 元后跌至 480 元」
                    # 会拿「失守」去解释 480 元，中间隔着 500 被判「不豁免」
                    # → 一条完全正常的多价位播报被拦（我第一版就栽在这）。
                    # 取最近的「跌至」，语境与金额才真正对得上。
                    for _crx in (_AMT_CTX, _AMT_QUOTE_CTX):
                        _ms = list(_crx.finditer(_pre))
                        if _ms and (_ctx is None or _ms[-1].end() > _ctx.end()):
                            _ctx = _ms[-1]
                    # ⚠️ 2026-11-06（P0-3）：语境词命中了还不够 —— 它必须
                    # **真的修饰这个金额**。原判据只看「命中金额前 12 字内有没有
                    # 语境词」，不看到底修饰谁，于是：
                    #   safe('非农 6.3 万另留 5万 备用') -> (True, None) ❌
                    #   safe('失守 500 元另留 5万 备用') -> (True, None) ❌
                    # 前一个「非农」修饰的是 6.3 万，后一个「失守 500 元」
                    # 是报价 —— 都跟后面那个个人金额无关。
                    # 判据：**语境词之后、本金额之前不能再出现数字**。
                    # 再出现数字说明中间隔了另一笔金额，语境词属于那一笔。
                    # `[实测]` 对以下三类全部正确：
                    #   放行：'非农 6.3 万'、'失守 500 元'、'非农 6.3 万、前值 2.9 万'
                    #   拦下：'非农 6.3 万另留 5万'、'失守 500 元另留 5万'
                    if (_ctx and not _AMT_BETWEEN_RE.search(_pre[_ctx.end():])
                            and not _priv_in_clause(_pre)):
                        continue            # 该命中是市场数据/行情报价，放行
                    return False, _mm.group(0)
                continue                    # 全部命中都带语境 —— 放行 AMT_RE
            m = _re_.search(_v)
            if not m:
                continue
            return False, m.group(0)
    # `a/b` 仓位分数：放在结构正则循环**之后**逐命中判 ——
    # 「减 1/3」若同时命中 STATE_RE 的「仓位」词，先按词表命中
    # （命中串更利于日志排查）；只有词表/其它正则都放行时才轮到它。
    for _v in _variants:
        _ab = _state_ab_hit(_v)
        if _ab:
            return False, _ab
    # 「收益率」语境判据（2026-10-30，从 BANNED 移出，见 _YIELD_RE）。
    for _v in _variants:
        _yh = _yield_hit(_v)
        if _yh:
            return False, _yh
    # 软词放最后：它们最容易被误伤（财经标准用语），
    # 命中后由 push() 的降级链**剔除含该词的句子**再推，而不是整条丢弃。
    #
    # ⚠️ 2026-10-16 复核（P0）：原文本身可能含**孤立代理码点**（Cs 类，
    # `news._flat()` 按 .strip() 处理、不断字符边界；数据源转义残留 / AI
    # 输出截断都可能引入）。`safe()` 对它放行，而 `analyze.push()` 的
    # `_body.encode("utf-8")`（analyze.py:541）直接抛 UnicodeEncodeError ——
    # 被 `_boot()` 捕成 [BOOT] 后 push() 不返回、批次回滚、下一轮复现。
    # 这里在闸门入口**显式拒绝**（而不是静默替换）：代理码点不是合法的
    # 自然语言内容，出现即上游数据有问题，应触发重写而不是被放行。
    if any(0xD800 <= ord(_c) <= 0xDFFF for _c in t):
        return False, "孤立代理码点(Cs)"
    # ⚠️ SOFT 走 `_yield_neutral(t)` 而不是原文（2026-10-30）：软词表里有
    # 「收益」，「美债10年期收益率」会命中它 —— 债券口径的市场数据必须
    # 与「个人收益」区分开，否则整条 AI 正文照样被打回重写。
    _w = _hits(_yield_neutral(t), SOFT)
    if _w: return False, _w
    # ⚠⚠ 2026-11-08（复核）：裸数字金额。放在**所有结构正则之后** ——
    # 它是最宽的一条（不要求单位），放在前面会抢掉后面那些
    # 带语境豁免的判定机会。两个变体（原文/规范化）都跑。
    for _v in _variants:
        _b = _bare_amt_hit(_v)
        if _b:
            return False, _b
    return True, None

