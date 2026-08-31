# mailcleaner installer for Windows.
#
#   irm https://raw.githubusercontent.com/RishikeshSreekumar/gmail_cleaner/main/install.ps1 | iex
#
# Downloads the standalone mclean.exe for the latest GitHub release, verifies
# its checksum, and puts it on your PATH.
#
#   $env:MAILCLEANER_VERSION  tag to install (default: latest release)
#   $env:MAILCLEANER_BIN_DIR  where to put the command

$ErrorActionPreference = 'Stop'

$Repo = 'RishikeshSreekumar/gmail_cleaner'
$BinDir = if ($env:MAILCLEANER_BIN_DIR) { $env:MAILCLEANER_BIN_DIR } else { "$env:LOCALAPPDATA\Programs\mailcleaner" }
$Version = $env:MAILCLEANER_VERSION

# Only an x64 build is published; Windows on ARM runs it under emulation.
$Arch = switch ($env:PROCESSOR_ARCHITECTURE) {
    'AMD64' { 'x86_64' }
    'ARM64' { 'x86_64' }
    default { throw "Unsupported architecture: $env:PROCESSOR_ARCHITECTURE" }
}

if (-not $Version) {
    $latest = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest"
    $Version = $latest.tag_name
}
if (-not $Version) { throw 'Could not find the latest release; set $env:MAILCLEANER_VERSION' }

Write-Host "Installing mailcleaner $Version for windows/$Arch..."

$asset = "mclean-$Version-windows-$Arch.zip"
$base = "https://github.com/$Repo/releases/download/$Version"
$tmp = New-Item -ItemType Directory -Path (Join-Path $env:TEMP ("mailcleaner-" + [guid]::NewGuid()))

try {
    Invoke-WebRequest "$base/$asset" -OutFile "$tmp\$asset"
    Invoke-WebRequest "$base/checksums.txt" -OutFile "$tmp\checksums.txt"

    $expected = (Get-Content "$tmp\checksums.txt" | Select-String -Pattern "\s$([regex]::Escape($asset))$").ToString().Split()[0]
    $actual = (Get-FileHash "$tmp\$asset" -Algorithm SHA256).Hash.ToLower()
    if ($expected -ne $actual) { throw "Checksum mismatch for $asset" }

    Expand-Archive "$tmp\$asset" -DestinationPath $tmp -Force
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    Copy-Item "$tmp\mclean.exe" (Join-Path $BinDir 'mclean.exe') -Force
    # gclean is the pre-rename name, kept so old shells and docs keep working.
    Copy-Item "$tmp\mclean.exe" (Join-Path $BinDir 'gclean.exe') -Force
} finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath -notlike "*$BinDir*") {
    [Environment]::SetEnvironmentVariable('Path', "$userPath;$BinDir", 'User')
    Write-Host "Added $BinDir to your PATH. Open a new terminal to pick it up."
}

Write-Host ""
Write-Host "Installed mclean to $BinDir"
Write-Host ""
Write-Host "Next:  mclean add      # connect a mailbox"
Write-Host "       mclean sync     # build the local index"
Write-Host "       mclean          # open the dashboard"
