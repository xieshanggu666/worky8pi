# 碳排放核算与交易管理系统

面向控排企业的碳管理平台：活动数据采集、排放核算、配额分配、配额交易台账、履约清缴与年度 MRV 报告。

## 技术栈

- **后端**：Python 3.10+ / FastAPI / SQLAlchemy ORM / SQLite / JWT（Cookie 认证）
- **前端**：React 18（本地 UMD 运行时 + htm 模板引擎，无需构建工具，完全离线可用）
- **测试**：pytest（250 项全部通过，含多线程并发、交割/结算闭环与冲正/违约回退一致性，以及统一账本事件链重放与对账专项）

## 快速开始

```bash
pip install -r requirements.txt
python scripts/init_db.py      # 初始化数据库与演示数据（已有旧库先执行迁移脚本）
uvicorn app.main:app --reload  # 启动服务
```

> 升级旧库（新增幂等键列与唯一约束）：`python scripts/migrate_concurrency.py`，可重复执行。
>
> 已结算竞价冲正/违约回退链路升级（新增 3 张表与成交单冲正/违约列、场次自动追偿开关）：`python scripts/migrate_auction_reversal.py`，可重复执行。
>
> **统一账本重放/对账链路升级（新增 3 张账本表：事件流/检查点/对账记录，并把五类旧业务记录回填为可重放事件）**：`python scripts/migrate_ledger_replay.py`，可重复执行。

访问 http://127.0.0.1:8000

### 演示账号（密码均为 `123456`）

| 用户名    | 角色       | 说明                       |
|-----------|------------|----------------------------|
| `admin`   | 监管管理员 | 企业/因子/配额/清缴全权限   |
| `verifier`| 核查员     | 核验活动数据、批准 MRV 报告 |
| `elec`    | 控排企业   | 绿能电力集团（边界受限）    |
| `cement`  | 控排企业   | 恒固水泥股份（边界受限）    |

## 功能模块

1. **核算边界管理**：企业注册行业/地区/核算边界说明，范围一/二/三边界配置
2. **排放因子库**：因子编号、有效期、数据来源；修订自动记录版本历史
3. **活动数据台账**：企业按年度/周期录入活动量，核查员可逐条核验或**批量核验**（按 id 勾选或按 企业+年度 一键圈定）；批量核验在单一事务内联动「标记核验 → 年度重算 → MRV 草稿刷新」，任一步失败整批回滚；已提交报告快照过期自动退回草稿，**已批准（配额已冻结）年度拒绝再核验**
4. **排放核算引擎**：
   - `activity_factor`：排放量 = 活动量 × 因子值
   - `fuel_combustion`：排放量 = 燃料量 × 综合系数 × 碳氧化率 × 44/12
   - 因子按年度生效区间取值，重复核算幂等（先清后算）
   - **数据状态约束**：仅核查员核验通过（`verified=1`）的活动数据才能进入核算；未核验数据在源头被排除，核算接口返回未核验条数提示，核验后须重新核算方可计入
   - **批量核验与重算**：`POST /api/activity/batch-verify` 按 `activity_ids` 或 `company_id+year` 圈定；核验标记、逐企业年度重算（先清后算、幂等）与 MRV 草稿/已提交报告联动（草稿刷新、已提交退回草稿）共用同一事务，失败整体回滚；仪表盘返回 `verified_activity`/`pending_activity`
5. **配额管理**：免费配额分配（基准 + 分配量 + 调整量）、配额账户持仓、可用余额、履约冻结额与交易占用额
6. **配额交易台账**：买入/卖出/划转，实时校验**可用余额（持仓 - 履约冻结 - 交易占用）**，逐笔记录持仓、冻结与占用快照
7. **企业间交易订单**：双方挂单 → 双方确认（卖方配额转为**交易占用**）→ 交割（双方配额账户与流水同步）；交割前任一方可撤销并释放占用
8. **履约闭环（年度配额闭环）**：MRV 报告批准后按批准排放快照冻结配额；清缴优先核销冻结配额，不足部分扣减可用配额；缺口年度买入或补充分配后可补缴；**企业间订单交割默认在同一事务内自动核销买方同年度履约缺口**（先冻结核销、再用到账配额补缴），交割完成即见履约状态/配额状态/统计一致更新；挂单可通过 `auto_clear_deficit=false` 关闭联动，改为事后手动清缴；报告冲正会解冻并退还已清缴配额、归档旧履约记录（解冻只回落冻结额、不增持仓，退还仅针对已离仓的清缴量，系统总配额守恒）
9. **MRV 报告**：年度范围一二三汇总生成，草稿 → 提交 → 批准状态流转；已批准报告不得直接覆盖，须由监管/核查角色通过冲正接口异常回滚；**批准前双重拦截**：该企业年度仍有未核验活动数据将被拒绝；报告排放快照与最新核算合计不一致（核验重算后未重新生成报告）同样拒绝，防止旧快照污染配额冻结与履约结果
10. **碳配额集中竞价市场**：监管建场（草稿/开放）→ 买/卖方企业密封报价（卖出报价即占用可用配额）→ 监管统一撮合（最大成交量定价、价格-时间优先、自成交规避）→ 集中结算（双方配额账户与流水同事务落账并核销买方履约缺口）；支持撤单、撮合前/后撤场（逐级释放占用）、并发结算抢占、全场次操作与越权拒绝审计；**已结算成交支持监管冲正与违约回退**（见下）
11. **已结算成交监管冲正 / 违约回退**：监管可对已结算成交单做整笔、批量或部分数量冲正，同一事务内回退双方配额划转、按成交单流水归属精确回滚联动清缴（退还到账补缴）、同步回退履约清缴记录与配额状态并写冲正单/批次与审计；买方自由可用不足时只收回可得部分、不足登记为违约欠额（成交单 `defaulted`），买方可由监管手动追偿或在后续场次结算到账后自动追偿（`auto_recover_default`，先清缴后追偿），欠额结清后成交单转为 `reversed`；冲正批次与补缴均有幂等键，并发重复操作只生效一次；**与报告冲正顺序无关**：两条回退链路共用同一本成交单归属流水账（`auction_deficit_clear` − `auction_clear_refund`），同一吨到账补缴无论先冲报告还是先冲成交最多退还一次，系统总配额守恒
12. **统一账本事件链 · 重放 · 对账**：配额流水、企业订单、集中竞价、履约清缴与冲正回退五类业务统一投影到一条只追加（append-only）的事件账（`ledger_events`，全局 `seq` 全序 + SHA-256 链式哈希），任何业务回退都以追加反向补偿事件体现，不删改旧事件；每笔流水在**同一事务**内由会话钩子自动登记事件（同生共死），旧库五类业务记录可由迁移脚本幂等回填为 `is_legacy=1` 历史事件（含订单/场次/报价/成交单/履约/报告/冲正批次/违约追偿的状态时点）；账户投影支持全量重放与检查点增量重放（`ledger_checkpoints`），重放器逐笔核对落账三余额快照；对账器（`ledger_reconciliations`，每次运行可追溯、幂等重跑）做六维核对：① 事件链完整性（断序/断链/篡改）② 重放投影 vs 实际余额与账本不变量 ③ 期初+有符号流水勾稽与逐笔快照链 ④ 单据↔账本（占用必释放或出库、交割配对、冲正累计≤成交量、违约欠额=追偿、补缴不超额退还）⑤ 履约一致（清缴/冻结/缺口/配额状态/报告归档）⑥ 系统守恒（跨主体出入账两两相等、逐年度总配额恒等）；账户按（企业, 年度）开立、余额事件强制带年度，逐年度独立重放，跨年度不串账

### 统一账本事件链与对账设计要点

- **单一语义事实来源**（`app/core/event_semantics.py`）：20+ 种流水类型对（持仓/冻结/占用）的有符号投影规则集中登记一次，事件登记器、重放器、对账器全部引用同一张表，杜绝“业务记账一套语义、重放又是另一套”；未登记的历史类型降级为 `unknown` 并按 warning 披露，不阻断重放
- **事件即业务的同事务投影**：不在四个业务服务里手工埋点，而挂 SQLAlchemy 会话 `after_flush` 钩子——流水拿到主键后在同一轮 flush 内追加事件，业务回滚则事件 INSERT 一并回滚；事件按流水主键排序登记，保证同一账户逐笔快照链顺序确定。序号由进程锁 + `(source,source_ref,occurrence)` 与 `seq` 唯一约束双保险，双击/超时重试/并发回填同一业务事实只登记一次
- **可重放**：`replay_all/replay_account` 从空投影按 `seq` 应用事件得到账户余额；`ledger_checkpoints` 保存最近投影供增量重放，是可随时丢弃重建的派生数据；事件 `payload` 带落账时持仓/冻结/占用三快照，重放逐笔校验，旧记录快照不符按 warning、新事件不符按 error
- **可对账**：`POST /api/ledger/reconcile`（仅监管，支持 `Idempotency-Key`）做只读全量/企业/年度对账，差异结构化（code/severity/refs）落 `ledger_reconciliations`，系统守恒结论随运行持久化；库外篡改余额、物理删除事件、篡改事件载荷分别触发投影不符 / 断序断链 / 哈希不符
- **兼容旧记录**：`scripts/migrate_ledger_replay.py` 建表 + 幂等回填（实时记账库上只补缺、零重复）+ 重建检查点，回填后对账结论与实时记账一致；回填事件与升级后的实时事件共用同一条哈希链（旧事件在前、新事件勾连其后）
- **跨年度状态一致性**：所有余额事件强制带 `year`，缺失即对账报错；订单/成交单年度与其流水账户年度不一致报 `CROSS_YEAR_*`；每个（企业, 年度）独立重放与守恒校验

### 集中竞价市场设计要点

- **场次状态机**：`draft → open → matched → settled`，draft/open/matched 均可撤场为 `cancelled`（settled 终态）；状态流转用条件 UPDATE 抢占，并发撮合/结算/撤场只有一方成功
- **统一价格（uniform-price）集合竞价**：在各买卖报价档上计算可行成交量，取成交量最大档；多档并列先选未匹配量最小档、仍并列取候选档均价；价格-时间优先配对（买价降序/卖价升序、同价按报价时间），同一企业不与自身成交
- **卖出报价即占用**：卖出报价提交瞬间把报量从自由可用转为 `reserved`（与企业间订单共用同一套 `current ≥ frozen + reserved` 原子约束），撤单/未成交余量/撮合零成交/撤场逐级释放，成交部分保留至结算出库——从数据库层杜绝跨场次超卖
- **结算即清缴**：卖方占用出库（current/reserved 同减）、买方到账（current 同增）与买方同年度履约缺口核销共用一个事务、一套锁（`account: < auction: < clear: < order:`），先冻结核销、后到账补缴；逐笔成交的冻结核销/到账补缴流水均关联成交单（冻结核销用买方自有冻结配额、到账补缴为成交交付量），场次可通过 `auto_clear_deficit=false` 关闭联动
- **监管冲正与违约回退（settled → reversed/defaulted → reversed）**：`POST /api/auctions/{id}/reverse` 支持整笔/批量/部分数量冲正，冲正批次（`auction_reversal_batches`）、逐笔冲正单（`auction_trade_reversals`）、双方回退流水（`auction_clawback_out/in`、`auction_clear_refund`）、履约记录回退与审计在单一事务提交；仅到账补缴（`auction_deficit_clear`）随交易回退，自有冻结核销不因交易取消而回滚；买方自由可用不足时尽力收回、差额登记违约欠额（`defaulted_amount`），监管可 `POST /api/auctions/trades/{id}/repay` 逐笔或 `POST /api/auctions/defaults/{buyer}/recover` 按买方追偿（`auction_default_repay_out/in`），后续场次结算到账时在清缴之后自动追偿（绝不挪用履约配额）
- **权限审计**：场次管理仅 admin、企业只能为本企业报价/撤单且只见本企业成交、审计/冲正记录仅监管可见；建场/开放/报价/撤单/撮合/结算/**冲正/违约追偿**与每一次越权拒绝（含越权读审计、越权撤单）均写 `auction_audit_logs`

### 并发一致性保障（清缴 / 交易 / 企业间订单）

- **账户锁定**：进程内按键（账户 / 企业+年度履约 / 订单 / 竞价场次）串行化余额变更，多键按 `account: < auction: < clear: < order:` 全局锁序加锁防死锁；PostgreSQL/MySQL 额外加 `SELECT … FOR UPDATE` 行锁，SQLite 在写事务内以"空更新"抢占库级写锁并设置 `busy_timeout` 等待
- **原子扣减/冻结/占用**：原子 UPDATE 由数据库保证 `frozen ≥ 0`、`reserved ≥ 0`、`current ≥ frozen + reserved`；余额、冻结额、占用额与流水在同一事务提交，异常统一回滚
- **交易占用与履约冻结互不挤占**：企业间订单双方确认后，卖方配额从"可用"转为交易占用（reserved），既不能被重复卖出/划出，也不能被报告批准冻结或清缴补扣挪用；反过来已履约冻结的配额也不能被订单占用。订单撤销释放占用，交割时占用配额出库并向买方入账
- **订单状态抢占**：撤销/交割使用"状态必须为前置状态"的条件 UPDATE，并发交割与撤销只有一方成功，杜绝"已交割又释放占用"的脏账
- **交割即清缴（年度配额闭环）**：交割的卖方出库、买方到账与买方履约缺口核销共用同一事务、同一锁集合（交割锁序天然包含买方 `clear:`/`account:` 键）：先核销冻结配额，再用刚到账的自由可用配额补扣缺口（绝不触碰交易占用），履约记录、配额状态、账户余额与全部流水同生共死；交割与手动清缴并发时由企业年度键串行化，累计清缴不超过核查排放量
- **报告批准与冲正**：批准报告、创建履约记录、冻结配额同事务完成；冲正报告、归档履约、解冻/退还配额、更新配额状态同事务完成。冲正退还按性质拆分记账：`reversal_unfreeze`（解除冻结，只回落冻结额、不增持仓）、`reversal`（退还未归属成交单的已清缴量）、`auction_clear_refund`（按成交单退还竞价到账补缴）——报告冲正与竞价成交冲正共用同一本成交单归属流水账，两条回退链路任意先后顺序下同一吨补缴最多退还一次，账本守恒
- **重复提交**：流水、履约记录与订单均支持幂等键（请求体 `idempotency_key` 或 `Idempotency-Key` 请求头），双击 / 超时重试只入账一次；前端提交期间禁用按钮并自动生成幂等键
- **数据库兜底约束**：`quotas` 的 (企业, 年度) 唯一约束防止并发分配重复；活跃 `compliance_records` 的 (企业, 年度) 部分唯一索引允许冲正归档后重新批准；`trade_orders` 幂等键唯一约束防止重复挂单

## 数据表（24 张）

`users` `companies` `emission_scopes` `activity_data` `emission_factors` `factor_versions` `calculation_methods` `emission_results` `quotas` `allowance_accounts` `allowance_transactions` `compliance_records` `mrv_reports` `trade_orders` `auction_sessions` `auction_bids` `auction_trades` `auction_trade_reversals` `auction_reversal_batches` `auction_default_repayments` `auction_audit_logs` `ledger_events` `ledger_checkpoints` `ledger_reconciliations`

## API 摘要

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/login` | 登录（Cookie 会话） |
| GET | `/api/dashboard/stats` | 平台统计 |
| GET/POST | `/api/companies` | 企业列表/创建（admin） |
| POST | `/api/companies/{id}/scopes` | 添加核算边界（admin） |
| GET | `/api/companies/{id}/totals?year=` | 年度范围汇总 |
| GET/POST | `/api/activity` | 活动数据列表/录入 |
| POST | `/api/activity/batch-verify` | 批量核验（verifier/admin，按 id 列表或 企业+年度；同事务重算并联动 MRV 草稿，失败整批回滚） |
| POST | `/api/activity/{id}/verify` | 单条核验（走批量内核，联动重算与草稿） |
| GET/POST | `/api/factors` | 因子列表/创建（admin） |
| PUT | `/api/factors/{id}` | 修订因子并记版本（admin） |
| POST | `/api/companies/{id}/calculate?year=` | 触发核算（仅已核验数据，响应附 `unverified_count`/`warning`） |
| GET | `/api/companies/{id}/results?year=` | 核算明细 |
| GET/POST | `/api/quotas` | 配额列表/分配（admin） |
| POST | `/api/accounts/{id}/transfer` | 配额交易 |
| GET/POST | `/api/trade-orders` | 企业间订单列表（按参与方隔离）/挂单（发起方即确认，`auto_clear_deficit` 控制交割联动清缴） |
| GET | `/api/trade-orders/{id}` | 订单详情（仅买卖双方/监管） |
| POST | `/api/trade-orders/{id}/confirm` | 参与方确认（双方确认即占用卖方配额） |
| POST | `/api/trade-orders/{id}/cancel` | 交割前撤销（confirmed 释放占用，需原因可选） |
| POST | `/api/trade-orders/{id}/deliver` | 交割：双方账户与流水同步落账，并自动核销买方同年度履约缺口（响应附 `buyer_clearance`） |
| POST | `/api/companies/{id}/clear` | 履约清缴/缺口补缴（admin） |
| GET | `/api/compliance` | 履约记录 |
| POST | `/api/companies/{id}/reports/generate` | 生成 MRV 报告 |
| POST | `/api/reports/{id}/submit` / `/approve` | 提交/批准报告（批准即冻结配额） |
| POST | `/api/reports/{id}/reverse` | 冲正已批准报告并回滚冻结/清缴（verifier/admin，需原因） |
| GET/POST | `/api/auctions` | 竞价场次列表/建场（admin，可带 `open_at` 直接开放） |
| GET | `/api/auctions/{id}` | 场次详情 |
| POST | `/api/auctions/{id}/open` `/match` `/settle` | 开放报价/统一撮合/集中结算（admin） |
| POST | `/api/auctions/{id}/cancel` | 撤场（开放期释放全部报价占用，撮合后释放成交占用，需原因） |
| GET/POST | `/api/auctions/{id}/bids` | 场次报价列表（企业按本企业隔离）/密封报价（仅企业，卖出即占用） |
| POST | `/api/auctions/bids/{id}/cancel` | 撤单：企业撤本企业单 / admin 撤任意单，卖出占用即时释放 |
| GET | `/api/auctions/trades/all` | 全部成交（监管/核查，企业 403 并审计） |
| GET | `/api/auctions/my-trades` | 本企业参与的成交 |
| POST | `/api/auctions/{id}/reverse` | 监管冲正已结算成交（整笔/批量/部分数量，需原因，支持幂等键） |
| GET | `/api/auctions/reversals` | 冲正批次/逐笔冲正单/违约补缴记录（admin/verifier，企业 403 并审计） |
| GET | `/api/auctions/defaults` | 待追偿违约欠额（监管全部 / 企业仅本企业） |
| POST | `/api/auctions/trades/{id}/repay` | 监管对单笔违约成交手动追偿（amount 缺省为全额，尽力而为） |
| POST | `/api/auctions/defaults/{buyer_id}/recover?year=` | 监管按买方某年度汇总追偿全部欠额 |
| GET | `/api/auctions/audit-logs` | 权限与操作审计（admin/verifier，越权读取 403 并留痕） |
| GET | `/api/ledger/events` | 统一账本事件链时间线（游标分页，企业仅本企业；含逐笔持仓/冻结/占用演化） |
| GET | `/api/ledger/accounts/{id}/replay` | 单账户全量重放投影 vs 实际余额（企业仅本企业账户） |
| POST | `/api/ledger/reconcile` | 发起全量/企业/年度对账（admin/verifier，支持 Idempotency-Key） |
| GET | `/api/ledger/reconciliations` `/…/{id}` | 对账运行历史/详情（企业仅见本企业范围结论） |
| POST | `/api/ledger/backfill` | 旧五类业务记录回填进事件链并重建检查点（仅 admin，幂等可重跑） |
| POST | `/api/ledger/checkpoints/rebuild` | 全量重放重建账户检查点（仅 admin） |
| GET | `/api/ledger/chain/head` | 事件链头部锚点（head seq / 实时与历史事件数） |

## 测试

```bash
python -m pytest tests/ -v   # 250 passed
```

覆盖：核算引擎两种公式、因子按年取值、核算幂等、配额分配幂等、清缴达标/缺口与补缴、交易余额校验、MRV 状态机、API 冒烟、越权防护、企业间订单全状态机（挂单/单方及双方确认/撤销释放/交割双方入账/幂等与非法流转拒绝），以及多线程并发交易/清缴/订单（无超额扣减、占用与冻结互不挤占、流水三类快照链一致、幂等键去重、失败整体回滚、交割与撤销竞争只有一方成功、清缴与交易并发三方一致）；另有交割联动清缴闭环专项测试：足额/部分/超买补缴、纯冻结记录核销、关闭联动后手动清缴、卖方义务不被触动、重复交割只核销一次、两笔订单交割与手动清缴并发后"余额 / 流水 / 履约记录 / 仪表盘统计"四方一致且年度配额守恒（持仓 + 已清缴 = 分配总量）。

集中竞价市场专项（`test_auction.py` / `test_auction_api.py`，46 项）：场次草稿/开放/撮合/结算状态机与幂等建场；密封报价保留价校验、同企业同方向唯一、卖出超可用拒绝、撤单释放；统一价格撮合的最大成交量/未匹配量/均价并列规则、价格-时间优先、部分成交、零成交、自成交规避、卖出按可用封顶且未成交余量释放；结算双方划转与流水五快照链守恒、买方缺口足额/部分/超买联动核销、关闭联动留存缺口；撮合后撤场逐笔释放、开放期撤场批量释放报价占用；多线程并发结算只划转一次、结算与撤场竞争恰一方胜出、跨场次并发卖出总占用不超过自由可用、结算与手动清缴并发守恒；API 角色边界（企业/核查/监管）、越权撤单与越权读审计的拒绝留痕、HTTP 并发结算幂等。

冲正与违约回退专项（`test_auction_reversal.py` / `test_auction_reversal_api.py`，39 项）：无联动清缴的整笔/批量/部分数量冲正及重复冲正拒绝、幂等键去重；冲正按成交单归属精确回滚到账补缴、履约记录 cleared/deficit 与配额状态同步回退、部分冲正保持 settled 且可分次冲正至 reversed；买方配额已转出时冲正只收回自由可用余额、差额登记违约欠额与卖方待追偿敞口，监管逐笔/按买方手动追偿（受自由可用约束、超额自动封顶、无可用拒绝）、后续场次结算到账同事务自动追偿、关闭 `auto_recover_default` 不追偿，欠额结清后 defaulted → reversed；多线程并发冲正只生效一次、并发部分冲正累计不超过成交量、冲正与下一场结算自动追偿并发不超额；**报告冲正与成交冲正四种先后/交错顺序下归属补缴不重复退还、系统总配额守恒**，报告冲正→重新批准→冲正旧成交不污染新履约记录；流水三快照链与配额守恒；API 角色边界（仅 admin 可冲正/追偿、企业越权 403 并审计）、HTTP 并发冲正幂等、冲正/追偿审计留痕。

统一账本重放与对账专项（`test_ledger_replay.py` / `test_ledger_api.py`，32 项）：分配/订单/竞价/冲正/违约全链路每笔流水同事务生成事件、事务回滚事件同灭、事件 seq 连续与哈希链勾连；全量/增量重放投影与账户三余额一致、检查点重建往返、跨年度账户独立投影；旧库清空事件链后从五类业务表幂等回填（重放结论不变、重复回填零新增、回填后实时记账不重复）；部分冲正与违约自动追偿后对账平衡、系统守恒；库外篡改余额、物理删除事件、篡改事件载荷分别被投影/断序断链/内容哈希核对检出；多线程并发交割后链不重号不断链且账实相符；API 角色边界（对账仅监管、回填仅 admin、企业事件隔离与越权 403）、对账幂等键去重、事件链游标分页。
