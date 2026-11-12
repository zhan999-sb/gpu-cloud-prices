# GPU 价格站的 GitHub Actions 配置

生产站点是 <https://gpu-cloud-prices.pages.dev>，Cloudflare Pages 项目为
`gpu-cloud-prices`，生产分支为 `main`。日常采集、构建、统计审计、部署和历史保存
全部在 `ubuntu-latest` 执行；本机只负责审阅与推送代码，不参与定时生产任务。

## 人工一次性配置

1. **创建一个空 GitHub 仓库**，建议私有。不要初始化 README、License 或
   `.gitignore`，以免与首次推送冲突。记录 `OWNER/REPO` 的 HTTPS 地址。
   首次推送后，在仓库 **Settings → General → Default branch** 确认默认分支为
   `main`；定时任务只从默认分支运行。不要把 `data` 设为默认分支。
2. 在 GitHub **Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token** 创建给操作侧 agent 使用的 PAT。
   Resource owner 选择仓库所有者，Repository access 选择 **Only select
   repositories** 并仅勾选这个仓库。Repository permissions 精确设置为：

   | 权限 | 级别 | 用途 |
   | --- | --- | --- |
   | Contents | Read and write | 首次代码推送及必要的历史初始化 |
   | Workflows | Read and write | 推送 `.github/workflows/` |
   | Actions | Read and write | 操作侧日后触发、查看、管理运行 |
   | Metadata | Read-only（自动附带） | 仓库元数据 |

   设置有效期；组织仓库如要求审批，完成组织审批。通过 Windows Git Credential
   Manager 的凭据界面保存/更新凭据，让 agent 使用已有认证。不要将 PAT 放入
   Git remote URL、命令参数、聊天、`.env` 或仓库。这个 PAT **不需要**成为
   Actions secret；云端保存历史用 GitHub 自动提供的 `GITHUB_TOKEN`。
3. 在仓库 **Settings → Secrets and variables → Actions → New repository
   secret** 添加且仅需添加以下两项：

   | Secret 名称 | 内容 |
   | --- | --- |
   | `CLOUDFLARE_API_TOKEN` | 对目标账户具有 **Account → Cloudflare Pages → Edit** 权限的 API token，资源限制到该账户 |
   | `CLOUDFLARE_ACCOUNT_ID` | 拥有 `gpu-cloud-prices` 项目的 Cloudflare Account ID |

   项目必须已存在且生产分支为 `main`。联盟 ID 从受版本控制的
   `config/affiliates.json` 读取，是公开配置，无须添加联盟 ID secrets。
4. 在仓库 **Settings → Actions → General** 允许本工作流使用的 GitHub 官方
   actions。工作流已显式声明生产 job 的 `contents: write`，PR 检查只有
   `contents: read`，无需给所有工作流开放额外权限。若组织策略禁止写 token，
   由管理员允许该生产 job 写入 `data`；分支规则不得要求 bot 对 `data` 提 PR、
   获得代码审查或通过源代码检查。`main` 可照常受保护。
5. 让操作侧 agent 按下节推送。已有本机价格历史时，**先初始化 `data`，再首次
   推送 `main`**，防止首个云端任务从空历史开始。推送后确认默认分支 `main`，
   到 **Actions → GPU site → Run workflow**，选择 `main` 并运行一次。
   检查测试、采集、审计、部署、保存历史均成功；确认 `data` 分支只含
   `gpu_prices.sqlite` 和 `README.md`，并访问生产 URL。

如果该 Pages 项目另有 Cloudflare Git 自动构建集成，应在项目构建设置中关闭
该集成的自动生产/预览部署，让本工作流成为唯一生产发布入口。

## 操作侧 agent 怎样推送

本次实现没有执行任何远端推送或 GitHub/Cloudflare API 调用。以下是仓库创建且
认证配置好之后，由操作侧 agent 执行的命令；`OWNER/REPO` 必须替换为实际值。

在 Windows PowerShell 切换到项目根目录。先查看现有 remote；没有 `origin` 时
才添加，已有 remote 时先确认其指向正确仓库：

```powershell
git remote -v
git remote add origin https://github.com/OWNER/REPO.git
```

如果已有 `data/gpu_prices.sqlite` 价格历史，先在 Git Bash 执行下面两条，
**在首次 `main` 推送前**把历史快照提交到远端孤立分支：

```bash
bash ci/restore_db.sh data/gpu_prices.sqlite ci/runtime/history-base
bash ci/save_db.sh data/gpu_prices.sqlite ci/runtime/history-base
```

这条初始化路径仅用于远端尚无 `data` 的情形：restore 保留并校验本机数据库，
save 使用 SQLite backup 生成一致快照，不改写源数据库。若已有历史实际位于
`pipelines/gpu_prices/runtime/gpu_prices.sqlite`，由操作侧 agent 确认哪一份是
应延续的完整历史，然后将**两条命令的第一个参数均改为该路径**。不要凭文件
时间或 README 的旧默认值选择数据源。若远端已有 `data`，不要覆盖本机文件；
改用全新 checkout 的恢复流程。没有任何旧历史时省略初始化，首个云端采集会
自动建立数据库和无父提交的 `data` 分支。

操作侧 agent 应先与负责源码的 agent 合并并审阅完整首次提交，确保包含
`pipelines/`、它依赖的模块、`tests/`、`pytest.ini`、`config/` 和本次 CI 文件。
这里仅列出本次改动的暂存命令，**这些 CI 文件本身不能替代完整应用源码**：

```powershell
git add -- .github/workflows/ ci/ docs/ci-setup.md
git diff --cached --name-only
git diff --cached --stat
git commit -m "Move GPU price production pipeline to GitHub Actions"
git branch -M main
git push --set-upstream origin main
```

如果 `main` 已存在，按现有协作方式提交/合并即可，不重命名已有工作分支。
首次源码提交必须审阅暂存清单，排除 `.env`、`vault/`、数据库、日志和本机构建
目录；不要对整个未初始化工作区直接 `git add .`。数据库只进入独立 `data`
分支。本脚本不会切换当前分支或修改主工作树的暂存区。

## 自动运行及失败策略

- `gpu-site.yml`：每天 UTC **00:17、06:17、12:17、18:17** 运行，即北京时间/
  新加坡时间 **08:17、14:17、20:17、次日 02:17**；也接受手动触发，以及 `main`
  上 `pipelines/**`、`config/**`、`ci/**` 或生产工作流自身的 push。
  GitHub 的 schedule 可能延迟，不保证准点执行。
- 生产工作流使用一个固定并发组，`cancel-in-progress: false`，所有触发共享锁，
  不会相互中断或同时读写数据库。GitHub 可能替换旧的 pending 运行，故这不是
  保存每一次触发的持久队列。
- job 最长 30 分钟；采集步骤最长 20 分钟，部署最长 5 分钟，历史保存最长
  3 分钟。步骤上限不是相加后的保证；整个 job 仍受 30 分钟约束。
- 使用 Python 3.12、Node 22，官方 actions 均固定到具体版本 tag；只为测试安装
  固定版本 `pytest==8.4.2`，生产 Python 管线及 CI 辅助脚本均为标准库。
  Wrangler 固定为 `4.45.0`，部署命令为：

  ```bash
  npx --yes wrangler@4.45.0 pages deploy ci/runtime/site --project-name gpu-cloud-prices --branch main
  ```

- 恢复历史 → 现有离线测试及 CI 测试 → 全量 `collect` → `build` → 独立
  `audit` → 部署。所有管线命令均显式指定 `--db data/gpu_prices.sqlite`；
  构建与审计使用 `--out ci/runtime/site`，构建指定真实生产 origin。
  **生产没有 `--limit-pages`**，不会把截断采集当成完整采集。
- 当前 CLI 在单家失败时返回 1，因此 CI 检查 stdout JSON 的 `collections`。
  至少一家 `complete` 且有记录，其他结果均为已知状态、退出码与结果一致，
  才允许继续。失败供应商由现有渲染器标记 stale；CI 不补造价格、不改变
  供应商筛选规则。全部失败/全空、顶层 error、无效 JSON、未知/limited 状态
  或异常退出码都使 job 失败并跳过部署。供应商列表不硬编码在 CI 中。
- build 已自带统计审计，工作流另外执行一次 `audit` 并保存报告。
  审计非零退出会使 job 失败并阻止部署。这里不启用 `--live-sample`，避免
  网络抽样结果取代本地统计一致性门槛；离线统计审计覆盖本次生成的站点。
- 即使采集、构建、审计或部署失败，只要恢复成功且采集曾启动，仍尝试保存
  已提交的供应商批次和失败记录；因此成功采集不依赖 Cloudflare 是否可用。
  全部采集失败也不部署，但会保存失败运行记录。人工取消或整个 job 被强制
  超时/终止时不能保证清理步骤执行，最后一次远端快照仍保持完整。

## 历史数据库的保护及恢复

`data` 不是 Actions cache，也不是短期 artifact。每次都通过 Git 获取该分支，
通过 SQLite read-only 连接及 backup 接口读取完整快照（包含已提交的 WAL
数据），执行完整性检查后原子写入目标文件。远端鉴权/网络错误会直接失败，
只有 `ls-remote` 明确表示分支不存在时才允许首次初始化。

保存使用独立 Git index 和 `commit-tree`，仅提交数据库与 README，普通
fast-forward push，**不 force push**。共最多推送 3 次，每次重新读取远端。
仅 README/提交历史变化时可重建父提交后重试；若远端 DB 相对恢复基线已有
变化，拒绝覆盖，因为自动合并 SQLite 二进制文件可能丢失价格历史。
正常生产任务已串行，该保护针对其他外部写入者。

每个运行上传 `gpu-site-reports-<run_id>-<attempt>`（测试、采集、构建、审计
JSON/日志）及实际生成的 `gpu-site-build-<run_id>-<attempt>`，保留 7 天。
失败构建的 site artifact 可能不完整或尚未通过审计，不应直接作为生产版本。
保存历史失败时另上传 `gpu-history-recovery-<run_id>-<attempt>`，其中是本次
一致性数据库快照。先下载保存，比较远端历史并人工处理冲突，不要强制覆盖。

数据库达到 **95 MiB** 时保存会主动失败并保留恢复 artifact，提示迁移持久化
方案，避免碰到 Git 单文件大小限制。长期历史和 Git 中的二进制版本会持续
增加仓库体积，应监控增长，迁移前保留完整历史；本方案不自动截断历史。

脚本调用格式：

```bash
bash ci/restore_db.sh [数据库路径，默认 data/gpu_prices.sqlite] [基线文件，默认 ci/runtime/history-base]
bash ci/save_db.sh    [同一数据库路径]                        [同一基线文件]
```

使用 Bash（Ubuntu 或 Windows Git Bash），脚本启用 `set -euo pipefail`，其余
shell 命令使用 POSIX 写法。严格的 POSIX `/bin/sh`（Ubuntu 的 dash）不支持
`pipefail`，因此不要用 `sh ci/restore_db.sh`。工作流显式使用 Bash，不依赖
Windows 的可执行位。脚本只操作当前 checkout 的 `origin` 和固定 `data` 分支。

## 手动触发和回滚

日后手动刷新：GitHub 仓库 **Actions → GPU site → Run workflow → Branch:
main → Run workflow**。其他分支的手动运行不会获得生产 job。无需本机安装
Wrangler，也无需本机能访问各家供应商或 Cloudflare。

回滚生产页面：

1. 在 GitHub **Actions → GPU site → … → Disable workflow**，避免下次定时
   或 push 发布覆盖回滚。若有生产运行正在执行，先等其结束或明确取消。
2. 登录 Cloudflare Dashboard，进入 **Workers & Pages → gpu-cloud-prices →
   Deployments → All deployments**。
3. 找到以前成功的 **Production** 部署，打开右侧 `…`，选择 **Rollback to
   this deployment** 并确认。Preview 部署不能作为回滚目标。
4. 访问生产 URL 确认。Pages 回滚只切换站点版本，不回滚 `data` 分支，也不
   删除已积累的价格历史。修复代码/配置后重新启用工作流，再从 `main` 手动
   运行，避免一启用便再次发布故障代码。

以上 Cloudflare 操作见[官方回滚说明](https://developers.cloudflare.com/pages/configuration/rollbacks/)。

## Actions 用量估算

按私有仓库免费套餐 **2,000 分钟/月**、标准 Linux runner 估算。下面是预算，
**没有实际云端测时数据**；HTTP 分页量、供应商限流、首次 npm 下载及数据库
大小会影响耗时。

| 单次工作 | 预估墙钟时间 |
| --- | --- |
| checkout、Python/Node 与 pytest 准备、历史恢复、测试 | 1–2 分钟 |
| 全量多供应商采集 | 2–5 分钟 |
| 构建、两次统计审计、部署、历史保存、artifact 上传 | 1–3 分钟 |
| 生产完整运行 | **4–10 分钟，预算中值 6 分钟** |
| 单独 PR/push 离线检查 | **1–2 分钟** |

每天 4 次，30 天约 120 次、31 天约 124 次：定时生产约 **480–1,240 分钟/月**，
6 分钟中值约 **720–744 分钟/月**。若另有 20 次 PR 检查和 10 次额外生产
push/手动运行，按中值约 **810–834 分钟/月**。push 和 PR 可能各触发一次
检查，重跑和额外生产触发也都额外计入。

若每次长期接近 20 分钟采集上限，仅定时生产即至少 2,400 分钟/月，会超出
2,000 分钟；应先查看报告定位耗时，再调整运行频率/预算，不能缩短为不完整
采集来伪装全量覆盖。artifact 存储单独计量，7 天保留期不会让存储成本归零。
实际额度和共享用量以账户 Billing 页面为准，见
[GitHub Actions 计费文档](https://docs.github.com/en/billing/concepts/product-billing/github-actions)。

## 本地验证及实施假设

可在 Windows 11 根目录的 PowerShell 运行（需要已安装 Git Bash 和 pytest）：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
python -m pytest tests/test_gpu_prices.py -q -p no:cacheprovider --basetemp ci/runtime/pipeline-tests
python -m unittest discover -s ci/tests -v
bash -n ci/restore_db.sh
bash -n ci/save_db.sh
bash -n ci/history_common.sh
```

CI 集成测试仅向 `ci/runtime/` 内一次性本地 bare Git 仓库推送；不连接 GitHub、
Cloudflare 或任何外部服务。覆盖首次孤立提交、历史往返与追加、旧库初始化、
WAL 快照、损坏/不可达远端、目标文件保护、推送拒绝后的重试、重试耗尽、
真实推送竞争、并发数据库保护和 README 更新保留。采集门槛测试覆盖部分失败与全部失败。
`pr-checks.yml` 在 push（排除 `data`）/PR 上运行这些检查，无部署、无 secrets。

2026-11-12 实施时，新增 **16 项 CI 测试全部通过**。本机已有 PyYAML，两个工作流均通过 YAML 解析，所有 shell
脚本及工作流内的 Bash 代码均通过 `bash -n`。未安装 `actionlint`，本次也未
安装；解析和人工审阅不能替代 GitHub 服务端对 Actions 的最终校验。
现有站点离线测试得到 **160 通过、3 失败**：
`test_seo_sharing_structured_data_and_sitemap`、
`test_thin_content_noindex_and_sitemap_boundary[0]`、
`test_complete_snapshot_excludes_earlier_same_day_keys_without_deleting_history`。
三处均仍断言标题中的 `Azure regions`，当前多供应商渲染输出为 `regions`。
CI 未跳过或放宽这些测试；需由负责源码/测试的 agent 同步修复后再上线。
本次未在 GitHub runner 上运行，也未采集真实价格或实际部署。

实施假设：当前 CLI 保持 `collections` JSON、每家 `complete/failed` 状态及
失败时返回 1 的约定；生产源码与测试、公开配置会一并推入 `main`；Cloudflare
项目及上述 secrets 有效；GitHub 允许内置 token 创建和更新 `data`；该分支
专用于历史，不与外部写入者并发使用；DB 尚适合 Git 持久化。源码更新若改变
这些契约，需同步调整 CI 门槛。首次建分支依赖采集已执行并产生有效 SQLite；
测试失败不会创建空数据库来掩盖问题。
