$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $RepoRoot "Library\Tools\autosub\.venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$Requirements = Join-Path $RepoRoot "requirements.txt"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python was not found. Install Python 3.10 or newer, then run this script again."
}

if (-not (Test-Path $VenvPython)) {
    python -m venv $VenvDir
}

if (-not (Test-Path $VenvPython)) {
    throw "The virtual environment was not created correctly."
}

& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip." }

& $VenvPython -m pip install -r $Requirements
if ($LASTEXITCODE -ne 0) { throw "Failed to install Python dependencies." }

& $VenvPython -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw "Failed to install Chromium for Playwright." }

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "FFmpeg was not found. Installing it with winget..."
        winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "FFmpeg installation failed. Install FFmpeg manually and make sure it is in PATH."
        } else {
            Write-Host "FFmpeg installed. If ffmpeg is not found immediately, open a new terminal and run the GUI again."
        }
    } else {
        Write-Warning "FFmpeg and winget were not found. Install FFmpeg manually and make sure it is in PATH."
    }
} else {
    Write-Host "FFmpeg is already installed."
}

Write-Host ""
Write-Host "Installation complete. Start the GUI with:"
Write-Host ".\Library\Tools\autosub\.venv\Scripts\python.exe .\Library\Tools\autosub\autosub_gui.py"
