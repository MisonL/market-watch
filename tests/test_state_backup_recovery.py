"""`.prev` 回退回归：直接运行 python3 tests/test_state_backup_recovery.py。

每个用例在新子进程导入 analyze，MW_DIR 在导入 paths 前设置。
只测试 load_st/save_st；拦截网络及真实推送，数据全部放入临时目录。
.prev 仅保留上一版，不能保证最新一轮 fired 不丢失（见最后一个用例）。
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SRC = str(Path(__file__).resolve().parents[1] / "src")
HEADER = r'''
import datetime, json, os, socket, stat
from pathlib import Path
from unittest.mock import patch

def forbidden(*args, **kwargs):
    raise AssertionError("禁止真实推送/网络")
socket.socket.connect = forbidden
socket.create_connection = forbidden
import analyze as A
A.push = forbidden
A.M.push = forbidden
logs = []
A._log = lambda message, *args: logs.append(message)
p = Path(A.STATE)
p.parent.mkdir(parents=True, exist_ok=True)
prev = Path(A.STATE + ".prev")
today = datetime.date.today().isoformat()
def state(keys=("old",)):
    return dict(date=today, fired=list(keys), inflight=[], pending_idx=[],
                hourly_retry={}, count=len(keys))
def put(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")
def read(path):
    return json.loads(path.read_text(encoding="utf-8"))
def recovered():
    return any("已从 .prev 回退" in s for s in logs)
'''


class StateBackupRecovery(unittest.TestCase):
    def probe(self, code):
        with tempfile.TemporaryDirectory(prefix="mw_state_prev_") as root:
            env = dict(os.environ, MW_DIR=root, PYTHONDONTWRITEBYTECODE="1",
                       PYTHONPATH=SRC)
            proc = subprocess.run([sys.executable, "-B", "-c", HEADER + code],
                                  env=env, capture_output=True, text=True,
                                  timeout=20)
            self.assertEqual(proc.returncode, 0,
                             f"子进程 rc={proc.returncode}\n{proc.stdout}{proc.stderr}")

    def test_backup_0600_atomic_and_no_residue(self):
        self.probe(r'''
put(p, state())
put(prev, state(("older",)))
prev.chmod(0o644)
os.umask(0o022)
real_replace, real_fsync = os.replace, os.fsync
syncs = []
def sync(fd):
    syncs.append(stat.S_IFMT(os.fstat(fd).st_mode))
    return real_fsync(fd)
def replace(src, dst):
    if str(dst) == str(prev):
        assert read(prev)["fired"] == ["older"]  # 发布前旧版仍完整
        assert read(Path(src))["fired"] == ["old"]
        assert stat.S_IMODE(os.stat(src).st_mode) == 0o600
        assert stat.S_IFREG in syncs  # 内容 fsync 必须早于发布
    return real_replace(src, dst)
with patch.object(os, "replace", replace), patch.object(os, "fsync", sync):
    assert A.save_st(state(("old", "new"))) is True
assert read(prev)["fired"] == ["old"]
assert stat.S_IMODE(prev.stat().st_mode) == 0o600
assert not list(p.parent.glob("state.json.prev.tmp*"))
''')

    def test_bad_main_never_overwrites_good_prev(self):
        self.probe(r'''
for bad in ('{broken', '', '   ', '[]', 'null', '123', '{}',
            json.dumps(dict(date=today, fired=[])),
            json.dumps(dict(state(), fired="wrong")),
            json.dumps(dict(state(), count=True)),
            json.dumps(dict(state(), fired=[{}]))):
    p.write_text(bad)
    put(prev, state(("protected",)))
    before = prev.read_bytes()
    assert A.save_st(state(("new",))) is True
    assert prev.read_bytes() == before, f"坏主文件覆盖了好备份：{bad!r}"
    assert "new" in read(p)["fired"]  # 既有 fired 单调性守卫可并回有效旧键
''')

    def test_failed_backup_cleans_temp_preserves_prev_and_saves_main(self):
        self.probe(r'''
for phase in ("replace", "fsync"):
    put(p, state())
    put(prev, state(("protected",)))
    before = prev.read_bytes()
    real_replace, real_fsync = os.replace, os.fsync
    failed = []
    def replace(src, dst):
        if str(dst) == str(prev):
            failed.append(True)
            raise OSError("injected backup replace failure")
        return real_replace(src, dst)
    def sync(fd):
        if not failed:
            failed.append(True)
            raise OSError("injected backup fsync failure")
        return real_fsync(fd)
    target = replace if phase == "replace" else sync
    with patch.object(os, phase, target):
        assert A.save_st(state(("old", "new"))) is True
    assert failed, "没有执行备份故障注入"
    assert prev.read_bytes() == before
    assert not list(p.parent.glob("state.json.prev.tmp*")), "备份失败遗留临时文件"
    assert read(p)["fired"] == ["old", "new"]
''')

    def test_same_day_restore_and_next_save_preserves_good_backup(self):
        self.probe(r'''
p.write_text('{broken')
put(prev, state())
s = A.load_st()
assert s["fired"] == ["old"] and recovered()
s["fired"].append("new")
assert A.save_st(s) is True
assert read(prev)["fired"] == ["old"]
assert read(p)["fired"] == ["old", "new"]
assert A.save_st(s) is True
assert read(prev)["fired"] == ["old", "new"]  # 好主文件恢复后继续滚动
''')

    def test_stale_or_wrong_shape_backup_never_claims_recovery(self):
        self.probe(r'''
yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
for bad in (dict(state(), date=yesterday), {}, {"date": today}, [], None,
            dict(state(), fired="wrong"), dict(state(), fired=[{}]),
            dict(state(), inflight=None), dict(state(), count="1"),
            dict(state(), count=True), dict(state(), hourly_retry=[])):
    p.write_text('{broken')
    put(prev, bad)
    logs.clear()
    s = A.load_st()
    assert s["fired"] == [], f"错误恢复 {bad!r}"
    assert not recovered(), f"不可用备份却宣称恢复：{bad!r}"
''')

    def test_unusable_main_shape_uses_valid_backup(self):
        self.probe(r'''
# 旧路径对这些形状只能"清空/丢键"，等于去重凭据作废 -> 用同日且字段完整的 .prev。
# ⚠ 2026-11-10：`put(prev, state())` 必须在**每个 bad 之前**重置 ——
# 否则上一轮的 bad 会残留到 extra 字段里。另外「日期为空」的 bad
# 要重置 prev，否则 .prev 与主文件同样不可用 —— 那单独是另一种场景。
# 只列**主文件根本不是 dict** 的形状。
# ⚠ 2026-11-10：主文件能解析成 dict 时，由归一保住可用字段、只将坏键置空
# （见 tests/test_inflight_stage.py）；那时拿 .prev 回退会抹掉本轮新提交的键。
for bad in ([], None, 3, "text", ["fired"], {"a": 1}["a"]):
    put(prev, state())
    put(p, bad)
    logs.clear()
    # 应当保住的是**去重凭据**。到达这一点有两条合法路径：
    #   ① 从 .prev 回退（容器/日期整体不可用时）；
    #   ② 走现有类型归一——只把坏的那个键置空并记 ERR，fired 原样保留。
    # ⚠ 2026-11-10：旧版这里硬要求 `recovered()` —— 实测
    # inflight=None / hourly_retry=[] / count="1" 三个形状走的是 ②，
    # fired 正确保住、ERR 正确落下，却被判成失败。
    s = A.load_st()
    assert s["fired"] == ["old"], f"去重凭据丢了：{bad!r}"
    assert recovered() or any("类型异常" in x for x in logs), \
        f"既未回退也未归一留痕：{bad!r}"
''')

    def test_old_format_missing_keys_not_overwritten_by_backup(self):
        self.probe(r'''
# 旧格式只是**缺**字段：既有 setdefault 能补，且不拿上一版覆盖 ——
# 回退会把上一轮的旧 fired 并回来，反而抹掉刚提交的去重键。
put(p, dict(date=today, fired=["current"], inflight=[], count=0,
            hourly_retry={}))                      # 缺 pending_idx
put(prev, state(("stale-round",)))
logs.clear()
assert A.load_st()["fired"] == ["current"]
assert not recovered()
''')

    def test_missing_main_and_missing_backup_compatible(self):
        self.probe(r'''
assert A.load_st()["fired"] == []
assert not logs and not prev.exists()  # 新安装不误报警
put(prev, state())
assert A.load_st()["fired"] == []  # 保持缺主文件语义，不盲目复活备份
assert not logs
prev.unlink()
assert A.save_st(state()) is True
assert not prev.exists()
p.write_text('{broken')
assert A.load_st()["fired"] == []
assert logs and not recovered()  # 缺备份不崩、不虚报恢复
''')

    def test_valid_main_without_backup_and_day_rollover(self):
        self.probe(r'''
put(p, state())
assert A.load_st()["fired"] == ["old"]
yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
put(p, dict(state(), date=yesterday))
put(prev, state(("do-not-revive",)))
assert A.load_st()["fired"] == []  # 跨日主文件是正常换日，不用备份覆盖
assert not recovered()
''')

    def test_prev_can_lose_latest_round_dedup(self):
        self.probe(r'''
put(p, state())
assert A.save_st(state(("old", "latest-round"))) is True
p.write_text('{broken')
s = A.load_st()
assert s["fired"] == ["old"]
assert "latest-round" not in s["fired"]  # 诚实边界：上一版不含刚提交的去重键
''')


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(StateBackupRecovery))
    if result.wasSuccessful():
        print("ALL PASS (state backup/recovery)")
    sys.exit(0 if result.wasSuccessful() else 1)
