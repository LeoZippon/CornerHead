# 部署文档

本文档维护两套状态明确的部署面：

1. **研究控制台**：仓库已经实现，包含计算主机服务、前端反向代理、SSH 隧道、访问控制和保活。
2. **QMT 实盘**：客户端内文件桥已实现（`ops/qmt/qmt_client_bridge.py`：实时导出恒开启 + 配置闸门下的显式订单执行，默认 dry-run），等待交易日实测验收；订单生成（决策侧信号→显式订单 payload）仍未实现，不具备端到端自动实盘能力。

实盘目标使用 QMT 客户端内置 Python 策略 API，不采用 xtquant/miniQMT 外接方案。QMT 在目标合同实现并通过上线门槛前，只能保持 standby、只读检查和人工 dry-run 准备。

**相关边界**

- 数据下载、源单位和 raw 审计见 [数据文档](data_documentation.md)。
- PIT 窗口、Sandbox、工具和回测见 [Environment 设计](environment_design.md)。
- Agent 工作合同和策略产物格式见 [Agent 设计](agent_design.md)。
- 研究侧 Pipeline、冻结和 held-out 见 [Pipeline 设计](pipeline_design.md)。
- 参数默认值速查见 [参数参考](parameters_reference.md)。

**职责边界**

部署层负责研究控制台的运行拓扑与运维边界，并维护 QMT 目标链路的本机决策侧、文件桥、客户端执行器、风控和对账合同。部署层不负责定义 raw 数据、PIT 回放、Agent 策略或实验编排，也不把尚未实现并验收的 QMT 目标合同描述为现有能力。

**术语说明**

| 中文名 | 代码/英文名 | 含义 |
|---|---|---|
| 按时点可见 | PIT | 只使用决策时点已经可见的数据，避免未来信息进入订单 |
| 滚动前推 | WFO | Walk-Forward 滚动策略探索与冻结测试流程；只有冻结后的结果可以进入实盘候选 |
| 大模型影子模式 | `LLM shadow` | 大模型只做影子审计或提出建议，不直接修改订单 |
| 实验账本 | `ledger` | 研究侧实验账本，不等同于券商成交和持仓记录 |
| 订单载荷 | `payload` | 本机生成并传输到远端收件目录的订单 JSON |
| 客户端执行器 | `executor` | QMT 客户端内常驻的 Python 策略脚本，负责轮询订单、调用内置 API 下单或撤单，并回写确认、成交和状态 |
| 文件桥 | `QMTBroker` | 本机侧交易协议适配器，负责把下单或撤单请求写成订单文件，并把远端回写快照转换为查询结果 |
| 试运行 | `dry-run` | 只检查解析、风控和预算，不向券商发送真实委托 |
| 远端状态 | `state` | 远端记录的策略持仓、待处理委托和已处理订单状态 |

**导航**

- [1. 研究控制台部署](#1-研究控制台部署)
- [2. QMT 状态与目标架构](#2-qmt-状态与目标架构)
- [3. QMT 运行流程与上线门槛](#3-qmt-运行流程与上线门槛)
- [4. QMT 配置、协议与执行合同](#4-qmt-配置协议与执行合同)
- [5. QMT 故障处理](#5-qmt-故障处理)
- [6. 实时导出、同步与飞书通知](#6-实时导出同步与飞书通知)

## 1. 研究控制台部署

本章集中说明研究控制台的网络拓扑、访问控制、部署、保活和故障排查。

### 1.1 三机网络架构与通信设计

本节定义计算主机、前端服务器和研究者终端之间的网络角色、允许流向和认证边界。

**设备与角色**

| 设备 | 角色 | 网络位置 | 对外暴露 |
|---|---|---|---|
| 计算主机（本机 Linux） | 计算枢纽：实验管线、数据、Docker、控制台 API | 教育网（CERNET），无公网入站，只能主动出站 | 无 |
| 前端服务器 `121.41.5.179` | 前端控制节点：静态 SPA + API 反代（Debian 12，1.6G 内存） | 公网 | 仅 sshd（22） |
| 研究者 MacBook | 客户端接入终端 | 任意 | — |

研究控制台前端 `121.41.5.179` 与 QMT Windows 节点 `39.105.46.212` 是两台独立服务器；控制台隧道不承担 QMT 接入或交易执行。

**通信链路**

```text
MacBook ──ssh -N -L 8888:127.0.0.1:8080──▶ 前端服务器 sshd
                                             │ nginx 127.0.0.1:8080（仅回环）
                                             │   ├─ /            静态 SPA（/opt/cornerhead/static）
                                             │   └─ /api/  ─▶ 127.0.0.1:38889（仅回环）
                                             ▲
计算主机 ──autossh -N -R 127.0.0.1:38889:.runtime/webui/console.sock──┘（主动出站，教育网约束下唯一可行方向）
          console API：uvicorn 绑定 Unix socket .runtime/webui/console.sock（0700 目录，仅 lzp 可达）；实验 worker 为独立分离进程
```

控制台服务依赖（FastAPI/Uvicorn）经可选依赖组安装：`pip install -e '.[webui]'`（裸 `pip install -e .` 不含 Web 服务依赖）。

**设计原则**

- 公网面只有前端 sshd；nginx、控制台 API、反向隧道监听端口全部绑定回环。
- 教育网只能出站 → 计算主机主动向前端建立反向隧道，前端永不入站连接计算主机。
- 访问控制依赖获授权的 SSH 私钥，没有 Web 层账号体系。授权绑定的是可复制的私钥，不是不可迁移的物理终端；root 是最终信任锚。
- 静态资产部署在前端（页面秒开、计算主机重启期间页面仍可加载并显示“计算主机离线”）；隧道中只走 JSON API 与 SSE。

### 1.2 前端部署与访问控制

本节定义研究控制台的 SSH 白名单、Unix socket、反向代理、隧道和公网访问控制。

**SSH 密钥白名单**

- sshd 只读取 root 管理的中央密钥目录，普通用户的个人授权文件不生效。
- 非 root 账户不能自行加入新密钥；root 可以修改中央目录和 sshd 配置，因此是最终信任锚。
- 只允许列明的账户并强制公钥认证。云厂商若依赖个人授权文件注入临时密钥，其网页终端可能失效；本地救援控制台不受影响。

**专用隧道用户 `cornerhead`**（`/usr/sbin/nologin`，无 shell/exec 能力），中央 key 文件内按 key 精确限权：

| 终端 | key 选项 | 能力 |
|---|---|---|
| 计算主机（`~/.ssh/id_ed25519.pub`） | `restrict,port-forwarding,permitlisten="127.0.0.1:38889"` | 只能反向监听 38889（暴露控制台 API） |
| 授权研究者设备（中央白名单内逐 key 管理） | `restrict,port-forwarding,permitopen="127.0.0.1:8080"` | 只能本地转发到 8080（访问控制台） |

**计算主机侧本地访问控制**

- 控制台 API 只绑定 Unix socket，不监听 TCP；socket 目录权限为 0700，由服务账户持有。
- 共享机上的其他普通用户由内核文件权限隔离。反向隧道以同一服务账户访问 socket，并只在前端回环暴露。
- 本地诊断应直接使用 Unix socket；不得再桥接到共享机 TCP，否则会绕过这层用户隔离。

**前端侧本地访问控制**

- 防火墙按进程用户限制两个回环端口：控制台入口只允许运维、研究者和隧道账户，裸 API 只允许运维与反向代理账户。
- 规则使用数字 uid，避免启动期名称解析失败。
- 当前 provisioning 会刷新整套防火墙规则，因此前端必须是专用服务器。若机器已有其他防火墙或租户规则，执行前必须备份并合并，不能直接覆盖。

**nginx**

- 只在回环地址的 8080 端口监听，提供静态 SPA，并把 API 和 SSE 转发到反向隧道端口。
- SSE 关闭缓冲并使用长读超时；计算主机离线时返回明确的 503 JSON，而不是默认错误页。
- 删除发行版默认站点并不保证 80/443 没有其他监听。部署后必须检查所有启用站点、完整 nginx 配置和实际监听端口。

**部署命令**（均在计算主机上执行）：

- 首次/幂等 provisioning：`bash ops/webui/frontend_setup.sh`（创建用户、写 authorized_keys、装 nginx 配置）。
- 完整控制台更新：完成代码修改后运行 `ops/webui/webui_stack.sh deploy`（先同步静态 SPA，再只回收控制台 API；除 Unix-socket 健康外，还要求新进程启动时加载的 source fingerprint 与当前仓库完全一致，否则部署失败；保留反向隧道和独立实验 worker）。
- 仅 UI 资产更新时可用 `ops/webui/webui_stack.sh sync`（tar-over-ssh，归一化属主与权限）。Python/API/schema 变更不能只运行 `sync`，必须使用 `deploy` 让长驻进程重新加载代码。

### 1.3 保活、启动流程与故障排查

本节说明控制台相关服务的保活方式、启动顺序、状态检查和故障排查入口。

**计算主机侧**（唯一入口 `ops/webui/webui_stack.sh`）：

| 命令 | 作用 |
|---|---|
| `start` / `stop` / `status` | 启停控制台 API（uvicorn，Unix socket `.runtime/webui/console.sock`）与 autossh 反向隧道；start 在返回前等待本地 API 健康，status 同时报告进程代码是否等于当前仓库以及前端端到端健康；`.runtime/webui` 与 `logs/webui` 均为 `0700` |
| `ensure` | 缺什么补什么（keepalive 目标，幂等） |
| `sync` | 推送静态 SPA 到前端 |
| `deploy` | 推送静态 SPA，优雅重启控制台 API并校验 source fingerprint；保持隧道和实验 worker 不动 |
| `install-cron` | 安装托管 crontab 块：`*/2` 分钟 `ensure` + `@reboot` |

保活分三层：

- autossh 通过 keepalive 和转发失败检测自愈网络断连。
- cron 定期确保控制台和隧道进程存在。
- 前端 nginx 与 sshd 由 systemd 管理；sshd 最多约 90 秒回收异常断开的旧监听。

实验 worker 是分离进程，因此控制台或隧道重启不影响正在运行的实验。

稳定性细节：

- 手动操作和 cron 共用进程锁，避免重复拉起。
- 进程存活同时校验 pid 和命令行，避免 pid 复用误判。
- `code_version` 在干净工作树中是短 commit id；存在已跟踪或未跟踪改动时追加内容 hash，因此服务启动后继续修改代码也会在 30 秒内被 health、`status` 和页面「控制台代码过期」提示发现，不再被未变化的 Git HEAD 掩盖。部署成功只证明该返回时刻代码一致；之后再次修改仍须重跑 `deploy`。
- 受管控制台以 `-B`、`PYTHONDONTWRITEBYTECODE=1` 和仓库外空 cache prefix 启动；它及其分离 worker 不读取或写入仓库内 `.pyc`，运行行为不依赖残留的 timestamp cache。手工分析仍应遵守同一原则，尤其不得直接导入冻结策略目录。
- 日常检查保持静默，只记录实际拉起、轮转和失败。
- 控制台与保活日志超过 10 MB 后轮转，保留一代。
- 控制台关闭逐请求 access log；启动、告警、异常和 traceback 仍写入 `logs/webui/console.log`，前端健康轮询不再淹没有效诊断。

时间显示约定：后端一律存 UTC ISO 时间戳；WebUI 前端统一按 UTC+8（Asia/Shanghai）渲染显示。

**MacBook 侧**（一次性把下面片段加入 `~/.ssh/config`，之后 `ssh -N cornerhead` 即接通，浏览器打开 <http://localhost:8888>）：

```text
Host cornerhead
    HostName 121.41.5.179
    User cornerhead
    IdentityFile ~/.ssh/id_ed25519   # 私钥须对应前端已授权公钥
    LocalForward 8888 127.0.0.1:8080
    ServerAliveInterval 30
    ExitOnForwardFailure yes
```

**故障排查**

| 症状 | 排查 |
|---|---|
| 浏览器打不开 localhost:8888 | Mac 侧隧道未建立：重跑 `ssh -N cornerhead`；确认私钥对应已授权公钥 |
| 页面能开、API 返回 503“计算主机离线” | 计算主机→前端隧道断：在计算主机 `ops/webui/webui_stack.sh status`，通常等 2 分钟 keepalive 自愈 |
| status 显示 console DOWN | 看 `logs/webui/console.log`；`webui_stack.sh ensure` 拉起 |
| status 或页面显示控制台代码过期 | 当前进程早于仓库 source fingerprint；先完成正在进行的编辑/提交，再运行 `ops/webui/webui_stack.sh deploy`，确认 `console code: current` |
| UI 文案/样式是旧版本 | 忘记同步静态资产：`webui_stack.sh sync`；资产响应禁用持久缓存，普通刷新即可 |
| 新参数/API 字段不可见 | 长驻 API 仍加载旧 Python schema：`webui_stack.sh deploy`；随后用 `status` 验证端到端健康 |

## 2. QMT 状态与目标架构

本章区分当前已具备的 QMT 能力和尚待实现、验证的目标执行架构。

### 2.1 当前状态

本节说明研究控制台、远端 QMT 客户端和自动实盘链路当前已经具备或仍然缺失的能力。

- 远端 QMT Windows 节点 `39.105.46.212` 和国金全功能 QMT 客户端已部署，可作为未来交易执行端（历史 miniQMT 组件仅作已部署遗留，不再是目标路径）；仓库不保存该节点的登录用户、私钥或口令。
- 研究侧的数据、PIT、WFO/held-out 和审计链路已实现；尚无冻结可交易模型。
- 当前仓库没有实盘调度入口、订单生成器或本地 tick 执行器，回放是唯一可运行的策略执行路径；客户端内文件桥（`ops/qmt/qmt_client_bridge.py`）已部署但订单执行默认关闭、未经交易日验收。历史 xtquant/miniQMT 方案已废弃，`C:\xquant` 下相关文件仅作遗留归档。
- 下文的逐 tick 环路和客户端执行器均为目标合同，不得据此认定实盘链路可用。
- 任何 QMT 操作默认只读或 dry-run。真实委托必须等到模型、策略、订单合约、风控和对账流程全部冻结后，由人工构造满足三重闸门（配置 `execution.enabled` ∧ payload `execute` ∧ `confirm==payload_id`）的 payload 才会执行。

**SSH 运维接入**

截至 2026-07-12，本机已确认 `39.105.46.212:22` 网络可达，服务端为 `OpenSSH_for_Windows_9.5`，且只接受公钥认证；本机现有密钥尚未获该节点授权，因此“端口可达”不代表已经具备 SSH 登录能力。当前观测到的 ED25519 主机指纹为：

```text
SHA256:tbYZOSygvTsHNSnqguAANXyVSxEQQSGfUrWHFaq5u24
```

首次接入按以下顺序完成：

1. 复用本机连接研究控制台前端所用的 `~/.ssh/id_ed25519`；私钥仍只留在本机，不写入仓库或传给服务器。先核对指纹并复制完整公钥行：

   ```bash
   ssh-keygen -lf ~/.ssh/id_ed25519.pub
   cat ~/.ssh/id_ed25519.pub
   ```

   当前公钥指纹应为 `SHA256:PTtwQxes6zm4ynrGFyc7lIEP1BKfSkVO3cfjRqrmNBE`。

2. 通过阿里云控制台或 RDP 登录 QMT Windows，先确认实际 SSH 账户、sshd 状态和服务器本地记录的主机指纹：

   ```powershell
   whoami
   Get-Service sshd
   ssh-keygen -lf C:\ProgramData\ssh\ssh_host_ed25519_key.pub
   ```

   控制台读取的指纹必须与本机将要信任的指纹一致；不一致时停止连接并排查服务器重装、密钥轮换或中间人风险。

3. 把第 1 步输出的完整公钥行加入该 Windows 账户。若账户属于本机 Administrators 组，使用管理员 PowerShell 写入公共管理员 key 文件并收紧 ACL：

   ```powershell
   $PublicKey = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINHmBvqjK7X8pj4xSMXL3/Gyy2XsIX3uEewQB37rLdNS lzp2002@icloud.com'
   $AuthorizedKeys = "$env:ProgramData\ssh\administrators_authorized_keys"
   New-Item -ItemType File -Force $AuthorizedKeys | Out-Null
   if (-not (Select-String -Path $AuthorizedKeys -SimpleMatch $PublicKey -Quiet)) {
       Add-Content -Encoding ascii -Path $AuthorizedKeys -Value $PublicKey
   }
   icacls.exe $AuthorizedKeys /inheritance:r /grant "*S-1-5-32-544:F" /grant "*S-1-5-18:F"
   ```

   若使用非管理员账户，则以该账户登录 Windows，把公钥写入其个人文件，而不是管理员公共文件：

   ```powershell
   $PublicKey = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINHmBvqjK7X8pj4xSMXL3/Gyy2XsIX3uEewQB37rLdNS lzp2002@icloud.com'
   $SshDir = Join-Path $HOME '.ssh'
   $AuthorizedKeys = Join-Path $SshDir 'authorized_keys'
   New-Item -ItemType Directory -Force $SshDir | Out-Null
   New-Item -ItemType File -Force $AuthorizedKeys | Out-Null
   if (-not (Select-String -Path $AuthorizedKeys -SimpleMatch $PublicKey -Quiet)) {
       Add-Content -Encoding ascii -Path $AuthorizedKeys -Value $PublicKey
   }
   ```

4. 在本机从 Windows 控制台复核指纹后固定 host key，并配置不经过其他服务器的直连别名：

   ```bash
   ssh-keyscan -T 5 -t ed25519 39.105.46.212 > /tmp/qmt-node.hostkey
   ssh-keygen -lf /tmp/qmt-node.hostkey
   cat /tmp/qmt-node.hostkey >> ~/.ssh/known_hosts
   rm /tmp/qmt-node.hostkey
   ```

   ```sshconfig
   Host qmt-node
       HostName 39.105.46.212
       User <第 2 步核验的 Windows SSH 用户>
       IdentityFile ~/.ssh/id_ed25519
       IdentitiesOnly yes
       StrictHostKeyChecking yes
       ServerAliveInterval 30
       ServerAliveCountMax 3
   ```

5. 只读验收登录身份和 QMT 路径；两项都正确后，才允许使用后文的状态读取或文件传输命令：

   ```bash
   ssh -o BatchMode=yes qmt-node "hostname && whoami"
   ssh -o BatchMode=yes qmt-node "powershell -NoProfile -Command \"Test-Path 'C:\\国金证券QMT交易端'\""
   ```

出现 `Permission denied (publickey)` 时，依次核对 SSH 用户、应使用的 authorized-keys 文件、文件 ACL 和公钥是否完整；出现 host-key mismatch 时不得跳过校验。Windows OpenSSH 的 key 路径和 ACL 规则以微软官方的 [Key-Based Authentication](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_keymanagement) 与 [Server Configuration](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh-server-configuration) 为准。

### 2.2 目标架构

本节定义本机决策侧、远端 QMT 执行侧、文件通信和实盘状态真相源的目标架构。

- 本机 Linux：负责 TuShare/本地 raw 数据更新、审计、PIT snapshot 构造、模型推理、信号审计、订单 payload 生成。
- 远端 Windows：运行国金全功能 QMT 客户端；下述客户端内文件单执行架构负责账户/持仓/成交查询（`get_trade_detail_data`）、订单执行（`passorder`/`cancel`）、策略 state、pending 委托和 payload 归档。
- 通信：本机直接向 QMT 节点通过 `scp`/SMB 把 JSON payload 以临时名传入远端 inbox，同目录校验并原子改名后才对轮询器可见；`ssh` 只用于文件搬运和状态快照读取等运维辅助，不经过研究控制台前端。
- 状态：远端策略 state 是实盘对账的权威来源；本机实验 ledger 只能作为研究和审计记录，不能替代 broker 成交状态。

**统一逐 tick 实盘环路**

- 本地执行器按 Asia/Shanghai 真实时钟推进，与回放共享 `main(ctx)` 和策略侧 Broker 原语。
- 普通账户和信用账户保持独立；每个交易动作必须明确账户语义、证券、数量、价格约束和幂等身份。
- 决策侧运行在项目自己的现代 Python 环境，通过文件桥提交订单或撤单意图；客户端内执行器只使用券商内置 API 落地并回写状态。
- 竞价、盘中和盘外时点沿用 Environment 的策略时钟合同。盘外只准备计划，不自动顺延或提交交易所委托。
- 回写的账户、委托、成交和持仓是实盘真相；本地提交成功不等于券商成交。
- 当前只存在这套接口方向，尚无可替换回放 Broker 的实盘适配器实现。

**客户端内文件单执行架构**

- 目标执行链使用全功能 QMT 客户端内置 Python 策略 API，不采用 xtquant/miniQMT 外接。
- 零售客户端没有可直接复用的文件扫单模块，需要自行实现客户端内轮询器。
- 官方文档只证明接口存在；文件权限、定时器、回调和重启行为仍须实测。
- 仓库中的 `ops/qmt/qmt_client_bridge.py` 是 Python 3.6/标准库-only 的客户端内文件桥：`ContextInfo.run_time()` 5 秒回调内完成实时导出（快照 + 增量 orders/deals JSONL）与 inbox 订单执行（三重闸门：配置 enabled ∧ payload execute ∧ confirm==payload_id，全假即 dry-run；幂等 remark 对照柜台当日委托）；标准配置、payload schema 与交易日测试步骤见 [`ops/qmt/README.md`](../ops/qmt/README.md)。

**分工**

| 侧 | 运行环境 | 职责 |
|---|---|---|
| 决策侧 | 本地 Linux，自有 Python | 跑冻结策略与 `main(ctx)` 环路；生成带完整证据和幂等身份的订单文件，经操作系统通道送入远端 inbox |
| 执行侧 | QMT 客户端内常驻策略脚本，仅标准库 | 轮询本地 inbox、校验并调用券商 API、回写状态；脚本不自建 HTTP 或其他外部网络通道，QMT 客户端自身的券商连接不在此禁令内 |

**客户端内执行器合同**

- **调度**：使用短时、非阻塞定时回调轮询 inbox；禁止常驻阻塞循环、线程和网络服务，避免冻结客户端共享运行线程。
- **校验**：先验证协议版本、账户、幂等身份、预算、停牌、价格限制、T+1、手数和当日信用资格，再调用券商接口。
- **幂等**：每笔订单带稳定客户身份；提交前同时检查已处理记录、当日委托和成交，重复到达不得重复下单。
- **账户**：交易动作决定普通或信用账户；账户间资金划转不通过策略交易 API 自动执行，只生成待人工处理的工单。
- **异步状态**：提交、委托、成交和拒绝分别记录。成交只以成交回调和券商成交查询为准，不能把返回的委托号当成交。
- **恢复**：启动时先读持久状态，再用券商当日账户、委托、成交和持仓覆盖；回调重推必须去重。
- **时间**：Windows 使用 Asia/Shanghai 和 NTP；所有下单窗口按券商时间再次门控。
- **授权**：模拟模式和订单内执行标志构成独立 dry-run 闸门；真实模式还需人工确认字段。

**需在真实国金客户端上验证的开放问题**

1. 客户端内策略对 inbox、state 和 archive 的实际读写权限，以及临时文件原子发布能力。
2. `run_time`/回调在实盘模式的稳定性与断线重连重推行为。
3. 隔夜重启自动恢复链路（自启 + 自动登录 + 自动执行）。
4. 各 op 在国金柜台的 opType/prType 实测映射（尤其信用 27–34）。
5. 内置 Python 3.6.8 与三方库白名单约束（执行器目标：仅标准库）。
6. GBK 源文件环境下 UTF-8 JSON 数据文件读写。
7. 上线前核实程序化交易报备和申报速率阈值，并以实测最坏频率验收。
8. 盘后固定价格申报（2026-07-06 起全 A 股开通，回测已建模为 15:05 tick 按收盘价即时成交）：客户端内 `passorder` 是否支持收盘定价申报及对应 opType/prType 未核验——核验前实盘执行器应忽略/拒绝盘后定价时段的策略订单。

## 3. QMT 运行流程与上线门槛

本章说明当前可执行流程、目标链路上线后的运行顺序，以及进入 paper 或 live 阶段前的验收门槛。

### 3.1 当前日常流程

本节说明仓库当前可执行的研究流程和仅供遗留排查的只读操作。

**仓库当前支持的流程**

1. 增量下载并审计 raw 数据。
2. 构造 PIT snapshot，运行 development、held-out 和影子分析。
3. 不生成订单文件，不启动生产文件桥或客户端内执行器，不提交真实委托；只读桥样例仅允许人工在 QMT 模拟模式下做连通性验收。

仓库外遗留 standby 环境存在时，可以额外做只读检查：

- 确认 QMT 已登录、账户正确且基本资产可读。
- 核对遗留工具的在途委托、策略状态和 inbox 没有无法解释的内容。
- 人工 payload 只可在确认遗留工具存在、轮询器停止或运行于模拟模式时做 dry-run；这不是当前仓库支持的执行链。

**遗留只读检查**

以下命令依赖仓库外已部署的历史工具，不属于当前仓库支持的实盘实现，只能在确认远端文件存在时用于 standby 健康检查：

```bash
ssh qmt-node "C:\\xquant\\Python38\\python.exe C:\\xquant\\qmt_executor.py status"
ssh qmt-node "C:\\xquant\\Python38\\python.exe C:\\xquant\\qmt_executor.py reconcile"
```

`reconcile` 不下单，只用当日成交同步远端策略状态和在途委托。这两条命令属于遗留只读工具，仅用于 standby 健康检查；目标执行器上线后，应直接读取其回写状态。

### 3.2 QMT 上线后流程

本节定义 QMT 链路完成实现和验收后的日常顺序、下单重校验和状态持久化要求。

**上线后日常顺序**

1. 收盘后或指定决策时点构造 PIT snapshot。
   - 默认日频策略只允许使用当时已可见数据。
   - 09:25 盘前决策必须有独立的数据合同，不能使用当日 `daily` / `daily_basic`。
2. 运行冻结模型或冻结规则。
   - development 阶段结果不得直接下单。
   - held-out 完成并审计后，由研究者作出明确 promotion 决定，再记录模型、配置、数据合同和账本身份。
3. 生成订单协议文件。
   - 必须包含唯一身份、决策时间、冻结策略与数据证据、订单列表、风险标签和执行授权。
4. 上传远端并先 dry-run。
   - 检查预算、股数、涨跌停/停牌、可用持仓、重复 payload、账户 ID。
5. 人工确认后才允许真实执行。
   - 实盘 payload 必须满足三重闸门：桥配置 `execution.enabled`、payload `execute:true` 与 `confirm==payload_id`（见 §4.3）。
   - 下单后必须运行对账，不能把委托号当成成交。

**实盘下单前重校验**

- 当日 `margin_secs` 近似资格，即当前研究口径下该标的当日是否可担保品买入、可融资买入、可融券卖出。
- 信用账户约束：保证金可用余额、授信额度、融券卖出限价（申报价不低于最新成交价）。
- 全部交易约束：可用现金、T+1 可卖余额、涨跌停价限、停牌、最小交易单位（手）。

回放中的同日动态资格校验是这一步的仿真等价物：Broker 使用订单到达日可见的融资融券标的集合，对担保品买入、融资买入和融券卖出设闸。实盘不得用研究决策时点的旧集合替代成交时刻的当日校验。

**实盘状态持久化**

回测中 `ctx.state_dir` 是每次 run 的临时 scratch（每次回测重置、不跨 run 保留），不能作为实盘的权威状态。实盘部署必须把在途委托、下单计划和持仓跟踪持久化在两处可恢复来源上：

- QMT 自身查询：`get_trade_detail_data` 的 ORDER / POSITION / DEAL 记录，作为成交与持仓的权威对账来源（客户端内执行器周期性快照进 `state/`）。
- 目标执行器的持久状态：保存已处理文件、在途委托、策略归属和归档索引，用于跨进程恢复；精确目录与 schema 在实现时冻结，不能直接沿用遗留布局假定。

任何实盘状态判断都不得依赖回测 `ctx.state_dir`。

### 3.3 上线门槛

本节列出任何策略进入 paper 或 live 阶段前必须满足的模型、数据、协议、风控和审计条件。

- 已有冻结的 strategy config、model ID、prompt/model provider 版本和数据合同。
- held-out 或 quasi-forward 评估结果已审计，并明确允许进入 paper/live 阶段。
- 任何获准影响交易的组件都必须经过单独审计；当前 LLM shadow 不能影响交易。
- 本机订单生成器和远端执行器的 payload schema 已冻结，并有单元测试或 dry-run 样例。
- 远端 `inbox`、`pending_orders`、`strategy_positions` 状态干净或可解释。
- 实盘规模、单票上限、行业/组合约束、跌停/停牌处理、T+1 约束和最大回撤停机规则已写入配置。
- 手工仓位和策略仓位边界明确，卖出逻辑不会误卖非策略仓位。
- 已完成小额或模拟 dry-run 全链路：生成协议文件、原子传输、远端解析、预算与拒单检查、券商查询对账和重启恢复。

## 4. QMT 配置、协议与执行合同

本章汇总远端运行条件、订单协议、文件传输、执行授权和成交对账合同。

### 4.1 远端布局与目标配置

本节记录遗留 standby 布局、QMT 路径依据、目标执行器配置和本金口径。

**遗留 standby 布局**

以下目录属于仓库外已部署的遗留环境，不是目标执行器的已冻结布局：

```text
C:\xquant\
  Python38\
  qmt_executor.py
  inbox\
  outbox\
  logs\
  state\
  archive\
```

其中的独立 Python 和 `qmt_executor.py` 只服务遗留只读工具。遗留 inbox 中执行过、测试过或废弃的文件应归档，避免误执行。

**QMT 路径和官方参考**

**QMT 常见路径**

```text
C:\国金证券QMT交易端
C:\国金证券QMT交易端\userdata_mini
```

**官方参考**

- 客户端内 Python 策略 API 文档（本仓库副本）：[QMT Python API](../external_references/gjzq-da-qmt/qmt_python_api_doc.html)
- 迅投内置 Python 文档：http://docs.thinktrader.net/QMT/
- 迅投常见问题（单线程运行时、异步交易接口、查询缓存刷新）：https://dict.thinktrader.net/innerApi/question_answer.html

**目标执行器配置草案**

**远端 Python**

目标订单执行运行在 QMT 客户端内置 Python 3.6.8，仅使用标准库。独立 Python 3.8 和历史 xtquant 安装只属于遗留环境或文件运维辅助，不参与目标下单链；新机器无需为目标执行器安装。

以下环境变量只是目标实现草案，当前没有执行器消费：

```powershell
setx CQ_QMT_DATA_PATH "C:\国金证券QMT交易端\userdata_mini"
setx CQ_XQUANT_ROOT "C:\xquant"
setx CQ_STOCK_ACCOUNT_ID "<普通账户 id>"
setx CQ_CREDIT_ACCOUNT_ID "<信用账户 id>"
```

目标执行器应按交易动作在普通和信用账户间路由；两个账户都必须显式配置，缺失时拒绝启动。

**本金口径**

```powershell
setx CQ_MAX_PRINCIPAL "100000"
```

本金环境变量同样属于草案。目标实现若未显式设置上限，可以读取券商账户总资产作为本金口径，但上线前必须与回测资金口径一致并写入冻结配置。

### 4.2 订单协议（schema_version 2）

本节记录本机与 QMT 客户端之间订单 payload 的执行语义。协议已由 `ops/qmt/qmt_client_bridge.py` 实现并冻结；精确字段、唯一权威 payload 示例、严格类型校验（拒绝 NaN/Infinity、布尔必须为 JSON true/false、同包逐单 remark 幂等键必须互不相同）、逐单意图/终态日志与交易日测试流程见 [`ops/qmt/README.md`](../ops/qmt/README.md)，本文不再复制示例。

**必要语义**

- 全局唯一 `payload_id` 和逐单幂等 remark（`MQ:<payload_id>:<序号或自定义 remark>`）；重复到达不得重复下单，同包 remark 冲突在校验期整包拒绝。
- 每笔订单只携带 `code`/`side`/`volume`/`price`（可选 `remark`）；`op_type`、价格类型等柜台参数由客户端配置派生，payload 不携带。
- dry-run 与 live 由三重独立闸门控制：配置 `execution.enabled` ∧ payload `execute` ∧ `confirm == payload_id`。
- 提交、委托、成交和拒绝的分离回写（`execute_*.json` / `error_*.json` / 逐单 journal）。
- 决策时间与冻结策略/配置/数据/账本 hash 暂不进入 payload——属未实现的目标语义；接入实盘前由决策侧凭 `payload_id` 与研究账本线下对账。

**决策侧拆单要求**

- 已有持仓和在途委托必须先对账；任何增仓都要由决策侧明确表达并保持幂等，执行侧不得自行推断再平衡意图。
- 卖出只根据远端策略 state 和 broker 可用持仓生成，不卖出非本策略仓位。
- 再平衡必须在本机拆成明确的 BUY/SELL 订单，远端不负责理解研究语义。

### 4.3 Dry-run 与实盘执行

本节说明订单文件的传输、dry-run 检查、真实执行限制和成交对账流程。

**上传和执行命令**

**传输示例**

在原子发布能力完成实测前，以下命令只可在远端轮询器停止时用于传输测试文件：

```bash
scp order.json qmt-node:C:/xquant/inbox/.order.json.tmp
```

目标传输器必须在同目录完成临时文件校验和原子改名后，轮询器才能读取最终文件名；不能直接把网络传输写到最终名。

**三重闸门（已实现，见 §2.2）**：真实下单要求 ①桥配置 `execution.enabled` 为 JSON true；②payload `"execute": true`；③payload `"confirm"` 精确等于该 payload 的 `payload_id`。任一为假即 dry-run——执行器只校验、写 ack 与逐单日志，不调用交易接口。人工确认动作 = 生成并上传满足三闸的 payload，本身必须由人完成；QMT 客户端的运行/实盘模式只决定客户端行为，不构成网关闸门。

**成交对账**

不要把提交或委托号当成交。成交以成交回调与券商成交查询为准；查询读取本地缓存，提交后至少等待一个刷新周期再对账。目标执行器用券商查询和持久状态恢复策略状态；遗留 standby 工具才使用 `reconcile`。

## 5. QMT 故障处理

本章汇总 QMT 文件链路、客户端执行和远端环境的常见故障及处理方式。

**常见故障**

- 执行器未运行：检查 QMT 已登录、策略在模型交易列表且"终端启动后自动执行"勾选、`run_time` 定时器在实盘模式下已挂起。
- 多账户：普通/信用账户 id 分别用 `CQ_STOCK_ACCOUNT_ID` / `CQ_CREDIT_ACCOUNT_ID` 显式配置，禁止自动选择。
- 重复 payload：默认拒绝。人工对账后的重发协议尚未冻结，不承诺 repeat 开关。
- pending 未清：目标执行器查询券商委托、成交和持久状态；遗留 standby 环境可使用其 `reconcile` 命令。
- inbox 有旧文件：移动到 `C:\xquant\archive\...`，不要直接手工执行。
- 本机模型或数据状态不确定：停止生成 live payload，只保留研究 ledger 和 dry-run。

## 6. 实时导出、同步与飞书通知

本章覆盖 2026-07-12 起的只读实盘观测链路与研究控制台的决策提醒。凭据（飞书 app id/secret、群 chat_id、QMT 节点 ssh 身份）全部存于计算服务器 gitignored `.env`，仓库不保存任何密钥或登录身份。

**QMT 只读实时导出（Windows 侧）**

- `ops/qmt/qmt_client_bridge.py` 部署于 `C:\xquant\` 并手工导入大 QMT 客户端（内置 Python 3.6，仅标准库，不自建网络通道；xtquant/miniQMT 方案已废弃）。
- 每次 `run_time` 回调（5 秒）原子写 `outbox\account_snapshot.json`（资产/持仓/计数全量快照），并按增量追加 `outbox\orders_YYYYMMDD.jsonl`（新委托或委托状态/成交量变化）与 `outbox\deals_YYYYMMDD.jsonl`（新成交，按 traded_id 去重）；导出去重与订单幂等状态持久化在 `state\bridge_state.json`，客户端重启不重发。
- 账户列表来自 `config\qmt_bridge.json`；查询异常时写 `ok=false` 的错误快照（含 traceback），链路中断可被计算服务器侧告警捕获。
- 同一脚本还承担 inbox 订单执行（配置闸门，默认关闭）；协议、幂等与交易日测试流程见 `ops/qmt/README.md`。

**同步与成交通知（计算服务器侧）**

- `ops/qmt/qmt_monitor.sh start|stop|status` 管理 `scripts/live/qmt_live_monitor.py`（核心逻辑在 `src/autotrade/live/qmt_monitor.py`）：每 20s 经 `scp`（`QMT_SSH_DEST`，逐文件拉取，容忍远端缺文件）把 outbox 同步到 `data/qmt_live/`；运行日志追加到 `logs/qmt/qmt_live_monitor.log`。
- 全部通知以交互式卡片投递（彩色标题按事件类型：待批准橙/提问蓝/失败与告警红/买入红/卖出绿；`.env` 配 `CONSOLE_BASE_URL` 时决策卡片附「打开控制台」跳转按钮）。每笔**新成交**经专用飞书 bot（`FEISHU_QMT_*`）向群推送一张卡片：代码/方向/量价/金额/委托号/成交时间/策略标记 + 账户总资产/可用/持仓市值/持仓数；已通知 traded_id 持久化在 `data/qmt_live/.monitor_state.json`，重启不重发。
- 导出端异常（如 MiniQMT 断开）按错误内容去重推送一次「实盘链路告警」，链路中断不会静默。

**研究控制台决策提醒**

- 交互式 worker 的状态迁移钩子（`FEISHU_*` bot）在进入需要人工决策的状态时向群推送：会话等待批准（waiting_user）、Step 门控挂起（waiting_step_user，附验证收益）、Agent 提问（waiting_user_reply，附问题原文）、实验失败（failed，附错误）。凭据缺失时自动禁用；推送线程 best-effort，永不阻塞或破坏 worker。
- 强制终止反馈：worker 在独立进程组内，SIGTERM/SIGKILL 作用于整组；优雅退出后也按实验标签回收残留 Sandbox 容器。SIGKILL 升级后管理端在 status.json 落 `terminated` 终态（控制台徽标「已强制终止」，可恢复续跑）；终止按钮先提示"正在终止（宽限约 10 秒）"，完成后按实际结果提示优雅退出或强杀。若被终止的当前计划会话尚未落业务账本，管理端同时撤销该会话批准、保留可编辑的指令/Prompt 草稿；即使 auto 会话原本没有显式批准项，也切到 `manual`，恢复后必定回到待批准状态。已落账会话和普通暂停不撤销，也不改变模式。
