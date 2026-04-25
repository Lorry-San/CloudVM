# CloudVM 魔方财务服务器模块

这个目录是给魔方财务/ZJMF 使用的服务器模块，接口对接 `docs/platform-api.md` 描述的 CloudVM Platform API。

## 安装

1. 将 `cloudvmserver` 整个目录复制到魔方财务的服务器模块目录。
2. 在魔方后台添加服务器：
   - IP：CloudVM 面板域名或 IP
   - 端口：反代端口，例如 `443` 或 `8080`
   - 访问密钥/accesshash：`PLATFORM_API_TOKEN`
3. 创建商品并选择 `cloudvmserver` 模块。
4. 在“自动开通”里只配置网络模式、存储、流量等固定策略。
5. 在“产品配置”里添加 CPU、内存、硬盘、系统镜像等可配置项。

平台 API 地址和 Token 不在商品里配置，统一使用服务器分组里的 IP/端口/accesshash。

## 产品配置项

建议在魔方商品的“产品配置”里创建这些配置项，配置项名称或键名尽量使用英文：

| 用途 | 推荐键名 | 示例值 |
| --- | --- | --- |
| 系统镜像 | `image` | `debian-12` |
| CPU 核心数 | `cpus` | `2` |
| 内存 MB | `memory_mb` | `2048` |
| 硬盘 GB | `disk_gb` | `40` |
| 端口速率 Mbps | `rate_mbps` | `100` |

插件也兼容部分别名：`cpu/cores`、`memory/ram`、`disk`、`os/system`、`bandwidth/带宽/端口速率`。但为了避免魔方传参差异，推荐使用上表键名。

“自动开通”里保留的 `流量重置规则`：

- `开机日期`：按开通当天作为每月重置日，日期超过 28 时按 28 处理
- `每月1号`：固定每月 1 号重置

## 已实现功能

- 测试连接：`GET /api/v1/status`
- Token 校验：`GET /api/v1/auth/check`
- 开通 VM：`POST /api/v1/vms`
- 删除 VM：`DELETE /api/v1/vms/{vmid}`
- 开机/恢复：`POST /api/v1/vms/{vmid}/resume`
- 关机/暂停：`POST /api/v1/vms/{vmid}/pause`
- 重启：先 pause 后 resume
- 状态查询：`GET /api/v1/status?vmid=...`
- 实例详情：`GET /api/v1/vms/{vmid}`
- IP 绑定：`GET /api/v1/vms/{vmid}/ips`
- 最近任务：`GET /api/v1/vms/{vmid}/tasks`
- 重装系统：`POST /api/v1/vms/{vmid}/reinstall`
- 套餐变更：`PUT /api/v1/vms/{vmid}/config`
- 控制台凭据读取/更新：`GET|PUT /api/v1/vms/{vmid}/credentials`
- VNC 控制台：`POST /api/v1/consoles/token`
- 流量查看/重置：`GET|PUT /api/v1/vms/{vmid}/traffic`
- 断开/恢复网络：`POST /api/v1/vms/{vmid}/network/disconnect|connect`
- 到期动作：`POST /api/v1/vms/{vmid}/expiration`

## 新版 API 字段

插件已同步新版 `VmCreateRequest` 的主要字段：`template_vmid`、`ssh_keys`、`searchdomain`、`boot_order`、`expires_at`、`traffic_reset_hour`、`traffic_reset_timezone` 等。重装接口也补充了 `slot`、`free_old`、`dry_run`。

## VMID 存储

开通成功后，插件会把 `vmid`、网络模式、IP、NAT SSH 端口等写入魔方主机记录的 `assignedips` 字段，后续操作优先从这里读取 VMID。
