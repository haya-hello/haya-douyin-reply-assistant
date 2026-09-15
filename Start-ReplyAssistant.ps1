param(
    [ValidateSet('status','start','stop','doctor','doctor-dm','restart-check','backup','freeze','verify-release','restore-check','preview','reply','dm-preview','dm-reply','handoffs')]
    [string]$Action = 'status',
    [switch]$Online,
    [switch]$ConfirmSend
)
$ErrorActionPreference = 'Stop'
# 不依赖当前工作目录 / Resolve paths independently of the caller's working directory.
$pythonExe = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..\external\DMShoot\.venv\Scripts\python.exe'))
if (-not (Test-Path -LiteralPath $pythonExe)) { throw '缺少项目Python环境，请按RECOVERY.md恢复，不要新建替代项目。' }
if ($Action -in @('reply','dm-reply') -and -not $ConfirmSend) { throw '真实发送需要当前用户明确要求并指定 -ConfirmSend。' }
$replyArguments = @('-X','utf8',(Join-Path $PSScriptRoot 'haya_reply.py'),$Action)
if ($Online) { $replyArguments += '--online' }
if ($ConfirmSend) { $replyArguments += '--confirm-send' }
& $pythonExe @replyArguments
exit $LASTEXITCODE
