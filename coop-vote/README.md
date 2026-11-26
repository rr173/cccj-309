# 合作社匿名表决后端（FastAPI + SQLite）

本地可演示的匿名表决系统。不接入任何真实身份平台：会员身份是本地名册里的
会员令牌，投票时使用服务端单独签发的**投票凭证**，两套令牌哈希空间隔离、互不通用。

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

## 数据模型（SQLite）

- `members`：本地名册，只存会员令牌哈希。
- `ballots`：`draft → open → settled` 状态机。
- `eligibilities`：开启时冻结的资格快照，含 `active/suspended` 状态与变更纪元 `epoch`。
- `pseudonyms`：每个冻结资格一个随机匿名代号，投票只与代号绑定，与会员表无直接外键暴露给投票接口。
- `credentials`：投票凭证只存哈希；重领把旧凭证 `active=0`。
- `votes`：对匿名代号唯一，改投即覆盖。
- `results`：结算时刻的三项总数快照。

## 快速开始

```bash
python3 -m venv --without-pip .venv          # 本机 ensurepip 缺失时的引导方式
python3 bootstrap/get-pip.py  # 如已自带 pip 可跳过；本仓库曾用 https://bootstrap.pypa.io/get-pip.py
.venv/bin/pip install -r requirements.txt

# 1) 创建首位管理员（打印一次性会员令牌）
.venv/bin/python seed_admin.py

# 2) 启动服务
.venv/bin/uvicorn app.main:app --reload
# 交互文档： http://127.0.0.1:8000/docs

# 3) 或直接看完整故事线演示（无需启服务）
.venv/bin/python demo.py

# 4) 跑测试
.venv/bin/python -m pytest -q
```

环境变量：`COOPVOTE_DB`（数据库路径，默认 `./coopvote.db`）、
`COOPVOTE_CRED_SALT`（令牌/凭证哈希盐值，生产请自行设置）。

## 接口一览

| 方法/路径 | 鉴权 | 说明 |
| --- | --- | --- |
| `POST /admin/members` | 管理员 | 建会员，返回一次性会员令牌 |
| `POST /admin/ballots` | 管理员 | 创建表决（draft） |
| `POST /admin/ballots/{id}/open` | 管理员 | 开启并冻结会员范围 |
| `POST /admin/ballots/{id}/settle` | 管理员 | 结算并固化三项总数 |
| `POST /admin/ballots/{id}/eligibility` | 管理员 | 暂停/恢复某会员资格 |
| `GET  /admin/ballots/{id}/eligibility` | 管理员 | 名册：资格 + 是否已领凭证（无选择） |
| `POST /me/ballots/{id}/credential` | 会员令牌 | 领取/重领投票凭证 |
| `POST /ballots/{id}/vote` | **仅投票凭证** | 匿名投票/改投，不接受身份信息 |
| `GET  /public/ballots/{id}` | 无 | 结算前仅状态，结算后仅三项总数 |

典型调用顺序：

```bash
# 管理员
curl -X POST localhost:8000/admin/members -H "X-Member-Token: $ADMIN" \
  -H 'Content-Type: application/json' -d '{"code":"M01","name":"张三"}'
BID=$(curl -s -X POST localhost:8000/admin/ballots -H "X-Member-Token: $ADMIN" \
  -H 'Content-Type: application/json' -d '{"title":"预算案"}' | jq .id)
curl -X POST localhost:8000/admin/ballots/$BID/open -H "X-Member-Token: $ADMIN"

# 会员领凭证（会员令牌）
CRED=$(curl -s -X POST localhost:8000/me/ballots/$BID/credential \
  -H "X-Member-Token: $MEMBER_TOKEN" | jq -r .credential)

# 匿名投票（请求里没有任何会员身份）
curl -X POST localhost:8000/ballots/$BID/vote -H 'Content-Type: application/json' \
  -d "{\"credential\":\"$CRED\",\"choice\":\"yes\"}"

# 结算与公开结果
curl -X POST localhost:8000/admin/ballots/$BID/settle -H "X-Member-Token: $ADMIN"
curl localhost:8000/public/ballots/$BID
```

## 测试覆盖（21 项）

- `tests/test_lifecycle.py`：冻结范围、重领不增资格、改投覆盖、停权/恢复语义、
  结算计数、结算固化、draft 拒绝、幂等。
- `tests/test_privacy.py`：名册无选择字段、投票响应无身份、会员令牌不能当凭证、
  凭证明文不落库、公开端无逐票信息、越权 403/401。
- `tests/test_concurrency.py`：多线程下并发改投、停权 vs 投票、结算 vs 投票、
  反复停权/恢复，断言结果始终等价于某个确定的服务端接受顺序。

## 安全边界说明

本系统的威胁模型**不**包含能直接读取 SQLite 文件的数据库超级管理员；
它保证的是各 HTTP 业务接口与各自权限范围不跨读：管理员看不到任何个人选择，
投票端拿凭证换不回会员身份，公开端拿不到逐票与时间序列。
