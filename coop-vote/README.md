# 合作社匿名表决后端（FastAPI + SQLite）

本地可演示的匿名表决系统，并支持把表决结果直接用于**授权实际拨款与付款**。
不接入任何真实身份平台：会员身份是本地名册里的会员令牌，投票时使用服务端
单独签发的**投票凭证**，两套令牌哈希空间隔离、互不通用。资金侧通过一个
**独立持久化的本地清算模拟端**（伪银行）完成扣款，主服务在失联时不臆断成败。

## 表决规则实现对照

| 需求 | 实现方式 |
| --- | --- |
| 三个选项：赞成/反对/弃权 | `choice ∈ {yes, no, abstain}`，数据库 CHECK 约束兜底 |
| 开放时冻结有资格会员范围 | `open` 时把当前全部会员快照写入 `eligibilities`，之后新会员无法参与 |
| 管理员可暂停/恢复范围内会员资格 | 只改冻结快照的 `status/epoch`，与名册本体解耦；操作幂等 |
| 停权期间不得领新凭证或投票 | 领凭证、投票两个事务都校验 `eligibilities.status='active'` |
| 停权前的票暂不计入 | 停权事务内删除该匿名代号下的票，结算 SQL 只统计 active 资格 |
| 恢复后旧票不恢复、须重新投票 | 删除不可逆；恢复不复活任何票，会员重新领凭证后重投 |
| 重领/重试不能获得第二份计票资格 | 重领只作废旧凭证并签发新凭证，`eligibility` 与匿名代号 `voter_pid` 不变；`votes` 对代号唯一 |
| 有效资格期内可改投，截止只计最后一次 | 投票是对同一 `votes` 行的 UPSERT 覆盖 |
| 名册管理员可核对领票/资格状态，不能读选择 | 名册接口白名单仅 `code/name/status/epoch/has_credential`，系统不提供任何按人读取选择的接口 |
| 投票接口不能凭凭证揭示身份 | 该接口无会员鉴权依赖、不接受身份字段，响应只含匿名代号与选择 |
| 公开端只有结算后三项总数 | 结算前 `results=null`；结算后仅 `{yes,no,abstain}`，无逐票/顺序/分时数据 |
| 不防数据库超级管理员通读存储 | 仅保证**业务接口**不跨权限读；令牌与凭证在库中均为 SHA-256 哈希，明文只在签发时返回一次 |
| 关闭、改投、停权并发按服务端接受顺序 | 所有写操作经全局锁 + `BEGIN IMMEDIATE` 串行提交，顺序即获锁顺序 |
| 已结算结果不受之后资格变化影响 | 结算时把总数固化到 `results` 表；结算后资格变更、领票、投票全部 409 拒绝 |

## 拨款与付款规则实现对照

金额一律为**整数分**；拨款上限与单笔付款都必须为正数（Pydantic 与数据库
CHECK 双重校验）。

| 需求 | 实现方式 |
| --- | --- |
| 管理员可提出拨款（固定收款方 + 最高金额） | draft 表决绑定 `allocate` 提案；一场表决至多一项提案 |
| 管理员可提出撤销已生效拨款 | draft 表决绑定 `revoke` 提案，目标必须是 `active` 拨款 |
| 开放后不得新增或更改提案 | 仅 `draft` 可绑定提案；`open/settled` 一律 409 |
| 只有赞成票多于反对票才执行决议 | 结算事务内 `yes > no` 判定；平票/反对多 → 提案 `rejected` |
| 批准拨款即从资金池保留全部额度 | 同一事务内增加 `fund_pool.reserved_cents` 并建立 `grants` 行 |
| 余额不足：票数照常公布，拨款不生效且不自动恢复 | 提案置 `insufficient_funds`，不建拨款、不占额度；之后入金也不会补生效 |
| 在生效额度内向固定收款方分次付款 | 付款收款方取拨款快照，管理员不能改收款方；受理即占用额度 |
| 受理占用、确认扣款转已付、确认失败释放 | `accepted → paid/failed` 状态机，占用/已付/资金池三方账目联动 |
| 同请求号重试返回原付款及当前状态 | `(grant_id, request_no)` 唯一；重试幂等，不重复占用或扣款 |
| 已受理请求号不得改金额重用 | 同请求号金额不一致 → 409 |
| 撤销后不受理新付款，未占用额度立即退回 | 拨款置 `revoked`，只退未被受理中付款锁住的保留额 |
| 已付款不追回；受理中款仍可完成 | 撤销不触碰 `paid` 与 `accepted` 付款 |
| 受理中款最终失败：占用退回资金池，不恢复拨款 | 失败时按拨款当前状态决定退回方向（生效中→拨款额度；已撤销→资金池） |
| 撤销与付款、多提案争余额按服务端受理顺序 | 全部写操作走同一把全局锁 + `BEGIN IMMEDIATE`，并发测试验证两种顺序都自洽 |
| 同一决议不能重复拨款或退款 | 提案 `status` 状态守卫 + 计票与决议在同一事务原子提交 |

### 资金池恒等式

```
deposited_cents = available_cents + reserved_cents + paid_cents
```

- `available`：可再分配给新拨款的钱；
- `reserved`：已被生效拨款保留的钱（含受理中付款的占用）；
- `paid`：已确认扣款、离开资金池的钱。

测试中有 `_assert_ledger_invariant` 在每个关键步骤后校验该恒等式，并额外
校验资金池科目与所有拨款科目之和始终相等。

## 清算模拟端（独立伪银行）

`app/clearing/` 是一个**独立的 FastAPI 应用 + 独立 SQLite 文件**，用
`run_clearing.py` 以独立进程启动，与主服务物理隔离：

- `POST /payments`：按业务方给定的 `ref` 幂等去重，重复提交绝不重复扣款；
- `GET /payments/{ref}`：查询付款结果；
- `POST /sim/control`：故障注入（仅本地演示/测试）——
  `offline` 整体宕机、`drop_after_debit` 扣款后断连、`fail_next` 下一笔失败。

主服务的付款流程是**先把付款以 `accepted` 落库（占用额度），再在事务外
调用清算端**：

- 清算端明确返回成功/失败 → 推进到 `paid/failed`；
- 连接拒绝、读超时、响应中断（含扣款后断连）→ 抛 `ClearingUnavailable`，
  **绝不把失联当失败**，付款保持 `accepted`；
- 服务重启不丢失受理中付款（已持久化），启动时与后台线程定期对账，
  清算端恢复后凭同一 `ref` 查询/重试补齐终态；清算端去重保证不会付两次。

## 机具成套预约与实物出借（本地办理接口）

这部分完全复用 `members` 本地会员身份与 `X-Member-Token`（也兼容
`Authorization: Bearer <token>`），不接外部系统、不涉及收费。所有状态推进都是
懒触发：预约、维修、交接、归还以及任意状态查询都会在同一个 SQLite 写事务中
先处理到期/逾期并重算递补，因此不需要额外定时服务。

### 业务规则与实现对照

| 需求 | 实现 |
| --- | --- |
| 管理员登记独立编号和能力标签 | `equipment.asset_no UNIQUE` + `equipment_tags`；编号可传 `asset_no/asset_code/code/serial_number` 等常见别名 |
| 一张单是一整套，每项要求全部标签 | `reservation_items` + `reservation_item_tags`；求解时要求请求标签集合是机具标签集合的子集 |
| 同一台机具不能在同一套中充当两件 | 同一预约的各清单项时间区间相同，求解器将重叠要求视为冲突；手工指定交接号也校验去重 |
| 同一台机具必须覆盖整段借期/剩余借期 | 候选机具必须在整个 `[max(starts_at, now), ends_at)` 内无维修、硬借约或逾期不可承诺区间 |
| 区间左闭右开 | 统一用 `a.start < b.end and b.start < a.end` 判重叠；端点相接可复用 |
| 预约开始不得早于服务端当前时间 | 创建事务内用服务端 UTC 时钟比较；相等允许 |
| 只有能与其他确认单共同兑现才确认，否则整单等候 | 每次重算都对“已保留订单 + 当前订单”求整套派工；任一清单项无解则整单不进入确认集合 |
| 确认的是可领齐，不锁序列号 | `reservation_assignments` 只是可重算的派工方案；未领取单在可用性变化时可整体改派 |
| 先保留旧确认，再递补等候，均按最初受理 ID 排序 | 两趟处理：先 `confirmed`，再当前全部 `waiting`；ID 即 SQLite 受理顺序 |
| 无法在不牺牲更早订单时满足就略过并继续 | 每个候选单都在“此前已保留集合”上求解；失败只跳过/降级该单，不影响后续与其他订单相容的单 |
| 会员可取消尚未领取的本人预约 | `POST /me/equipment/reservations/{id}/cancel`；已交接单不能取消，重复取消幂等 |
| 未领取且到结束时间即失效 | 查询/办理时把 `ends_at <= server_now` 的 `waiting/confirmed` 置为 `expired`，随后释放能力并递补 |
| 管理员在借用时段一次交接并由系统给实物号 | `POST /admin/reservations/{id}/handover`；空体即按当前可行方案一次创建全部 `equipment_loans` |
| 手工选择的机具不能破坏其他确认单 | 先校验本单标签/唯一性/剩余借期，再把所选实物作为额外硬占用，对其他全部确认单重新求解；失败整笔回滚 |
| 已交接实物不能改派 | 自动重算只改 `reservation_assignments`；未归还 `equipment_loans` 是求解器硬占用 |
| 可逐件归还 | 每个清单项/实物一行 loan；`POST /admin/loans/{loan_id}/return` 或 `POST /admin/equipment/returns` |
| 未到期外借预计结束时可再用 | 未逾期 loan 只形成 `[checked_out_at, due_at)` 占用；相接的下一预约可确认 |
| 到期未还不能再承诺，直到实际归还 | `now >= due_at AND returned_at IS NULL` 的机具进入 `overdue` 集合，对任何新派工都不可候选；实际归还后立即恢复并递补 |
| 维修不会把会员手中机具视为归还 | 维修窗口与未还 loan 分别存储；登记/撤销维修绝不写 `returned_at` |
| 查看时反映逾期、失效和递补 | 所有会员/管理查询入口均在事务内执行 `_tick()` 后再组装响应 |
| 并发状态对应服务端接受顺序 | 沿用全局写锁 + `BEGIN IMMEDIATE`；预约、维修、交接、归还均在单事务内完成“推进—校验—落库—重算” |
| 重启不丢承诺和未还实物 | 全部状态在 SQLite 表中；无内存派工/借约状态 |

派工求解器在小规模本地机具集合上使用 MRV 回溯，并用时间原子段上的二分匹配
做 Hall/容量剪枝，确保不是只按标签数量粗略判断。

### 机具接口

| 方法/路径 | 鉴权 | 说明 |
| --- | --- | --- |
| `POST /admin/equipment` | 管理员 | 登记 `{asset_no, name?, tags[]}`；`/admin/machines`、`/admin/tools`、`/admin/assets` 为别名 |
| `GET /admin/equipment` | 管理员 | 机具标签、当前 `available/on_loan/overdue/maintenance` 状态、维修窗口、当前借用人 |
| `GET /admin/equipment/{id}` | 管理员 | 机具详情 |
| `GET /admin/equipment/by-asset/{asset_no}` | 管理员 | 按独立编号查询 |
| `POST /admin/maintenance` | 管理员 | 用 `equipment_id` 或 `asset_no` 登记 `{starts_at, ends_at, reason?}` |
| `POST /admin/equipment/{id}/maintenance` | 管理员 | 为指定机具登记维修时段 |
| `GET /admin/maintenance` | 管理员 | 维修时段及当前是否生效 |
| `POST/DELETE /admin/maintenance/{id}/revoke` | 管理员 | 撤销维修时段，随后立即重算保留/递补 |
| `GET /admin/reservations?status=&member_code=` | 管理员 | 全部预约、清单项、计划实物、实际借还与逾期信息 |
| `GET /admin/reservations/{id}` | 管理员 | 单笔预约办理视图 |
| `POST /admin/reservations/{id}/handover` | 管理员 | 在借用时段内一次领齐；可空体由系统选号，也可传 `{asset_nos:[...]}` 或 `{equipment_ids:[...]}`；`checkout/pickup/collect` 为路径别名 |
| `GET /admin/loans?open=true&returned=true&overdue=true` | 管理员 | 核对实际领出/归还/逾期实物 |
| `POST /admin/loans/{loan_id}/return` | 管理员 | 逐件归还 |
| `POST /admin/equipment/returns` | 管理员 | 用 `{loan_id}` 或 `{asset_no}` 归还一件，重试幂等 |

### 会员预约接口

| 方法/路径 | 鉴权 | 说明 |
| --- | --- | --- |
| `POST /me/equipment/reservations` | 会员 | body：`{starts_at, ends_at, items:[{label?, tags:[]}]}`；也接受 `start/end`、`required_tags`、标签二维数组等常见写法 |
| `GET /me/equipment/reservations` | 会员 | 只列本人预约；每次查询先做失效/逾期/递补推进 |
| `GET /me/equipment/reservations/{id}` | 会员 | 查 `waiting/confirmed/cancelled/expired/collected/returned`、中文 `status_label`、是否逾期、计划号或实际实物号 |
| `POST /me/equipment/reservations/{id}/cancel` | 会员 | 取消本人尚未领取的预约并触发递补；`DELETE` 同路径也可 |

`/me/reservations/**` 与 `/reservations/**` 是兼容别名。会员访问他人预约返回
404，不泄露预约是否存在。

### 本地演示与测试

```bash
# 不依赖表决/清算外部进程；演示使用 TestClient 和可控演示时钟
python3 demo_equipment.py

# 机具专项测试
python3 -m pytest tests/test_equipment.py -q

# 全部现有 + 新增测试
python3 -m pytest -q
```

机具专项测试覆盖：整套确认/等候、同机不能充当两件、全标签匹配、区间相接、
开始时间校验、取消递补顺序、懒失效与剩余借期、维修导致降级/撤销后递补、
跳过无法满足的一单且继续保留后续单、一次交接、手工选号不得破坏其他确认、
逐件归还、到期未还阻断承诺、实际归还后递补、维修不等于归还、重启持久化、
并发预约不超额确认、并发交接只产生一组借约。

## 数据模型（SQLite）

主库 `coopvote.db`：

- `members`：本地名册，只存会员令牌哈希。
- `ballots`：`draft → open → settled` 状态机。
- `eligibilities` / `pseudonyms` / `credentials` / `votes` / `results`：匿名表决相关。
- `fund_pool`：资金池单行（入池/保留/已付/可用）。
- `proposals`：提案（`allocate`/`revoke`），与表决一一对应，含执行结果状态。
- `grants`：已生效拨款（保留额、已付额、`active/revoked`）。
- `payments`：付款（请求号唯一、清算幂等键 `clearing_ref` 唯一、三态状态机）。
- `equipment` / `equipment_tags`：实物机具独立编号与能力标签。
- `maintenance_windows`：维修停用时段（`active/revoked`，撤销保留审计行）。
- `reservations` / `reservation_items` / `reservation_item_tags`：会员成套预约与清单。
- `reservation_assignments`：未领取确认单的可重算派工方案，不是序列号锁。
- `equipment_loans`：交接后的逐件实物借还记录、约定到期时间与实际归还时间。

清算库 `clearing.db`：`clearing_payments`（按 `ref` 去重的流水）、
`sim_controls`（故障注入开关）。

## 快速开始

```bash
python3 -m venv --without-pip .venv          # 本机 ensurepip 缺失时的引导方式
python3 bootstrap/get-pip.py  # 如已自带 pip 可跳过
.venv/bin/pip install -r requirements.txt

# 1) 创建首位管理员（打印一次性会员令牌）
.venv/bin/python seed_admin.py

# 2) 终端 A：启动独立清算模拟端（默认 127.0.0.1:8090、clearing.db）
.venv/bin/python run_clearing.py

# 3) 终端 B：启动表决主服务
.venv/bin/uvicorn app.main:app --reload
# 交互文档： http://127.0.0.1:8000/docs

# 4) 不启服务的完整故事线演示（脚本会自己拉起清算端）
.venv/bin/python demo.py            # 原有匿名表决演示
.venv/bin/python demo_treasury.py   # 拨款/付款/失联/重启/撤销全流程

# 5) 跑测试
.venv/bin/python -m pytest -q
```

环境变量：`COOPVOTE_DB`（主库路径，默认 `./coopvote.db`）、
`COOPVOTE_CRED_SALT`（令牌/凭证哈希盐值）、
`COOPVOTE_CLEARING_URL`（清算端地址，默认 `http://127.0.0.1:8090`）、
`COOPVOTE_CLEARING_DB`（清算库路径，默认 `./clearing.db`）、
`COOPVOTE_RECONCILE_INTERVAL`（后台对账周期秒数，默认 2）。

## 接口一览

### 匿名表决（原有，权限模型不变）

| 方法/路径 | 鉴权 | 说明 |
| --- | --- | --- |
| `POST /admin/members` | 管理员 | 建会员，返回一次性会员令牌 |
| `POST /admin/ballots` | 管理员 | 创建表决（draft） |
| `POST /admin/ballots/{id}/open` | 管理员 | 开启并冻结会员范围 |
| `POST /admin/ballots/{id}/settle` | 管理员 | 结算、固化票数并执行绑定提案 |
| `POST /admin/ballots/{id}/eligibility` | 管理员 | 暂停/恢复某会员资格 |
| `GET  /admin/ballots/{id}/eligibility` | 管理员 | 名册：资格 + 是否已领凭证（无选择） |
| `POST /me/ballots/{id}/credential` | 会员令牌 | 领取/重领投票凭证 |
| `POST /ballots/{id}/vote` | **仅投票凭证** | 匿名投票/改投，不接受身份信息 |
| `GET  /public/ballots/{id}` | 无 | 结算前仅状态，结算后仅三项总数 |

### 提案与资金办理（新增，全部仅管理员）

| 方法/路径 | 说明 |
| --- | --- |
| `POST /admin/ballots/{id}/proposals/allocate` | draft 表决绑定拨款提案（收款方、上限正整数分） |
| `POST /admin/ballots/{id}/proposals/revoke` | draft 表决绑定撤销提案（目标拨款 id） |
| `GET  /admin/ballots/{id}/proposal` | 查该表决绑定的提案及执行结果 |
| `POST /admin/treasury/pool/deposit` | 资金池入金（正整数分） |
| `GET  /admin/treasury/pool` | 资金池可用额/保留额/已付额 |
| `GET  /admin/treasury/grants` | 全部拨款（含是否生效、额度占用） |
| `GET  /admin/treasury/grants/{id}` | 单笔拨款详情（查是否生效） |
| `GET  /admin/treasury/grants/{id}/payments` | 该拨款下全部付款 |
| `POST /admin/treasury/grants/{id}/payments` | 发起付款（请求号 + 正整数分），幂等重试 |
| `GET  /admin/treasury/payments` | 全部付款及状态 |
| `GET  /admin/treasury/payments/{id}` | 单笔付款状态 |
| `POST /admin/treasury/payments/{id}/reconcile` | 手动对单笔受理中付款补齐终态 |
| `POST /admin/treasury/reconcile` | 批量对账所有受理中付款 |

系统**不新增**任何按会员查询投票选择的入口；匿名规则与公开结果口径保持不变。

## 测试覆盖（63 项）

- `tests/test_lifecycle.py`：冻结范围、重领不增资格、改投覆盖、停权/恢复、结算固化。
- `tests/test_privacy.py`：名册无选择字段、投票响应无身份、令牌不能当凭证、公开端无逐票信息。
- `tests/test_concurrency.py`：投票侧并发顺序确定性。
- `tests/test_treasury.py`：提案绑定/冻结、决议门槛、全额保留、余额不足不生效、
  撤销退款语义、已付金额占用可用额、资金池恒等式、管理员权限。
- `tests/test_payments.py`：付款三态、额度占用、请求号幂等/改金额拒绝、
  扣款后断连不判失败、宕机受理、恢复对账、清算端去重、重启不丢不重复付。
- `tests/test_treasury_concurrency.py`：并发付款不超额、同请求号并发不重复占用、
  撤销 vs 付款两种受理顺序、多提案争同一余额。
- `tests/test_equipment.py`：成套预约可行性、标签/同机冲突/左闭右开区间、
  取消与失效后的顺序递补、维修停用与撤销、确认保留/跳过/改派、实物交接选号
  保护其他确认、逐件归还、逾期未还阻断与归还恢复、重启持久化和并发交接。

## 安全边界说明

本系统的威胁模型**不**包含能直接读取 SQLite 文件的数据库超级管理员；
它保证的是各 HTTP 业务接口与各自权限范围不跨读：管理员看不到任何个人选择，
投票端拿凭证换不回会员身份，公开端拿不到逐票与时间序列。资金侧管理员能
办理拨款与付款、查看资金池和付款状态，但同样无法读取任何会员的投票选择。
