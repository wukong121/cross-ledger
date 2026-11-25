# 用 rclone 获取海外数据（研发 / 数据团队指南）

> 本文适用于可选的**自助模式**（`enable_self_service = true`）。默认的工单驱动流程中，数据由 worker 自动搬运，业务方不需要使用 rclone，见 [runbook.md](runbook.md)。

> 默认的数据通道是**自助拉取**：研发或数据人员用 [rclone](https://rclone.org/)，以**自己的 Entra ID 身份**读取海外源数据。读权限只在 PIM 申请获批后的时间窗口内有效，窗口内的每一次读取都会被 Azure 记录，并按工单号汇总进台账。全程不需要密钥、SAS 或任何共享账号。

## 1. 两个典型场景

| | 场景 A：研发开发机 / 开发容器 | 场景 B：数据团队批量搬运 |
|---|---|---|
| 在哪里 | 国内公有云开发机 / 开发容器 | 国内云上的一台搬运机或批处理任务 |
| 做什么 | 按需拉取部分数据到本地或共享盘 | 把成批数据搬到国内云的对象存储（S3 兼容）|
| rclone 目标 | 本地路径 | S3 兼容对象存储（各云厂商在 rclone 中都有现成的 provider）|
| 申请的角色 | `source-reader` | `source-reader` |

> 如果源数据不允许对外开放、搬运人员不应持有读权限，或者需要长时间无人值守的批量搬运，可以改用**托管搬运组件**：数据人员只提交任务，由 Azure 上的 worker 以服务身份运行 rclone 完成搬运（见 [self-service.md](self-service.md) 第 6 节）。

## 2. 前置条件

| 项 | 说明 |
|---|---|
| 工具 | `rclone` ≥ 1.65、Azure CLI（`az`）。两者都是单文件或包管理器安装 |
| 资格 | 已在 data-team 组中（`xl roles` 能看到 `source-reader  eligible = yes`）|
| 网络 | 运行 rclone 的环境，其**公网出口 IP** 已由 IT 加入源存储的网络安全边界白名单（见第 6 节）|

## 3. 一次性配置

```bash
# 1) 用自己的账号登录 Azure（容器里没有浏览器，用设备码）
az login --use-device-code

# 2) 生成 rclone 远端配置（也可以手写，见下方）
xl rclone-config >> ~/.config/rclone/rclone.conf
```

生成的配置不包含任何密钥：

```ini
[overseas]
type = azureblob
account = <源存储账号名>
use_az = true          # 通过 az 取当前登录用户的 Entra 令牌
```

场景 B 还需要一个目的地远端，按国内云厂商的文档配置 S3 兼容存储即可，例如：

```ini
[domestic]
type = s3
provider = <厂商对应的 provider>      # rclone 内置了主流国内云的 provider
endpoint = <对象存储 endpoint>
access_key_id = ...                    # 建议使用国内云的临时凭证 / 实例角色
secret_access_key = ...
```

## 4. 日常流程

```bash
# ① 申请（工单号是台账主键，同一件事用同一个工单号）
xl request source-reader -t DT-2026-0123 -r "Q4 数据分析：拉取 batch-001～010" --hours 4
#    → IT 运维在 PIM 中审批

# ② 操作
#   场景 A：拉到本地
rclone copy  overseas:datasets/batch-001 /data/batch-001 --checksum --transfers 16
rclone check overseas:datasets/batch-001 /data/batch-001          # 逐对象比对大小 + MD5

#   场景 B：搬到国内对象存储
rclone copy  overseas:datasets/batch-001 domestic:bucket/batch-001 --checksum --transfers 32
rclone check overseas:datasets/batch-001 domestic:bucket/batch-001

# ③ 用完立即交还（注意：激活后至少 5 分钟才能交还，这是 PIM 的规则）
xl revoke source-reader -t DT-2026-0123
```

交还或到期后，rclone 的读取会被拒绝（`AuthorizationPermissionMismatch`），通常在 1–3 分钟内生效（数据平面 RBAC 的传播时间）。

## 5. 台账里会留下什么

```
request  access.requested   申请人、理由、时长
approve  access.approved    审批人、审批意见
approve  access.activated   授权窗口起止
operate  data.accessed      窗口内实际读了什么：操作数、对象数、字节数、对象样本、来源 IP
revoke   access.revoked / access.expired
```

`data.accessed` 来自 Azure 存储服务端的访问日志，属于**观察到的**事实，不依赖操作者自述。它能回答“谁、在什么时间、从哪个 IP、读了哪些数据”。数据最终写到了国内哪个桶，Azure 侧观察不到；如果需要，可以把 `rclone check` 的结果作为附件归档到工单。

## 6. 网络：公网传输时的安全要求

推荐的最佳实践是**源存储保持私有**（Private Endpoint + 合规跨境专线）。如果当前阶段选择走公网，请不要直接打开存储防火墙，而是使用 **Network Security Perimeter（NSP）**：

| 控制 | 设置 |
|---|---|
| 访问入口 | `publicNetworkAccess = SecuredByPerimeter`，由 NSP 统一治理 |
| 来源 | 只允许白名单中的国内出口 IP（`perimeter_allowed_ips`），其他 IP 在网络层直接拒绝（`not authorized by network security perimeter`）|
| 身份 | 只认 Entra ID（共享密钥禁用），且必须有 PIM 激活的读权限 |
| 传输 | TLS 1.2+ |
| 审计 | NSP 访问日志（允许/拒绝）+ 存储访问日志，都进入 Log Analytics |

国内云上的出口 IP 通常是 NAT 网关的 EIP，请向平台管理员确认，并保持固定。

## 7. 大批量搬运的建议

- 数百 TB 级别建议在国内云上专门开一台**带宽充足的搬运机**（或多台按目录分工），不要在开发机 / 开发容器里做
- 并发：`--transfers 32 --checkers 32` 起步，根据出口带宽调整；对象很大时可以加 `--azureblob-chunk-size`
- 搬运时间可能超过单次授权时长：
  - 推荐由 IT 为数据团队单独配置一个**批量搬运角色**（例如最长 72 小时，仍需审批）
  - 兜底：授权到期后重新申请，`rclone copy` 重新执行时会自动跳过已传完的对象
- 每批搬完执行 `rclone check`，把输出与工单号一起归档

## 8. 常见问题

| 现象 | 处理 |
|---|---|
| `AuthorizationPermissionMismatch` | 没有激活的 `source-reader`，或刚激活/刚交还，RBAC 正在传播（等 1–3 分钟）|
| `not authorized by network security perimeter` | 当前出口 IP 不在白名单中，联系 IT |
| `ActiveDurationTooShort`（交还时）| PIM 规定激活后至少 5 分钟才能交还 |
| `az` 报 `TokenIssuedBeforeRevocationTimestamp` / `InteractionRequired` | 账号会话被撤销（例如改了密码），重新 `az login --use-device-code` |
| 申请时报 `RoleAssignmentDoesNotExist` | 刚被加入 data-team 组，PIM 还没同步到你，见 runbook |
