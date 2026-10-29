<#
.SYNOPSIS
    Starts the TechPulse-AI read-only API locally (Windows development runner).
.DESCRIPTION
    Launches uvicorn serving techpulse.api.main:app on 127.0.0.1:8000.
    Production deploy target is a separate always-on Linux box per AUTH-02 / Ticket DECbox.
.PARAMETER HostAddress
    The interface to bind (default: 127.0.0.1).
.PARAMETER Port
    The port to listen on (default: 8000).
#>
[CmdletBinding()]
param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir

Write-Host "Starting TechPulse-AI API locally at http://${HostAddress}:${Port}"
Write-Host "Local deployment target: SQLite + uvicorn (AUTH-02 / AUTH-06)"

Set-Location $RepoRoot
$env:UV_PROJECT_ENVIRONMENT = Join-Path $RepoRoot ".venv"

uv run uvicorn techpulse.api.main:app --host $HostAddress --port $Port
