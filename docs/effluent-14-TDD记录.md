# effluent-14 二次升代 — 三红三绿与逐页验收记录

- 槽位：排污卯第 14 槽 / effluent-14
- 日期：2026-11-25
- 拍板：**甲路径**，见 `docs/effluent-14-二次升代拍板说明.md`（当日封死，折中禁止）
- 事故夹具：`fixtures/effluent14_incident_n14.json`（SM-N14a 未作废 + SM-N14b 已正式的同口双正式态；另含影子双计态）

## 1. 先见证三红（未改四个服务之前）

命令均在仓库根、使用装有 requirements 的解释器执行。

| 红源文件 | 命令 | 见红结果（原始摘要） |
| --- | --- | --- |
| `tests/regen_a.py` | `pytest tests/regen_a.py` | `3 failed, 1 passed`：a1 `405 Method Not Allowed`（升代口不存在）；a2 `AttributeError: metric_lineage_svc has no attribute 'append_regen_edge'`；a3 缺拍板说明。a0 事故夹具表征测先过 |
| `tests/regen_b.py` | `pytest tests/regen_b.py` | `3 failed, 1 passed`：b1 双正式未报警 `assert None is True`（无 `double_count`）；b2 影子行无 `counted` 且被双计；b3 升代口 `405`。b0 复现“影子被对账列出”的事故表征先过 |
| `tests/regen_c.py` | `pytest tests/regen_c.py` | `1 failed`：真双会话两路 POST 同口升代均 `404`，`assert 0 == 1`，同口锁缺失 |
| `tests/race_resample.py` | `pytest tests/race_resample.py` | `1 failed`：双会话状态 `[404, 404]`，争用闸缺失 |

红阶段全量：`8 failed, 8 passed`（旧 6 测 + a0/b0 两个事故表征测保持绿）。

## 2. 三红分服务救绿（非单点插新）

- **采样服务 `samples_svc.py`**：新增 `regen_resample()` 甲路径 rite 与
  `POST /api/samples/resample`、`/api/samples/{id}/resample`、
  `/api/outfalls/{id}/resample`（另有 `/api/samples/regen|regenerate`、`/api/regen/resample` 同义口）。
  同一事务跨表：插新代 `sample_batches(gen_state='正式')` → 同 SQL 把同口旧代置
  `作废 + voided_at + void_reason + superseded_by` → 核销旧代开放挂起 →
  写 `metric_lineage` 二次升代父子边 → 写 `regen_runs` 与审计。任一步异常 rollback 整笔。
- **血缘服务 `metric_lineage_svc.py`**：`append_regen_edge()` 同事务供边；
  `supersede_outfall_edges()` 把旧边 `is_current=0`；`GET /api/lineage?current=1` 只回新代边。
- **对账服务 `reconcile_svc.py`**：影子/作废行 `counted=false` 不计账；
  按排放口统计正式效力世代，`>1` 即 `double_count/dual_formal=true`、双计灯亮；否则 `双计灯灭`。
- **封批服务 `seal_svc.py`**：作废旧代 / 影子代封批升代一律 `409`。
- **锁 `locks.py`**：`BEGIN IMMEDIATE`（`busy_timeout=0`）抢口锁原语 +
  `acquire_unique_lock()` 会话级持有人 + `record_contention()` 独立连接持久化争用痕（不被败方回滚吞掉）。
- **数据层 `db.py`**：`sample_batches` 增 `gen_state/voided_at/void_reason/superseded_by`，
  `metric_lineage` 增 `is_current/superseded_at`，新表 `regen_runs/regen_deviation_log`；
  对已存在旧库幂等 `ALTER TABLE` 迁移；`EFFLUENT_DB` 可指定隔离库。
- **偏航闸 `regen_policy.py`**：识别乙/影子/折中载荷，独立连接写审计动作
  `二次升代偏航` 与 `regen_deviation_log`，随后 `422` 拒写；
  `POST /api/regen/deviation-note` 对封死后的偏离说明同样留痕拒写。

绿结果：

```
pytest tests/regen_a.py            -> 4 passed
pytest tests/regen_b.py            -> 5 passed（b0 表征 + b1 亮灯 + b2 影子 + b3 甲路径 + b4 存量核销）
pytest tests/regen_c.py            -> 1 passed
pytest tests/race_resample.py      -> 1 passed
pytest                             -> 17 passed
python3 -m pytest（系统解释器）      -> 17 passed
```

`race_resample` 连跑 3 次、`regen_c` 连跑 5 次均绿；三源文件合并按评审方式调用
`pytest tests/regen_a.py tests/regen_b.py tests/regen_c.py tests/race_resample.py` → `11 passed`。

## 3. 争用：另起双会话打同口号二次采样

`tests/race_resample.py` 起真实 uvicorn，两个独立登录令牌（两路会话），
`threading.Barrier(2)` 让两 POST 同时打 `/api/outfalls/6/resample`（OF-N14-A）。
一次独立运行的真实回报：

```
"statuses": [200, 409]
"formal_count": 1
"sample_codes": ["SM-N14a", "SM-N14c"]      # 败方代号 SM-N14b 零入库（该次胜方为 B 会话）
"contention_count": 1                         # action=争用失败，detail=BEGIN IMMEDIATE 写锁忙
"lingering_locks": []
```

胜方由调度决定，测试按 409 动态识别败方代号并断言其零写入、同口正式代唯一、无残锁。

## 4. 五页逐页核（同一 FastAPI + SQLite 真源）

用真实 `vue.global.prod.js` 运行时 + jsdom 挂载真实 `frontend/js` 模块，
打开每个导航页并点击；只读巡检 `15/15` 通过、0 运行时错误。另起隔离库点真实
“同口再采升代”按钮的写操作巡检 `8/8` 通过、0 错误。

- **采样页**：有甲路径升代面板；SM-N14a 显示“已作废”徽标与作废因；SM-N14b 在页；
  点行内“同口再采升代”后成功条回报“新代 …（G3）正式，旧代 SM-N14b 已作废”，行变作废样式，自动新代在页。
- **血缘页**：横幅“只保留新代父子边”，SM-N14a→SM-N14b“二次升代”边带“新代当前边”徽标；可切全部留档（旧边“已替代留档”）。
- **封批页**：“禁止升代封批清单”列出 SM-N14a；UI 升代后 SM-N14b 也进入禁封清单；
  `POST /api/seals {sample_id:N14a}` 实测 `409 已废旧批禁止升代封批`。
- **对账页**：双计灯 `双计灯灭`（`.lamp-off`）；N14a `counted=false 旧代已作废`、N14b `counted=true`；
  再采一代后灯仍灭。事故双正式夹具下 b1 测试证明灯会转亮。
- **排放口页**：点 OF-N14-A 行，“同口只展示新代（当世代）”主表仅 SM-N14b；历史 `<details>` 才含已作废旧代；
  UI 再采后主表仅最新代 `SM-OF-N14-A-003`。

HTTP 同源抽核（干净种子库）：

```
GET /api/regen/decision -> 甲路径 A, sealed=true, 2026-11-25
GET /api/reconcile/DP-2026-N14 -> 双计灯灭 double_count=False
GET /api/lineage?current=1     -> 仅 is_current=1 的边
GET /api/outfalls/6            -> current_samples=['SM-N14b']
偏航 POST strategy=乙/shadow=true -> 422，audit action=二次升代偏航，影子代号未落库
```

## 5. 未做/说明

- 未把甲路径改成乙路径；乙仅保留为对账读模型的影子剔除哨，不是第二写入面。
- 未只改采样页角标：按钮直连同事务升代口，后端三红测试兜底。
- 前端无构建步骤，全部为静态 ESM；已用 `node --check`（ESM）校验改动 JS 语法。
