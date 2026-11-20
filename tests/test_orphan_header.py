"""占位行剔除不得留下「孤儿段头」（2026-11-20 实推缺陷）。

复现背景：生产 5 条推送里出现过光秃秃的段头、下面空无一字 ——
    11-17 09:22:59  **热点**
    11-19 09:23:31  **热点**
    11-19 10:04:45  **热点**
    11-20 11:42:12  **热点**
    11-13 09:19:25  **今日涨幅 TOP3**
成因：模板在候选池为空时写的是
    `**热点**\\n  （本段无数据）`
而 `_strip_placeholder_lines()` 把「（本段无数据）」当占位删掉，
**段头却留了下来**。读者看到"热点"标题、下面什么都没有，像渲染坏了；
而我们自己的日志还写着「占位内容已剔除」——等于宣称干净了。

这里锁两件事：
  ① 孤儿段头必须连段头一起删（不留空壳标题）
  ② 不得误伤：段头下有真实正文、或占位夹在行内的情况，一律保留
"""
import os, sys, tempfile, atexit, shutil
_d = tempfile.mkdtemp(prefix="mw_orph_")
os.environ["MW_DIR"] = _d
atexit.register(shutil.rmtree, _d, ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))
import analyze as A

A._log = lambda *a, **k: None
_ok = 0


def check(cond, msg):
    global _ok
    if not cond:
        raise SystemExit("  ❌ " + msg)
    _ok += 1


def _bare_headers(t):
    """返回『段头下面没有任何正文』的段头行。"""
    _ls = t.split("\n")
    _bad = []
    for _i, _l in enumerate(_ls):
        _s = _l.strip()
        if not _s.startswith("**") or _s.startswith("> "):
            continue
        # 只看「整行就是个段头」的形态
        import re as _re
        if not _re.fullmatch(r"\*\*[^*]+\*\*(（[^）]*）)?", _s):
            continue
        _nxt = next((x.strip() for x in _ls[_i + 1:] if x.strip()), None)
        # ⚠️ 只有「另一个段头」才说明本段没正文。
        # `> ` 是**建议段的正文**（引用块），不能当成新段 —— 否则
        # `**建议**\n> 风险 x` 会被误判成孤儿段头（第一版就错了）。
        if _nxt is None or _nxt.startswith("**"):
            _bad.append(_s)
    return _bad


# ---------- ① 孤儿段头必须被连根删掉 ----------
for _name, _src in [
    ("热点（两空格缩进的占位）", "**热点**\n  （本段无数据）\n\n**关注板块**\n半导体 -2%（-1 亿）\n"),
    ("热点（无缩进的占位）", "**热点**\n（本段无数据）\n\n**关注板块**\nA -1%\n"),
    ("涨跌榜（同一机制）", "**今日涨幅 TOP3**\n  （本段无数据）\n\n**建议**\n> 风险 x\n"),
    ("占位后直接到末尾", "**建议**\n> 风险 x\n\n**热点**\n  （本段无数据）\n"),
]:
    _out = A._strip_placeholder_lines(_src)
    check(not _bare_headers(_out),
          f"{_name}：仍留有孤儿段头 {_bare_headers(_out)}")
    check("本段无数据" not in _out, f"{_name}：占位行没删干净")

# ---------- ② 不得误伤：段头下面有真实正文 → 段头必须留 ----------
_keep = A._strip_placeholder_lines("**热点**\n  银行 +1.3%\n")
check("**热点**" in _keep, "段头下有真实正文时被误删了段头")
check(_bare_headers(_keep) == [], f"正常段被误判成孤儿：{_bare_headers(_keep)}")

# ---------- ②b 段头 + 引用块正文 = 正常段（建议段就是这形态）----------
_quote = "**建议**\n  （本段无数据）\n> 风险 真实判断\n"
_out_q = A._strip_placeholder_lines(_quote)
check("**建议**" in _out_q,
      f"`> ` 是引用块正文，不是新段头 —— 段头下紧跟 `> ` 时必须保留段头，"
      f"实得 {_out_q!r}")
check("> 风险 真实判断" in _out_q, "引用正文被删掉了")

# ---------- ③ 不得误伤：占位夹在行内是「部分缺失」的真实描述 ----------
_inline = "**盘面** 上证 -1.5%，半导体（本段无数据）之外全部下跌。\n"
check(A._strip_placeholder_lines(_inline) == _inline,
      "行内夹占位的真实描述被改动了 —— 那会隐瞒部分缺失")

# ---------- ④ 全空的极端情形：整篇只剩占位 → 原样返回（不退化成空推送）----------
_only = "**热点**\n  （本段无数据）\n"
check(A._strip_placeholder_lines(_only).strip() != "",
      "整篇只剩占位时应原样返回，不能变成空推送")

# ---------- ⑤ 真实生产形状：pool=0 的整点播报不得出现空热点段 ----------
import datetime
_hot = {"strong": [], "pool": 0, "start": []}
_now = datetime.datetime(2026, 11, 20, 11, 35)
_txt, _ok2, _h = A._fallback_text({}, [("hourly_11:30", "整点播报 11:30")],
                                  "hourly", _now, hot=_hot)
_final = A._strip_placeholder_lines(_txt)
check("**热点**" not in _final,
      f"候选池为空时仍留空热点段头：{_final[:200]!r}")

print(f"ALL PASS (孤儿段头 {_ok} 个断言)")
