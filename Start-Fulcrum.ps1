# Start-Fulcrum.ps1
# The one file you run. A numbered menu; no commands to remember.
# This is a manual launcher by design: Fulcrum never installs itself as a scheduled task or service.

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$Py = Join-Path $Root 'venv\Scripts\python.exe'

function Show-Header {
    Clear-Host
    Write-Host ''
    Write-Host '  FULCRUM  (paper trading only, no real money can move)' -ForegroundColor Cyan
    Write-Host '  Rules decide what to buy and sell. The AI can only say no.' -ForegroundColor DarkGray
    Write-Host ''
}

function Check-Setup {
    if (-not (Test-Path $Py)) {
        Write-Host '  The Python environment is missing. In this folder run:' -ForegroundColor Yellow
        Write-Host '    C:\Users\Dave\AppData\Local\Programs\Python\Python311\python.exe -m venv venv'
        Write-Host '    .\venv\Scripts\python.exe -m pip install -r requirements.txt'
        return $false
    }
    if (-not (Test-Path (Join-Path $Root 'config.toml'))) {
        Write-Host '  No config.toml yet. Copying config.example.toml to config.toml (practice mode).' -ForegroundColor Yellow
        Copy-Item (Join-Path $Root 'config.example.toml') (Join-Path $Root 'config.toml')
    }
    return $true
}

function Run-Fulcrum {
    param([string[]]$Command)
    & $Py -m fulcrum @Command
    Write-Host ''
    Write-Host '  (press Enter to go back to the menu)' -ForegroundColor DarkGray
    [void](Read-Host)
}

if (-not (Check-Setup)) { Read-Host 'Press Enter to close'; exit 1 }

while ($true) {
    Show-Header
    Write-Host '  1  Check setup            (tests the parts, sends nothing anywhere)'
    Write-Host '  2  Practice cycle         (one full decision round, pretend money, real prices)'
    Write-Host '  3  Start paper trading    (keeps running until you press Ctrl+C)'
    Write-Host '  4  Status                 (what it holds right now, in plain words)'
    Write-Host '  5  Report                 (how it has done so far, in plain words)'
    Write-Host '  6  History test           (run the strategy over past years and grade it)'
    Write-Host '  7  Open the docs folder'
    Write-Host '  0  Quit'
    Write-Host ''
    $choice = Read-Host '  Pick a number'
    switch ($choice) {
        '1' { Run-Fulcrum @('selftest') }
        '2' { Run-Fulcrum @('run', '--once', '--dry-run') }
        '3' { Run-Fulcrum @('run') }
        '4' { Run-Fulcrum @('status') }
        '5' { Run-Fulcrum @('report') }
        '6' { Run-Fulcrum @('backtest') }
        '7' { Start-Process explorer.exe (Join-Path $Root 'docs') }
        '0' { break }
        default { }
    }
    if ($choice -eq '0') { break }
}
