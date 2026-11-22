#!/usr/bin/env python3
"""测试沙箱**必须真的隔离**：`paths.refresh()`与 `MW_DIR`同源（2026-11-22 P0）。

##这个测试守的是什么事故

`analyze.push()`会往 `state/history/pushes.jsonl`追加一条投递记录。三个测试
（`test_slot_advice_output` / `test_audit_fixes_20261121` /早期版本）都调它，
都自称"不碰生产 state"。**实际是写穿的**：

`paths.DIR`及五个子目录常量**只在 import时算一次**，而
`runlog.py` / `marketlib.py`写的是 `from paths import HIST_DIR, CACHE_DIR …`
——那是**值拷贝**。于是「先 import paths（DIR固化为生产根）、后设 MW_DIR」
这个顺序，会让 `runlog.PUSHES`一直指向生产文件。

`[实测]`2026-11-22：同一段代码，`analyze.p_hist("pushes.jsonl")`返回
`/Users/…/.market-watch/state/history/pushes.jsonl`，一次 `push()`把合成
数据（`板块0主力净流出0亿…`）写进**生产**流水并标记 `ok: true`。
生产 `pushes.jsonl`里因此留下2条假记录（11-1915:59、11-2211:20）。

##为什么批量跑抓不到

`tests/run_all.sh`给每个测试**独立子进程**并**先设 MW_DIR**再启动 ——
那时 `paths.DIR`还没算，所以正确。**单独 `python3 tests/xxx.py`就写穿**，
而且不报错、不留痕。这正是「本地绿 ≠安全」的一种形态：
闸门（沙箱）在批量路径上生效，在单跑路径上失效。

##判据

1. `paths.refresh()`存在且幂等；
2.设 `MW_DIR`后调 `refresh()`，`p_hist` / `p_state` / `p_cache`全部转向新根；
3. **已 import的消费方**（`runlog.PUSHES`/`RUNS`、`marketlib.CACHE_DIR`）
也一起转 ——这是"值拷贝"那一层的接线，只改 `paths`自己不够；
4. `refresh()`不创建任何文件/目录（只改变量）；
5.三个会 `push()`的测试都必须**先设 MW_DIR（或先 refresh）再 import analyze**，
不许出现"先 import analyze再设 MW_DIR"的顺序。
"""
import io
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

_ok = [0]


def check(cond, msg):
	if cond:
		_ok[0] +=1
	else:
		print(f"❌ {msg}")
		raise SystemExit(1)


import paths # noqa: E402

# ----------①refresh存在且是函数----------
check(callable(getattr(paths, "refresh", None)),
 "paths没有 refresh() ——沙箱无法在运行期改根")

_orig = paths.DIR
_tmp = tempfile.mkdtemp(prefix="mw_iso_")
try:
	# ----------②refresh后 p_*全部转向新根----------
	os.environ["MW_DIR"] = _tmp
	paths.refresh()
	check(paths.DIR == _tmp, f"refresh后 DIR仍为 {paths.DIR}，应为 {_tmp}")
	for _fn, _name in ((paths.p_hist, "history"),
	 (paths.p_state, "state"),
	 (paths.p_cache, "cache"),
	 (paths.p_conf, "conf"),
	 (paths.p_log, "log")):
		_p = _fn("x")
		check(_p.startswith(_tmp),
		 f"refresh后 p_{_name}仍指向旧根：{_p}")
	# ----------③幂等----------
	paths.refresh()
	check(paths.DIR == _tmp, "refresh不幂等（第二次调用改了根）")
	# ----------④不创建文件/目录----------
	_before = sorted(os.listdir(_tmp))
	paths.refresh()
	check(sorted(os.listdir(_tmp)) == _before,
	      "refresh()创建了文件/目录（应当只改变量）")

	# ----------⑤已 import的消费方跟着转----------
	import runlog as _R
	_old_run = getattr(_R, "PUSHES", "")
	if _old_run:
		check(_old_run.startswith(_tmp),
		 f"runlog.PUSHES没跟着 refresh转：{_old_run}"
		 " ——它用 `from paths import HIST_DIR`拷了值，"
		 "写流水会落回生产根")
	check(getattr(_R, "RUNS", "").startswith(_tmp),
	      f"runlog.RUNS没跟着 refresh转：{getattr(_R, 'RUNS', '')}")
finally:
	os.environ.pop("MW_DIR", None)
	# ⚠️必须**显式**传回原目录：refresh()不传参时读 MW_DIR，
	#而它刚被 pop ——于是回落到生产根。套件内原目录是 run_all给的
	#沙箱，单独跑时才是生产根，两种情形都得恢复成"原来那个"。
	paths.refresh(_orig)
	check(paths.DIR == _orig, f"恢复后 DIR应为 {_orig}，实得 {paths.DIR}")

# ----------⑥三个 push测试的沙箱顺序----------
#
#判据分两种写法，都接受，但**必须**满足其中一种：
#①文件级：`MW_DIR`赋值出现在**任何** `import analyze`之前
# （最干净：paths尚未 import，DIR首次计算就拿到沙箱）；
#②函数级：在**同一个函数体内**，`refresh()`紧跟 `import analyze`之前
# （用于文件顶部要先 import analyze做纯函数测试的用例）。
#
# ⚠️不能只看全文第一个 `MW_DIR`的位置 —— `test_slot_advice_output`的
#文件顶部就要 import analyze测纯函数（合法），隔离在后面的沙箱函数里。
_SRC_OF = {}
for _f in ("test_slot_advice_output.py", "test_audit_fixes_20261121.py",
 "test_review_20261111.py"):
	with io.open(os.path.join(ROOT, "tests", _f), encoding="utf-8") as _fh:
		_SRC_OF[_f] = _fh.read()

for _f, _s in _SRC_OF.items():
	if "push(" not in _s:
		continue
	check("MW_DIR" in _s,
	      f"{_f}调 push()却完全没设 MW_DIR ——会写穿生产 pushes.jsonl")

	_first_an = _s.find("import analyze")
	_file_dir = _s.find('os.environ["MW_DIR"]')
	_file_ref = _s.find("refresh()")
	_file_ok = ((_file_dir >0 and _file_dir < _first_an) or
	            (_file_ref >0 and _file_ref < _first_an))

	#再找**每个**包住 refresh()的函数，检查其内部顺序
	_func_ok = False
	for _m in re.finditer(r"def (\w+)\([^)]*\):\n(.*?)(?=\ndef |\Z)",
	 _s, re.S):
		_body = _m.group(2)
		_r = _body.find("refresh()")
		_a = _body.find("import analyze")
		if _r >=0 and _a > _r:
			_func_ok = True
			break

	check(_file_ok or _func_ok,
	      f"{_f}既没有文件级 MW_DIR/refresh（早于 import analyze），"
	      f"也没有函数级 refresh()->import analyze的顺序 —— "
	      f"paths.DIR已固化为生产根，push()会写穿")

# ----------⑦生产 pushes.jsonl里不得有**新增**合成数据----------
#
# ⚠️历史污染**故意保留**：2026-11-1915:59:35与2026-11-2211:20:27这两条
#是真实发生过的写穿（前者还真的投进了群），删掉就等于掩盖事故。
#判据改成「不得**新增**」：白名单只认这两条既成事实，再有新的立刻红。
_KNOWN_POLLUTION = {"2026-11-19 15:59:35",
 "2026-11-22 11:20:27"}
_pushes = os.path.join(ROOT, "state", "history", "pushes.jsonl")
if os.path.exists(_pushes):
	_bad = []
	with io.open(_pushes, encoding="utf-8") as _fh:
		for _ln in _fh:
			if not (re.search(r"板块\d+\s*主力净流出\s*\d+\s*亿", _ln)
			 or "测试正文" in _ln):
				continue
			try:
				_t = json.loads(_ln).get("t", "")
			except Exception:
				_t = ""
			if _t not in _KNOWN_POLLUTION:
				_bad.append(_ln[:80])
	check(not _bad,
	      f"生产 pushes.jsonl新增了 {len(_bad)}条合成数据 —— "
	      f"测试又写穿了生产：{_bad[:2]}")

print(f"ALL PASS (沙箱真隔离 {_ok[0]}个断言)")
