# 部署与配置

## 1. 前置条件

| 项 | 要求 |
|---|---|
| Azure 订阅 | 部署者在订阅（或目标资源组的上级作用域）上拥有 **Owner** 权限：创建资源、配置 PIM 策略、分配角色 |
| Entra ID | 部署者能创建安全组（默认用户权限即可）。仅在开启自助模式（`enable_self_service = true`）时需要 **Microsoft Entra ID P2**（PIM）|
| 飞书 | 一个飞书租户的管理员：创建企业自建应用、发布应用、创建审批定义（见第 4 节）|
| 工具 | Azure CLI ≥ 2.60、Terraform ≥ 1.9、[uv](https://docs.astral.sh/uv/)（Python 3.11+ 由 uv 自动管理）。**不需要本地 Docker**，镜像通过 `az acr build` 在云端构建 |
| 区域 | 源数据所在区域（治理平面默认同区域）。Confidential Ledger 仅在部分区域提供，可通过 `confidential_ledger_location` 单独指定 |

订阅需要一次性注册 VNet 模式 Container Apps 所需的网络特性（`deploy.sh` 会自动检查并注册）：

```bash
az feature register --namespace Microsoft.Network --name AllowBringYourOwnPublicIpAddress
az provider register -n Microsoft.Network
```

需要的 Entra 目录角色：**不需要** Global Administrator。自助模式只使用 **PIM for Azure resources**，由订阅 Owner 管理。

## 2. 配置

```bash
cp infra/terraform.tfvars.example infra/terraform.tfvars   # 该文件已被 .gitignore 忽略
```

| 变量 | 默认值 | 说明 |
|---|---|---|
| `subscription_id` | — | 部署的订阅 |
| `source_location` | — | 境外源所在区域 |
| `governance_location` | 同 `source_location` | Log Analytics、Job、审计归档所在区域 |
| `confidential_ledger_location` | 同治理区域 | Confidential Ledger 所在区域 |
| `enable_confidential_ledger` | `true` | 关闭后只写 Log Analytics + WORM |
| `prefix` | `xl` | 资源名前缀（2–6 位小写字母或数字）|
| `it_ops_members` | `[]` | IT 运维的 UPN 列表（Owner）；也是 IT 审批人名单的默认值 |
| `auditor_members` | `[]` | 审计人员的 UPN 列表（只读台账）|
| `it_ops_owner_mode` | `active` | `eligible` = IT 的 Owner 也走 PIM 激活（生产推荐）|
| **工单接入** | | |
| `intake` | `feishu` | `feishu`：飞书审批驱动（部署 connector 和 `ticket-events` 队列，自动包含托管搬运组件）；`cli`：只能用 `xl transfer` 提交（需要自助模式）|
| `feishu_app_id` | — | 飞书企业自建应用的 App ID（App Secret 不放在这里，见第 3、4 节）|
| `feishu_approval_code` | — | 数据申请审批定义的 Code（只处理这一个定义）|
| `feishu_fields` | `数据集` / `目的地` / `用途` | 表单控件（custom_id、id 或名称）→ 含义（`dataset` / `destination` / `purpose`）|
| `feishu_domain` | `https://open.feishu.cn` | 飞书开放平台地址 |
| `it_approver_emails` | 同 `it_ops_members` | 审批通过后，审批人的飞书邮箱必须在这个名单中（大小写不敏感），且不能是申请人本人 |
| `datasets` | `{}` | 可申请的数据集：名称（表单中显示）→ `{ prefix = "源容器中的前缀/", description = "..." }` |
| `ticket_reconcile_days` | `7` | collector 对账时回看的天数 |
| **数据通道** | | |
| `source_public_access` | `private` | 源存储的网络入口。`private`（推荐，工单模式下只有 worker 读取）：只允许 Private Endpoint；`perimeter`：公网入口由 Network Security Perimeter 治理（自助模式下供国内环境直接读取）|
| `perimeter_allowed_ips` | `[]` | `perimeter` 模式下允许的公网 CIDR |
| `enable_managed_transfer` | `false` | 托管搬运组件（Service Bus + transfer Job + Key Vault + NAT Gateway）。`intake = "feishu"` 时自动开启 |
| `transfer_parallelism` | `4` | 同时运行的搬运分片数 |
| `rclone_transfers` | `16` | 每个分片内 rclone 的并发文件数 |
| `secrets_admin_ips` | `[]` | 允许通过数据面访问 Key Vault 的运维 IP。**在门户中登记目的地需要**（门户直接访问 Key Vault 数据面）；用 `xl dest add` / `xl feishu set-secret`（ARM 管理面）则不需要 |
| **自助模式（可选）** | | |
| `enable_self_service` | `false` | 开启后，`data_team_members` 可通过 PIM 申请 `source-reader` / `transfer-operator`，见 [self-service.md](self-service.md) |
| `data_team_members` | `[]` | 自助模式下可申请的人员 UPN |
| `max_activation_hours` | `8` | 单次激活的最长时长 |
| `eligibility_days` | `180` | 资格的有效期（到期需要重新认证）|
| `eligibility_mode` | `group` | `group` = 资格授予 data-team 组；`user` = 展开到每个成员，立即生效 |
| `alert_emails` | `[]` | 台账出现失败、拒绝、校验失败、证据缺失、越权访问时通知的邮箱 |
| `known_service_principals` | `{}` | 允许在授权窗口外读取数据的平台服务（对象 ID → 名称），例如数据分类扫描；其访问记为提示而非异常 |
| `collector_cron` | `*/5 * * * *` | collector 的运行频率 |
| `ledger_retention_days` | `2555` | WORM 保留期（约 7 年）|
| `ledger_immutability_locked` | `false` | 锁定 WORM 策略。**锁定后不可逆**，PoC 请保持 `false` |
| `vnet_cidr` | `10.60.0.0/16` | 方案 VNet 的地址空间（/23 给 Container Apps，/24 给 Private Endpoint）|
| `seed_batches` | `batch-001,batch-002,batch-003` | PoC：seed Job 生成的样例批次 |
| `log_retention_days` | `90` | Log Analytics 交互式保留期 |
| `ledger_table_total_retention_days` | `2556` | `XLedger_CL` 的总保留期（含长期保留）|

## 3. 部署

```bash
az login
az account set -s <subscription-id>
./scripts/deploy.sh
```

`deploy.sh` 依次执行：

1. `terraform apply`（阶段 1）：创建资源组、存储、Entra 组、托管身份、Service Bus、Key Vault（含 `feishu-app-secret` 占位机密）、Log Analytics、DCR、Confidential Ledger、ACR、Container Apps 环境、Job 与 connector（先使用占位镜像）
2. `az acr build`：在 ACR 中构建 `app/` 镜像（rclone + Python 3.12）
3. `terraform apply`（阶段 2）：把 Job 和 connector 切换到新镜像
4. 把 `cli_env` 输出写入仓库根目录的 `.xl.env`，供 `xl` CLI 使用

部署完成后写入飞书 App Secret（交互输入、不回显，经 ARM 管理面写入 Key Vault，不经过 tfvars 和 Terraform state）：

```bash
cd app
uv run xl feishu set-secret
uv run xl feishu restart        # connector 立即用新密钥连接（否则它每 60 秒重试一次）
```

PoC 环境再生成样例数据：`./scripts/seed-data.sh`（seed Job 在 VNet 内用专用托管身份写入源账号）。

## 4. 飞书：自建应用与审批定义（飞书租户管理员）

**4.1 创建企业自建应用**（[飞书开发者后台](https://open.feishu.cn/app)）

1. 创建企业自建应用；在“凭证与基础信息”中取得 **App ID**（写入 `feishu_app_id`）和 **App Secret**（用 `xl feishu set-secret` 写入 Key Vault）
2. 添加应用能力：**机器人**（用于给申请人发进度消息；未开通时消息发送失败，不影响搬运）
3. 权限管理中开通以下权限（名称以开发者后台显示为准）：

   | 用途 | 权限 |
   |---|---|
   | 查询审批定义、实例、订阅审批事件 | 审批：`approval:approval`（或只读 `approval:approval:readonly` + 订阅所需权限）|
   | 用审批人的 open_id 查邮箱（复核审批人是否属于 IT）| 通讯录：以应用身份读取通讯录基本信息（`contact:contact.base:readonly`）+ 获取用户邮箱信息（`contact:user.email:readonly`）|
   | 给申请人发消息 | 消息：以应用的身份发消息（`im:message:send_as_bot`）|

4. 权限管理 → **通讯录权限范围**：至少包含所有申请人和 IT 审批人（简单起见选“全部成员”）。不包含时查不到该成员的邮箱（错误码 41050），审批人会因此无法被认定为 IT
5. 版本管理与发布：创建版本，**可用范围**包含申请人和审批人（机器人只能给可用范围内的人发消息，否则报 230013），提交发布并在管理后台审核通过。**之后每次改权限或能力，都要再发布一个版本才生效**

**4.2 创建审批定义**（审批管理后台 → 创建审批）

| 控件 | 类型 | 选项 |
|---|---|---|
| 数据集 | 单选 | 与 `datasets` 的名称一致 |
| 目的地 | 单选 | 与 Key Vault 中启用的 `dest-<名称>` 一致 |
| 用途 | 多行文本 | — |

审批流程的审批人设为 IT 运维。保存后，编辑页地址中的 `definitionCode=...` 即审批定义 Code（写入 `feishu_approval_code`）。控件名称不同时，用 `feishu_fields` 做映射；`xl feishu check` 会列出每个控件被识别成的含义。

**4.3 订阅事件（connector 运行之后）**

开发者后台 → 事件与回调 → 订阅方式选择**使用长连接接收事件**（保存时飞书要求已有长连接在线，所以要在 connector 用正确的 App Secret 连上之后再保存）→ 添加事件“审批 → **审批实例状态变更**”。connector 启动时会自动为审批定义调用订阅接口。

**4.4 自检**

```bash
uv run xl feishu check     # 读取审批定义：控件 → 含义；下拉选项与登记表是否一致
```

用一个非 IT 账号提交一张测试单，由 IT 账号审批，然后 `uv run xl tickets` 查看台账。

## 5. 部署出的资源

| 资源组 | 资源 |
|---|---|
| `rg-<prefix>-<sfx>-source` | 源存储账号 `<prefix><sfx>src`（容器 `datasets`，开启版本控制与软删除）|
| `rg-<prefix>-<sfx>-gov` | VNet `vnet-*`（`snet-cae` 子网接入 Container Apps，`snet-pe` 子网放 Blob Private Endpoint）+ Private DNS · 控制账号 `<prefix><sfx>ctl`（容器 `collector-state`：collector 状态与处理标记）· 审计账号 `<prefix><sfx>audit`（WORM 容器 `ledger`：事件归档与工单快照）· Log Analytics `log-*` · DCE/DCR · Confidential Ledger `acl-*` · ACR · Container Apps 环境 `cae-*` · **connector** `ca-*-connector`（常驻，无入站）· Job `job-*-transfer` / `job-*-collector` / `job-*-seed`（PoC）· Service Bus `sb-*`（队列 `ticket-events`、`transfer-requests`）· Key Vault `kv-*`（目的地登记表、`feishu-app-secret`）· NSP `nsp-*` · NAT Gateway `ng-*` + 固定公网 IP `pip-*-egress` · 托管身份 · Workbook · 告警规则 `alert-*-ledger` |
| Entra ID | 安全组 `<prefix>-it-ops-<sfx>`、`<prefix>-data-team-<sfx>`（仅自助模式使用）、`<prefix>-auditors-<sfx>` |
| PIM | 仅自助模式：每个数据角色一份角色管理策略 + 资格分配 |
| 订阅 | 诊断设置：Activity Log → Log Analytics |

### 网络说明

- **治理平面**的存储账号（审计归档、collector 状态）只能通过 Private Endpoint 访问，collector 运行在接入 VNet 的 Container Apps 中
- **源存储**默认只开 Private Endpoint，只有 worker 读取；自助模式下按 `source_public_access` 二选一：
  - `private`：只有能访问 Private Endpoint 的网络（与 `vnet-*` 对等互连的 VNet、经专线/VPN 接入的网络）可以读数据；对等互连的 VNet 需要链接 Private DNS 区域 `privatelink.blob.core.windows.net`
  - `perimeter`：`publicNetworkAccess = SecuredByPerimeter`，只有 `perimeter_allowed_ips` 中的公网 IP 可以连接，其他来源返回 `not authorized by network security perimeter`。允许和拒绝的连接都记录在 Log Analytics 的 `NSPAccessLogs` 表中
- **connector** 只有出站连接：经 NAT Gateway 的固定出口 IP 连接飞书开放平台（长连接 + API）。需要确认 Azure 所在区域到 `open.feishu.cn` 的连通性
- `xl tickets / timeline / ledger-verify / dest / feishu` 只调用 ARM、Log Analytics 和 Confidential Ledger，在任何能访问 Azure 公共端点的地方都可以执行

> **不要直接打开存储账号的公网访问。** 很多组织（包括不少测试订阅）会用 Azure Policy 强制关闭存储公网访问，而 `SecuredByPerimeter` 是这类策略通常会豁免的合规做法。
>
> Terraform 的 azurerm provider 已设置 `storage { data_plane_available = false }`：存储的所有配置都通过 ARM 管理，部署者的机器不需要能访问存储的数据平面。

### 部署时可能遇到的问题

| 现象 | 处理 |
|---|---|
| 存储账号的公网访问被自动关闭 | 组织的 Azure Policy 强制关闭了存储公网访问；本方案默认就是私网，公网场景请使用 `perimeter` 模式 |
| Container Apps 环境创建失败，提示 `AllowBringYourOwnPublicIpAddress` | 订阅需要一次性注册该网络特性；`deploy.sh` 会自动处理 |
| 新加入组的成员申请时报 `RoleAssignmentDoesNotExist` | PIM 识别新组成员可能需要较长时间（PoC 中超过 40 分钟）。等待并重新登录，或设置 `eligibility_mode = "user"` |
| `terraform` 报 Graph 401：`TokenIssuedBeforeRevocationTimestamp` | 账号会话被撤销（例如改了密码），Azure CLI 缓存的旧令牌已失效。重新 `az login`；若仍报错，清除 Azure CLI 的访问令牌缓存后重试 |
| 公共 IP 的 `ip_tags` 出现漂移 | 部分订阅会由平台自动添加 IP 标签；已在代码中忽略该属性 |

### 登记目的地（IT 运维）

目的地 = Key Vault 中的一个机密 `dest-<名称>`：**值**是对象存储的 Secret Key，**标签**是连接信息和落地根目录（`provider`、`endpoint`、`access_key_id`、`root`，可选 `region`、`description`，以及固定的 `xl-kind=destination`）。**只有 IT 运维可以登记**。门户中的逐步操作见 [runbook.md](runbook.md) 第 1.1 节；命令行等价操作：

```bash
uv run xl dest add store-a --provider Other --endpoint https://s3.<region>.example.com \
  --access-key-id <AK> --root <bucket>/landing/ --description "国内对象存储 A"    # Secret Key 交互输入
uv run xl dest add idc --provider Other --endpoint https://s3.idc.example --access-key-id <AK> \
  --root datasets/ --ca-cert idc-ca.pem                                         # 私有 CA 存为 dest-idc-ca
```

建议使用**只能写入指定 bucket 的子账号 / 访问密钥**，不要使用主账号密钥。

**放行 worker 的出口 IP**：`terraform -chdir=infra output managed_transfer` 中的 `egress_ip` 是 worker 唯一的公网出口，在每个目的地的桶策略 / 防火墙中只放行这个 IP。

**连通性测试**：`uv run xl dest test <名称>`，从 worker 本身（同一身份、网络和出口 IP）列举、写入、读取、删除一个探测对象，结果写入台账。

| 维护操作 | 做法 |
|---|---|
| 轮换凭证 / 修改落地目录 | 门户中新建机密版本或改标签（或重新执行 `xl dest add <同名> ...`），下一个分片即生效 |
| 停用目的地 | 门户中禁用机密（或 `xl dest remove <名称>`），之后的工单会被复核拒绝 |
| 新增目的地 | 登记后，在飞书审批表单的“目的地”控件中加上同名选项（collector 会在不一致时告警）|

> 也支持把整个 rclone 远端写成 JSON 值的格式（`xl dest add <名称> --from-rclone <远端> --allow <bucket/前缀/>`），适合 rclone 有特殊参数的目的地。

## 6. 增删人员

- **业务方**：在飞书中管理（审批表单的可见范围），不需要 Azure 账号
- **IT 审批人**：修改 `it_approver_emails`（默认即 `it_ops_members`）后执行 `./scripts/deploy.sh`

Azure 侧的人员管理只涉及**组成员**：

- 用 IaC 管理：修改 `terraform.tfvars` 中的成员列表，再执行 `./scripts/deploy.sh`（或 `terraform apply`）
- 在门户中管理：Entra ID → 组 → 成员。注意下次 `terraform apply` 时，代码中未声明的成员不会被删除，但代码中声明的成员会被补回

## 7. 更新代码

修改 `app/` 后重新运行 `./scripts/deploy.sh` 即可，会构建新的镜像标签并滚动所有 Job 和 connector。

本地测试：

```bash
cd app
uv run pytest
uv run ruff check src tests
```

## 8. 清理

```bash
./scripts/destroy.sh
```

> 如果已经把 `ledger_immutability_locked` 设为 `true`，审计账号要等所有归档 blob 过了保留期才能删除。这是 WORM 的设计目的。

## 9. PoC：模拟“国内环境”的测试机

`scripts/sim-host.sh` 在**单独的资源组和 VNet**（不与方案 VNet 对等互连）中创建一台小虚拟机，模拟国内环境：用 `rclone serve s3` 开放 TLS 的 S3 兼容端点（模拟国内对象存储目的地），只允许 worker 的出口 IP 访问；不开放其他入站端口（通过 `az vm run-command` 操作）。自助模式下它也可以模拟国内的开发机，通过自己的公网 IP 经 NSP 读取源数据。

```bash
SIM_LOCATIONS="<region-1> <region-2>" ./scripts/sim-host.sh up    # 小规格 VM 容量不足时自动换规格/区域
# 两个 TLS S3 端点（模拟两个目的地），输出 rclone 远端选项和 CA 证书（JSON），再用 xl dest add --from-json 注册
EGRESS="$(terraform -chdir=infra output -json managed_transfer | jq -r .egress_ip)/32"
./scripts/sim-host.sh expose "$EGRESS" cloud-x 9443 train-data eval-data > cloud-x.json
./scripts/sim-host.sh expose "$EGRESS" idc 9444 datasets > idc.json
./scripts/sim-host.sh down                                         # 用完删除
```

测试机属于“客户的外部环境”，因此刻意不放在方案的 Terraform 里。
