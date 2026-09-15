# 版本、备份和恢复

## 冻结内容

`data/releases/v<version.json中的版本>/` 保存当前代码包、文件SHA-256、两个上游提交与本地补丁、Python精确版本清单；源码包包含npm锁文件。v1.0.0原包不变，V1.1使用独立的v1.1.0目录。

不包含实际安装的node_modules、.venv、下载缓存、浏览器档案或运行配置。不是离线整机镜像，也没有上传GitHub。数据库备份包含私信，不公开分享。

## 例行命令

```powershell
.\Start-ReplyAssistant.ps1 -Action verify-release
.\Start-ReplyAssistant.ps1 -Action backup
.\Start-ReplyAssistant.ps1 -Action restore-check
.\Start-ReplyAssistant.ps1 -Action restart-check
```

backup用SQLite在线备份复制回复库、评论状态库、DMShoot状态库；只在新副本删除config记录并安全清除残留，原库不变。

restore-check恢复到新隔离目录，校验源文件哈希、数据库完整性和数据摘要，不覆盖当前项目，不执行社交发送。

restart-check只重启本入口管理的评论桥接，用另一个工作目录的新Python进程读取规则和数据，再检查浏览器重连、账号及模型列表。不重启Windows、不关闭浏览器；没有管理记录或有未决发送/运行锁时拒绝继续。

## 损坏或换电脑

1. 保留现场和原库，不覆盖、不执行reset或clear。
2. 核验冻结包与备份哈希，先做隔离恢复验收。
3. 在新恢复目录还原源码。包内三个根分别对应本工具目录、`AI+自媒体/external/douyin-cli`、`AI+自媒体/external/DMShoot`。
4. 依manifest记录的Node/Python版本重建环境：评论端npm ci；DMShoot按requirements.freeze.txt安装。需要访问包源，此步骤未在全新电脑实测。
5. 单独配置模型、重登抖音并安装本地私有脚本；密码、Cookie、API Key不从恢复包自动还原。
6. 在隔离项目放入已验证状态库，核对发送记录与handoff。
7. 通过回归与只读体检后，经用户确认唯一入口再切换，禁止新旧目录同时发送。

## 版本维护

V1不覆盖。修改前备份，记录上游变化、补丁及回归，再建新版本。体检检测到源码漂移时先核查，不能删除manifest绕过。

可恢复不等于平台接口永不变化；遇到平台规则、验证或接口变化，停止自动发送并维护适配器。
