[CmdletBinding()]
param(
    [string]$Host = "127.0.0.1",
    [int]$Port = 9527
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeRoot = Join-Path $ProjectRoot ".runtime"
$DownloadsRoot = Join-Path $RuntimeRoot "downloads"
$PortablePythonRoot = Join-Path $RuntimeRoot "python"
$PortablePythonExe = Join-Path $PortablePythonRoot "python.exe"
$PortablePythonState = Join-Path $RuntimeRoot "portable-python.ready"
$VenvRoot = Join-Path $ProjectRoot ".venv"
$VenvPythonExe = Join-Path $VenvRoot "Scripts\python.exe"
$RequirementsPath = Join-Path $ProjectRoot "requirements.txt"
$RequirementsState = Join-Path $RuntimeRoot "requirements.sha256"
$LocalFfmpegRoot = Join-Path $RuntimeRoot "ffmpeg"
$LocalFfmpegExe = Join-Path $LocalFfmpegRoot "ffmpeg.exe"
$GetPipPath = Join-Path $DownloadsRoot "get-pip.py"

function Write-Step {
    param([string]$Message)
    Write-Host "[Screen casting] $Message"
}

function New-Directory {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Invoke-ExternalCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$ErrorMessage = "外部命令执行失败。"
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$ErrorMessage ExitCode=$LASTEXITCODE"
    }
}

function Get-SystemPython {
    $candidates = @("python.exe", "python3.exe", "python")
    foreach ($name in $candidates) {
        $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $command) {
            continue
        }
        $source = [string]$command.Source
        if (-not $source) {
            continue
        }
        if ($source -match "WindowsApps\\python(?:3)?(?:\.exe)?$") {
            continue
        }
        try {
            & $source "--version" *> $null
            if ($LASTEXITCODE -eq 0) {
                return $source
            }
        } catch {
        }
    }
    return $null
}

function Enable-EmbeddedPythonSitePackages {
    param([Parameter(Mandatory = $true)][string]$PythonRoot)

    $pthFile = Get-ChildItem -Path $PythonRoot -Filter "python*._pth" | Select-Object -First 1
    if (-not $pthFile) {
        throw "便携版 Python 缺少 ._pth 配置文件，无法启用 site-packages。"
    }

    $lines = Get-Content -LiteralPath $pthFile.FullName
    $updated = New-Object System.Collections.Generic.List[string]
    $sawSitePackages = $false
    $sawImportSite = $false
    foreach ($line in $lines) {
        if ($line -eq "#import site" -or $line -eq "import site") {
            $updated.Add("import site")
            $sawImportSite = $true
            continue
        }
        if ($line -eq "Lib\site-packages" -or $line -eq ".\Lib\site-packages") {
            $updated.Add("Lib\site-packages")
            $sawSitePackages = $true
            continue
        }
        $updated.Add($line)
    }
    if (-not $sawSitePackages) {
        $updated.Add("Lib\site-packages")
    }
    if (-not $sawImportSite) {
        $updated.Add("import site")
    }

    $sitePackagesDir = Join-Path $PythonRoot "Lib\site-packages"
    New-Directory -Path $sitePackagesDir
    Set-Content -LiteralPath $pthFile.FullName -Value $updated -Encoding Ascii
}

function Ensure-PortablePythonPip {
    param([Parameter(Mandatory = $true)][string]$PythonExe)

    & $PythonExe -m pip --version *> $null
    if ($LASTEXITCODE -eq 0) {
        return
    }

    New-Directory -Path $DownloadsRoot
    if (-not (Test-Path -LiteralPath $GetPipPath)) {
        Write-Step "正在下载 pip 引导脚本 ..."
        Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $GetPipPath -UseBasicParsing
    }

    Write-Step "正在为便携版 Python 安装 pip ..."
    Invoke-ExternalCommand -FilePath $PythonExe -Arguments @($GetPipPath, "--disable-pip-version-check") -ErrorMessage "便携版 Python 安装 pip 失败。"
}

function Install-PortablePython {
    New-Directory -Path $RuntimeRoot
    New-Directory -Path $DownloadsRoot

    $versions = @("3.12.10", "3.12.9", "3.12.8", "3.11.11", "3.11.10")
    $downloadedZip = $null
    foreach ($version in $versions) {
        $zipName = "python-$version-embed-amd64.zip"
        $zipPath = Join-Path $DownloadsRoot $zipName
        $url = "https://www.python.org/ftp/python/$version/$zipName"
        try {
            Write-Step "未检测到系统 Python，尝试下载便携版 Python $version ..."
            Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing
            $downloadedZip = $zipPath
            break
        } catch {
            if (Test-Path -LiteralPath $zipPath) {
                Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
            }
        }
    }

    if (-not $downloadedZip) {
        throw "未检测到可用 Python，且自动下载便携版 Python 失败。"
    }

    if (Test-Path -LiteralPath $PortablePythonRoot) {
        Remove-Item -LiteralPath $PortablePythonRoot -Recurse -Force
    }
    New-Directory -Path $PortablePythonRoot
    Expand-Archive -LiteralPath $downloadedZip -DestinationPath $PortablePythonRoot -Force
    Enable-EmbeddedPythonSitePackages -PythonRoot $PortablePythonRoot
    Ensure-PortablePythonPip -PythonExe $PortablePythonExe
    Set-Content -LiteralPath $PortablePythonState -Value (Get-Date).ToString("s") -Encoding Ascii
    return $PortablePythonExe
}

function Get-BridgePython {
    if (Test-Path -LiteralPath $VenvPythonExe) {
        return $VenvPythonExe
    }

    $systemPython = Get-SystemPython
    if ($systemPython) {
        Write-Step "检测到系统 Python，正在创建项目虚拟环境 ..."
        try {
            Invoke-ExternalCommand -FilePath $systemPython -Arguments @("-m", "venv", $VenvRoot) -ErrorMessage "创建项目虚拟环境失败。"
            return $VenvPythonExe
        } catch {
            Write-Step "系统 Python 虚拟环境初始化失败，回退到项目内便携版 Python。"
        }
    }

    if ((Test-Path -LiteralPath $PortablePythonExe) -and (Test-Path -LiteralPath $PortablePythonState)) {
        return $PortablePythonExe
    }
    return Install-PortablePython
}

function Ensure-Requirements {
    param([Parameter(Mandatory = $true)][string]$PythonExe)

    New-Directory -Path $RuntimeRoot
    $requirementsHash = (Get-FileHash -LiteralPath $RequirementsPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $needsInstall = $true

    if (Test-Path -LiteralPath $RequirementsState) {
        $savedHash = (Get-Content -LiteralPath $RequirementsState -Raw).Trim().ToLowerInvariant()
        if ($savedHash -eq $requirementsHash) {
            & $PythonExe -c "import yt_dlp, PIL" *> $null
            if ($LASTEXITCODE -eq 0) {
                $needsInstall = $false
            }
        }
    }

    if (-not $needsInstall) {
        return
    }

    Write-Step "正在安装或更新 Python 依赖 ..."
    & $PythonExe -m pip --version *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "当前 Python 环境没有可用的 pip。"
    }

    Invoke-ExternalCommand -FilePath $PythonExe -Arguments @("-m", "pip", "install", "--disable-pip-version-check", "-r", $RequirementsPath) -ErrorMessage "安装 requirements.txt 失败。"
    Set-Content -LiteralPath $RequirementsState -Value $requirementsHash -Encoding Ascii
}

function Ensure-LocalFfmpeg {
    if (Test-Path -LiteralPath $LocalFfmpegExe) {
        return $LocalFfmpegExe
    }

    New-Directory -Path $RuntimeRoot
    New-Directory -Path $DownloadsRoot
    $archiveCandidates = @(
        "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
        "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"
    )
    $archivePath = $null
    foreach ($url in $archiveCandidates) {
        $candidateArchive = Join-Path $DownloadsRoot (Split-Path -Path $url -Leaf)
        try {
            Write-Step "正在下载 ffmpeg ..."
            Invoke-WebRequest -Uri $url -OutFile $candidateArchive -UseBasicParsing
            $archivePath = $candidateArchive
            break
        } catch {
            if (Test-Path -LiteralPath $candidateArchive) {
                Remove-Item -LiteralPath $candidateArchive -Force -ErrorAction SilentlyContinue
            }
        }
    }

    if (-not $archivePath) {
        Write-Step "ffmpeg 自动下载失败，将继续尝试使用系统里已有的 ffmpeg。"
        return $null
    }

    $extractRoot = Join-Path $RuntimeRoot "ffmpeg-extract"
    if (Test-Path -LiteralPath $extractRoot) {
        Remove-Item -LiteralPath $extractRoot -Recurse -Force
    }
    New-Directory -Path $extractRoot
    Expand-Archive -LiteralPath $archivePath -DestinationPath $extractRoot -Force
    $found = Get-ChildItem -Path $extractRoot -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
    if (-not $found) {
        throw "ffmpeg 压缩包已下载，但未找到 ffmpeg.exe。"
    }

    New-Directory -Path $LocalFfmpegRoot
    Copy-Item -LiteralPath $found.FullName -Destination $LocalFfmpegExe -Force
    Remove-Item -LiteralPath $extractRoot -Recurse -Force -ErrorAction SilentlyContinue
    return $LocalFfmpegExe
}

New-Directory -Path $RuntimeRoot

$BridgePython = Get-BridgePython
Ensure-Requirements -PythonExe $BridgePython
$resolvedFfmpeg = Ensure-LocalFfmpeg
if ($resolvedFfmpeg) {
    $env:SCREEN_CASTING_FFMPEG = $resolvedFfmpeg
}

Write-Step "Bridge 即将启动。油猴里保持默认地址 http://127.0.0.1:$Port 即可。"
Invoke-ExternalCommand -FilePath $BridgePython -Arguments @((Join-Path $ProjectRoot "main.py"), "--bridge", "--host", $Host, "--port", "$Port") -ErrorMessage "启动本地投屏 bridge 失败。"
