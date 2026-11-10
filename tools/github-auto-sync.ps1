$ErrorActionPreference = "Stop"

$repository = Split-Path -Parent $PSScriptRoot
Set-Location $repository

function Get-ChangeState {
    $status = @(git status --porcelain)
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to read Git status."
    }
    return ($status -join "`n")
}

function Sync-Changes {
    $state = Get-ChangeState
    if ([string]::IsNullOrWhiteSpace($state)) {
        return
    }

    git add --all -- '*.py' '*.pyi' '*.ps1' '*.json' '*.md' '*.txt' '*.yml' '*.yaml'
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to stage code changes."
    }

    $staged = @(git diff --cached --name-only)
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to inspect staged changes."
    }
    if ($staged.Count -eq 0) {
        return
    }

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    git commit -m "Auto-sync code changes ($timestamp)"
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to create the automatic commit."
    }

    git push origin HEAD
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Automatic GitHub push failed. The commit is local; run 'git push' after fixing authentication or connectivity."
    } else {
        Write-Host "Automatic GitHub sync completed at $timestamp"
    }
}

$lastState = Get-ChangeState
Write-Host "GitHub auto-sync is running. Save a code file to sync it."

while ($true) {
    Start-Sleep -Seconds 2
    $currentState = Get-ChangeState
    if ($currentState -ne $lastState) {
        Sync-Changes
        $lastState = Get-ChangeState
    }
}
