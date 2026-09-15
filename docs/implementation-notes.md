# V1之前的实现记录（历史说明）

当前入口与规则以模块根目录 START_HERE.md、AGENTS.md、policy.json 为准；本页旧测试数和入口命令不代表最新状态。

## 当前可用范围

最新回复边界：同一顶层评论线程最多自动回复1次，用户手动回过也跳过，不继续自动接话。私信同一会话最多自动回复2次，优先1次解决；第2次必须先有对方的新消息，不主动追加、不因重启或换批次重置次数。私信批次执行器尚未接入，当前仅提供政策与持久计数辅助函数，不能宣称已全面启用。

没有把握的评论进入 `handoffs` 表，等待本人回复，下一批也不会自动重新尝试。用 `python -X utf8 reply_memory.py handoffs` 查看。模型必须明确标为high且不要求hold，并通过规则检查才可进入发送步骤；模型自评不是准确率保证。新增回归后本模块共13项测试通过。

仅在用户明确要求“帮我回评论”时启动一个批次。用户已允许本批逐条自动发送，不再要求每条确认；默认最多5个候选、串行、两次发送之间至少60秒。间隔不是平台认可或免封保证。没有后台调度、开机自启、上线即自动补发。

私信收发基础工具保留，但本模块目前只实现评论批次发送；私信历史导入、检索和拟稿已提供，不默认为全部私信自动发送。

## 数据库与证据边界

- 本地私有数据库：`data/reply-memory.sqlite3`。原始私信候选在 `data/dm-history-candidate.json`；两者均忽略版本控制，不放进第二大脑或公开仓库。
- `samples` 保存来源ID、渠道、原问题、账号回复、作品上下文、时间和来源分类。
- `dm_history` 保存采到的私信及会话归属判断；无法配对的发送记录不参与检索。
- `scans` 记录覆盖范围；`generations` 记录生成结果与引用样本ID；`send_attempts` 记录外发状态，异常不自动重发。
- 2026-09-16 首批扫描最近5条作品，评论每条最多60个顶层、每个线程最多50条回复；不是全量历史。首次私信抓到82条，51条显示由本账号发出，也不代表全部历史。
- 首批可参考样本：评论12条、私信27条；24条缺少可靠上文不学习；已知系统生成的评论/私信测试各1条排除。
- `account_history_unverified` 意味着已识别为本账号的历史发送，不能证明过去每句都是本人手写。当前自动化产生的消息排除，避免模型把自己的输出当作用户偏好。
- 评论、私信严格分渠道检索；发给模型的少量示例隐去常见手机号、邮箱、URL和密钥格式，但这不是完美的敏感信息检测。完整历史不一次性发给模型。
- 老回复仅作表达和处理方式参考，不自动确认旧活动、链接、报价和承诺仍有效。涉及敏感或商务等内容保留给本人。

## 命令

在本目录执行：

```powershell
python -X utf8 reply_memory.py collect-comments --videos 5
python -X utf8 reply_memory.py import
python -X utf8 reply_memory.py stats
python -X utf8 reply_memory.py search --channel comment --text '怎么开始做项目'
python -X utf8 reply_memory.py draft --channel dm --text '我也想参加活动'
```

私信历史刷新使用 DMShoot 已安装依赖的解释器：

```powershell
& '..\..\..\external\DMShoot\.venv\Scripts\python.exe' -X utf8 collect_dm_history.py
python -X utf8 reply_memory.py import
```

评论批次预览（不发送）：

```powershell
python -X utf8 reply_memory.py reply-comments --videos 5
```

只有当用户当次明确要求回复评论，才执行真正发送：

```powershell
python -X utf8 reply_memory.py reply-comments --videos 5 --send
```

批次会核对账号、作品归属、已有本人回复及目标是否变化；未知发送状态停止后续发送。运行锁或 `attempting/uncertain_stop_no_retry` 不能未经核实就删除/重置。不要使用上游 `suggest --auto --force` 绕过这些检查。

## 验证与当前限制

- `python -X utf8 -m unittest test_reply_memory.py`：10项测试通过，包括来源去重、渠道隔离、生成样本排除、预览不发送、模拟单次发送与回读、状态不明后阻断重试。
- 历史采集及基于库的评论/私信拟稿已用真实数据运行。本轮没有额外发送消息。
- 评论和私信底层单条发送已在此前真实测试通过；新批次执行器本轮仅通过模拟发送测试，尚未实际批量外发。
- 目前检索采用中文相邻字匹配，并非语义向量检索；无相似样本时会明确标记证据不足，不声称已学会用户全部口吻。
- 首批私信来自网站历史初始化数据，配对依赖上游解析器；不保证补齐全部遗漏历史。后续需要扩大范围时继续按批采集和核对。
