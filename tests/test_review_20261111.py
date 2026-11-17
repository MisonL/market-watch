#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026-11-11 全面复核的四个修复，逐条钉住。

四个问题都不是"写法不好"，而是**会让读者看到错误事实**：

  ① 午休时段新鲜度闸门整体失效 → 昨日收盘价当现价写进「午间小结」
  ② 汇率陈旧闸门是死代码     → 源冻结在几周前也照样推「离岸人民币 x%」
  ③ 面板孤儿轮次归因是死代码 → 被 kill 的轮次被告知"launchd 没触发"
  ④ 长轮次必然误报停摆       → 24 分钟的轮次跑到第 16 分钟就喊"主链路停摆"

⚠️ 这些断言必须**不依赖真实日期/真实行情**。上次就栽在这里：
`test_webui_slots.py` 初版把日期写死成 2026-11-10，当天绿、第二天红。
"""
import datetime as _dt
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

if not os.environ.get("MW_DIR"):
    os.environ["MW_DIR"] = tempfile.mkdtemp(prefix="mw_rv11_")
_D = os.environ["MW_DIR"]
for _sub in ("state/history", "state/cache", "conf", "logs"):
    os.makedirs(os.path.join(_D, _sub), exist_ok=True)

import marketlib as M          # noqa: E402
import paths                   # noqa: E402

_FAIL = []


def check(cond, msg):
    if not cond:
        _FAIL.append(msg)


# ══════════════════════════════════════════════════════════════════
# ① 午休豁免必须限定「时间戳是当天的」
# ══════════════════════════════════════════════════════════════════
def t_lunch_same_day():
    """昨日收盘在午休时必须是 stale；当天 11:30 的合法午休值必须放行。

    A 股午休 11:30–13:00，`_LUNCH` 就是这个区间。11:30 槽位的窗口是
    11:30–11:50（TTL 20 分钟），**正好落在盲区里** —— 原判据只看
    「现在是不是午休」，不看 `t` 的日期，于是那一带的陈旧值全部放行。
    """
    yday_close = "20261110150000"          # 2026-11-10 收盘
    for h, m in ((11, 20), (11, 45), (12, 30), (13, 5), (14, 0)):
        now = _dt.datetime(2026, 11, 11, h, m)
        check(M._is_ts_stale(yday_close, now) is True,
              f"A股 {h:02d}:{m:02d} 判昨日收盘为新鲜（应 stale）")
    # 当天 11:30 的值，在午休里必须继续有效
    today_1130 = "20261111113000"
    for h, m in ((11, 45), (12, 30)):
        now = _dt.datetime(2026, 11, 11, h, m)
        check(M._is_ts_stale(today_1130, now) is False,
              f"A股 {h:02d}:{m:02d} 误杀当天 11:30 的合法午休值")
    # 港股午休窗口不同（12:00–13:00），同样要限定当天
    now = _dt.datetime(2026, 11, 11, 12, 30)
    check(M._is_ts_stale("2026/11/10 16:00:00", now, market="hk") is True,
          "港股 12:30 判昨日 16:00 收盘为新鲜（应 stale）")
    check(M._is_ts_stale("2026/11/11 11:30:00", now, market="hk") is False,
          "港股 12:30 误杀当天 11:30 的合法午休值")


# ══════════════════════════════════════════════════════════════════
# ② 汇率陈旧闸门不能因为「纯日期解析不出」而整段失效
# ══════════════════════════════════════════════════════════════════
def t_fx_date_only():
    """`_ts_parse` 对纯日期返回 None —— 这是**契约**，不要改它。

    但 `_ov_fx` 的闸门必须自己补上纯日期解析，否则整段是死代码。
    这里用真实载荷形状验证：末列 `2026-11-11`。
    """
    check(M._ts_parse("2026-11-11") is None,
          "_ts_parse 不应接受纯日期（改了会波及所有调用方）")
    check(M._ts_parse("2026-11-11 00:00:00") is not None,
          "_ts_parse 应接受带时分的格式")

    # 闸门必须能算出「纯日期 → 当日 00:00」并据此判衰老
    #
    # ⚠⚠ 2026-11-15：原先把两个日期**写死**成 `2026-11-11`（判不陈旧）与
    # `2026-11-05`（判陈旧）。这在写的那天是对的，但 `_OV_STALE_H` 是
    # **96 小时**（4 天）—— 到了 11-15，`2026-11-11` 已经 114 小时，
    # 于是「应该不陈旧」的那条变成陈旧，测试**自己随时间失效**：
    #
    #     ❌ 纯日期 2026-11-11 的衰老判定不符预期（114h）
    #
    # 这类「硬编码日期 + 相对时间阈值」的断言，写的那天必绿、过几天必红，
    # 是**测试自身的缺陷**，不是被测代码回归。改成相对今天生成日期：
    # 用 1 天前（远小于 96h）与 10 天前（远大于 96h），两个方向都留足余量，
    # 永远不会因为"过了几天"而翻面。
    _now = _dt.datetime.now()
    _today = _now.date()
    _fresh_ds = (_today - _dt.timedelta(days=1)).isoformat()    # 24h  < 96h
    _stale_ds = (_today - _dt.timedelta(days=10)).isoformat()   # 240h > 96h
    for ds, should_drop in ((_fresh_ds, False), (_stale_ds, True)):
        _d = _dt.datetime.combine(_dt.date.fromisoformat(ds), _dt.time.min)
        _ah = (_now - _d).total_seconds() / 3600.0
        _drop = (_ah > M._OV_STALE_H or _ah < -1)
        check(_drop == should_drop,
              f"纯日期 {ds} 的衰老判定不符预期（{_ah:.0f}h）")

    # ── 端到端注入：拿一份真实形状的回包走完 `_ov_fx` 全程 ──
    # ⚠ 上面那段只验证了「纯日期能算出衰老」，而那与 `_ov_fx` 里的
    # `if _dtp is not None` 是否真的生效**毫无关系** —— 第一版就是这么
    # 写的，把修复注入回去测试照样全绿。必须在 `subprocess.run`
    # 上打桩，让它吐出“几周前的日期”，看 `_ov_fx` 是否真的丢弃。
    import subprocess as _sp
    _real_run = _sp.run

    def _fake_run(cmd, *a, **kw):
        """仿真 curl 的返回（字节串，gbk）。"""
        class _R:
            stdout = _FAKE_BODY.encode("gbk", "ignore")
        return _R()

    def _with_body(body, expect_empty):
        global _FAKE_BODY
        _FAKE_BODY = body
        _sp.run = _fake_run
        try:
            _r = M._ov_fx()
        finally:
            _sp.run = _real_run
        check(isinstance(_r, list), f"_ov_fx 应返回 list，实得 {type(_r).__name__}")
        if expect_empty:
            check(_r == [], f"陈旧日期应被丢弃，实得 {_r}")
        else:
            check(len(_r) == 1, f"当日数据应调用成功，实得 {_r}")

    _today = _dt.date.today().isoformat()
    _old = (_dt.date.today() - _dt.timedelta(days=40)).isoformat()
    # 字段：[0]时刻 [3]昨收 [8]最新 [11]绝对变动 末列日期
    _mk = lambda d: (f'var hq_str_fx_susdcnh="08:12:06,6.6761,6.6764,6.6763,62,'
                     f'6.6766,6.6806,6.6744,6.6761,x,-0.020000,-0.0002,'
                     f'0.0009,,6.9957,6.6427,,{d}";')
    _with_body(_mk(_today), expect_empty=False)
    _with_body(_mk(_old), expect_empty=True)


# ══════════════════════════════════════════════════════════════════
# ③ 孤儿轮次归因：只能比 analyze 的 start/end
# ══════════════════════════════════════════════════════════════════
def t_orphan_attribution():
    """`collect` 只写 end，`analyze` 才写 start+end。

    原判据对 `runs`（全量）数头尾 → `_n_end` 恒大于 `_n_start` →
    「进程已启动但未正常结束」永不出现，被 kill 的轮次被告知
    「该时段窗口内无执行记录」（把人引向 launchd，而真因是被杀）。
    """
    import json
    import webui

    day = _dt.date(2026, 11, 11)

    class _FakeDate(_dt.date):
        @classmethod
        def today(cls):
            return day

    class _FakeDT(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _dt.datetime(2026, 11, 11, 10, 30)

        @classmethod
        def today(cls):
            return _dt.datetime(2026, 11, 11, 10, 30)

    json.dump({"date": "2026-11-11", "fired": []},
              open(os.path.join(_D, "state", "state.json"), "w",
                   encoding="utf-8"))
    open(webui.PUSHES, "w", encoding="utf-8").close()
    rows = [{"t": "2026-11-11 09:25:01", "script": "analyze",
             "phase": "start", "status": "ok"}]
    for h in range(10, 14):
        rows.append({"t": f"2026-11-11 {h:02d}:00:00", "script": "collect",
                     "phase": "end", "status": "ok"})
    with open(webui.RUNS, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    _rd, _rdt = _dt.date, _dt.datetime
    webui.datetime.date = _FakeDate
    webui.datetime.datetime = _FakeDT
    try:
        v = webui.build_slots("2026-11-11")
    finally:
        webui.datetime.date = _rd
        webui.datetime.datetime = _rdt

    row = next((r for r in ((v or {}).get("slots") or [])
                if r.get("key") == "09:25"), None)
    check(row is not None, "09:25 槽位行缺失")
    if row:
        check(row.get("status") == "missed",
              f"09:25 有 start 无 end 应为 missed，实得 {row.get('status')!r}")
        _why = str(row.get("reason") or "")
        check("未正常结束" in _why,
              f"归因应指向「进程未正常结束」，实得 {_why!r}")
        check("窗口内无执行记录" not in _why,
              f"归因误报成 launchd 未触发，实得 {_why!r}")


# ══════════════════════════════════════════════════════════════════
# ④ 正在跑 ≠ 停摆
# ══════════════════════════════════════════════════════════════════
def t_stall_not_while_running():
    """真实轮次最长 24.3 分钟，阈值只有 15 分钟。

    心跳只在 run() 首尾写，长轮次中间必然陈旧 —— 只看年龄就会
    在企业微信里喊「主链路疑似停摆」，而 analyze 正在跑。更坏的是
    `_maybe_stall_alert` 按日去重：假警报发出后，当天真停摆就告不了。
    """
    now = _dt.datetime(2026, 11, 11, 10, 30)
    check(M.stall_alert_should_fire(now, 1500, True, running=False) is True,
          "已停 25 分钟且未在跑 → 应告警")
    check(M.stall_alert_should_fire(now, 1500, True, running=True) is False,
          "正在跑（锁被持有）时不应告警")
    check(M.stall_alert_should_fire(now, 300, True, running=False) is False,
          "心跳新鲜时不应告警")
    check(M.stall_alert_should_fire(_dt.datetime(2026, 11, 11, 21, 0),
                                    9999, False, running=False) is False,
          "盘外不应告警")

    # 锁探测本身：持锁 → True，释放 → False，文件缺失 → False（不抛）
    check(M.analyze_lock_held() is False, "当前无 analyze 在跑，应探测为 False")
    import fcntl
    _lk = paths.p_log("watch.lock")
    os.makedirs(os.path.dirname(_lk), exist_ok=True)
    _fd = os.open(_lk, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        check(M.analyze_lock_held() is True, "本进程持锁时应探测为 True")
    finally:
        try:
            fcntl.flock(_fd, fcntl.LOCK_UN)
        finally:
            os.close(_fd)
    check(M.analyze_lock_held() is False, "释放锁后应探测为 False")
    _tmp = _lk + ".bak"
    os.replace(_lk, _tmp) if os.path.exists(_lk) else None
    try:
        check(M.analyze_lock_held() is False, "锁文件缺失时应返回 False 而非抛异常")
    finally:
        if os.path.exists(_tmp):
            os.replace(_tmp, _lk)


# ══════════════════════════════════════════════════════════════════
# ⑤ 缺失资金金额不得伪装成「≈0」
# ══════════════════════════════════════════════════════════════════
def t_missing_amount_not_zero():
    """`f184` 有值、`f62` 缺失时必须说「金额未知」，不能说「≈0」。

    原写 `_num(x.get("f62"), 0.0)` —— 源没给 f62 时默认成 0.0，
    落到 `else "≈0"` 分支，推出「半导体主力≈0(占+9.0%)」：
    **断言了一个确定的近零金额**，而事实是金额未知。读者据此判断
    「资金没动」，而它可能正是大幅流出。
    """
    import analyze as A
    import datetime as _d
    st = {"date": "2026-11-11", "fired": [], "inflight": [],
          "pending_idx": [], "hourly_retry": {}, "count": 0}
    at = _d.datetime(2026, 11, 11, 10, 0)
    # f184=9.0 越过 watch_fund_pct(8.0)，但完全没有 f62
    q = {"90.BK1036": {"f2": 1000.0, "f3": -2.0, "f14": "半导体", "f184": 9.0}}
    got = [d for k, d in A.detect(q, None, at, st) if k.startswith("watch_")]
    if not got:
        # 夹具里没有 watch 段时无法验证，明确跳过而不是假装通过
        print("  (watch 配置缺失，跳过金额显示断言)")
        return
    check(any("金额未知" in str(x) for x in got),
          f"缺失 f62 应显示「金额未知」，实得 {got}")
    check(not any("≈0" in str(x) for x in got),
          f"缺失 f62 不得显示「≈0」，实得 {got}")


# ══════════════════════════════════════════════════════════════════
# ⑥ 配额预警与计数必须同一次落盘（丢更新）
# ══════════════════════════════════════════════════════════════════
def t_quota_warn_single_write():
    """跨过 80% 档时，`count` 与 `warned_tier` 必须一次写进去。

    原写法先在锁内写 count、**解锁**，再在预警块里重新读文件、写回自己
    那份快照 —— 注释还写着「这里处在 RATE 文件锁内」，与代码相反。
    后果是丢更新：并发预留时计数被旧快照覆盖，配额限制与预警一起失真。
    """
    rf = M.RL.RATE_FILE
    _bak = None
    try:
        if os.path.exists(rf):
            _bak = open(rf, "rb").read()
        M._atomic_write_json(rf, {"date": M.RL.day, "count": 1599,
                                  "warned_tier": 0})
        M.RL.count = 1599
        M.RL._synced = 1599
        # ⚠⚠ 关键断言：**一次预留只允许落盘一次**。
        # 原写法先写 count、解锁、再读文件写回预警字段 —— 两次写。
        # 两次写之间的窗口就是丢更新的窗口：别的进程在这个空隙里
        # 预留、落盘，随即被本进程的旧快照覆盖。
        # 只断言“最终计数”是抓不到的（单进程顺序执行结果恰好一致）。
        _writes = []
        _orig_w = M._atomic_write_json

        def _counting(*a, **k):
            _writes.append(a[0] if a else k.get("path"))
            return _orig_w(*a, **k)

        M._atomic_write_json = _counting
        try:
            ok = M.RL._reserve()
        finally:
            M._atomic_write_json = _orig_w
        check(ok is True, "预留应成功")
        _rate_writes = [w for w in _writes
                        if isinstance(w, str) and w.endswith("rate.json")]
        check(len(_rate_writes) == 1,
              f"一次预留应只落盘 1 次 rate.json，"
              f"实得 {len(_rate_writes)} 次（多于 1 次就有丢更新窗口）")
        d = M._load_json(rf, {}) or {}
        check(d.get("count") == 1600,
              f"预留 1 次后 count 应为 1600，实得 {d.get('count')}")
        check(d.get("warned_tier") == 8,
              f"跨过 80% 档应记 warned_tier=8，实得 {d.get('warned_tier')}")
        # 再预留一次：计数必须继续累加（证明没有被旧快照覆盖）
        M.RL._reserve()
        d2 = M._load_json(rf, {}) or {}
        check(d2.get("count") == 1601,
              f"再预留 1 次后 count 应为 1601，实得 {d2.get('count')}")
    finally:
        try:
            if _bak is None:
                os.path.exists(rf) and os.remove(rf)
            else:
                open(rf, "wb").write(_bak)
        except Exception:
            pass
        M.RL.count = M.RL._synced = 0


# ══════════════════════════════════════════════════════════════════
# ⑦ 原子写失败必须如实反映
# ══════════════════════════════════════════════════════════════════
def t_atomic_write_result_honest():
    """`_locked_update` 不得在写入失败时 `return True`。"""
    import json as _j
    p = os.path.join(_D, "state", "cache", "_rv11_atomic.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    check(M._locked_update(p, lambda d: d.__setitem__("a", 1)) is True,
          "正常写入应返回 True")

    _orig = M._atomic_write_json
    M._atomic_write_json = lambda *a, **k: False
    try:
        r = M._locked_update(p, lambda d: d.__setitem__("b", 2))
    finally:
        M._atomic_write_json = _orig
    check(r is False, f"写入失败时必须返回 False，实得 {r}")


# ══════════════════════════════════════════════════════════════════
# ⑧ 新闻结构损坏不得缓存成「健康的空」
# ══════════════════════════════════════════════════════════════════
def t_news_struct_not_healthy_empty():
    """`fastNewsList` 是数字/字符串时，必须当「源不可用」，不能当「没新闻」。

    原写归一成 `[]` → 被当成合法空结果缓存 120s，**且不去试备用源** ——
    故障被伪装成正常。
    """
    import news
    news._NEWS_MEM.update({"ts": 0, "limit": 0, "data": None})
    _orig = news.curl_json
    calls = []

    def fake(url, *a, **kw):
        calls.append(url)
        if "getFastNewsList" in url:
            return {"data": {"fastNewsList": 42}}
        return []

    news.curl_json = fake
    try:
        r = news.fetch_news(limit=5)
    finally:
        news.curl_json = _orig
        news._NEWS_MEM.update({"ts": 0, "limit": 0, "data": None})
    check(r is None, f"结构损坏应返回 None（源不可用），实得 {type(r).__name__}={r!r}")
    check(news._NEWS_MEM.get("data") is None,
          "结构损坏不得把空结果写进缓存 data")


# ══════════════════════════════════════════════════════════════════
# ⑨ 心跳读改写加锁 + 唯一临时文件 + 0600
# ══════════════════════════════════════════════════════════════════
def t_beat_lock_and_tmp():
    """analyze 与 collect 是两个进程，beat() 的读改写必须互斥。

    原写法裸 read→modify→replace，且临时文件固定为 `.tmp`：
      ① collect 读到旧 analyze 心跳 → analyze 写新 → collect 把旧值写回
         → analyze 心跳被逆回，判停摆（两个任务刚跑完）
      ② 共用 `.tmp`：A 写一半、B 截断同一文件，内容互相污染
    另：锁文件曾按 umask 落成 0644，自检抓到（同 beat.json 的历史教训）。
    """
    import runlog
    runlog.beat("analyze")
    runlog.beat("collect")
    d = __import__("json").load(open(runlog.BEAT_FILE, encoding="utf-8"))
    check(set(d) >= {"analyze", "collect"},
          f"两次心跳后两个键都应在，实得 {sorted(d)}")
    _dir = os.path.dirname(runlog.BEAT_FILE)
    _leftover = [f for f in os.listdir(_dir) if f.endswith(".tmp")]
    check(not _leftover, f"残留临时文件: {_leftover}")
    _lk = runlog.BEAT_FILE + ".lock"
    check(os.path.exists(_lk), f"锁文件应存在: {_lk}")
    if os.path.exists(_lk):
        check((os.stat(_lk).st_mode & 0o777) == 0o600,
              f"锁文件权限应为 0600，实得 "
              f"{oct(os.stat(_lk).st_mode & 0o777)}")


# ══════════════════════════════════════════════════════════════════
# ⑩ 截断删掉必带段后必须被结构闸门抓到
# ══════════════════════════════════════════════════════════════════
def t_truncation_keeps_required_sections():
    """截断可以删内容，但不能删掉必带段之后还放行。

    `_safe()` 只查禁词与正则、**不查结构**，所以结构闸门在截断前检查过
    一次并不够 —— 它检查的已经不是将要发出的文本。
    `[实测]` 7729 字节、结构完整的正文截到 1947 字节后，
    `> 风险` / `> 机会` 两行被删掉，最终复核照样放行。
    """
    import analyze as A
    _limit = 4096
    # ⚠ 夹具必须自带≥两个 `_STRUCT_TAGS`
    # （'触发'/盘面/关注板块/建议/隔夜全球/隔夜外盘）。
    # `[实测]` 我第一版只用了「隔夜」+「关注板块」，而
    # 「隔夜」不在标签表里 → 完整原文也被判「1 个结构标签」。
    # 故用「隔夜全球」+「关注板块」两个真标签。
    body = ("> **盘前简报 · 测试**\n\u3000\n**隔夜全球** " + "美股收跌，" * 10 + "\n"
            "**关注板块** CPO概念、半导体\n**关键位** 上证 3300\n"
            + ("板块方面，" + "资金面延续分化态势，" * 40 + "\n") * 6
            + "> 风险 硬件链承压\n> 机会 半导体国产化\n")
    _raw = body.encode("utf-8")[:(_limit - 200) // 2]
    while _raw and (_raw[-1] & 0xC0) == 0x80:
        _raw = _raw[:-1]
    cut = _raw.decode("utf-8", "ignore")
    check("> 风险" not in cut or "> 机会" not in cut,
          "夹具没造出「必带段被截掉」的情形，断言无意义")
    _sr = A._struct_reason(cut, "", "09:25")
    check(bool(_sr), f"截断后缺必带段应判不合格，实得 {_sr!r}")
    # 完整原文必须合格（否则说明闸门本身有 bug，不是截断的问题）
    check(A._struct_reason(body, "", "09:25") is None,
          f"完整原文应合格，实得 {A._struct_reason(body, '', '09:25')!r}")

    # ⚠⚠ 上面只验证了「截断后的文本不合格」，与 `push()` 里**是否真的
    # 做了这次复核**毫无关系 —— 第一版就是这么写的，把生产里的
    # `if _truncated else None` 改成 `None` 后测试照样全绿。
    # 必须**走完整 push()**：发出去的那份正文必须仍带必带段（要么原文被保留、
    # 要么改用兜底模板）。
    import json as _js
    import types as _tp
    _sent = []

    def _fake_run(argv, **kw):
        _b = kw.get("input") or b""
        if isinstance(_b, bytes):
            _b = _b.decode("utf-8", "ignore")
        _sent.append((list(argv), str(_b)))
        return _tp.SimpleNamespace(returncode=0, stdout='{"errcode":0}',
                                   stderr="")

    _old_cfg, _old_run = A.CFG, A.subprocess.run
    A.CFG = {"webhook": "https://example.invalid/not-real"}
    A.subprocess.run = _fake_run
    try:
        A.push(body, slot="09:25", kind="hourly")
    finally:
        A.CFG, A.subprocess.run = _old_cfg, _old_run
    _pushes = [b for a, b in _sent if "-K" in a]
    check(bool(_pushes), "未捕获到推送请求")
    if _pushes:
        try:
            _content = _js.loads(_pushes[-1])["markdown"]["content"]
        except Exception:
            _content = _pushes[-1]
        check("> 风险" in _content or "> 机会" in _content,
              f"发出去的正文已丢失风险/机会段：{_content[-120:]!r}")


# ══════════════════════════════════════════════════════════════════
# ①① 取数预算必须穿进单次请求
# ══════════════════════════════════════════════════════════════════
def t_fetch_budget_reaches_request():
    """`VR_BUDGET` 只在板块之间检查是不够的。

    一条请求链最坏是「直连 3 次重试 + 代理 + firecrawl」。
    注释里早写了「预算必须**穿进单次 cj**」，但实际调用
    **没传 timeout** —— `[\u5b9e\u6d4b]` 一条链 191.5s，而预算只有 80s。
    同理 `_fallback_get` 的 firecrawl 曾硬编码 90s，完全不看传入的 timeout。
    """
    import hot
    seen = []
    _orig = hot._cj

    def _fake(url, **kw):
        seen.append(kw.get("timeout"))
        return None

    hot._cj = _fake
    _hosts = getattr(hot, "VR_HOSTS", None)
    hot.VR_HOSTS = ["https://h1"]
    # ⚠⚠ 必须把时钟固定到盘中（2026-11-12 补）：
    # `kline_stats()` 开头就有 `if hm <= 9*60+30: return None, None`
    # 与 `if elapsed < 15: return None, None`。
    # `[实测]` 09:11 跑本项 → 提前返回，一次 cj 都没调 →
    # 报「未捕获到 cj 调用」—— **归因错了**，与预算传递无关。
    # 同一份测试 14:06 跑全绿、20:56 与 09:11 跑就红，就是这个翻译。
    import datetime as _dtb
    try:
        hot.kline_stats("BK1036", None,
                        now=_dtb.datetime(2026, 11, 11, 10, 30), budget_s=8)
    finally:
        hot._cj = _orig
        if _hosts is not None:
            hot.VR_HOSTS = _hosts
    check(bool(seen), "未捕获到 cj 调用")
    check(seen and seen[0] is not None,
          f"预算 8s 时 cj 应收到 timeout，实得 {seen[:1]}")
    if seen and seen[0] is not None:
        check(seen[0] <= 8, f"传入的 timeout 应 ≤ 预算 8s，实得 {seen[0]}")

    # firecrawl 超时不得硬编码忽略入参
    import inspect
    _src = inspect.getsource(M._fallback_get)
    check("min(FIRECRAWL_TIMEOUT" in _src,
          "firecrawl 超时应取 min(自身上限, 传入 timeout)，"
          "而不是固定 FIRECRAWL_TIMEOUT")


# ══════════════════════════════════════════════════════════════════
# ⑬ 超长正文：削中间保首尾，不得换成「数据本轮不可用」
# ══════════════════════════════════════════════════════════════════
def t_oversize_keeps_sections_not_fake_unavailable():
    """超长正文只能削内容，不能把「数据完好」说成「数据不可用」。

    `[实测]` 我上一版在截断后调 `_fallback_text({}, [], slot, now)`：
    空 q / 空 hits 让它产出一条「市场盯盘 · 数据本轮不可用（数据源异常）」。
    于是一篇 5427 字节、数据完好的真分析，被替换成 324 字节的假消息，
    而 `push()` 返回 True —— 上层把槽位标成 done，真分析**不会补发**。
    那比不推更糟：读者会据此认为「今天没数据」，而实际是满的。
    """
    import json as _js
    import types as _tp
    import analyze as A
    sent = []

    def _fake(argv, **kw):
        _b = kw.get("input") or b""
        if isinstance(_b, bytes):
            _b = _b.decode("utf-8", "ignore")
        sent.append((list(argv), str(_b)))
        return _tp.SimpleNamespace(returncode=0, stdout='{"errcode":0}',
                                   stderr="")

    _old_cfg, _old_run = A.CFG, A.subprocess.run
    A.CFG = {"webhook": "https://example.invalid/not-real"}
    A.subprocess.run = _fake
    try:
        _body = ("> **上午小结 · 权重失血**\n\u3000\n"
                 "**触发** 半导体主力 -40亿\n\n"
                 "**盘面** 上证 3300 -0.8%\n"
                 + ("板块方面，CPO概念资金回流，光通信活跃。" * 90 + "\n")
                 + "**关注板块** CPO概念、半导体\n\n"
                   "**建议**\n> 风险 硬件链承压\n> 机会 半导体国产化\n")
        _r = A.push(_body, slot="13:30", kind="hourly")
    finally:
        A.CFG, A.subprocess.run = _old_cfg, _old_run
    _pushes = [b for a, b in sent if "-K" in a]
    check(bool(_pushes), "超长正文未产生推送")
    if _pushes:
        try:
            _c = _js.loads(_pushes[-1])["markdown"]["content"]
        except Exception:
            _c = _pushes[-1]
        check(_r is True, f"可截断的超长正文应推送成功，实得 {_r}")
        check("数据本轮不可用" not in _c,
              "超长正文被换成了「数据本轮不可用」——数据是好的，只是太长")
        for _tag in ("**关注板块**", "> 风险", "> 机会"):
            check(_tag in _c, f"截断后丢了必带段 {_tag!r}：{_c[-120:]!r}")
        check(len(_c.encode("utf-8")) > 2000,
              f"截断过度，只剩 {len(_c.encode('utf-8'))} 字节 —— "
              f"信息量不该为了长度被砍到这个程度")


# ══════════════════════════════════════════════════════════════════
# ⑭ 结构闸门判据必须对正文，不对带时间戳的文本
# ══════════════════════════════════════════════════════════════════
def t_struct_gate_uses_body_not_stamped():
    """时间戳里含「触发」二字，而它是 `_STRUCT_TAGS` 之一。

    拿 `_fin(t)` 去过闸门，会把「只有 1 个真标签」的正文涨成 2 个而放行。
    `produce_text` 里那次检查用的是未盖戳的 `txt` —— 两处必须同口径。
    """
    import analyze as A
    # ⚠ 夹具里不能出现其他结构标签。
    # `[实测]` 我第一版写了「盘面描述若干」，而「盘面」
    # 正是 `_STRUCT_TAGS` 之一 → 标签数自己就是 2，夹具前提不成立。
    # ⚠ 必须**超长**：结构闸门只在 `_truncated` 时才跑，
    # 而正文不超长就不会触发截断 → 断言无意义。
    # `[实测]` 我第一版只写了 300 多字 → `_truncated=False`
    # → 闸门未跑 → push 直接成功 → 测试报错。
    _one_tag = ("**盘面** 上证 3300 -0.8%，恒生科技 4101 -1.4%\n\n"
                + ("资金面延续分化，板块互有强弱。" * 400
                   + "\n\n> 风险 硬件链承压\n> 机会 半导体国产化\n"))
    _leak = [t for t in A._STRUCT_TAGS if t in _one_tag]
    check(_leak == ["盘面"], f"夹具只能含「盘面」一个标签，实得 {_leak}")
    _n = sum(1 for _tg in A._STRUCT_TAGS if _tg in _one_tag)
    check(_n < 2, f"夹具应有 <2 个标签，实得 {_n}")
    check(A._struct_reason(_one_tag, "", "13:30") is not None,
          "只有 1 个标签的正文应被结构闸门拒绝")
    check("触发" in A._STRUCT_TAGS, "「触发」应是结构标签（本项的前提）")

    # ⚠⚠ 上面只验证了「夹具只有 1 个标签」，与生产用哪个变量无关联
    # —— 把生产改成 `_fin(t)` 时上述断言照样全绿（第一版就是这样漏网的）。
    # 必须走完整 `push()`：正文只有 1 个真标签、超长触发截断，
    # 正确实现应判不合格 → fail-closed 不推；用 `_fin(t)` 则时间戳补上
    # 第二个「触发」→ 放行 → 推出一条只有 1 个真标签的正文。
    import types as _tp2
    _sent2 = []

    def _fake2(argv, **kw):
        _b = kw.get("input") or b""
        if isinstance(_b, bytes):
            _b = _b.decode("utf-8", "ignore")
        _sent2.append(str(_b))
        return _tp2.SimpleNamespace(returncode=0, stdout='{"errcode":0}',
                                    stderr="")

    _oc, _or = A.CFG, A.subprocess.run
    A.CFG = {"webhook": "https://example.invalid/not-real"}
    A.subprocess.run = _fake2
    try:
        # ⚠⚠ 必须给出轮次起点：`_stamp` 只在 `_ROUND_TDETECT or _ROUND_T0`
        # 非空时才构造（见 `_stamp = ""` 那段）。为空时 `_fin(t) == t`，
        # 「时间戳装带触发 两个字」这个前提根本不成立。
        # `[实测]` 我第一版没设，于是注入「用 `_fin(t)` 判」后测试照样绿。
        _ot0, _otd = A._ROUND_T0, A._ROUND_TDETECT
        A._ROUND_T0 = A._ROUND_TDETECT = 1762837800.0
        _r2 = A.push(_one_tag, slot="13:30", kind="hourly")
    finally:
        A.CFG, A.subprocess.run = _oc, _or
        A._ROUND_T0, A._ROUND_TDETECT = _ot0, _otd
    check(_r2 is False,
          "只有 1 个真结构标签的正文应 fail-closed，实得 push="
          f"{_r2}（若为 True，说明判据用了带时间戳的文本 —— 时间戳里的"
          "「触发」把标签数补成了 2）")


# ══════════════════════════════════════════════════════════════════
# ⑮ pending.json 读改写必须跨进程互斥
# ══════════════════════════════════════════════════════════════════
def t_pending_readmodify_locked():
    """并发跑 4 个 `collect._main()`，hist 必须一条不少。

    `collect` 对 `pending.json` 是「读历史 → append → 整体写回」。
    `_atomic_write_json` 只保证**单次写**不撕裂，**不保证读-改-写不丢更新**。
    丢 hist 会让【盘中变化】段少帧；丢 events 去重表会**重复推送**。

    ⚠⚠ 必须**跑生产的 `_main()`**，不能像我第一版那样只调
    `_pending_lock()` 助手 —— 那样测的是助手本身，把 `_main` 里那行
    `_pnd_lk = _pending_lock(OUT)` 改成 `None` 也不会变红（实测确认）。
    `[\u5b9e\u6d4b]` 加锁：4 进程 → hist=4；去掉锁：4 进程 → hist=3。
    """
    import subprocess as _sp
    import sys as _sys

    _probe = os.path.join(_D, "_rv11_collect_probe.py")
    with open(_probe, "w", encoding="utf-8") as f:
        f.write(
            "import os, sys, time\n"
            "sys.path.insert(0, os.environ['SRC'])\n"
            "import datetime as _dt\n"
            "import marketlib as ML\n"
            "import collect\n"
            # ⚠⚠ 必须固定到交易时段内（2026-11-11 补）：`collect._main()` 开头
            # 就检交易日与 `TH["session"]`（9:25~15:05），收盘后返回 out_of_window、
            # **一个字节都不写**。`[实测]` 15:40 跑本项 → 4 个进程全部早退
            # → hist=0，报「读改写没互斥」—— **归因错了**，与锁无关。
            "class _FDT(_dt.datetime):\n"
            "    @classmethod\n"
            "    def now(cls, tz=None):\n"
            "        return _dt.datetime(2026, 11, 11, 10, 30)\n"
            "collect.datetime = type('_D', (), {\n"
            "    'datetime': _FDT, 'date': _dt.date, 'timedelta': _dt.timedelta})()\n"
            "collect.HOLIDAYS = set()\n"
            "collect.index_secids = lambda: ['1.000001']\n"
            "collect.watch_secids = lambda: ['90.BK1036']\n"
            "collect.hk_secids = lambda: []\n"
            "collect._quotes = lambda req: {s: {'f2': 100.0, 'f3': 1.0,\n"
            "                                 'f14': s} for s in req}\n"
            "collect._trend = lambda s: None\n"
            "collect._news = lambda *a, **k: []\n"
            "collect._log = lambda *a, **k: None\n"
            # 放大读-写窗口：不这样做，丢更新窗口太窄、偶然才丢
            "_orig_load = ML._load_json\n"
            "def _slow_load(p, d=None):\n"
            "    r = _orig_load(p, d)\n"
            "    if str(p).endswith('pending.json'):\n"
            "        time.sleep(0.12)\n"
            "    return r\n"
            "ML._load_json = _slow_load\n"
            "collect._main()\n")
    _src = os.path.join(os.path.dirname(_HERE), "src")
    _env = dict(os.environ, SRC=_src, MW_DIR=_D,
                PYTHONDONTWRITEBYTECODE="1")
    _ps = []
    for _i in range(4):
        _ps.append(_sp.Popen([_sys.executable, "-B", _probe], env=_env,
                             stdout=_sp.DEVNULL, stderr=_sp.DEVNULL))
    for _pr in _ps:
        _pr.wait()
    # ⚠ `pending.json` 在 `state/` 下，**不在 `state/cache/`**。
    # `[实测]` 我第一版按 `state/cache/` 读，于是永远读不到 →
    # `_n` 恒为 0 → 测试恒红（而不是因为没加锁）。
    _p = os.path.join(_D, "state", "pending.json")
    _d = M._load_json(_p, {}) or {}
    _n = len(_d.get("hist") or [])
    check(_n == 4, f"4 个并发采集应保住 4 条 hist，实得 {_n} 条"
                   f"（丢了 {4 - _n} 条 —— 读改写没互斥）"
                   f"｛探针落盘文件：{_d.get('time')!r}｝")


# ══════════════════════════════════════════════════════════════════
# ⑯ 最终尺寸裁剪不得把尾部必带段切残
# ══════════════════════════════════════════════════════════════════
def t_final_trim_keeps_tail_sections():
    """`_safe` / 结构闸门跑完之后，还有一段尺寸裁剪在改正文。

    原写只削尾巴 `t = t[:-1]`，削完**不再复核** —— 于是可能得到
    「> 机会 半导体国产」这样的半句，而风险行连同前半句一起没了。
    `[实测]` 裁剪后正文末尾是 `> 机会 半导体国产`（残句），
    而 `_safe` 与结构闸门检查的都是裁剪前的文本。
    """
    import json as _js
    import types as _tp
    import analyze as A
    _sent = []

    def _fake(argv, **kw):
        _b = kw.get("input") or b""
        if isinstance(_b, bytes):
            _b = _b.decode("utf-8", "ignore")
        _sent.append(str(_b))
        return _tp.SimpleNamespace(returncode=0, stdout='{"errcode":0}',
                                   stderr="")

    _oc, _or = A.CFG, A.subprocess.run
    _ot0, _otd = A._ROUND_T0, A._ROUND_TDETECT
    A.CFG = {"webhook": "https://example.invalid/not-real"}
    A.subprocess.run = _fake
    # 固定轮次起点：否则 `_stamp` 为空、尺寸判据与生产不同源
    A._ROUND_T0 = A._ROUND_TDETECT = 1762837800.0
    try:
        _body = ("> **上午小结**\n\u3000\n**触发** 半导体 -40亿\n\n"
                 "**盘面** 上证 3300\n"
                 + ("板块方面，CPO概念资金回流。" * 200 + "\n")
                 + "**关注板块** CPO、半导体\n\n**建议**\n"
                   "> 风险 硬件链承压\n> 机会 半导体国产化\n")
        A.push(_body, slot="13:30", kind="hourly")
    finally:
        A.CFG, A.subprocess.run = _oc, _or
        A._ROUND_T0, A._ROUND_TDETECT = _ot0, _otd
    check(bool(_sent), "未捕获到推送")
    if _sent:
        try:
            _c = _js.loads(_sent[-1])["markdown"]["content"]
        except Exception:
            _c = _sent[-1]
        check("> 风险" in _c, f"发出的正文已丢风险段：{_c[-80:]!r}")
        check("> 机会" in _c, f"发出的正文已丢机会段：{_c[-80:]!r}")
        # 机会行必须是完整句（末尾不能是被砍断的半句）
        _tail = _c.split("> 机会", 1)[-1]
        _tail = _tail.split("\n\u3000")[0].strip()
        check(_tail.endswith("。") or _tail.endswith("化") or len(_tail) > 6,
              f"机会行被截成残句: {_tail!r}")


# ══════════════════════════════════════════════════════════════════
# ⑰ 占位行剔除必须两条路径共用
# ══════════════════════════════════════════════════════════════════
def t_placeholder_strip_both_paths():
    """AI 正常路径也要剔占位行，不能只在兜底分支剔。

    `_strip_placeholder_lines` 原先只在「AI 不可用 → 兜底模板」那支调用。
    而结构闸门只拦「（暂无数据）」「（暂无资金数据）」，**不拦「（未提供）」**
    —— 后者在 `_PLACEHOLDER_RE` 里明明有。
    `[实测]` 一份含裸「（未提供）」、其余结构完整的正文，闸门判「合格（放行）」，
    于是那行孤零零地进了推送，读者不知道缺的是什么。
    """
    import inspect
    import analyze as A
    # ① 函数本身要能剔「（未提供）」
    _b = ("**触发** x\n\n**盘面** 上证 3300\n（未提供）\n\n"
          "**关注板块** CPO\n\n**建议**\n> 风险 r\n> 机会 o\n")
    _o = A._strip_placeholder_lines(_b)
    check("（未提供）" not in _o, f"未剔掉占位行: {_o!r}")
    check("**关注板块**" in _o and "> 机会" in _o,
          "剔占位行时连真实内容也删了")
    # ② 生产代码里它必须**在兜底分支之外**也被调用
    #   （否则 AI 正常路径仍会漏剔）
    _src = inspect.getsource(A.produce_text)
    _idx_fb = _src.find("_fallback_text(q, hits, slot, now, hot=hot)")
    _idx_st = _src.find("_strip_placeholder_lines(txt)")
    check(_idx_fb >= 0 and _idx_st >= 0, "生产源码里找不到两个关键调用")
    if _idx_fb >= 0 and _idx_st >= 0:
        check(_idx_st > _idx_fb,
              "占位行剔除应在兜底分支**之后**、两路共用的位置")
        # 确认它不是写在那个 if 块里（缩进 8 空格则在块内）
        _line = [ln for ln in _src.split("\n") if "_strip_placeholder_lines(txt)" in ln][0]
        check(not _line.startswith("        "),
              f"打印位置仍在 if 块内（缩进过深）：{_line!r}")


# ══════════════════════════════════════════════════════════════════
# ⑱ 量比取数失败不得冒充「量比1.0」
# ══════════════════════════════════════════════════════════════════
def t_vr_missing_not_shown_as_one():
    """K 线取数失败时 `vr=None` 被替换成 1.0，正文显示「量比1.0」。

    与「缺失金额显示 ≈0」是同一类缺陷：读者会把 1.0 当成实测的
    「成交量正常」，而事实是**没取到**。
    `[实测]` 修复前：`_vr_ok=False` 与 `vr=1.8` 渲染完全无法区分。
    """
    import analyze as A
    import hot as _H
    # ① **数据层**：走真实的 `hot.analyze()`，验证 K 线取数失败时会标 `_vr_ok=False`。
    # ⚠ 上一版只直接构造 `{"_vr_ok": False}` 字典 → 测的是**渲染器**，
    # 把 hot.py 里那行 `_vr_ok = bool(M.isnum(vr))` 改成 `True` 也不会变红（实测确认）。
    _row = {"f12": "BK001", "f14": "AI应用", "_tag": "ind", "f3": 2.0,
            "f24": -1.0, "f6": 4e8, "f134": 10.0, "f184": 10.0, "f69": 8.0,
            "f10": 1.0, "f8": 2.0, "f62": 5e8, "f104": 30, "f105": 10,
            "f128": "龙头"}
    _ofs, _omb, _oks = _H.fetch_sectors, _H.members, _H.kline_stats
    _H.fetch_sectors = lambda: [dict(_row)]
    _H.members = lambda code: set(str(i) for i in range(12))
    _H.kline_stats = lambda *a, **k: (None, None)      # K 线取数失败
    try:
        _hr = _H.analyze()
    finally:
        _H.fetch_sectors, _H.members, _H.kline_stats = _ofs, _omb, _oks
    _hs = (_hr.get("strong") or [None])[0]
    check(_hs is not None, "hot.analyze() 未产出 strong 行（夹具失效）")
    if _hs is not None:
        check(_hs.get("_vr_ok") is False,
              f"K 线取数失败时应标 `_vr_ok=False`，实得 {_hs.get('_vr_ok')!r}")

    _base = dict(name="半导体", chg=-2.0, f24=-5.0, _f24_ok=True,
                 pct=3.0, lead="某某", amt=4e8, stage="延续")
    _ok_row = {**_base, "vr": 1.8, "_vr_ok": True}
    _bad_row = {**_base, "vr": 1.0, "_vr_ok": False}
    _old_row = {**_base, "vr": 1.0}          # 旧缓存：无标记

    def _line(r):
        _out, _bad = A._hot_strong_lines(
            {"strong": [r], "start": [], "top_up": [], "top_dn": [], "pool": 0})
        return _out[0] if _out else ""

    _l_ok, _l_bad, _l_old = _line(_ok_row), _line(_bad_row), _line(_old_row)
    check(bool(_l_ok) and bool(_l_bad), f"渲染失败: {_l_ok!r} / {_l_bad!r}")
    check("量比1.8" in _l_ok, f"可信量比应显示数字: {_l_ok!r}")
    check("量比未知" in _l_bad,
          f"量比取数失败时应显示「量比未知」，"
          f"实得: {_l_bad!r}")
    check("量比1.0" not in _l_bad,
          f"取数失败却显示了「量比1.0」: {_l_bad!r}")
    # 旧缓存（hot_cache 升级前写的行）没有标记，应保持原样
    check("量比1.0" in _l_old,
          f"旧缓存无 `_vr_ok` 标记时应保持兼容，实得: {_l_old!r}")


# ══════════════════════════════════════════════════════════════════
# ⑲ ISO-8601 时间戳必须能解析
# ══════════════════════════════════════════════════════════════════
def t_iso_timestamp_parsed():
    """带 `T` 分隔符或带时区的 ISO-8601 时间戳曾一律解析失败。

    `_is_ts_stale` 对 None 的处理是「宁可放行」→ 陈旧行被当新鲜使用，
    且备用源会继续选中旧价、不再尝试下一个源。
    `[实测]` 修复前 `_ts_parse("2026-11-10T15:00:00+08:00")` 返回 None。
    """
    import marketlib as _M
    for _ts in ("2026-11-10T15:00:00+08:00", "2026-11-10T15:00:00",
                "2026-11-10 15:00:00", "20261110150000",
                "2026/11/10 15:00:00"):
        _p = _M._ts_parse(_ts)
        check(_p is not None, f"ISO/常见格式解析失败: {_ts!r}")
        if _p is not None:
            check((_p.year, _p.month, _p.day, _p.hour, _p.minute) ==
                  (2026, 11, 10, 15, 0),
                  f"解析结果不对: {_ts!r} -> {_p}")


# ══════════════════════════════════════════════════════════════════
# ⑳ 推送超时不得被当成「未发出」而去重失效
# ══════════════════════════════════════════════════════════════════
def t_push_timeout_keeps_dedup():
    """curl 超时 = **投递结果未知**，服务端可能已经收到。

    `[实测]` 本机起一个「收到请求后拖 25 秒才响应」的服务器，curl 带
    `--max-time 3` 发出 → rc=28 超时，**而服务端确实收到了该请求**。
    原写超时直接 `return False`，上层据此 `_rollback()` 摘掉去重键
    → 下一轮 detect() 的 busy 不再含它 → 同一事件**重复推送**。
    """
    import types as _tp
    import analyze as A
    _sent = []

    def _fake(argv, **kw):
        _b = kw.get("input") or b""
        if isinstance(_b, bytes):
            _b = _b.decode("utf-8", "ignore")
        _sent.append(str(_b))
        raise A.subprocess.TimeoutExpired(cmd=argv, timeout=20)

    _oc, _orun = A.CFG, A.subprocess.run
    A.CFG = {"webhook": "https://example.invalid/not-real"}
    A.subprocess.run = _fake
    try:
        A._PUSH_OUTCOME_UNKNOWN = False
        _r = A.push("**盘面** 测试\n\n**建议**\n> 风险 x\n> 机会 y",
                    slot="13:30", kind="hourly")
        _flag = A._PUSH_OUTCOME_UNKNOWN
    finally:
        A.CFG, A.subprocess.run = _oc, _orun
        A._PUSH_OUTCOME_UNKNOWN = False
    check(bool(_sent), "未发出请求（夹具未走到 curl）")
    check(_r is False, f"超时仍应返回 False（确实不知道成不成），实得 {_r}")
    check(_flag is True,
          "超时应标记「投递结果未知」（否则上层会回滚去重键 → 重复推送）")

    # ⚠⚠ 上面只能证明 `push()` 自己把标记写到了模块级。
    # 但**消费端**在 `_run_after_inflight`，它也必须有 `global` 声明 ——
    # 否则那里的赋值（每轮推送前重置为 False）变成局部变量，
    # 而 `push()` 写的是另一个（模块级的）—— 两边各写各的，标记永远对不上。
    # `[实测]` 我把 global 错加在 `run()` 里（那里不用这个变量），
    # 于是 `_run_after_inflight` 里的赋值失效；而日志照样写「保留去重键」——
    # **行为与日志相反**，比不写日志更坏。
    import ast as _ast
    _tree = _ast.parse(open(
        os.path.join(os.path.dirname(_HERE), "src", "analyze.py"),
        encoding="utf-8").read())
    for _fn in _ast.walk(_tree):
        if not isinstance(_fn, _ast.FunctionDef):
            continue
        _uses = [n for n in _ast.walk(_fn)
                 if isinstance(n, _ast.Name) and n.id == "_PUSH_OUTCOME_UNKNOWN"]
        if not _uses:
            continue
        _has_g = any(isinstance(n, _ast.Global)
                     and "_PUSH_OUTCOME_UNKNOWN" in n.names
                     for n in _ast.walk(_fn))
        check(_has_g, f"函数 {_fn.name}() 使用了 "
                      f"`_PUSH_OUTCOME_UNKNOWN` 但没有 `global` 声明 "
                      f"—— 那里的赋值会变成局部变量，"
                      f"标记与消费端对不上")


def main():
    for fn in (t_lunch_same_day, t_fx_date_only, t_orphan_attribution,
               t_stall_not_while_running, t_missing_amount_not_zero,
               t_quota_warn_single_write, t_atomic_write_result_honest,
               t_news_struct_not_healthy_empty,
               t_beat_lock_and_tmp, t_truncation_keeps_required_sections,
               t_fetch_budget_reaches_request,
               t_oversize_keeps_sections_not_fake_unavailable,
               t_struct_gate_uses_body_not_stamped,
               t_pending_readmodify_locked,
               t_final_trim_keeps_tail_sections,
               t_placeholder_strip_both_paths,
               t_vr_missing_not_shown_as_one,
               t_iso_timestamp_parsed,
               t_push_timeout_keeps_dedup):
        try:
            fn()
        except Exception as e:
            _FAIL.append(f"{fn.__name__} 抛异常: {type(e).__name__}: {e}")
    if _FAIL:
        for m in _FAIL:
            print(f"  ❌ {m}")
        print(f"FAIL ({len(_FAIL)} 处)")
        return 1
    print("ALL PASS (午休当日限定/汇率纯日期/孤儿归因/停摆不误报/金额未知/配额单次落盘/原子写诚实/新闻结构/截断必带段/预算穿链/超长不造假/标签口径/pending锁/尾部未残/占位行共用/量比未知/ISO时间戳/超时不重推)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
