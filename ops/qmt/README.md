# 大 QMT 只读文件桥示例

`qmt_readonly_bridge.py` 是全功能 QMT 客户端内置 Python 3.6 策略示例。它每 15 秒读取配置中的普通/信用账户 `ACCOUNT`、`POSITION`、`ORDER` 和 `DEAL`，再把快照原子写到：

```text
C:\xquant\outbox\account_snapshot.json
```

示例没有 `passorder`、`cancel` 或其他写交易状态的调用，不能下单。

## 1. 准备 Windows 配置

从本机上传配置样例和策略源码：

```bash
ssh Administrator@39.105.46.212 \
  'powershell -NoProfile -Command "New-Item -ItemType Directory -Force C:\xquant\config,C:\xquant\outbox | Out-Null"'
scp ops/qmt/qmt_bridge_config.example.json \
  Administrator@39.105.46.212:C:/xquant/config/qmt_bridge.json
scp ops/qmt/qmt_readonly_bridge.py \
  Administrator@39.105.46.212:C:/xquant/qmt_readonly_bridge.py
```

在 Windows 上编辑 `C:\xquant\config\qmt_bridge.json`，把占位符替换为大 QMT 界面显示的真实普通账户和信用账户 ID；没有的账户条目直接删除。账号配置只留在远端，不要提交回仓库。

## 2. 导入大 QMT

1. 通过 RDP 进入大 QMT，保持“行情+交易”登录。
2. 进入“模型研究”，新建“Python 策略”。
3. 打开 `C:\xquant\qmt_readonly_bridge.py`，把全文复制到 QMT 策略编辑器。
4. 编译策略；编译错误必须先解决，不能继续到模型交易。
5. 进入“模型交易”，选择该策略、主图代码、周期和正确账户。
6. 运行模式选择“模拟”，再启动策略。此示例本身不含下单函数，但仍统一从模拟模式开始验收。

不要把源码直接复制进 QMT 的 `.rzrk`、`formulas`、`python` 或 `bin.x64\Lib` 内部目录；文件出现不等于策略已经被客户端注册和编译。

## 3. 验收

启动后等待约 15 秒，在本机读取快照：

```bash
ssh Administrator@39.105.46.212 \
  'powershell -NoProfile -Command "Get-Content -Raw C:\xquant\outbox\account_snapshot.json"'
```

成功输出必须包含：

```json
{
  "ok": true,
  "mode": "read_only",
  "accounts": []
}
```

实际 `accounts` 应包含配置的账户以及账户、持仓、委托和成交对象。若 `ok=false`，查看 JSON 中的 `error`，并检查 QMT 日志：

```text
C:\国金证券QMT交易端\userdata\log\XtClient_Formula_YYYYMMDD.log
C:\国金证券QMT交易端\userdata\log\XtClient_FormulaOutput_YYYYMMDD.log
```

## 4. 边界

- 这是只读连通性样例，不是实盘执行器。
- QMT 返回的是客户端本地交易缓存；提交后的委托/成交不能假定立即可见。
- 定时回调必须快速返回；禁止阻塞循环、线程、网络服务器和 `sleep`。
- 后续订单桥必须另行实现输入校验、幂等、双授权、预算/持仓/交易规则重校验、回调去重和重启恢复，不能直接在本示例中加一行 `passorder` 后投入实盘。

## 实时导出 + 同步 + 飞书通知（2026-07-12 起）

链路：`qmt_realtime_export.py`（Windows，只读，无网络）→ scp 拉回（计算服务器）→ 飞书成交/告警通知。

Windows 侧（需 QMT 客户端已登录 MiniQMT）：

```powershell
C:\xquant\Python38\python.exe C:\xquant\qmt_realtime_export.py
# 开机自启（可选）：
schtasks /Create /TN xquant_realtime_export /SC ONLOGON `
  /TR "C:\xquant\Python38\python.exe C:\xquant\qmt_realtime_export.py"
```

产物（C:\xquant\outbox）：`account_snapshot.json`（原子全量快照）、
`orders_YYYYMMDD.jsonl`（新委托/状态变化增量）、`deals_YYYYMMDD.jsonl`（新成交增量）。
多账户需设 `CQ_EXPECTED_ACCOUNT_ID`；去重状态在 `state\realtime_export_seen.json`。

计算服务器侧（.env 需 FEISHU_QMT_APP_ID/APP_SECRET/CHAT_ID + QMT_SSH_DEST）：

```bash
ops/qmt/qmt_monitor.sh start|stop|status   # 同步到 data/qmt_live/ 并逐成交推送飞书
```

每笔新成交推送一条消息（代码/方向/量价/金额/委托号 + 账户总资产/可用/持仓市值/持仓数）；
导出端异常（如 MiniQMT 断开）按错误内容去重后推送一次链路告警。
