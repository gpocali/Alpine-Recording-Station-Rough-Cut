#Requires -Version 5.1
<#
.SYNOPSIS
    Zero-intervention Windows installer for Alpine Recording Station Rough Cut.
#>
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host "  Alpine Recording Station - Rough Cut Windows Installer" -ForegroundColor Cyan
Write-Host "==================================================================" -ForegroundColor Cyan

function Find-WorkingPython {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:APPDATA\uv\python\cpython-3.11.16-windows-x86_64-none\python.exe",
        "$env:LOCALAPPDATA\Python\pythoncore-3.14-64\python.exe",
        "$env:LOCALAPPDATA\Python\bin\python.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) {
            return $c
        }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and ($cmd.Source -notlike "*WindowsApps*")) {
        return $cmd.Source
    }
    return $null
}

$pyExe = Find-WorkingPython
if (-not $pyExe) {
    Write-Host "Python not found locally. Installing Python 3.12 via winget..." -ForegroundColor Yellow
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    $pyExe = Find-WorkingPython
}

if (-not $pyExe) {
    throw "Could not locate a valid Python 3.10+ interpreter."
}

Write-Host "Using Python interpreter: $pyExe" -ForegroundColor Green
& $pyExe "$PSScriptRoot\setup_env.py" @args
