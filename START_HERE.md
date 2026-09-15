# 蛤鸭回复助手 V1 接力入口

## 唯一位置

`D:\自媒体项目\AI+自媒体\accounts\蛤鸭-抖音\reply-assistant`

私有 GitHub 版本库：`https://github.com/haya-hello/haya-douyin-reply-assistant`。本地生产目录仍是上面的唯一运行入口；GitHub 只保存脱敏源码与文档，不保存 `data/`、密钥、Cookie 或浏览器登录态。完整的三组件脱敏冻结源码、哈希清单和补丁位于仓库的 `v1.0.0` Release，用于恢复，不作为在线运行数据。

先读本页，再读AGENTS.md、policy.json、version.json。不依赖旧聊天，不从旧测试脚本恢复发送。无需专用Skill，正式入口为本地受限命令行。

## 能力边界

- 按需评论批次已实现；底层单条发送已实测，新批次以模拟回归和非外发重启验收为依据。
- 私信历史导入、检索和拟稿可用，底层单条收发已实测；私信批次尚未实现，不启动旧DMShoot无限自动回复。
- 首批历史样本不是全量覆盖。历史学习不受三天限制，发送候选只允许最近72小时。
- 不驻守、不定时、不随上线自动补发；用户每次明确叫用才执行一个批次。

## 固定规则

1. 只处理本人作品评论；默认扫描最近20条作品，每条最多60条顶层评论、每个线程最多50条回复，不冒充全量。
2. 原评论距现在不超过72小时，发送前再查一次；未知、缺失或未来时间不发送。
3. 同一评论线程最多自动回复1次，手动回过也跳过；不确定项保存handoff，等待本人，不在后续批次自动放行。
4. 一批最多5个候选，逐条发送，发送尝试间隔至少60秒并跨进程保留；间隔不是免封保证。
5. 平台限制、验证码、账号错误、投递不明时停住，不绕过体检、不盲目重试。
6. 私信未来批次上限为同会话2次，第二次须新入站；当前此能力未接入。

## 日常入口

在任意目录使用完整路径，或在本目录执行：

```powershell
.\Start-ReplyAssistant.ps1 -Action status
.\Start-ReplyAssistant.ps1 -Action start
.\Start-ReplyAssistant.ps1 -Action doctor -Online
.\Start-ReplyAssistant.ps1 -Action handoffs
```

只有用户本次明确要求“帮我回评论”，才执行：

```powershell
.\Start-ReplyAssistant.ps1 -Action reply -ConfirmSend
```

start只启动本地桥接，不重开浏览器、重登账号或启动私信监听。浏览器未连接时，请用户打开已安装脚本的抖音页面并登录。

## 状态与证据

- `data/reply-memory.sqlite3`：样本、发送尝试、人工接管、跨运行间隔。
- `data/runtime/bridge.json`：本入口管理的服务实例，只能停止身份匹配的进程。
- `data/qa/doctor-latest.json`：最近一次体检。
- `data/qa/restart-acceptance.json`：桥接重启及不同工作目录新进程重载验收。
- `data/qa/restore-acceptance.json`：隔离目录恢复验收。
- `data/releases/v1.0.0/manifest.json`：冻结源码哈希、上游提交、运行版本。
- `data/backups/*/manifest.json`：三份数据库的一致性备份，配置凭据不包含在内。

重启验收不代表重启操作系统、另建聊天或在干净电脑重装。实际结果读取报告，不能凭本页宣称通过。

## 下一步

正常使用无需改代码。升级走新版本、回归与冻结，不覆盖V1。私信批次作为后续独立验收项，不混进V1完成状态。
