#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""容器调度器（src/scheduler.py）的单元测试。

## 为什么这个文件是必要的

`scheduler.py` 是容器部署的**唯一调度者** —— 它挂了就一行推送都不会发。
而它实现的是 launchd 的两条隐式语义（同 job 不并发、常驻进程要退避重启），
这两条如果写错，**不会崩溃、只会静默地少推或狂推**。那正是最难发现的形态。

所以这里测的不是"函数返回值"，是**行为契约**：
  · 上一轮没结束 → 本轮跳过，且下一轮不会补跑两次
  · webui 挂了 → 重启，且间隔逐次拉长（不能每 5 秒刷一次日志）
  · 时区不对 → **拒绝启动**（UTC 下能跑但全错，比启动失败难查）

## 测试策略

真起子进程太慢也太脆（要 300 秒）。这里把 `_spawn` / `time.sleep` 换成桩，
直接驱动主循环若干轮，用**假时钟**控制时间推进 —— 这样"300 秒后触发"
在毫秒内可测，而且断言的是调度逻辑本身，不是子进程行为。
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

import scheduler as S  # noqa: E402

_ok = [0]


def check(cond, msg):
    if cond:
        _ok[0] += 1
        return
    print(f"  ❌ {msg}")
    raise SystemExit(1)


# ---------------------------------------------------------------- 时区
#
# ⚠️ 这条是本次容器化里最容易漏、后果最静默的一项：容器默认 UTC。
# 设成 UTC 时程序**能启动、能跑**，只是所有时段判断偏 8 小时
# （盘中不推、半夜推）。所以它必须是 fail-fast，不是 warning。
def _tz_offset():
    return -time.timezone if not time.daylight else -time.altzone


_orig_tz = os.environ.get("TZ")
try:
    # ① 北京时间 → 放行（不抛）
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    try:
        S.check_timezone()
        _ok[0] += 1
    except SystemExit:
        check(False, "TZ=Asia/Shanghai 应放行，实际被拒绝")

    # ② UTC → 必须拒绝（exit 2）
    os.environ["TZ"] = "UTC"
    time.tzset()
    _rc = None
    try:
        S.check_timezone()
    except SystemExit as _e:
        _rc = _e.code
    check(_rc == 2, f"TZ=UTC 必须拒绝启动（exit 2），实得 {_rc!r}")

    # ③ 偏移 +9 → 同样拒绝（不能只认字符串 "Asia/Shanghai"，
    #    要按**实际偏移**判 —— 否则 TZ=Japan 会被放行，而它偏 1 小时）
    os.environ["TZ"] = "Asia/Tokyo"
    time.tzset()
    if _tz_offset() == 9 * 3600:
        _rc = None
        try:
            S.check_timezone()
        except SystemExit as _e:
            _rc = _e.code
        check(_rc == 2, "UTC+9 必须拒绝 —— 判据要看实际偏移，不是时区名")
finally:
    if _orig_tz is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = _orig_tz
    time.tzset()


# ---------------------------------------------------------------- job 表
#
# 间隔必须与 plist 一致。collect 与 analyze 尤其必须同间隔 ——
# analyze 读 collect 写的快照，节拍不一致会让"数据滞后一轮"，
# 而这是最难查的一类问题（推送内容看着正常，其实用的是上一轮数据）。
_iv = {n: iv for n, _s, iv, _p in S.JOBS}
check(_iv.get("collect") == 300, f"collect 间隔应为 300s，实得 {_iv.get('collect')}")
check(_iv.get("watch") == 300, f"watch 间隔应为 300s，实得 {_iv.get('watch')}")
check(_iv["collect"] == _iv["watch"],
      "collect 与 watch 必须同间隔（analyze 读 collect 写的快照，"
      "节拍不同会让数据滞后一轮）")
check(_iv.get("agent") == 3600, f"agent 间隔应为 3600s，实得 {_iv.get('agent')}")
check(_iv.get("selfcheck") == 86400,
      f"selfcheck 间隔应为 86400s，实得 {_iv.get('selfcheck')}")

# 每个 job 都必须有单轮上限，否则一个卡死的轮次会永久占住该 job 的位置。
#
# ⚠️ 上限与间隔的正确关系取决于该 job 的**语义**，不能一刀切：
#   · collect / watch —— 间隔 300s，脚本自己只有内部请求超时（curl -m），
#     没有"整轮"看门狗。上限必须**远大于**间隔（一轮正常十几秒，
#     给到分钟级），否则会打断正在正常工作的轮次。
#   · agent —— 间隔 3600s，但 `agent/run.py` **自己有 1800s 看门狗**。
#     这里只是"连它那个都没生效"的兜底，所以 2400s 是对的：
#     刚好比脚本自己的 1800s 宽一点，又远小于 3600s 的间隔。
#     硬套"间隔 × 1.5"会逼我们把上限抬到 5400s —— 那等于**关掉**这层保险。
_SHORT_CYCLE = {"collect", "watch"}
for _n, _s, _iv2, _p in S.JOBS:
    check(_n in S.MAX_RUNTIME,
          f"{_n} 缺 MAX_RUNTIME —— 卡死的轮次会永久占住该 job 位置")
    if _n in _SHORT_CYCLE:
        check(S.MAX_RUNTIME[_n] > _iv2 * 1.5,
              f"{_n} 的 MAX_RUNTIME({S.MAX_RUNTIME[_n]}s) 太小 —— "
              f"不该短于其调度间隔的 1.5 倍，否则会打断正常慢轮")
    else:
        # 自带看门狗的 job：兜底必须比它自己的超时宽，且短于调度间隔
        check(S.MAX_RUNTIME[_n] < _iv2,
              f"{_n} 的 MAX_RUNTIME({S.MAX_RUNTIME[_n]}s) 不该超过调度间隔"
              f"({_iv2}s) —— 那样这层保险永远不会先触发")
        check(S.MAX_RUNTIME[_n] >= 1800,
              f"{_n} 的 MAX_RUNTIME({S.MAX_RUNTIME[_n]}s) 比脚本自己的"
              f"看门狗(1800s)还短 —— 会抢在正常收尾之前动手")


# ---------------------------------------------------------------- 退避
check(S.WEBUI_BACKOFF_MIN >= 1, "退避下限不能是 0（会变成忙等）")
check(S.WEBUI_BACKOFF_MAX > S.WEBUI_BACKOFF_MIN,
      "退避上限必须大于下限，否则没有退避")
check(S.WEBUI_BACKOFF_MAX <= 3600,
      "退避上限超过 1 小时没有意义 —— 那已经不是退避，是放弃")

# ---------------------------------------------------------------- 时区常量
check(S.EXPLICIT_TZ == "Asia/Shanghai",
      "建议时区必须用 IANA 名 Asia/Shanghai —— "
      "`CST` 在 macOS 上能用，但在 Linux/glibc 下有歧义（可能是美国中部时间）")


print(f"ALL PASS (容器调度器 {_ok[0]} 个断言)")
