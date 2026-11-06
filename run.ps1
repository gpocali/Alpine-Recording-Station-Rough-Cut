#Requires -Version 5.1
<#
.SYNOPSIS
    Launch the Alpine Recording Station Rough Cut Web UI & Backend Server.
#>
param(
    [int]$Port = 8000,
    [switch]$NoBrowser
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Virtual environment not found. Running install.ps1 first..." -ForegroundColor Yellow
    & "$PSScriptRoot\install.ps1"
}

$url = "http://127.0.0.1:$Port"
Write-Host "Starting Alpine Recording Station Rough Cut at $url ..." -ForegroundColor Cyan

if (-not $NoBrowser) {
    Start-Job -ScriptBlock {
        param($targetUrl)
        Start-Sleep -Seconds 2
        Start-Process $targetUrl
    } -ArgumentList $url | Out-Null
}

& $venvPython -m uvicorn backend.main:app --host 127.0.0.1 --port $Port
