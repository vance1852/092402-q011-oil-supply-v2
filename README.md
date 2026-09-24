# 油气供应韧性与现场准入服务

本项目是一套可直接运行的 Python 服务端系统，用于记录原油基准报价、油田与终端设施、输送线路、库存批次、日提名和供应情景，并保留油田巡检机器人统计准入流程。系统面向价格连续波动、关键输油线路恢复、库存调拨和现场设备验证同时发生的运营环境，让调度、风险和审计人员在同一个 SQLite 数据库中获得可追溯结论。

供应调度子域提供以下能力：

- 原油基准报价按交易日和来源修订登记，历史版本不会被覆盖；
- 油田、储罐、终端与炼厂设施建档，线路保存日能力、在途时间和损耗规则；
- 线路停运或降容事件按 UTC 时间区间生效，日分配会计算实际可用能力；
- 库存批次保留油品、牌号、数量、单位成本和接收时间，可计算加权库存成本；
- 托运提名支持载荷级幂等、优先级分配、库存扣减和在途交接；
- 供应情景保存价格变化、线路能力变化和需求变化，审批后产生可重放的确定性结果；
- 关键写操作进入哈希串联审计日志，可离线验证事件顺序和内容完整性。

质量隔离子域解决实验室复测不一致时的批次处置问题：

- 质量样品绑定库存批次并记录代表数量；检测结果（RON/MON 等）只能追加为后继版本，历史版本保留；
- 检测结论必须经两名与记录人不同的人员确认（草稿→确认），不合格或存疑结论才能立案；
- 混兑按配料实际数量建立谱系边，并强制产出量等于配料合计；
- 隔离案件沿实际数量谱系传播：罐内剩余按比例锁定，已混兑到下游批次的数量继续传播，仍在途转运标记为 `held` 待处置，已交付历史不回滚；同罐无关批次不受影响；
- 发运和库存预留在事务中拒绝使用被影响数量；隔离也不会与既有预留重叠；
- 解除隔离必须为每个传播目标给出处置数量（放行/返炼/降级/销毁），合计与隔离数量严格相等才提交；
- `GET /inventory/lots/{lot_id}/trace` 从任一库存批次查看来源谱系、影响比例、当前空闲/预留/隔离/可用数量和完整决定链。

现场准入子域位于 `robot_trials` 包，负责油田巡检机器人的设备构建登记、不可变试验协议、观测分片导入、异常观测复核、统计任务租约、准入决定和审计报告。该子域不连接机器人硬件，只处理已经结构化的试验记录。

## 目录

- `src/oil_supply/`：报价、设施、线路、库存、提名、供应情景、HTTP API 与离线验收；
- `src/robot_trials/`：油田巡检机器人试验与统计准入；
- `fixtures/`：现场准入演示协议和结构化观测；
- `tests/`：核心规则、错误边界、API 和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 无第三方运行依赖

在依赖已经准备好的容器中安装：

```bash
python3 -m pip install --no-index --no-deps .
```

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

测试使用内存数据库和临时目录，不访问公网，也不会启动常驻服务。

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m oil_supply.acceptance --workspace .
```

该命令会在内存数据库中登记六个交易日的布伦特报价，创建油田、终端和输送线路，完成库存入账、提名分配、发运及供应情景分析，最后输出一行 JSON。成功时退出码为 `0` 且 `status` 为 `ok`。

现场准入子域也保留独立验收入口：

```bash
PYTHONPATH=src python3 -m robot_trials.acceptance --workspace .
```

## HTTP 服务

```bash
PYTHONPATH=src python3 -m oil_supply.api --database oil_supply.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查为 `GET /health`。除健康检查外，请求通过 `X-Actor-Id` 携带操作者编号。可用接口覆盖报价、设施、线路、停运事件、库存批次、提名、能力分配、发运（`POST /transfers`）、到货（`POST /transfers/{id}/receive`）、供应情景、质量样品（`POST /quality/samples`）、检测版本（`POST /quality/tests` 与 `POST /quality/tests/{id}/confirm`）、混兑（`POST /blends`）、库存预留（`POST /inventory/reservations`）、隔离案件（`POST /isolation/cases`、`GET /isolation/cases/{id}`、`POST /isolation/cases/{id}/release`）、批次谱系视图（`GET /inventory/lots/{id}/trace`）和审计链。新增 `quality` 角色负责取样与检测登记，隔离立案与解除由 `risk` 角色执行。服务重启后，SQLite 中的业务状态和历史版本会继续保留。
