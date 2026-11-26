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

## 数据模型（SQLite）

主库 `coopvote.db`：

- `members`：本地名册，只存会员令牌哈希。
- `ballots`：`draft → open → settled` 状态机。
- `eligibilities` / `pseudonyms` / `credentials` / `votes` / `results`：匿名表决相关。
- `fund_pool`：资金池单行（入池/保留/已付/可用）。
- `proposals`：提案（`allocate`/`revoke`），与表决一一对应，含执行结果状态。
- `grants`：已生效拨款（保留额、已付额、`active/revoked`）。
- `payments`：付款（请求号唯一、清算幂等键 `clearing_ref` 唯一、三态状态机）。

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

## 测试覆盖（47 项）

- `tests/test_lifecycle.py`：冻结范围、重领不增资格、改投覆盖、停权/恢复、结算固化。
- `tests/test_privacy.py`：名册无选择字段、投票响应无身份、令牌不能当凭证、公开端无逐票信息。
- `tests/test_concurrency.py`：投票侧并发顺序确定性。
- `tests/test_treasury.py`：提案绑定/冻结、决议门槛、全额保留、余额不足不生效、
  撤销退款语义、已付金额占用可用额、资金池恒等式、管理员权限。
- `tests/test_payments.py`：付款三态、额度占用、请求号幂等/改金额拒绝、
  扣款后断连不判失败、宕机受理、恢复对账、清算端去重、重启不丢不重复付。
- `tests/test_treasury_concurrency.py`：并发付款不超额、同请求号并发不重复占用、
  撤销 vs 付款两种受理顺序、多提案争同一余额。

## 安全边界说明

本系统的威胁模型**不**包含能直接读取 SQLite 文件的数据库超级管理员；
它保证的是各 HTTP 业务接口与各自权限范围不跨读：管理员看不到任何个人选择，
投票端拿凭证换不回会员身份，公开端拿不到逐票与时间序列。资金侧管理员能
办理拨款与付款、查看资金池和付款状态，但同样无法读取任何会员的投票选择。
