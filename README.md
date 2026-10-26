# FrogHacker Ant Swarm 🐜

<div align="center">

**去中心化蚁群协作核心（开源版）** — 信息素调度 · CAS 抢占 · Worker P2P

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](./pyproject.toml)
[![零依赖](https://img.shields.io/badge/依赖-纯标准库-important.svg)]()

响应时代号召，开源蛙客网络安全分析自动化平台 CCM 协作引擎的**蚁群协作核心**。

</div>

## 这是什么

蛙客（FrogHacker）的 CCM（Claude Collaboration Manager）协作引擎采用**蚁群模型**做多 Agent 任务分配。本仓库开源其中的核心三件套：

> **多 Agent 任务分配不需要中心调度器。** 所有 Worker 读写同一块 SQLite 黑板，靠信息素做局部决策、自组织认领任务 —— 与蚁群觅食同构。

| 模块 | 角色 |
|------|------|
| `pheromone.py` | 信息素层：四种信息素 + CAS 抢占 + **方向位**（通/不通） |
| `protocol.py` | Worker 生命周期状态机 + 消息类型 |
| `worker_p2p.py` | Worker 直连：点对点消息 / 求助广播 / 任务交接 |

对比中心化编排（AutoGen GroupChatManager / CrewAI Manager）：无单点故障、天然并发、CAS 防重。

## ✨ 特性

- **四种信息素** — trail（我解过）/ alarm（卡住了）/ recruit（急招人）/ territory（我正在做）
- **CAS 原子抢占** — `BEGIN IMMEDIATE` 事务串行化，同一任务恰好一人认领
- **信息素方向位** — `outcome ∈ {dead_end/failed/blocked}` 记 -1，避免「此路不通」被当正反馈
- **role 感知打分** — role 提示的 recruit 只对匹配 role 的 Worker 开放（避免帮倒忙）
- **Worker P2P** — 点对点消息 / 求助广播 / 任务交接，基于同一块信息素黑板，无需独立消息中间件
- **零依赖** — 纯 Python 标准库（sqlite3 + threading + enum）

## 🚀 快速上手

```python
from ant_swarm import PheromoneDB, pick_best_task

# Commander 投放任务（recruit 信息素）
db = PheromoneDB(".ant_swarm/pheromone.db")
db.connect()
db.deposit("recruit", "issue:42", "pm", {"deadline_hours": 8})

# 每个 Worker 进程里（各开各的 connection 指向同一文件）：
db = PheromoneDB(".ant_swarm/pheromone.db")
db.connect()
while True:
    best = pick_best_task(db.scan(), worker_id, role="web_pentester")  # 本地打分
    if best is None:
        break
    if db.claim(best, worker_id):          # CAS 抢占
        do_work(best)
        db.deposit("trail", best, worker_id, {"outcome": "success"})
        db.release(best, worker_id)        # 释放地盘
```

### Worker P2P 直连

```python
from ant_swarm import WorkerP2P

p2p = WorkerP2P("worker_memory_01")
p2p.broadcast_help("vol3 can't parse this profile, anyone?")       # 求助广播
p2p.send_to("worker_binary_03", {"task_id": "T1", "result": {...}}) # 点对点结果

# 其他 Worker 轮询求助：
msgs = p2p.poll_help_requests(my_roles=["memory_analyst"])
```

## 🏗️ 架构

```
                  ┌─────────────────────────────────┐
                  │   SQLite WAL 共享黑板（信息素）  │
                  └─────────────────────────────────┘
        deposit trail/alarm/recruit/territory   ▲  scan + score + CAS claim
                              │                  │
        ┌──────────────┐      │     ┌───────────────────────────┐
        │  Commander   │──────┴────▶│  Worker A / B / C（自选任务）│
        └──────────────┘            └───────────────────────────┘
                                             │ P2P 直连
                                             ▼
                                    点对点 / 求助广播 / 任务交接
```

## 📁 目录结构

```
froghacker-ant-swarm/
├── LICENSE               Apache-2.0
├── NOTICE                专利保留声明
├── MANIFEST.md           文件清单与剥离说明
├── pyproject.toml        打包配置
├── ant_swarm/            蚁群协作核心包
│   ├── __init__.py
│   ├── pheromone.py      信息素层
│   ├── protocol.py       Worker 状态机
│   └── worker_p2p.py     P2P 直连
└── tests/                单元测试 + 压力测试
```

## 🛡️ 专利保护与开源边界

本仓库开源的蚁群协作调度**不引用任何数学引擎**，与专利04「知识库结构自检」（同调/FCA/因果/范畴/NCD/KL 等）零交集。但为明确起见，专利保留声明见 [NOTICE](./NOTICE)（申请日 2026-10-13）。

## 📄 许可证

Apache License 2.0，© 2026 FrogHacker (wwyhji)。专利保留声明见 [NOTICE](./NOTICE)。
