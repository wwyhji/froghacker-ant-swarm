"""去中心化蚁群协作核心（CCM 蚁群组件开源版）。

从蛙客 CCM 协作引擎抽取的核心模块，零第三方依赖、纯标准库：

  - pheromone   信息素层（四种信息素 + CAS 抢占 + 方向位 + role 感知打分）
  - protocol    Worker 状态机与消息类型
  - worker_p2p  Worker 直连（P2P 消息，基于信息素黑板）
  - quality     跨 Worker 质量验证（交叉验证 + 冲突解决）

核心主张：多 Agent 任务分配不需要中心调度器。所有 Worker 读写同一块
SQLite 黑板，靠四种信息素做局部决策、自组织认领任务 —— 与蚁群觅食同构。

快速开始：
    from ant_swarm import PheromoneDB, pick_best_task
    db = PheromoneDB(".ant_swarm/pheromone.db")
    db.connect()
    db.deposit("recruit", "issue:42", "pm", {"deadline_hours": 8})
"""

from .pheromone import (
    Pheromone,
    PheromoneDB,
    score_tasks,
    pick_best_task,
    TRAIL_TTL_MS,
    ALARM_TTL_MS,
    RECRUIT_TTL_MS,
    TERRITORY_TTL_MS,
    DEFAULT_DB_PATH,
)
from .protocol import (
    WorkerState,
    MessageType,
    StateMachine,
    CCMError,
)
from .worker_p2p import (
    WorkerP2P,
    P2PMessage,
)
from .quality import (
    Verdict,
    verify_finding,
    cross_verify,
    check_answer_completeness,
)

__all__ = [
    "Pheromone", "PheromoneDB", "score_tasks", "pick_best_task",
    "TRAIL_TTL_MS", "ALARM_TTL_MS", "RECRUIT_TTL_MS", "TERRITORY_TTL_MS",
    "DEFAULT_DB_PATH",
    "WorkerState", "MessageType", "StateMachine", "CCMError",
    "WorkerP2P", "P2PMessage",
    "Verdict", "verify_finding", "cross_verify", "check_answer_completeness",
]
