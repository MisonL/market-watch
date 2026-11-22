#!/usr/bin/env python3
"""全新安装的自检**不许是红的**：「文件不存在」≠「文件坏了」（2026-11-22）。

##为什么要这条

首次真跑 Docker镜像（fresh install）暴露：`src/selfcheck.py` 有两处把
「还没有数据文件」当「文件坏了」处理——

1. `[执行流水新鲜度]` 读 `state/history/runs.jsonl`：全新安装根本没有
 该文件，`FileNotFoundError`落进 `except` → `ok_all = False`，
 用户第一天看到的自检就是❌，且**无从修起**（要修只能先跑几天攒流水）；
2. `[DSML/历史回放]` 读 `state/history/pushes.jsonl`：同样
 `FileNotFoundError` → `_bad22.append("历史推送回放失败: ...")` →❌。

危害不是"红得难看"，是**告警疲劳 +信号污染**：刚装好的系统把一个
「正常状态」（无流水）报成「故障状态」（读不出流水），用户学会的第一课
就是"自检的❌ 可以无视"——等真坏的那天（权限/损坏）也没人看了。

##判据（代码里必须长这样，不是注释里）

两处 except块都必须先问 `os.path.exists()`：
- 文件不存在 → `_unverified[0] +=1` +ℹ️「还没有 …（全新安装…）」，
 **不得**碰 `ok_all` / `_bad22`；
- 文件在但读不出（权限/损坏）→ 保持原❌路径不动。

接线断（只写判据不写代码）也是本测试要抓的变异体：删掉 exists守卫、
把 `ok_all = False`挪回守卫之前、把 `_bad22.append`挪出 else，
都会在这里被抓住。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

_ok = [0]


def check(cond, msg):
	if cond:
		_ok[0] +=1
	else:
		print(f"❌ {msg}")
		raise SystemExit(1)


def _src():
	with open(os.path.join(ROOT, "src", "selfcheck.py"), encoding="utf-8") as f:
		return f.read()


_src = _src()


def _block(begin_marker, end_marker):
	i = _src.find(begin_marker)
	check(i >0, f"找不到 {begin_marker!r}（结构变了，测试要跟着改）")
	j = _src.find(end_marker, i)
	check(j > i, f"找不到结束标记 {end_marker!r}")
	return _src[i:j]


# ----------①执行流水新鲜度：runs.jsonl缺文件→未验证，不置红----------
_b16 = _block("except Exception as _e16a:", "独立锚")
check("os.path.exists(_runs_p)" in _b16,
	"runs.jsonl 的 except块没有 exists守卫（缺文件照样置红）")
check("_unverified[0] += 1" in _b16,
	"runs.jsonl缺文件分支没有计入 _unverified（未验证 ≠ 通过 ≠失败）")
check("全新安装" in _b16,
	"runs.jsonl缺文件提示没写明是全新安装（用户无从判断该不该管）")
check("ok_all = False" in _b16, "runs.jsonl坏文件路径丢了❌（回归到不检查）")
_i_guard = _b16.find("os.path.exists(_runs_p)")
_i_fail = _b16.find("ok_all = False")
check(_i_guard < _i_fail,
	"ok_all = False跑到 exists守卫前面了（缺文件又会置红）")

# ----------② DSML历史回放：pushes.jsonl缺文件→未验证，不置红----------
_b22 = _block("except Exception as _e22:", "剥离链必须")
check('os.path.exists(p_hist("pushes.jsonl"))' in _b22,
	"pushes.jsonl 的 except块没有 exists守卫（缺文件照样记 _bad22）")
check("_unverified[0] +=1" in _b22,
	"pushes.jsonl缺文件分支没有计入 _unverified")
check("全新安装" in _b22,
	"pushes.jsonl缺文件提示没写明是全新安装")
_i_guard22 = _b22.find('os.path.exists(p_hist("pushes.jsonl"))')
_i_bad22 = _b22.find('_bad22.append(f"历史推送回放失败')
check(_i_guard22 < _i_bad22,
	"_bad22.append没有收进 else（缺文件还是会记❌）")
check(_b22.count('_bad22.append(f"历史推送回放失败') ==1,
	"历史推送回放失败的❌路径丢了（文件真坏时无人知晓）")
check(_src.count("except Exception as _e22:") ==1,
	"_e22 的 except 出现多次（窗口抓错块了，测试要跟着改）")

# ----------③ 行为面：守卫表达式本身对两种文件状态给出两种结论----------
# 直接 eval那两行判据，确认它真的区分「不存在」和「存在」：
# 在临时目录里制造「无文件」与「空文件」两种状态各验一次。
import tempfile
import importlib

paths = importlib.import_module("paths")
with tempfile.TemporaryDirectory() as _td:
	os.environ["MW_DIR"] = _td
	importlib.reload(paths)
	_p = paths.p_hist("pushes.jsonl")
	check(not os.path.exists(_p), "临时 MW_DIR 下 pushes.jsonl 不应存在")
	check(os.path.exists(_p) is False,
		"exists判据在「无文件」时必须为 False（守卫才会走未验证分支）")
	os.makedirs(os.path.dirname(_p), exist_ok=True)
	open(_p, "w").close()
	check(os.path.exists(_p) is True,
		"exists判据在「有文件」时必须为 True（坏文件才会走❌ 分支）")

print(f"ALL PASS (全新安装不置红 {_ok[0]}个断言)")
