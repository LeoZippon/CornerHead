# 大 QMT 客户端内文件桥（qmt_client_bridge.py）

`qmt_client_bridge.py` 是唯一的客户端内策略脚本（全功能 QMT 内置 Python 3.6，仅标准库，不自建网络通道），在一个 5 秒定时回调里同时承担：

1. **实时导出（恒开启）**：原子写 `C:\xquant\outbox\account_snapshot.json`（资产/持仓全量快照），并按增量追加 `orders_YYYYMMDD.jsonl`（新委托或状态/成交量变化）与 `deals_YYYYMMDD.jsonl`（新成交，traded_id 去重）；去重与幂等状态持久化在 `C:\xquant\state\bridge_state.json`，客户端重启不重发。
2. **订单执行（配置闸门，默认关闭）**：轮询 `C:\xquant\inbox` 的信号 payload，校验后经 `passorder` 提交（幂等 remark，重复到达不重复下单），结果写 `outbox\execute_*.json` / `error_*.json`，payload 移入 `archive\`。**仓位测算属于决策侧**（本机持有同步回来的账户快照）；客户端侧只接受显式 code/side/volume/price。

历史 xtquant/miniQMT 方案（qmt_executor.py 会话式执行、qmt_realtime_export.py 导出）已废弃；`C:\xquant` 下的遗留文件仅作归档。

## 1. 标准配置

从本机上传配置样例与策略源码：

```bash
ssh Administrator@39.105.46.212 \
  'powershell -NoProfile -Command "New-Item -ItemType Directory -Force C:\xquant\config,C:\xquant\inbox,C:\xquant\outbox,C:\xquant\archive,C:\xquant\state | Out-Null"'
scp ops/qmt/qmt_bridge_config.example.json \
  Administrator@39.105.46.212:C:/xquant/config/qmt_bridge.json
scp ops/qmt/qmt_client_bridge.py \
  Administrator@39.105.46.212:C:/xquant/qmt_client_bridge.py
```

在 Windows 上编辑 `C:\xquant\config\qmt_bridge.json`：

- `accounts[0].account_id` 换成大 QMT 界面显示的真实普通账户 ID（当前执行只路由第一个账户；信用账户支持落地前不要添加 CREDIT 条目）。
- `execution.enabled` 保持 `false` 完成只读验收；交易日测试下单前才改 `true`。
- `op_type_buy/op_type_sell/order_type/pr_type_limit` 是国金柜台映射（文档开放问题 #4），默认 23/24/1101/11；实测不符时只改配置、不改代码。
- `max_order_notional` / `max_payload_notional` 是硬风控上限；`trading_windows` 之外的 live payload 一律拒绝。

账号配置只留在远端，不要提交回仓库。

## 2. 导入大 QMT

1. RDP 进入大 QMT，保持“行情+交易”登录。
2. “模型研究”→ 新建“Python 策略”，把 `C:\xquant\qmt_client_bridge.py` 全文复制进策略编辑器并编译（编译错误必须先解决）。
3. “模型交易”：选择该策略、任意主图代码与周期、正确账户；运行模式先选“模拟”启动。
4. 不要把源码直接复制进 QMT 的内部目录（`.rzrk`、`formulas`、`python`、`bin.x64\Lib`）；文件存在不等于策略已注册编译。

## 3. 只读验收（execution.enabled=false）

启动约 10 秒后在本机确认快照与同步链路：

```bash
ssh Administrator@39.105.46.212 "type C:\\xquant\\outbox\\account_snapshot.json"
ops/qmt/qmt_monitor.sh status     # 计算服务器守护：20 秒拉回 data/qmt_live/ 并推送飞书成交/告警
```

`ok=true` 且 `source="qmt_client_bridge"` 即导出链路就绪；此时 inbox 中的 payload 只会被校验并以 dry_run 结果回写，不会触碰 passorder。

## 4. 信号 payload（schema_version 2）

决策侧生成**显式订单**，先写临时名再原子改名进 inbox（例：`signal_20260713_093000.json`）：

```json
{
  "schema_version": 2,
  "payload_id": "wfB_q50_agree60_20260713_buy_0930",
  "strategy_id": "wfB_q50_agree60",
  "trade_date": "20260713",
  "execute": false,
  "confirm": "",
  "orders": [
    {"code": "600000.SH", "side": "BUY", "volume": 100, "price": 10.50}
  ]
}
```

执行语义：

- 三重独立闸门：配置 `execution.enabled` ∧ payload `execute` ∧ `confirm == payload_id`，全部为真才会 `passorder`；任一为假即 dry_run（校验+回写，不下单）。
- 校验：schema/白名单 strategy_id/当日 trade_date/SH·SZ 代码/BUY 100 股整手/正数量价/单笔与整包名义金额上限/交易时段。
- 幂等：每单 remark = `MQ:<payload_id>:<序号或自定义 remark>`；提交前对照柜台当日委托 remark 与本地已处理记录，重复到达不重复下单。同一 payload_id 永远只处理一次。
- 结果：`outbox\execute_<时间戳>.json`（逐单 submitted/skipped/note）或 `error_<时间戳>.json`（校验失败原因）；原 payload 归档至 `archive\`。

## 5. 交易日测试建议顺序

1. 只读验收（第 3 节）通过、飞书群能看到链路告警/恢复。
2. 投一个 `execute=false` 的 payload → 收到 dry_run 结果回写。
3. 配置 `enabled=true`，投 `execute=true, confirm=payload_id` 的**单股最小单**（100 股低价股，勿超 `max_order_notional`）→ 确认柜台委托出现、成交后飞书收到成交通知、`deals_*.jsonl` 有记录。
4. 重复投递同一 payload → 确认被幂等拒绝（"payload already processed"）。
5. 测试完把 `enabled` 改回 `false`。

## 6. 计算服务器侧（同步 + 飞书通知）

`.env` 需 `FEISHU_QMT_APP_ID/APP_SECRET/CHAT_ID` 与 `QMT_SSH_DEST`：

```bash
ops/qmt/qmt_monitor.sh start|stop|status
```

每笔新成交推送一条群消息（代码/方向/量价/金额/委托号/时间/策略标记 + 账户总资产/可用/持仓市值/持仓数）；导出端异常按错误内容去重推送一次链路告警。同步产物落 `data/qmt_live/`，已通知状态在 `data/qmt_live/.monitor_state.json`。
