# ClipStudio - installation du runtime portable (Windows).
# A lancer une seule fois : start.bat le fait tout seul si necessaire.
# Tout reste dans le dossier du projet, aucun droit administrateur requis.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$runtime = Join-Path $root ".runtime"
$pyDir   = Join-Path $runtime "python"
$pyExe   = Join-Path $pyDir "python.exe"
$ffDir   = Join-Path $runtime "ffmpeg\bin"
$ffExe   = Join-Path $ffDir "ffmpeg.exe"
$tmp     = Join-Path $runtime "_dl"

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch {
    # Windows tres ancien : on continue, Invoke-WebRequest negociera seul.
}

New-Item -ItemType Directory -Force -Path $runtime | Out-Null
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

function Get-File([string]$url, [string]$out) {
    Write-Host "      $url"
    Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing
}

Write-Host ""
Write-Host "ClipStudio - installation du runtime portable"
Write-Host "Dossier : $root"
Write-Host ""

# 1. Python portable -----------------------------------------------------------
if (-not (Test-Path $pyExe)) {
    Write-Host "[1/5] Python portable..."
    $zip = Join-Path $tmp "python.zip"
    $done = $false
    foreach ($version in @("3.12.8", "3.12.7", "3.12.6")) {
        try {
            Get-File "https://www.python.org/ftp/python/$version/python-$version-embed-amd64.zip" $zip
            $done = $true
            break
        } catch {
            Write-Host "      version $version indisponible, on tente la suivante."
        }
    }
    if (-not $done) {
        throw "Telechargement de Python impossible. Verifie la connexion internet."
    }
    if (Test-Path $pyDir) { Remove-Item $pyDir -Recurse -Force }
    Expand-Archive -LiteralPath $zip -DestinationPath $pyDir -Force

    # Le ._pth de la distribution embeddable n'expose pas site-packages : on l'active.
    $pth = Get-ChildItem -Path $pyDir -Filter "python*._pth" | Select-Object -First 1
    if ($pth) {
        $lines = @(Get-Content -LiteralPath $pth.FullName)
        $lines = $lines | ForEach-Object {
            if ($_ -match '^\s*#\s*import site') { 'import site' } else { $_ }
        }
        if (-not ($lines -match 'site-packages')) { $lines += 'Lib\site-packages' }
        Set-Content -LiteralPath $pth.FullName -Value $lines -Encoding ASCII
    }
} else {
    Write-Host "[1/5] Python portable deja present."
}

# 2. pip -----------------------------------------------------------------------
if (-not (Test-Path (Join-Path $pyDir "Lib\site-packages\pip"))) {
    Write-Host "[2/5] pip..."
    $getpip = Join-Path $tmp "get-pip.py"
    Get-File "https://bootstrap.pypa.io/get-pip.py" $getpip
    & $pyExe $getpip --no-warn-script-location
    if ($LASTEXITCODE -ne 0) { throw "Installation de pip impossible." }
} else {
    Write-Host "[2/5] pip deja present."
}

# 3. Dependances du backend ----------------------------------------------------
Write-Host "[3/5] Dependances Python (FastAPI, Uvicorn, yt-dlp)..."
& $pyExe -m pip install --disable-pip-version-check -q --no-warn-script-location -r (Join-Path $root "backend\requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Installation des dependances impossible (internet requis)."
}

# 4. FFmpeg --------------------------------------------------------------------
if (-not (Test-Path $ffExe)) {
    Write-Host "[4/5] FFmpeg portable (~110 Mo, une seule fois)..."
    $zip = Join-Path $tmp "ffmpeg.zip"
    try {
        Get-File "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" $zip
    } catch {
        throw "Telechargement de FFmpeg impossible. Verifie la connexion internet."
    }
    $out = Join-Path $tmp "ffmpeg"
    if (Test-Path $out) { Remove-Item $out -Recurse -Force }
    Expand-Archive -LiteralPath $zip -DestinationPath $out -Force

    $bin = Get-ChildItem -Path $out -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
    if (-not $bin) { throw "ffmpeg.exe introuvable dans l'archive telechargee." }
    New-Item -ItemType Directory -Force -Path $ffDir | Out-Null
    Copy-Item (Join-Path $bin.DirectoryName "ffmpeg.exe") $ffDir -Force
    $probe = Join-Path $bin.DirectoryName "ffprobe.exe"
    if (Test-Path $probe) { Copy-Item $probe $ffDir -Force }
} else {
    Write-Host "[4/5] FFmpeg deja present."
}

# 5. Sous-titres automatiques (facultatif) -------------------------------------
# faster-whisper est gros (~150 Mo) et se telecharge a part : si l'installation
# echoue, le reste de ClipStudio fonctionne, seuls les sous-titres manquent.
Write-Host "[5/5] Sous-titres automatiques (faster-whisper)..."
& $pyExe -m pip install --disable-pip-version-check -q --no-warn-script-location faster-whisper
if ($LASTEXITCODE -ne 0) {
    Write-Host "      Echec (optionnel) : les sous-titres resteront indisponibles."
    Write-Host "      Relance setup.ps1 plus tard pour reessayer."
} else {
    Write-Host "      OK. Le modele vocal (~140 Mo) se telecharge au premier usage."
}

# Les fichiers viennent d'internet : on retire la marque "bloque" de Windows.
Get-ChildItem -Path (Join-Path $runtime "ffmpeg") -Recurse -Filter *.exe -ErrorAction SilentlyContinue |
    Unblock-File -ErrorAction SilentlyContinue
Get-ChildItem -Path $pyDir -Recurse -Filter *.exe -ErrorAction SilentlyContinue |
    Unblock-File -ErrorAction SilentlyContinue

Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Installation terminee."
Write-Host "Lance start.bat : le navigateur s'ouvrira sur http://127.0.0.1:8000"
Write-Host ""
