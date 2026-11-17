"""空操作对照的**污染诊断**必须能点名翻转段（2026-11-03 修）。

缺陷：`_run_one_mutual(None, …)` 的污染分支读 `_ctrl_res.get("line")`，但返回的
dict 从来没有 `line` 这个键 —— 于是全量回归报「空操作对照被误判」时永远打印
「对照未取得结果」，读者无法知道是**哪一段**的未验证行在负载下翻转，只能另写
探针逐个复跑（本次就是这么查到 `[执行流水新鲜度]` 的墙钟算术的）。
这类"诊断代码读一个不存在的键"属于 [[market-watch-mutation-anchor-drift]]
记的同一类坑：验收代码自身不可达 / 读不到目标。

行为契约：
  · 干净 → status="clean"，line=None
  · 污染 → status="polluted"，line 含**新增段名**与区分「❌ / 未验证」的字样
  · 对照 rc 由 0 变非 0 而输出无新增行 → 同样判污染，line 说明 rc 变化
"""
import os, sys, tempfile, atexit, shutil
# 只在**本文件自己建**临时 MW_DIR 时才注册清理（setdefault 语义）。
if "MW_DIR" not in os.environ:
    _td = tempfile.mkdtemp(prefix="mw_t_")
    os.environ["MW_DIR"] = _td
    atexit.register(shutil.rmtree, _td, ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))
import selfcheck_mutation as SM

_orig_base = SM._base
_orig_run = SM.run
_orig_probe = SM._with_agent_probe


def _with(base, run_out):
    SM._BASE = base
    SM._BASE_OUT = "(fixture)"
    def fake_run(S, skip_ai=True, **_kw):
        return run_out
    # ⚠️ 真探针会追加 `[agent 摘要链路]` 段并可能产 ❌ —— 会污染本用例的
    # 基线比较。恒等替换，让 out 完全可控。
    SM.run = fake_run
    SM._with_agent_probe = lambda _S, _o: _o
    # ⚠️ 2026-11-17（测试提速）：原实现在这里调一次 `SM.make_sandbox()`，
    # 但返回的 S **没有传给任何被测调用** —— `_run_one_mutant()` 内部会
    # 自己再建一个。于是每个用例白做一次完整沙箱复制（源码/配置/日志/
    # history 全拷一遍），4 个用例共 8 次复制 = 22.5s。
    # 这里临时把 `make_sandbox` 换成"只建空目录"，让 `_run_one_mutant`
    # 照常走它自己的完整路径（含创建与清理），只是不再重复外层那次。
    _orig_sandbox = SM.make_sandbox
    SM.make_sandbox = lambda **kw: tempfile.mkdtemp(
        prefix="mw_ctrl_", dir=os.environ["MW_DIR"])
    try:
        return SM._run_one_mutant(None, lambda _t: None)
    finally:
        SM.make_sandbox = _orig_sandbox


def _restore():
    SM.run, SM._with_agent_probe = _orig_run, _orig_probe
    SM._BASE = None
    SM._BASE_OUT = None


# 1) 干净：输出与新基线一致 → clean / line=None
r = _with(({("[甲]", "x ❌")}, {("[乙]", "y 未验证")}, 0),
          (0, "[甲]\n x ❌\n[乙]\n y 未验证\n"))
_restore()
assert r["status"] == "clean", r
assert r["line"] is None, r["line"]
print("PASS 1 clean-has-no-line")

# 2) 污染（新增 ❌ 行）→ polluted，line 点名该段
r = _with((set(), set(), 0),
          (0, "[新坏段]\n 这行坏 ❌ 具体\n"))
_restore()
assert r["status"] == "polluted", r
assert r["line"] and "新坏段" in r["line"] and "新增 ❌" in r["line"], r["line"]
print("PASS 2 polluted-names-bad-section")

# 3) 污染（新增未验证行）→ polluted，line 点名该段并标「未验证」
r = _with((set(), set(), 0),
          (0, "[假污染段]\n ⏭️ 基线里没有 —— 计入未验证\n"))
_restore()
assert r["status"] == "polluted", r
assert r["line"] and "假污染段" in r["line"] and "新增未验证" in r["line"], r["line"]
print("PASS 3 polluted-names-unv-section")

# 4) 输出全同、仅 rc 变 → polluted，line 说明 rc 变化
r = _with((set(), set(), 0),
          (2, "[甲]\n 正常行\n"))
_restore()
assert r["status"] == "polluted", r
assert r["line"] and "rc" in r["line"], r["line"]
print("PASS 4 polluted-rc-only")

print("\n全部通过")
