# 部署与配置

## 1. 前置条件

| 项 | 要求 |
|---|---|
| Azure 订阅 | 部署者在订阅（或目标资源组的上级作用域）上拥有 **Owner** 权限：创建资源、配置 PIM 策略、分配角色 |
| Entra ID | 租户具备 **Microsoft Entra ID P2**（或 Entra ID Governance）许可证，PIM 需要；部署者能创建安全组（默认用户权限即可）|
| 工具 | Azure CLI ≥ 2.60、Terraform ≥ 1.9、[uv](https://docs.astral.sh/uv/)（Python 3.11+ 由 uv 自动管理）。**不需要本地 Docker**，镜像通过 `az acr build` 在云端构建 |
| 区域 | 两个区域：一个模拟“境外源”，一个模拟“境内落地区”。Confidential Ledger 仅在部分区域提供，可通过 `confidential_ledger_location` 单独指定 |

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
| `landing_location` | — | 落地区所在区域（PoC 中模拟境内）|
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
| `transfer_parallelism` | `4` | 同时运行的传输分片数 |
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
2. `az acr build`：在 ACR 中构建 `app/` 镜像（AzCopy + Python 3.12）
3. `terraform apply`（阶段 2）：把两个 Job 切换到新镜像
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
| `rg-<prefix>-<sfx>-landing` | 落地区存储账号 `<prefix><sfx>land`（容器 `datasets`，含 `_manifests/`）|
| `rg-<prefix>-<sfx>-gov` | VNet `vnet-*`（`snet-cae` 子网接入 Container Apps，`snet-pe` 子网放 4 个 Blob Private Endpoint）+ Private DNS `privatelink.blob.core.windows.net` · Service Bus `sb-*`（队列 `transfer-requests`，自带死信队列）· 控制账号 `<prefix><sfx>ctl`（容器 `collector-state`）· 审计账号 `<prefix><sfx>audit`（WORM 容器 `ledger`）· Log Analytics `log-*` · DCE/DCR · Confidential Ledger `acl-*` · ACR · Container Apps 环境 `cae-*` · Job `job-*-transfer` / `job-*-collector` / `job-*-vendor`（PoC）· 托管身份 ×3 · Workbook |
| Entra ID | 安全组 `<prefix>-it-ops-<sfx>`、`<prefix>-data-team-<sfx>`、`<prefix>-auditors-<sfx>` |
| PIM | 3 份角色管理策略 + 3 条组级资格分配 |
| 订阅 | 诊断设置：Activity Log → Log Analytics |

### 网络说明

存储账号**不开放公网**。以下操作在任何能访问 Azure 公共端点的地方都可以执行：`xl request / approve / transfer / timeline / tickets / ledger-verify`，它们只调用 ARM、Service Bus、Log Analytics 和 Confidential Ledger。

**直接读写数据**（例如激活 `source-reader` 后用 `az storage blob list` 浏览）则必须位于能解析并访问 Private Endpoint 的网络中：与 `vnet-*` 对等互连的 VNet、经 VPN/ExpressRoute 接入的企业网，或 VNet 内的跳板机/Dev Box。对等互连的 VNet 还需要链接 Private DNS 区域 `privatelink.blob.core.windows.net`。

> 部分组织（包括很多测试订阅）用 Azure Policy 强制关闭存储账号的公网访问。本方案默认就是私网，不受这类策略影响。

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

## 8. 境内拉取节点（生产）

PoC 只部署 Azure 侧。生产环境中，境内本地 IDC 和第三方公有云的 K8s 集群接入 Azure Arc，并部署同一个镜像作为拉取节点，详见 [production.md](production.md)。
