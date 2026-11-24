#!/usr/bin/env python3
"""`_split_wall_lines`必须在任何宽度下都终止，且不改内容（2026-11-21）。

##为什么需要这个测试

`[实测]`首行正好88字时，`_split_wall_lines` **无限循环**：

首段=87 -> rows=[88,3,88,4]
首段=88 -> HANG(>2s CPU loop) <=卡死
首段=89 -> rows=[88,88,7]

成因是**进度归零**：首行填满到正好88后，循环底部算出的 `_left`是0，
于是 `_cut = _left =0` -> `_p = _p[0:]`长度不变 ->永远原地转。

后果不是"少推一条"，而是 analyze进程挂死：`watch.lock`不释放，
launchd每300s起的新轮全被挡，当天后续推送**全部消失**。

旧的 `test_push_layout.py`抓不到：它只断言"去空白后拼回等于原文"+
"每行 <=88"，而这两个判据**在死循环面前根本走不到**（进程已经卡住）。
所以这里必须**带超时**跑，否则测试自己也会挂死。

##判据

1.各种首段宽度（含88）都必须终止；
2.每行 <=88（含段头）；
3.去空白拼回等于原文（不丢字、不重复）。
"""
import os
import signal
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

# ⚠️⚠️ 必须在 `import analyze` **之前**把 MW_DIR 指向临时目录。
# `_split_wall_lines` 命中时会 `_log(...)`，而 `_log` 写的是
# `MW_DIR/logs/watch.log` —— 不沙箱就是**生产日志**。
# `[实测]` 本用例单跑一次就往生产 watch.log 里写了 **751 行**
# （它对几千个随机串调拆行，每次命中都记一条）。
# `tests/run_all.sh` 会给每个用例建沙箱，所以 CI 与全量跑都不受影响 ——
# 但**直接跑单个用例**（开发时最常用）会污染生产。
os.environ.setdefault("MW_DIR", tempfile.mkdtemp(prefix="mw_t_splithang_"))

import analyze as A # noqa: E402

_ok = [0]
_LIMIT = A._SPLIT_LIMIT


def check(cond, msg):
	if cond:
		_ok[0] +=1
	else:
		print(f" ❌ {msg}")
		raise SystemExit(1)


def _alarm(*_a):
	raise TimeoutError("hang")


def split_guarded(text, seconds=5):
	"""带硬超时的调用：死循环时抛 TimeoutError，而不是把测试挂死。"""
	signal.signal(signal.SIGALRM, _alarm)
	signal.alarm(seconds)
	try:
		return A._split_wall_lines(text)
	except TimeoutError:
		return None
	finally:
		signal.alarm(0)


def _flat(s):
	return "".join(s.split())


# ----------①首段宽度扫描（88是历史死循环点）----------
for _w in range(_LIMIT -6, _LIMIT +8):
	#段头 `**盘面** `占9字；把正文补到首段正好 _w字
	_body = "甲" * (_w -9 -1) + "；" + "乙" *90
	_text = "**盘面** " + _body
	_out = split_guarded(_text)
	check(_out is not None, f"首段={_w}字时 _split_wall_lines没有终止（死循环回归）")
	_rows = _out.split("\n")
	check(max(len(r) for r in _rows) <= _LIMIT,
		f"首段={_w}字时出现超宽行：{[len(r) for r in _rows]} > {_LIMIT}")
	check(_flat(_out) == _flat(_text),
		f"首段={_w}字时内容被改动：拼回与原文不一致")

# ----------②无标点长段（走硬切分支）----------
_long = "**资金** " + "丙" *300
_out = split_guarded(_long)
check(_out is not None, "无标点300字长段没有终止")
_rows = _out.split("\n")
check(max(len(r) for r in _rows) <= _LIMIT,
	f"无标点长段出现超宽行：{[len(r) for r in _rows]}")
check(_flat(_out) == _flat(_long), "无标点长段内容被改动")

# ----------③只有逗号没有顿号（曾经把「资金」劈成「资」+「金」）----------
_comma = "**热点** " + "丁" *40 + "，" + "戊" *40 + "，"
_out = split_guarded(_comma)
check(_out is not None, "逗号长段没有终止")
check(max(len(r) for r in _out.split("\n")) <= _LIMIT, "逗号长段出现超宽行")

# ----------④短行不得被拆 ----------
_short = "**盘面**一句话。"
check(A._split_wall_lines(_short) == _short, "短行被无谓拆开")

# ----------⑤形态 × 首段宽度的全量扫描（2026-11-22）----------
#只扫 `_LIMIT-6..+7` 一种形态是不够的：回归可能出在「不可断长串」
#「纯数字」「大量空格」这些各自走不同 `_cut` 分支的路径上，
#而且这些宽度在 86~94 之外一样会触发。判据仍是终止/不超宽/不丢字。
_SHAPES = {
	"顿逗分隔": lambda w: "**盘面**" + "甲" * w + "；" + "乙" * 90,
	"不可断串": lambda w: "**资金**" + "x" * w + "，" + "y" * 60,
	"无标点": lambda w: "**热点**" + "丙" * w,
	"纯数字": lambda w: "**资金**" + ("1234567890" * (w // 10 + 1))[:w],
	"含空格": lambda w: "**盘面**" + ("涨 " * (w // 2 + 1))[:w] + "；尾",
}
for _name, _mk in _SHAPES.items():
	for _w in range(1, 201):
		_text = _mk(_w)
		_out = split_guarded(_text, seconds=3)
		check(_out is not None,
			f"[{_name}]首段={_w}字时没有终止（死循环回归）")
		check(max(len(r) for r in _out.split("\n")) <= _LIMIT,
			f"[{_name}]首段={_w}字时出现超宽行")
		check(_flat(_out) == _flat(_text),
			f"[{_name}]首段={_w}字时内容被改动（丢字或多字）")

print(f"ALL PASS (拆行终止性/宽度/内容保真 {_ok[0]}个断言)")
