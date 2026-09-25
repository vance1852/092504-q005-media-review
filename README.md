# 数字作品评审协作基础服务

本项目提供数字作品训练机构、评审场所、作者评委与作品资料的统一后台基础能力，负责机构、场所、操作者和领域资料的登记，支持请求幂等、角色权限、SQLite 事务与哈希串联审计。各项资料通过稳定业务键保存，相同请求会返回原回执，不同内容复用编号时返回明确冲突。

在基础能力之上，`ReviewService` 提供**数字交互媒体设计作品的评审后台**。后台只登记作品的脚本摘要、元数据、素材许可凭据与交付清单（压缩包以 SHA-256 摘要标识），**不渲染、不存储任何媒体内容**。

## 评审业务规则

- **版本只追加、不可改写**：截止前可不断追加新版本，旧版本永久保留；同一压缩包内容（相同摘要）不得重复追加，同名压缩包内容不同时按摘要区分版本。
- **截止冻结送审快照**：到达截止时间才能冻结，冻结后生成不可变的送审快照（指向当时最新版本），此后不能再追加版本。
- **版权凭据门槛**：作品按 `required_credentials` 声明必要素材凭据；凭据未补齐时作品为 `pending_evidence`，截止时仍未补齐则冻结为 `quarantined`（待补证）。待补证作品不参与分派与评分，截止后补齐凭据并经 `admit-quarantine` 核验准入后才送审。
- **利益冲突与回避分派**：作者本人、与作者同组织、或已登记冲突关系的评委自动回避；评委可对自己的分派提出回避，系统按同一回避规则补位。
- **评分绑定具体版本**：评分必须引用作品当前冻结送审的版本，维度包括 `script`、`material_licensing`、`interaction_design`、`delivery_completeness`、`overall`。重复评分返回原决定（`duplicate`/`replayed`）；对已有决定提交不同分值，无论是否复用同一请求，都暴露冲突，旧分不可改写。
- **申诉**：申诉期间原分与复核分同时保留，由同评审组其他成员独立复核；裁决时按规则生成最终结果——复核分与原分差距达到阈值（默认 10 分，含）采用复核分（`changed`），否则维持原分（`kept`）。
- **两类查询**：公开查询 `GET /works/{id}/public` 只返回脱敏汇总（作者/标题打码、仅维度均分）；审计查询 `GET /works/{id}/audit` 需 admin 或 auditor，可追到素材凭据、版本快照、分派回避、每次评分与申诉决定，并与哈希审计链互相印证。

## 目录

- `src/skills_workspace/`：领域模型、SQLite 存储、基础权限服务、作品评审服务（`review.py`）、审计链、HTTP 路由和离线验收；
- `tests/`：核心规则、事务边界、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m skills_workspace.acceptance
PYTHONPATH=src python3 -m skills_workspace.review_acceptance
```

命令会在临时 SQLite 数据库中登记机构、操作者、场所和领域资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。`review_acceptance` 额外走完整评审流程：多版本追加、截止冻结、缺凭据隔离与补证准入、回避分派、评分引用版本、重复/冲突评分、申诉裁决以及公开与审计查询。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m skills_workspace.api --database skills_workspace.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。业务写入接口通过 `X-Actor-Id` 标识操作者，支持机构、操作者、场所和领域资料登记，以及审计事件查询。评审相关接口：

- `POST /competitions`、`POST /works`、`POST /work-versions`、`POST /credentials`
- `POST /freeze`、`POST /admit-quarantine`
- `POST /conflicts`、`POST /assignments`、`POST /recusals`
- `POST /scores`、`POST /appeals`、`POST /appeal-reviews`、`POST /appeal-decisions`
- `GET /works/{id}/public`（公开脱敏汇总）、`GET /works/{id}/audit`（审计全链路）
- `GET /works/{id}/versions`、`GET /works/{id}/decisions`

服务重启后，SQLite 中的业务状态和审计链继续保留。
