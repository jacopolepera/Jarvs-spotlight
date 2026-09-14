[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "Medium")]
param(
    [string]$InstallRoot = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$PinnedVersion = "0.20.0"
$PinnedTag = "v2026.8.3"
$PinnedCommit = "3c27eb6234bf91b8ceee9e9071591b31e9b148cb"
$PinnedInstallerSha256 = "7A9C854DABCB7D3E5859902EA626F444196777CFCF74A6BB0508D0F063DBF161"
$OfficialRepository = "https://github.com/NousResearch/hermes-agent.git"
$OfficialInstaller = "https://raw.githubusercontent.com/NousResearch/hermes-agent/$PinnedCommit/scripts/install.ps1"
$AttestationName = ".jarvs-hermes-attestation.json"

function Resolve-TrustedGitForWindows {
    $securityModule = Join-Path $PSHOME "Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1"
    if (-not (Test-Path -LiteralPath $securityModule -PathType Leaf)) {
        throw "The system Authenticode verification module is unavailable."
    }
    Import-Module -Name $securityModule -ErrorAction Stop

    $candidates = @()
    $machineProgramFiles = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)
    $machineProgramFilesX86 = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFilesX86)
    if ($machineProgramFiles) {
        $candidates += Join-Path $machineProgramFiles "Git\cmd\git.exe"
        $candidates += Join-Path $machineProgramFiles "Git\bin\git.exe"
    }
    if ($machineProgramFilesX86) {
        $candidates += Join-Path $machineProgramFilesX86 "Git\cmd\git.exe"
    }
    $candidates = $candidates |
        Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) }

    foreach ($candidate in $candidates) {
        $resolved = (Resolve-Path -LiteralPath $candidate).Path
        $file = Get-Item -LiteralPath $resolved
        $version = $file.VersionInfo
        if ($version.FileDescription -ne "Git for Windows" -or
            $version.ProductName -ne "Git" -or
            $version.OriginalFilename -ne "git.exe") {
            continue
        }
        $signature = Microsoft.PowerShell.Security\Get-AuthenticodeSignature -LiteralPath $resolved
        if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
            -not $signature.SignerCertificate) {
            continue
        }
        return $resolved
    }

    throw "A signed Git for Windows installation under Program Files is required. PATH shims and unsigned git.exe files are not accepted."
}

function ConvertTo-RelativePosixPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BasePath,
        [Parameter(Mandatory = $true)]
        [string]$TargetPath
    )

    $baseUri = [Uri](([IO.Path]::GetFullPath($BasePath).TrimEnd('\') + '\'))
    $targetUri = [Uri][IO.Path]::GetFullPath($TargetPath)
    return [Uri]::UnescapeDataString($baseUri.MakeRelativeUri($targetUri).ToString())
}

function Write-HermesAttestation {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RuntimeRoot,
        [Parameter(Mandatory = $true)]
        [string]$PythonPath
    )

    $venvRoot = Join-Path $RuntimeRoot "venv"
    $lockPath = Join-Path $RuntimeRoot "uv.lock"
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
        throw "Installed runtime is missing uv.lock."
    }

    $resolvedRuntime = (Resolve-Path -LiteralPath $RuntimeRoot).Path
    $resolvedVenv = (Resolve-Path -LiteralPath $venvRoot).Path
    $resolvedVenvUri = [Uri]($resolvedVenv.TrimEnd('\') + '\')
    $resolvedRuntimeUri = [Uri]($resolvedRuntime.TrimEnd('\') + '\')
    if (-not $resolvedRuntimeUri.IsBaseOf($resolvedVenvUri) -or
        (Split-Path -Leaf $resolvedVenv) -ne "venv") {
        throw "Refusing to clean or attest a virtual environment outside the managed runtime."
    }

    # Bytecode is mutable runtime cache, not an installed dependency. Remove it
    # only from the verified managed venv; the adapter redirects future caches
    # into each ephemeral run home and rejects cache drift under this tree.
    $bytecodeFiles = Get-ChildItem -LiteralPath $resolvedVenv -Recurse -Force -File |
        Where-Object { $_.Extension -in @(".pyc", ".pyo") }
    foreach ($file in $bytecodeFiles) {
        Remove-Item -LiteralPath $file.FullName -Force
    }
    $cacheDirectories = Get-ChildItem -LiteralPath $resolvedVenv -Recurse -Force -Directory |
        Where-Object { $_.Name -eq "__pycache__" } |
        Sort-Object { $_.FullName.Length } -Descending
    foreach ($directory in $cacheDirectories) {
        if (($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Refusing to remove a reparse-point Python cache: $($directory.FullName)"
        }
        Remove-Item -LiteralPath $directory.FullName -Recurse -Force
    }

    $pythonInfo = & $PythonPath -I -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>&1
    if ($LASTEXITCODE -ne 0 -or @($pythonInfo).Count -ne 1) {
        throw "The managed Hermes Python could not report its version."
    }
    $pythonVersion = ("$pythonInfo").Trim()
    if ($pythonVersion -notmatch '^3\.(11|12|13)(\.\d+)?$') {
        throw "Unsupported managed Python version: $pythonVersion"
    }

    $files = [ordered]@{}
    $venvDirectories = Get-ChildItem -LiteralPath $venvRoot -Recurse -Force -Directory
    foreach ($directory in $venvDirectories) {
        if (($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "The managed Hermes environment contains a reparse-point directory: $($directory.FullName)"
        }
    }
    $venvFiles = Get-ChildItem -LiteralPath $venvRoot -Recurse -Force -File |
        Sort-Object FullName
    foreach ($file in $venvFiles) {
        if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "The managed Hermes environment contains a reparse-point file: $($file.FullName)"
        }
        $relative = ConvertTo-RelativePosixPath -BasePath $venvRoot -TargetPath $file.FullName
        $files[$relative] = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash.ToLowerInvariant()
    }
    if ($files.Count -eq 0 -or $files.Count -gt 50000) {
        throw "The managed Hermes environment has an invalid attested file count: $($files.Count)"
    }

    $pythonRelative = ConvertTo-RelativePosixPath -BasePath $RuntimeRoot -TargetPath $PythonPath
    $attestation = [ordered]@{
        schema_version = 1
        hermes_version = $PinnedVersion
        hermes_tag = $PinnedTag
        hermes_revision = $PinnedCommit
        python_executable = $pythonRelative
        python_version = $pythonVersion
        dependency_lock_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $lockPath).Hash.ToLowerInvariant()
        files = $files
    }
    $json = $attestation | ConvertTo-Json -Depth 4 -Compress
    if ([Text.Encoding]::UTF8.GetByteCount($json) -gt (8 * 1024 * 1024)) {
        throw "The Hermes environment attestation exceeds the 8 MiB adapter limit."
    }
    $attestationPath = Join-Path $RuntimeRoot $AttestationName
    if (Test-Path -LiteralPath $attestationPath) {
        $existingAttestation = Get-Item -LiteralPath $attestationPath -Force
        if (-not $existingAttestation.PSIsContainer -and
            ($existingAttestation.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) {
            # A regular previous attestation is safe to replace atomically below.
        } else {
            throw "The existing Hermes attestation is not a regular file."
        }
    }
    $utf8 = [Text.UTF8Encoding]::new($false)
    $temporaryAttestation = "$attestationPath.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        [IO.File]::WriteAllText($temporaryAttestation, $json, $utf8)
        Move-Item -LiteralPath $temporaryAttestation -Destination $attestationPath -Force
    } finally {
        if (Test-Path -LiteralPath $temporaryAttestation -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryAttestation -Force
        }
    }
}

function Invoke-CheckedGit {
    param(
        [Parameter(Mandatory = $true)]
        [string]$GitPath,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $output = & $GitPath @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        $detail = (($output | Select-Object -First 4) -join " ").Trim()
        throw "Git command failed (exit $LASTEXITCODE): $detail"
    }
    return $output
}

if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw "LOCALAPPDATA is unavailable. Pass an absolute -InstallRoot."
    }
    $InstallRoot = Join-Path $env:LOCALAPPDATA "Jarvs\HermesLab"
}

$expandedRoot = [Environment]::ExpandEnvironmentVariables($InstallRoot)
if (-not [IO.Path]::IsPathRooted($expandedRoot)) {
    throw "InstallRoot must be an absolute path."
}

$LabRoot = [IO.Path]::GetFullPath($expandedRoot)
$pathRoot = [IO.Path]::GetPathRoot($LabRoot)
if ($LabRoot.TrimEnd('\') -eq $pathRoot.TrimEnd('\')) {
    throw "Refusing to use a drive root as InstallRoot."
}

$RuntimeRoot = Join-Path $LabRoot "runtime"
$DataRoot = Join-Path $LabRoot "data"
$GitPath = Resolve-TrustedGitForWindows

if (Test-Path -LiteralPath $RuntimeRoot) {
    if (-not (Test-Path -LiteralPath (Join-Path $RuntimeRoot ".git"))) {
        throw "Runtime path already exists but is not a Git checkout: $RuntimeRoot"
    }

    $existingHead = ((Invoke-CheckedGit -GitPath $GitPath -Arguments @(
        "-C", $RuntimeRoot, "rev-parse", "HEAD"
    )) | Select-Object -First 1).Trim()
    if ($existingHead -ne $PinnedCommit -and -not $Force) {
        throw "Existing Hermes HEAD is $existingHead. Re-run with -Force only after reviewing the managed checkout."
    }

    $existingChanges = Invoke-CheckedGit -GitPath $GitPath -Arguments @(
        "-C", $RuntimeRoot, "status", "--porcelain", "--untracked-files=normal"
    )
    $unexpectedExistingChanges = @($existingChanges | Where-Object {
        $statusLine = "$_"
        -not ($statusLine.StartsWith("?? ") -and
            $statusLine.Substring(3).Trim() -eq $AttestationName)
    })
    if ($unexpectedExistingChanges) {
        throw "The managed Hermes checkout contains local changes. Review or move it before installing; this wrapper will not discard them."
    }
}

$operation = "Install pinned Hermes $PinnedVersion ($PinnedTag, $PinnedCommit)"
if (-not $PSCmdlet.ShouldProcess($LabRoot, $operation)) {
    return
}

New-Item -ItemType Directory -Path $LabRoot -Force | Out-Null
New-Item -ItemType Directory -Path $DataRoot -Force | Out-Null

$installerName = "jarvs-hermes-installer-{0}.ps1" -f [Guid]::NewGuid().ToString("N")
$installerPath = Join-Path ([IO.Path]::GetTempPath()) $installerName

try {
    Write-Host "Downloading the official Hermes installer from pinned commit $PinnedCommit..."
    Invoke-WebRequest -UseBasicParsing -Uri $OfficialInstaller -OutFile $installerPath

    $installerSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $installerPath).Hash
    if ($installerSha256 -ne $PinnedInstallerSha256) {
        throw "Installer integrity check failed. Expected SHA-256 $PinnedInstallerSha256, received $installerSha256."
    }

    $systemDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::System)
    $WindowsPowerShell = Join-Path $systemDirectory "WindowsPowerShell\v1.0\powershell.exe"
    if (-not (Test-Path -LiteralPath $WindowsPowerShell -PathType Leaf)) {
        throw "The system Windows PowerShell executable is unavailable."
    }
    $installerArguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $installerPath,
        "-SkipSetup",
        "-Commit", $PinnedCommit,
        "-ForceCommit",
        "-HermesHome", $DataRoot,
        "-InstallDir", $RuntimeRoot
    )

    Write-Host "Running the verified upstream installer in the isolated lab directory..."
    $previousPath = $env:PATH
    try {
        # The upstream installer invokes `git` by name. Put the already verified
        # Git for Windows directory first so a user-writable PATH shim cannot win.
        $env:PATH = "$(Split-Path -Parent $GitPath);$previousPath"
        & $WindowsPowerShell @installerArguments
        if ($LASTEXITCODE -ne 0) {
            throw "The official Hermes installer failed with exit code $LASTEXITCODE."
        }
    } finally {
        $env:PATH = $previousPath
    }

    $installedHead = ((Invoke-CheckedGit -GitPath $GitPath -Arguments @(
        "-C", $RuntimeRoot, "rev-parse", "HEAD"
    )) | Select-Object -First 1).Trim()
    if ($installedHead -ne $PinnedCommit) {
        throw "Installed Hermes revision mismatch. Expected $PinnedCommit, received $installedHead."
    }

    $remoteUrl = ((Invoke-CheckedGit -GitPath $GitPath -Arguments @(
        "-C", $RuntimeRoot, "remote", "get-url", "origin"
    )) | Select-Object -First 1).Trim()
    $allowedRemotes = @(
        $OfficialRepository,
        "git@github.com:NousResearch/hermes-agent.git"
    )
    if ($allowedRemotes -notcontains $remoteUrl) {
        throw "Installed checkout has an unexpected origin: $remoteUrl"
    }

    $tagRef = "refs/tags/" + $PinnedTag
    $peeledTagRef = $tagRef + "^{}"
    $tagLines = Invoke-CheckedGit -GitPath $GitPath -Arguments @(
        "-C", $RuntimeRoot, "ls-remote", "--tags", "origin", $tagRef, $peeledTagRef
    )
    $tagMatchesCommit = @($tagLines | Where-Object {
        ("$_").StartsWith($PinnedCommit + "`t")
    }).Count -gt 0
    if (-not $tagMatchesCommit) {
        throw "Official tag $PinnedTag did not resolve to the pinned commit."
    }

    $projectFile = Join-Path $RuntimeRoot "pyproject.toml"
    if (-not (Test-Path -LiteralPath $projectFile -PathType Leaf)) {
        throw "Installed runtime is missing pyproject.toml."
    }
    $projectText = Get-Content -LiteralPath $projectFile -Raw
    $versionMatch = [regex]::Match($projectText, '(?m)^version\s*=\s*"([^"]+)"')
    if (-not $versionMatch.Success -or $versionMatch.Groups[1].Value -ne $PinnedVersion) {
        throw "Installed Hermes package version does not match $PinnedVersion."
    }

    $gatewayEntry = Join-Path $RuntimeRoot "tui_gateway\entry.py"
    $pythonPath = Join-Path $RuntimeRoot "venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $gatewayEntry -PathType Leaf)) {
        throw "Installed runtime is missing tui_gateway\entry.py."
    }
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        throw "Installed runtime is missing venv\Scripts\python.exe."
    }

    Write-Host "Creating an exact local attestation for the managed Python environment..."
    Write-HermesAttestation -RuntimeRoot $RuntimeRoot -PythonPath $pythonPath

    $installedChanges = Invoke-CheckedGit -GitPath $GitPath -Arguments @(
        "-C", $RuntimeRoot, "status", "--porcelain", "--untracked-files=normal"
    )
    $unexpectedChanges = @($installedChanges | Where-Object {
        $statusLine = "$_"
        -not ($statusLine.StartsWith("?? ") -and
            $statusLine.Substring(3).Trim() -eq $AttestationName)
    })
    if ($unexpectedChanges) {
        throw "The installed Hermes checkout is not clean; refusing to report it as verified."
    }

    Write-Host ""
    Write-Host "Hermes installation verified." -ForegroundColor Green
    Write-Host "  Version:      $PinnedVersion"
    Write-Host "  Tag:          $PinnedTag"
    Write-Host "  Commit:       $installedHead"
    Write-Host "  Runtime path: $RuntimeRoot"
    Write-Host "  Data path:    $DataRoot"
    Write-Host ""
    Write-Host "Enter the runtime and data paths in Jarvs Settings > Hermes specialist, then enable it."
} finally {
    if (Test-Path -LiteralPath $installerPath -PathType Leaf) {
        Remove-Item -LiteralPath $installerPath -Force
    }
}
