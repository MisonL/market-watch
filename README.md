# market-watch

A 股 / 港股盘中盯盘与推送系统。定时抓取行情、判断异动、生成简报并推送
到企业微信群。

面向**个人自用**，在 macOS 上用 `launchd` 常驻运行。不依赖数据库，
不依赖付费数据源，全部配置为纯 JSON 文件。

---

## 它做什么

- **定时采集**：交易日盘中每 5 分钟抓取一次行情
- **异动判断**：板块资金异动（主力净额占比越过阈值）、指数破位、
  港股触发线穿越等
- **简报生成**：调用 LLM 把原始数据组织成人类可读的盘面叙事，
  失败时退回确定性模板
- **推送**：企业微信群机器人（markdown 消息）

典型输出：

```
> **午后开局 · 硬件链午后二次杀跌，港股独强难传导**

**触发** CPO 主力净流出 94.87 亿（占成交额 -8.3%）、印制电路板
-37.20 亿（占 -8.7%），双双越过 8% 资金异动线。

**盘面** 上证 -0.74%（涨 646 跌 1642，全面下跌）、创业板指 -3.19%…

**建议**
> 风险 硬件链主力占比越过 8% 异动线且跌幅同步扩大，反弹宜分批减仓。
```

---

## 目录结构

```
src/            源码
  marketlib.py    行情抓取、降级链、交易日历、工具函数
  feeds.py        备用数据源（腾讯 / 新浪）
  collect.py      定时采集，写快照
  analyze.py      异动检测 + 简报生成 + 推送
  hot.py          热点板块算法
  alert.py        紧急告警
  news.py         资讯抓取
  webui.py        本地面板
  runlog.py       执行流水
  paths.py        路径真值源
  selfcheck.py    自检（健康检查 + 契约校验）
  selfcheck_mutation.py  变异测试（注入缺陷，验证自检能否抓住）
conf/           配置
  config.json         主配置（含 webhook，**不入库**）
  config.example.json 主配置模板
  sectors.json        监控板块与阈值
  plan.example.md     注入 AI 的市场语境模板
tests/          测试
```

---

## 快速开始

### 1. 依赖

- Python **3.11+**（用了 `typing.NotRequired`，3.10 没有）
- `curl`（取数用子进程调用）

```bash
python3 -m pip install -r requirements.txt   # 目前无第三方依赖
```

### 2. 配置

```bash
cp conf/config.example.json conf/config.json
# 编辑 conf/config.json，填入你的企业微信 webhook
chmod 600 conf/config.json
```

`conf/sectors.json` 定义监控哪些板块、触发阈值是多少。改完即时生效，
不需要重启。

`conf/plan.md` 是**注入 AI prompt 的市场语境**（当前结构位置、关键位、
写法要求）。它由模板生成，需要你自己维护：

```bash
cp conf/plan.example.md conf/plan.md
```

它已被 `.gitignore` 排除（属于个人市场判断）。自检有个
`[plan.md 新鲜度]` 项会检查它的日期标注 —— 超过 5 个交易日未更新会报 ❌，
因为过期认知会被 AI 当成"当前市场"写进推送。

### 3. 运行

```bash
# 手动跑一轮
python3 src/collect.py
python3 src/analyze.py

# 自检
python3 src/selfcheck.py

# 单元测试（本仓库 47/47；全新克隆是 45 通过 + 2 跳过 ——
# 那 2 个需要 agent/.venv，而 agent/ 是独立仓库、不随本仓库分发）
bash tests/run_all.sh
```

### 4. 常驻（macOS）

系统用 `launchd` 调度。需要你自己创建 plist，示例见
`conf/launchd.example.plist`。

**标签前缀可配置**：默认 `com.mison.market*`，用环境变量
`MW_LAUNCHD_PREFIX` 改成你自己的。

---

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MW_DIR` | `~/.market-watch` | 数据根目录 |
| `MW_PROXY` | 空（禁用） | 代理地址，如 `http://127.0.0.1:7890` |
| `MW_LAUNCHD_PREFIX` | `com.mison.market` | launchd 标签前缀 |
| `MW_FULL` | — | 变异测试强制全量 |
| `MW_ONLY` | — | 只跑指定变异体（`a\|b\|c`） |
| `MW_JOBS` | 3 | 变异测试并行度（**CPU 密集型**，刻意取小） |
| `MW_TEST_JOBS` | 动态 | 单元测试并行度（**IO 密集型**；不设则按实测吞吐自适应 4~12 路） |
| `MW_SKIP_AI_PROBE` | — | 跳过 AI 探活（离线自检用） |
| `MW_NO_LOCK` | — | 跳过 run 锁（仅调试） |

---

## 数据源

| 源 | 用途 | 备注 |
|---|---|---|
| 东方财富 `push2*` | 主源：指数、板块、资金流（`f62` 主力净额、`f184` 占比、`f104/f105` 涨跌家数） | 会间歇性抖动 |
| 腾讯 `qt.gtimg.cn` | 备源：指数与港股价格 | 无资金流字段 |
| 新浪 `hq.sinajs.cn` | 备源：指数价格 | 需 `Referer` |

**降级链**：东财多主机轮询 → 代理 → firecrawl 云端兜底。
主机失败进入 5 分钟冷却，到期后半开探测自动恢复。

> 板块数据（`90.BKxxxx`）**只有东财提供**。东财完全不可用时，
> 板块与资金相关段落会明确标注「无实时报价」，而不是编造数字。

---

## 自检与测试

```bash
# 自检：健康检查 + 契约校验（约 1~3 分钟）
python3 src/selfcheck.py

# 单元测试
bash tests/run_all.sh

# 变异测试：逐条注入缺陷，验证自检能否抓住（全量约 2~6 小时）
python3 src/selfcheck_mutation.py
```

**变异测试是本项目的核心质量保障**：它不检查「代码写对没有」，而是
检查「自检有没有判别力」——往源码里注入一个已知缺陷，看自检是否
报错。379 条变异体覆盖全部检查段。

---

## 已知限制

- **只有 macOS 支持**：调度依赖 `launchd`，`sched_setaffinity` 等
  Linux 专属接口不可用
- **板块数据单点依赖**：仅东财提供，无替代源
- **资金流字段无替代**：`f62`/`f184` 是东财独有加工数据
- **节假日表**：内置 2026–2027 年 A 股与港股假期，到期前需更新

---

## 许可

MIT
