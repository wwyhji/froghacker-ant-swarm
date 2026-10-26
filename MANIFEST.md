# ant-swarm-oss 文件清单与剥离说明

> 生成日期：2026-10-26
> 来源仓库：`D:\froghacker`（蛙客 v5.2，主仓库为专有软件）

## 一、开源内容（本仓库）

| 文件 | 能力 | 来源 |
|------|------|------|
| `ant_swarm/pheromone.py` | 信息素层（四种信息素 + CAS 抢占 + 方向位 + role 感知打分） | `tools/ccm/pheromone.py` |
| `ant_swarm/protocol.py` | Worker 状态机 + 消息类型 + CCMError | `tools/ccm/protocol.py` |
| `ant_swarm/worker_p2p.py` | Worker 直连（P2P 消息，基于信息素黑板） | `tools/ccm/worker_p2p.py` |
| `ant_swarm/quality.py` | 跨 Worker 质量验证（交叉验证 + 冲突解决） | `tools/ccm/quality.py` |
| `tests/` | test_pheromone + test_pheromone_stress | `tests/` |

## 二、改造说明（相对主仓库原版）

1. **去平台依赖**：`worker_p2p.py` 三处 `from tools.swallow_trap import record`（平台错误记录模块）→ 标准库 `logging`。
2. **绝对导入 → 相对导入**：`from tools.ccm.pheromone` → `from .pheromone`；`from tools.ccm.protocol` → `from .protocol`。
3. **默认路径**：`.froghacker/pheromone.db` → `.ant_swarm/pheromone.db`（`DEFAULT_DB_PATH` 与 docstring 示例）。
4. **测试导入**：`from tools.ccm.pheromone` → `from ant_swarm.pheromone`。

## 三、未开源（本仓库不含）

| 内容 | 原因 |
|------|------|
| `orchestrator.py` / `hub_client.py` / `journal.py` / `context.py` | 依赖蛙客 Hub 服务端（平台中心调度架构），非纯蚁群核心 |
| `dialect.py` | 依赖 `tools.crypto.encoding` + typer，属通信加密而非调度核心 |
| `autonomous.py` / `dispatcher.py` / `worker_pool.py` 等 | 平台编排/调度胶水，超出蚁群核心范围 |
| `tools.swallow_trap` | 平台错误记录模块，已替换为 logging |
| `compare.py` / `ant_swarm.py`（labs 抽取版） | 已被 `pheromone.py` 原版取代（原版含方向位） |

## 四、与专利04 的关系

本仓库三个模块**不引用任何数学引擎**（无同调/FCA/因果/范畴/NCD/KL/群表示/Stone），
与专利04「知识库结构自检」零交集。专利保留声明见 NOTICE。
