# market-watch —— 容器镜像
#
# ## 为什么基础镜像选 debian-slim 而不是 alpine
#
# 运行时要起 `curl`（所有行情请求都走它，见 `marketlib.cj`）。alpine 用
# musl libc，`curl` 的证书链与 DNS 解析行为和 glibc 有差异 —— 对一个
# **靠每天 288 次 HTTP 请求活着**的程序，这类差异会在半夜以"取数全失败"
# 的形式出现。debian-slim 的 glibc 与 CI（ubuntu-latest）一致，
# 测试通过的行为在生产大概率也一致。
#
# 体积代价约 +40MB，换取"和 CI 同构"，值得。
FROM python:3.12-slim-bookworm

# 时区数据：调度器会校验 TZ=Asia/Shanghai，缺 tzdata 时无法正确定位
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      curl ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

# ⚠️ 非 root 运行。程序会读写 state/、logs/，但不需要任何系统权限。
# UID/GID 固定为 10001，方便挂载卷时对齐宿主权限。
RUN groupadd -g 10001 mw \
 && useradd -u 10001 -g mw -m -s /usr/sbin/nologin mw

WORKDIR /app

# 先只拷依赖清单，让依赖层可缓存（改代码不必重装依赖）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY conf/*.example.* ./conf/
COPY README.md LICENSE ./
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
# ⚠️ 必须显式 755，不能只 `chmod +x`。
# `[实测]` 第一版用的是 `chmod +x`，结果镜像里是 **711**（-rwx--x--x）——
# 属主是 root，而运行用户是 mw(10001)，它对脚本只有 x 没有 r。
# shell 脚本**必须可读**才能被解释器解析，于是报
# `/bin/sh: 0: cannot open ...: Permission denied`。
# `+x` 只在原有 r 位上叠加 x，不会补 r —— 所以显式写 755。
RUN chmod 755 /usr/local/bin/entrypoint.sh

# 数据目录：state/ 与 logs/ 挂卷，logs 便于 `docker logs` 之外再留档
RUN mkdir -p /app/state /app/logs /app/conf \
 && chown -R mw:mw /app

# ⚠️ 时区**默认就设成北京时间**：这是最容易漏、后果最静默的一项。
# 设成 UTC 时程序能启动、能跑，只是所有时段判断偏 8 小时（盘中不推、半夜推）。
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MW_DIR=/app

# ⚠️ `MW_DIR=/app` 是必须的，不是顺手加的。
# `src/paths.py` 的运行根默认是 `~/.market-watch`，容器里 HOME=/home/mw，
# 于是它会去找 `/home/mw/.market-watch/state/...` —— 而我们挂在 /app。
# `[实测]` 不加这句时自检报：
#     ❌ 历史推送回放失败: FileNotFoundError:
#        '/home/mw/.market-watch/state/history/pushes.jsonl'
#     ❌ [Errno 2] '/home/mw/.market-watch/conf/config.json'
# 表现是"容器能起来、webui 能开、但每一轮都失败"—— 最难查的形态。
# `MW_DIR` 是本项目官方的沙箱/重定向入口（paths.py 的"铁律"），
# 用它而不是改代码，生产与容器的路径逻辑就完全同构。

USER mw

# 健康检查：webui 的 /api/health（只读，不产生副作用）
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${MW_PORT:-8787}/api/health" >/dev/null || exit 1

# 暴露 webui 端口（仅容器内部使用时可不映射）
EXPOSE 8787

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python3", "-B", "src/scheduler.py"]
