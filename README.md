# 蛤鸭回复助手 V1

正式接力入口：[START_HERE.md](START_HERE.md)。这是现有蛤鸭账号项目内的工具，不是另一个生产副本。

本仓库为私有源码版本库。数据库、历史评论和私信、Cookie、API Key、浏览器登录态、运行日志、备份及恢复副本均不提交。当前运行仍依赖本机经过适配的 `douyin-cli` 与 `DMShoot`；本仓库不是可在新电脑上一键安装的独立发行包。

日常直接说“帮我回复最近三天的评论”即可。助手必须读取本目录的AGENTS.md和policy.json，通过体检后才运行一次发送批次。

统一命令入口：`Start-ReplyAssistant.ps1`。默认status不发送；doctor -Online只检查；真实发送需要 `-Action reply -ConfirmSend`，且当次用户明确要求回复。

已固定：最近72小时、评论线程最多一次、私有回复库、逐条处理、有疑问交本人、未知发送结果不重试、数据和代码快照。

未包含：私信批次自动发送、开机自启、后台值守、全部历史采集。单条评论/私信收发此前实测通过，新批次执行器未进行真实批量外发验收。

恢复方法见[RECOVERY.md](RECOVERY.md)，旧过程见[docs/implementation-notes.md](docs/implementation-notes.md)。
