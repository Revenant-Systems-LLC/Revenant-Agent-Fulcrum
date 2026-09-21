# Installs the tracked pre-commit secret-scan hook into this clone's .git/hooks.
# Run once after cloning: powershell -File scripts\Install-Hooks.ps1
$ErrorActionPreference = 'Stop'
$root = (git rev-parse --show-toplevel).Trim()
$src = Join-Path $root 'scripts\hooks\pre-commit'
$dst = Join-Path $root '.git\hooks\pre-commit'
Copy-Item $src $dst
Write-Output "Installed $dst"
