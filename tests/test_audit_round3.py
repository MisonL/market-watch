#!/usr/bin/env python3
"""第三轮审计修复的回归测试（2026-11-21/22）。

覆盖：拆行死循环之外的面板/行情校验/配额/进程组五处。每条都对应一个
**已实测的缺陷**，注释里写明"原行为是什么、用户会看到什么"。
"""
import datetime
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

_ok = [0]


def check(cond, msg):
	if cond:
		_ok[0] +=1
	else:
		print(f" ❌ {msg}")
		raise SystemExit(1)


# ══════════①面板：空串不是回环地址 ══════════
import webui as W # noqa: E402

check(W._is_loopback("") is False,
	"MW_HOST=\"\"被当成回环 ->绕过「拒绝启动」守卫，零鉴权面板绑到0.0.0.0")
check(W._is_loopback("localhost") is True, "localhost应判为回环")
check(W._is_loopback("0.0.0.0") is False, "0.0.0.0不是回环")

# ══════════②面板：非交易时段不得判「采集停摆」 ══════════
_h = W.collect_health()
if not _h.get("in_session"):
	check(_h.get("stale") is False,
		"非交易时段仍判 stale=True ->每个交易日16:05后和整个周末，"
		"面板都红字「疑似停摆」（实测周六 age=110720s）")

# ══════════③面板：页面必须自己刷新 ══════════
check("setInterval(" in W.PAGE,
	"页面没有 setInterval ->打开后永不刷新，09:20开到11:00仍显示09:20快照")
check("visibilitychange" in W.PAGE,
	"切回标签页不立即刷新 ->最多要等一个周期才看到最新状态")

# ══════════④面板：窗口函数读的是已合并常量 ══════════
_i0 = W.PAGE #触发模块级初始化
_src_w = open(os.path.join(ROOT, "src", "webui.py"), encoding="utf-8").read()
_seg = _src_w.split("def _slot_window_grace",1)[1].split("def _slot_min",1)[0]
check("_FALLBACK_SLOT_TTL" not in _seg,
	"_slot_window_grace仍读兜底副本 -> analyze侧改了 TTL，面板按旧值判"
	"「还在窗口内」，与真实上界再次分叉")

# ══════════⑤行情校验：f3错位与荒谬点位必须被剔 ══════════
import marketlib as M # noqa: E402

check(M._valid_quotes({"1.000001": {"f2":3533.79, "f3": -1.23}}) is not None,
	"正常行被误剔")
check(M._valid_quotes({"1.000001": {"f2":3533.79, "f3":60.64}}) is None,
	"f3=+60.64仍放行 -> alert的3σ会推出「上证 +60%」假异动")
check(M._valid_quotes({"1.000001": {"f2":999999.0, "f3":0.1}}) is None,
	"f2=999999仍放行 ->荒谬点位进快照与推送")
check(M._valid_quotes({"90.BK0459": {"f2":41078.32, "f3":1.0}}) is not None,
	"板块指数（41078）被误剔 ——上限必须高于真实最高值")

# ══════════⑥配额：跨日必须重置 warned_tier ══════════
#必须走**子进程**：marketlib在首次 import时就固化了 state路径，
#同进程改 MW_DIR再 reload并不保证生效（本测试第一版就是这么假失败的）。
_d = tempfile.mkdtemp(prefix="mw_r3_")
os.makedirs(os.path.join(_d, "state"), exist_ok=True)
os.makedirs(os.path.join(_d, "logs"), exist_ok=True)
_yday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
_rf = os.path.join(_d, "state", "rate.json")
with open(_rf, "w", encoding="utf-8") as _f:
	json.dump({"date": _yday, "count":1850, "warned_tier":9}, _f)
_probe = (
	"import json,sys;sys.path.insert(0,%r);"
	"import marketlib as M;"
	"r=M.RateLimiter(daily_cap=10);r._roll();r._reserve();"
	"print(json.load(open(%r)).get('warned_tier','ABSENT'))"
	% (os.path.join(ROOT, "src"), _rf))
_env = dict(os.environ, MW_DIR=_d)
_out = subprocess.run([sys.executable, "-B", "-c", _probe], env=_env,
	capture_output=True, text=True).stdout.strip()
check(_out in ("ABSENT", "0"),
	f"跨日后 warned_tier={_out} ->新一天 count从1起，pct//10永远不大于残留"
	f" tier，80%/90%预警从此永久失效（线上实测残留值9）")

# ══════════⑦进程组：leader退出后仍要杀整组 ══════════
#⚠️ agent/是**独立子仓库**，CI里不 clone（参照 test_scheduler的同一约定）。
#缺目录时不能直接 open() ——那会让整个文件 rc=1，把前面12条已经验过的
#断言一起拖成"失败"。这里显式记一笔跳过，并在末行写明，不装作验过。
_skipped = []
_p = os.path.join(ROOT, "agent", "run.py")
if not os.path.exists(_p):
	_skipped.append("killpg整组（agent/未 clone）")
else:
	import ast # noqa: E402

	_tree = ast.parse(open(_p, encoding="utf-8").read())
	_fn = next(n for n in _tree.body
		if isinstance(n, ast.FunctionDef) and n.name == "_killpg")
	_ns = {"os": os, "signal": __import__("signal"), "time": __import__("time")}
	exec(compile(ast.Module(body=[_fn], type_ignores=[]), _p, "exec"), _ns)
	_code = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(300)"
	_pr = subprocess.Popen([sys.executable, "-c", _code], start_new_session=True)
	__import__("time").sleep(0.4)
	_g = os.getpgid(_pr.pid)
	_ns["_killpg"](_pr, grace=0.3)
	__import__("time").sleep(0.4)
	try:
		os.killpg(_g,0)
		_alive = True
	except OSError:
		_alive = False
	check(_alive is False,
		"leader退出后直接 return，忽略 SIGTERM的孙进程仍然存活 ->孤儿继续写文件并推群")
	if _pr.poll() is None:
		_pr.kill()

if _skipped:
	print(f"ALL PASS (第三轮审计修复 {_ok[0]}个断言；"
		f"{len(_skipped)}项未验证：{chr(65307).join(_skipped)})")
else:
	print(f"ALL PASS (第三轮审计修复 {_ok[0]}个断言)")
