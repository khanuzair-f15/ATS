<# 
JobIndex S1+ task helpers for Windows (PowerShell).
Usage: .\tasks.ps1 <target>  where target = up, migrate, test, lint, fmt, clean
#>

param(
    [ValidateSet("up","migrate","test","lint","fmt","clean")]
    [string]$Target
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
$VenvPython = "$RepoRoot\.venv\Scripts\python.exe"

function Up {
    docker compose up -d
}

function Migrate {
    & $VenvPython -m alembic upgrade head
}

function Test {
    & $VenvPython -m pytest -q
}

function Lint {
    & $VenvPython -m ruff check src tests
}

function Fmt {
    & $VenvPython -m ruff format src tests
}

function Clean {
    docker compose down -v
    Remove-Item -Recurse -Force .venv, .pytest_cache, .ruff_cache, .mypy_cache, "src/jobindex.egg-info" -ErrorAction SilentlyContinue
}

switch ($Target) {
    "up"      { Up }
    "migrate" { Migrate }
    "test"    { Test }
    "lint"    { Lint }
    "fmt"     { Fmt }
    "clean"   { Clean }
}