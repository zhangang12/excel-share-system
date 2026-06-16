# Pull production backups (DB + uploaded files) down to a LOCAL Windows server -- offsite cold backup.
#
# Run this ON the local Windows machine (via Task Scheduler). Uses built-in OpenSSH ssh/scp.
# If your local server is Linux/NAS, use pull-backup-to-local.sh instead.
# Full Chinese guide: see ops/本地异地备份方案.md
#
# Prereq: passwordless SSH from local -> cloud
#   1) ssh-keygen -t ed25519
#   2) type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh root@8.141.123.141 "cat >> ~/.ssh/authorized_keys"
#   3) ssh root@8.141.123.141 "echo ok"
#
# Usage (PowerShell):
#   .\pull-backup-to-local.ps1 -Remote root@8.141.123.141 -LocalDir D:\pms-backup
#   .\pull-backup-to-local.ps1 -Remote root@8.141.123.141 -LocalDir D:\pms-backup -RunRemoteBackup
#
param(
    [Parameter(Mandatory=$true)][string]$Remote,           # root@8.141.123.141
    [string]$RemoteBackupDir = "/backup",
    [string]$RemoteOpsDir    = "/root/excel-share-system-main/ops",
    [string]$LocalDir        = "D:\pms-backup",
    [int]$KeepDays           = 90,
    [int]$SshPort            = 22,
    [switch]$RunRemoteBackup
)
$ErrorActionPreference = "Stop"
function Log($m){ Write-Host ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m) }
if (-not (Test-Path $LocalDir)) { New-Item -ItemType Directory -Force -Path $LocalDir | Out-Null }

$sshArgs = @("-p", "$SshPort", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=accept-new")

# 1) (optional) make a fresh backup on the cloud first
if ($RunRemoteBackup) {
    Log "Triggering fresh backup on cloud..."
    & ssh @sshArgs $Remote "bash $RemoteOpsDir/backup.sh"
    if ($LASTEXITCODE -ne 0) { throw "remote backup.sh failed" }
}

# 2) find newest db + uploads backup on cloud, pull only those two (scp has no incremental)
Log "Querying newest backups on cloud..."
$latestDb = (& ssh @sshArgs $Remote "ls -t $RemoteBackupDir/pms-db-*.sql.gz 2>/dev/null | head -1").Trim()
$latestUp = (& ssh @sshArgs $Remote "ls -t $RemoteBackupDir/pms-uploads-*.tar.gz 2>/dev/null | head -1").Trim()

foreach ($rf in @($latestDb, $latestUp)) {
    if ([string]::IsNullOrWhiteSpace($rf)) { Log "WARN: a backup type not found on cloud, skip"; continue }
    $name = Split-Path $rf -Leaf
    $dest = Join-Path $LocalDir $name
    if (Test-Path $dest) { Log "exists, skip $name"; continue }
    Log "pulling $name ..."
    & scp -P $SshPort -o BatchMode=yes -o StrictHostKeyChecking=accept-new "${Remote}:$rf" "$dest"
    if ($LASTEXITCODE -ne 0) { throw "scp failed: $rf" }
    $sz = "{0:N1} MB" -f ((Get-Item $dest).Length/1MB)
    Log "OK $name ($sz)"
}

# 3) local retention cleanup (by last write time)
$cutoff = (Get-Date).AddDays(-$KeepDays)
Get-ChildItem -Path $LocalDir -Filter "pms-*.gz" -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt $cutoff } | Remove-Item -Force -ErrorAction SilentlyContinue
$nDb = (Get-ChildItem -Path $LocalDir -Filter "pms-db-*.sql.gz" -ErrorAction SilentlyContinue).Count
$nUp = (Get-ChildItem -Path $LocalDir -Filter "pms-uploads-*.tar.gz" -ErrorAction SilentlyContinue).Count
Log "Done. Local: $nDb db backups / $nUp file backups (keep $KeepDays days)"
