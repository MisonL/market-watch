#!/usr/bin/env python3
"""调度表只排**本部署真的有的脚本**（2026-11-22）。

##为什么要这条

容器镜像按 `.dockerignore` **不含** `agent/`（它带着本机 agent 的
state/logs，不该进镜像），而 `scheduler.JOBS` 里固定有 `agent/run.py`。
原实现是每轮循环 `os.path.exists()` 判一次、缺了就 `⚠️ agent: agent/run.py
不存在，跳过` —— 于是容器里**每小时一条**、一天 24 条、永不消失。

危害不是"日志难看"，是**告警疲劳**：日志里长期有一条永远为真的 ⚠️ 之后，
真的告警也一起被忽略。而它其实是**静态事实**（这套部署没有这个脚本），
静态事实该在启动时报一次。

##判据

1. `_present_jobs()` 按存在性分成 (保留, 缺失) 两组，自定义表同样生效；
2. 真实仓库里 collect/watch/selfcheck 三个必须在保留组（少一个就是漏活）；
3. `main()` 的**两处**遍历都必须用筛过的 `_jobs`，不能再直接用 `JOBS`
   —— 只改一处的话，另一处照旧每小时复读（这正是"闸门在≠闸门生效"）；
4. 运行期缺脚本的分支必须在 `_missing_logged` 守卫内（每个名字只报一次）。
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import scheduler as S # noqa: E402

_ok = [0]


def check(cond, msg):
	if cond:
		_ok[0] +=1
	else:
		print(f" ❌ {msg}")
		raise SystemExit(1)


def _src():
	with open(os.path.join(ROOT, "src", "scheduler.py"), encoding="utf-8") as f:
		return f.read()


def _tbl(*names):
	return [(n, f"src/{n}.py", 300, False) for n in names]


# ----------①按存在性分组----------
with tempfile.TemporaryDirectory() as _td:
	os.makedirs(os.path.join(_td, "src"))
	open(os.path.join(_td, "src", "a.py"), "w").close()
	open(os.path.join(_td, "src", "c.py"), "w").close()

	_keep, _miss = S._present_jobs(_td, _tbl("a", "b", "c"))
	check([j[0] for j in _keep] == ["a", "c"],
		f"保留组应只有存在的 a/c，实际 {[j[0] for j in _keep]}")
	check(_miss == ["b"], f"缺失组应只有 b，实际 {_miss}")

	_keep, _miss = S._present_jobs(_td, _tbl("a", "c"))
	check(len(_keep) ==2 and _miss == [], "全都存在时不应报缺失")

	_keep, _miss = S._present_jobs(_td, _tbl("x", "y"))
	check(_keep == [] and _miss == ["x", "y"],
		f"全都不存在时应全部缺失，实际 keep={_keep} miss={_miss}")

# ----------②真实仓库：三个核心 job 一个都不能被筛掉----------
_keep, _miss = S._present_jobs(ROOT)
_names = {j[0] for j in _keep}
for _must in ("collect", "watch", "selfcheck"):
	check(_must in _names, f"真实仓库里 {_must} 被筛掉了（该脚本明明存在）")
check(set(_names) | set(_miss) == {j[0] for j in S.JOBS},
	"筛过的表 + 缺失表必须正好等于原 JOBS（不能凭空多/少一个 job）")

# ----------③接线：两处遍历都得用 _jobs----------
_src = _src()
check("_jobs, _absent = _present_jobs(root)" in _src,
	"main() 没有调用 _present_jobs(root) 做启动期筛选")
check(_src.count("for name, script, interval, _persist in _jobs:") ==1,
	"主循环没有遍历筛过的 _jobs（会照旧每轮判缺文件）")
check("for name, _script, interval, _persist in _jobs:" in _src,
	"next_at 初始化没有遍历筛过的 _jobs")
check("_log(\"jobs: \" + \", \".join(f\"{n}/{iv}s\" for n, _s, iv, _p in _jobs)"
	in _src, "启动日志里的 job 列表还不是筛过的表（与实际调度不一致）")
check('_log(f"⚠️ {name}: {script} 不存在，跳过")' not in _src,
	"仍留着每轮复读的「不存在，跳过」日志（应改为启动期一次 + 运行期只报一次）")

# ----------④运行期缺脚本：只报一次----------
check("_missing_logged = set()" in _src, "缺少 _missing_logged 去重集合")
_i = _src.find("if not os.path.exists(os.path.join(root, script)):")
check(_i > 0, "运行期的存在性检查被删掉了（脚本运行中消失就无人知晓）")
_block = _src[_i:_i + 600]
check("if name not in _missing_logged:" in _block,
	"运行期缺脚本分支没有 _missing_logged 守卫（会每小时复读）")
check("_missing_logged.add(name)" in _block,
	"运行期缺脚本分支没有记录下来（守卫永不生效）")

print(f"ALL PASS (调度的存在性筛选 {_ok[0]}个断言)")