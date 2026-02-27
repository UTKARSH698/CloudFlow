# run.ps1 — Windows equivalent of the Makefile
# Usage: .\run.ps1 <command>
# Example: .\run.ps1 setup

param([string]$Command = "help")

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

function Write-Header($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Invoke-Setup {
    Write-Header "Installing Python dependencies..."
    pip install -r "$Root\requirements-dev.txt"

    Write-Header "Installing CDK infrastructure dependencies..."
    pip install -r "$Root\infrastructure\requirements.txt"

    Write-Header "Installing AWS CDK CLI..."
    npm install -g aws-cdk

    Write-Host ""
    Write-Host "Setup complete." -ForegroundColor Green
}

function Invoke-LocalUp {
    Write-Header "Starting LocalStack..."
    docker compose -f "$Root\docker-compose.yml" up -d --wait
    Write-Host ""
    Write-Host "LocalStack running at http://localhost:4566" -ForegroundColor Green
}

function Invoke-LocalDown {
    Write-Header "Stopping LocalStack..."
    docker compose -f "$Root\docker-compose.yml" down
}

function Invoke-TestUnit {
    Write-Header "Running unit tests..."
    $env:PYTHONPATH = "$Root\services"
    pytest "$Root\tests\unit" -v --tb=short -q
}

function Invoke-TestIntegration {
    Write-Header "Running integration tests (needs LocalStack)..."
    $env:USE_LOCALSTACK = "true"
    $env:LOCALSTACK_ENDPOINT = "http://localhost:4566"
    $env:PYTHONPATH = "$Root\services"
    pytest "$Root\tests\integration" -v --tb=short -m integration
}

function Invoke-Test {
    Invoke-TestUnit
    Invoke-TestIntegration
}

function Invoke-Deploy {
    Write-Header "Deploying all CDK stacks to AWS..."
    Push-Location "$Root\infrastructure"
    try { cdk deploy --all --require-approval never }
    finally { Pop-Location }
}

function Invoke-Diff {
    Write-Header "Showing CDK diff..."
    Push-Location "$Root\infrastructure"
    try { cdk diff --all }
    finally { Pop-Location }
}

function Invoke-Bootstrap {
    Write-Header "Bootstrapping CDK..."
    Push-Location "$Root\infrastructure"
    try { cdk bootstrap }
    finally { Pop-Location }
}

function Invoke-Demo {
    Write-Header "Submitting a demo order..."
    $env:PYTHONPATH = "$Root\services"
    python "$Root\scripts\seed_data.py" --local
}

function Invoke-Clean {
    Write-Header "Cleaning build artifacts..."
    Get-ChildItem -Path $Root -Recurse -Include "__pycache__","*.pyc",".pytest_cache","*.egg-info" |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -Path "$Root\coverage.xml","$Root\.coverage" -ErrorAction SilentlyContinue
    Write-Host "Cleaned." -ForegroundColor Green
}

function Invoke-Help {
    Write-Host ""
    Write-Host "CloudFlow — run.ps1 Commands" -ForegroundColor Yellow
    Write-Host "────────────────────────────────────────────"
    Write-Host "  .\run.ps1 setup            Install all dependencies"
    Write-Host "  .\run.ps1 local-up         Start LocalStack (Docker)"
    Write-Host "  .\run.ps1 local-down       Stop LocalStack"
    Write-Host "  .\run.ps1 test             Run all tests"
    Write-Host "  .\run.ps1 test-unit        Unit tests only (no Docker)"
    Write-Host "  .\run.ps1 test-integration Integration tests (needs LocalStack)"
    Write-Host "  .\run.ps1 cdk-bootstrap    Bootstrap CDK (run once per account)"
    Write-Host "  .\run.ps1 deploy           Deploy all stacks to AWS"
    Write-Host "  .\run.ps1 diff             Show CDK diff"
    Write-Host "  .\run.ps1 demo             Submit a sample order end-to-end"
    Write-Host "  .\run.ps1 clean            Remove build artifacts"
    Write-Host ""
}

switch ($Command) {
    "setup"            { Invoke-Setup }
    "local-up"         { Invoke-LocalUp }
    "local-down"       { Invoke-LocalDown }
    "test"             { Invoke-Test }
    "test-unit"        { Invoke-TestUnit }
    "test-integration" { Invoke-TestIntegration }
    "deploy"           { Invoke-Deploy }
    "diff"             { Invoke-Diff }
    "cdk-bootstrap"    { Invoke-Bootstrap }
    "demo"             { Invoke-Demo }
    "clean"            { Invoke-Clean }
    "help"             { Invoke-Help }
    default {
        Write-Host "Unknown command: $Command" -ForegroundColor Red
        Invoke-Help
        exit 1
    }
}
