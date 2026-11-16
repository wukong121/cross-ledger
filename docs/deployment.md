# 部署与配置

## 1. 前置条件

| 项 | 要求 |
|---|---|
| Azure 订阅 | 部署者在订阅（或目标资源组的上级作用域）上拥有 **Owner** 权限：创建资源、配置 PIM 策略、分配角色 |
| Entra ID | 租户具备 **Microsoft Entra ID P2**（或 Entra ID Governance）许可证，PIM 需要；部署者能创建安全组（默认用户权限即可）|
| 工具 | Azure CLI ≥ 2.60、Terraform ≥ 1.9、[uv](https://docs.astral.sh/uv/)（Python 3.11+ 由 uv 自动管理）。**不需要本地 Docker**，镜像通过 `az acr build` 在云端构建 |
| 区域 | 源数据所在区域（治理平面默认同区域）。Confidential Ledger 仅在部分区域提供，可通过 `confidential_ledger_location` 单独指定 |

订阅需要一次性注册 VNet 模式 Container Apps 所需的网络特性（`deploy.sh` 会自动检查并注册）：

```bash
az feature register --namespace Microsoft.Network --name AllowBringYourOwnPublicIpAddress
az provider register -n Microsoft.Network
```

需要的 Entra 目录角色：**不需要** Global Administrator。本方案只使用 **PIM for Azure resources**，由订阅 Owner 管理。若改用 PIM for Groups 或 Entra 访问包，则需要 Privileged Role Administrator / Identity Governance Administrator。

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
| `it_ops_members` | `[]` | IT 运维的 UPN 列表（Owner + 审批人）|
| `data_team_members` | `[]` | 研发/算法/数据人员的 UPN 列表（可申请）|
| `auditor_members` | `[]` | 审计人员的 UPN 列表（只读台账）|
| `it_ops_owner_mode` | `active` | `eligible` = IT 的 Owner 也走 PIM 激活（生产推荐）|
| `max_activation_hours` | `8` | 单次激活的最长时长 |
| `eligibility_days` | `180` | 资格的有效期（到期需要重新认证）|
| `source_public_access` | `private` | 源存储的网络入口。`private`（推荐）：只允许 Private Endpoint；`perimeter`：公网入口由 Network Security Perimeter 治理，只放行 `perimeter_allowed_ips` |
| `perimeter_allowed_ips` | `[]` | `perimeter` 模式下允许的公网 CIDR（国内云 NAT 网关 / IDC 的固定出口 IP）|
| `enable_managed_transfer` | `false` | 可选的托管搬运组件：Service Bus 提交任务 → Container Apps Job 以服务身份运行 rclone，经私网读取源数据并写入目的地对象存储；附带 Key Vault（目的地凭证）和 NAT Gateway（固定出口 IP）。默认的数据通道是 rclone 自助拉取，不需要它 |
| `transfer_parallelism` | `4` | 托管搬运组件同时运行的分片数 |
| `rclone_transfers` | `16` | 托管搬运中每个分片内 rclone 的并发文件数 |
| `secrets_admin_ips` | `[]` | 允许通过数据面管理 Key Vault 的运维 IP（可选；推荐通过 ARM 管理面写入凭证，见下文）|
| `alert_emails` | `[]` | 台账出现失败、拒绝、校验失败、证据缺失、越权访问时通知的邮箱 |
| `known_service_principals` | `{}` | 允许在授权窗口外读取数据的平台服务（对象 ID → 名称），例如数据分类扫描；其访问记为提示而非异常 |
| `collector_cron` | `*/5 * * * *` | collector 的运行频率 |
| `ledger_retention_days` | `2555` | WORM 保留期（约 7 年）|
| `ledger_immutability_locked` | `false` | 锁定 WORM 策略。**锁定后不可逆**，PoC 请保持 `false` |
| `vnet_cidr` | `10.60.0.0/16` | 方案 VNet 的地址空间（/23 给 Container Apps，/24 给 Private Endpoint）|
| `seed_batches` | `batch-001,batch-002,batch-003` | PoC：vendor Job 生成的样例批次 |
| `eligibility_mode` | `group` | `group` = 资格授予 data-team 组（推荐，增删人员无需重新部署；但 PIM 识别新加入的成员可能有较长延迟）；`user` = 资格展开到每个成员，立即生效 |
| `log_retention_days` | `90` | Log Analytics 交互式保留期 |
| `ledger_table_total_retention_days` | `2556` | `XLedger_CL` 的总保留期（含长期保留）|

## 3. 部署

```bash
az login
az account set -s <subscription-id>
./scripts/deploy.sh
```

`deploy.sh` 依次执行：

1. `terraform apply`（阶段 1）：创建资源组、存储、Entra 组、托管身份、PIM 策略与资格、Log Analytics、DCR、Confidential Ledger、ACR、Container Apps 环境与 Job（先使用占位镜像）
2. `az acr build`：在 ACR 中构建 `app/` 镜像（rclone + Python 3.12）
3. `terraform apply`（阶段 2）：把 Job 切换到新镜像
4. 把 `cli_env` 输出写入仓库根目录的 `.xl.env`，供 `xl` CLI 使用

部署完成后：

```bash
./scripts/seed-data.sh      # 运行 vendor Job（在 VNet 内，用供应商托管身份）在源账号中生成样例批次
cd app && uv run xl roles   # 检查自己的资格
```

> 部署者本人如果同时在 `data_team_members` 和 `it_ops_members` 中，可以发起申请，但需要**另一位** IT 成员审批（PIM 禁止自我审批）。

## 4. 部署出的资源

| 资源组 | 资源 |
|---|---|
| `rg-<prefix>-<sfx>-source` | 源存储账号 `<prefix><sfx>src`（容器 `datasets`，开启版本控制与软删除）|
| `rg-<prefix>-<sfx>-gov` | VNet `vnet-*`（`snet-cae` 子网接入 Container Apps，`snet-pe` 子网放 4 个 Blob Private Endpoint）+ Private DNS `privatelink.blob.core.windows.net` · 控制账号 `<prefix><sfx>ctl`（容器 `collector-state`）· 审计账号 `<prefix><sfx>audit`（WORM 容器 `ledger`）· Log Analytics `log-*` · DCE/DCR · Confidential Ledger `acl-*` · ACR · Container Apps 环境 `cae-*` · Job `job-*-collector` / `job-*-vendor`（PoC）· 托管身份 · Workbook · `perimeter` 模式下的 NSP `nsp-*` · 告警规则 `alert-*-ledger` · 可选组件：Service Bus `sb-*`、Job `job-*-transfer`、Key Vault `kv-*`、NAT Gateway `ng-*` + 固定公网 IP `pip-*-egress` |
| Entra ID | 安全组 `<prefix>-it-ops-<sfx>`、`<prefix>-data-team-<sfx>`、`<prefix>-auditors-<sfx>` |
| PIM | 每个数据角色一份角色管理策略 + 资格分配（`source-reader`；启用托管搬运时还有 `transfer-operator`）|
| 订阅 | 诊断设置：Activity Log → Log Analytics |

### 网络说明

- **治理平面**的存储账号（审计归档、collector 状态）只能通过 Private Endpoint 访问，collector 运行在接入 VNet 的 Container Apps 中
- **源存储**按 `source_public_access` 二选一：
  - `private`：只有能访问 Private Endpoint 的网络（与 `vnet-*` 对等互连的 VNet、经专线/VPN 接入的网络）可以读数据；对等互连的 VNet 需要链接 Private DNS 区域 `privatelink.blob.core.windows.net`
  - `perimeter`：`publicNetworkAccess = SecuredByPerimeter`，只有 `perimeter_allowed_ips` 中的公网 IP 可以连接，其他来源返回 `not authorized by network security perimeter`。允许和拒绝的连接都记录在 Log Analytics 的 `NSPAccessLogs` 表中
- `xl request / approve / timeline / tickets / ledger-verify` 只调用 ARM、Log Analytics 和 Confidential Ledger，在任何能访问 Azure 公共端点的地方都可以执行

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

### 托管搬运组件：配置目的地

启用 `enable_managed_transfer` 后，还需要两步：

1. **国内对象存储放行 worker 的出口 IP**：`terraform -chdir=infra output managed_transfer` 中的 `egress_ip` 是 worker 唯一的公网出口，在目的地桶策略 / 防火墙中只放行这个 IP。
2. **写入目的地凭证**（Key Vault 机密 `transfer-destination`，JSON 格式）。除 `name`、`bucket`、`ca_cert_pem` 外，其余字段都会作为 rclone 远端的配置项，因此可以对接 rclone 支持的任意对象存储：

```json
{
  "name": "domestic",
  "bucket": "<bucket>",
  "type": "s3",
  "provider": "<rclone 中对应云厂商的 provider>",
  "endpoint": "https://<对象存储 endpoint>",
  "region": "<region>",
  "access_key_id": "<只写、限定桶的访问密钥>",
  "secret_access_key": "<...>",
  "ca_cert_pem": "<可选：目的地使用私有 CA 时提供>"
}
```

推荐由 IT 运维通过 **ARM 管理面**写入。这样不需要能访问 Key Vault 数据面的网络，只需要对资源组的 Owner / Key Vault Contributor 权限：

```bash
KV_ID=$(az keyvault show -n <kv-name> --query id -o tsv)
python3 -c "import json;print(json.dumps({'properties':{'value':open('destination.json').read(),'contentType':'application/json'}}))" > body.json
az rest --method put --url "https://management.azure.com$KV_ID/secrets/transfer-destination?api-version=2023-07-01" --body @body.json
shred -u destination.json body.json
```

worker 在每个分片开始时读取该机密，因此**轮换凭证不需要重新部署**。所有读取都记录在 Key Vault 审计日志中。

## 5. 增删人员

人员管理只涉及**组成员**：

- 用 IaC 管理：修改 `terraform.tfvars` 中的成员列表，再执行 `./scripts/deploy.sh`（或 `terraform apply`）
- 在门户中管理：Entra ID → 组 → 成员。注意下次 `terraform apply` 时，代码中未声明的成员不会被删除，但代码中声明的成员会被补回

## 6. 更新代码

修改 `app/` 后重新运行 `./scripts/deploy.sh` 即可，会构建新的镜像标签并滚动两个 Job。

本地测试：

```bash
cd app
uv run pytest
uv run ruff check src tests
```

## 7. 清理

```bash
./scripts/destroy.sh
```

> 如果已经把 `ledger_immutability_locked` 设为 `true`，审计账号要等所有归档 blob 过了保留期才能删除。这是 WORM 的设计目的。

## 8. PoC：模拟“国内环境”的测试机

`scripts/sim-host.sh` 在**单独的资源组和 VNet**（不与方案 VNet 对等互连）中创建一台小虚拟机，模拟国内公有云的开发机 / 开发容器和搬运机：只通过自己的公网 IP 访问源数据，不开放任何入站端口（通过 `az vm run-command` 操作），预装 rclone、Azure CLI，并用 `rclone serve s3` 在本机模拟国内的 S3 兼容对象存储。

```bash
SIM_LOCATIONS="<region-1> <region-2>" ./scripts/sim-host.sh up    # 输出公网 IP；小规格 VM 容量不足时自动换规格/区域
# 把该 IP 加入 infra/terraform.tfvars：perimeter_allowed_ips = ["<ip>/32"]，并设置 source_public_access = "perimeter"
terraform -chdir=infra apply
# 托管搬运验证：在测试机上开放 TLS S3 端点，只允许 worker 的出口 IP 访问，并输出目的地凭证 JSON
./scripts/sim-host.sh expose "$(terraform -chdir=infra output -json managed_transfer | jq -r .egress_ip)/32" > destination.json
./scripts/sim-host.sh down                                         # 用完删除
```

测试机属于“客户的外部环境”，因此刻意不放在方案的 Terraform 里。
