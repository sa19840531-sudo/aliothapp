# Alioth local deployment via Docker Compose
# Usage: .\scripts\start-local.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "=== Alioth Deploy ===" -ForegroundColor Cyan

$envLocal = Join-Path $Root "infra\env\.env.local"
if (-not (Test-Path $envLocal)) {
    Copy-Item (Join-Path $Root "infra\env\.env.local.example") $envLocal
}
if (-not (Test-Path (Join-Path $Root ".env"))) {
    Copy-Item (Join-Path $Root ".env.example") (Join-Path $Root ".env")
}

$dockerBin = "C:\Program Files\Docker\Docker\resources\bin"
if (Test-Path $dockerBin) {
    $env:Path = "$dockerBin;" + $env:Path
}

$dockerReady = $false
try {
    docker info *> $null
    $dockerReady = $true
} catch {
    Write-Host "Starting Docker Desktop..."
    Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe" -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 36; $i++) {
        Start-Sleep -Seconds 5
        try {
            docker info *> $null
            $dockerReady = $true
            break
        } catch {}
    }
}

if (-not $dockerReady) {
    Write-Host "Docker not ready. Please start Docker Desktop manually." -ForegroundColor Red
    exit 1
}

$env:ALIOTH_ENV_FILE = './env/.env.local'
Write-Host "Starting containers..."
docker compose -f infra/docker-compose.yml up --build -d

Write-Host "Waiting for services..."
Start-Sleep -Seconds 12

Write-Host "Running database migrations..."
docker exec alioth-api python -m alembic -c services/api/migrations/alembic.ini upgrade head

try {
    $health = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/health" -TimeoutSec 15
    Write-Host "Health check OK: $($health.status)" -ForegroundColor Green
} catch {
    Write-Host "API not ready yet: http://localhost:8000/api/v1/health" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "API docs:  http://localhost:8000/docs"
Write-Host "Admin UI:  http://localhost:5173"
Write-Host "Health:    http://localhost:8000/api/v1/health"
Write-Host "Seed data: docker exec alioth-api python scripts/seed_first_ruleset.py"
