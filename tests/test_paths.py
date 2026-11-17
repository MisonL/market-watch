import sys, os
# 本文件在 tests/，paths.py 在 ../src（2026-10-31 分层后）
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import paths
assert paths.DIR == os.environ["MW_DIR"], f"DIR={paths.DIR} != MW_DIR"
assert paths.MAIN_ROOT == os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "src")), "MAIN_ROOT != src/"
assert paths.CONF_DIR == os.path.join(paths.DIR, "conf")
assert paths.STATE_DIR == os.path.join(paths.DIR, "state")
assert paths.LOGS_DIR == os.path.join(paths.DIR, "logs")
assert paths.CACHE_DIR == os.path.join(paths.STATE_DIR, "cache")
assert paths.HIST_DIR == os.path.join(paths.STATE_DIR, "history")
assert paths.p_conf("config.json") == os.path.join(paths.CONF_DIR, "config.json")
assert paths.p_state("state.json") == os.path.join(paths.STATE_DIR, "state.json")
assert paths.p_log("watch.log") == os.path.join(paths.LOGS_DIR, "watch.log")
assert paths.p_cache("daily.json") == os.path.join(paths.CACHE_DIR, "daily.json")
assert paths.p_hist("pushes.jsonl") == os.path.join(paths.HIST_DIR, "pushes.jsonl")
print("PATHS TEST PASS")
