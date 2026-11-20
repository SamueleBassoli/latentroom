# One command, from anywhere:  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build.ps1
# lint -> tests -> exe (PyInstaller) -> exe smoke -> installer (Inno Setup 7.1.0) -> sha256 ->
# silent install into build\install-test, silent update over it, run it, silent uninstall,
# check what was kept/removed. Whatever happens, the install test then removes what it
# installed; the last setup log stays in build\install-test-setup.log.
# Exits non-zero on the first failure.
# -Isolated: the install test uses a copy of the installer with its own AppId and shortcut
# names, for a PC where Latentroom is really installed.
param([switch]$Isolated)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$env:PYTHONDONTWRITEBYTECODE = '1'
$test = Join-Path $root 'build\install-test'   # scratch: never the real %LOCALAPPDATA%\Latentroom
$env:LATENTROOM_HOME = Join-Path $test 'home'

$IsVersion = '7.1.0'
$IsUrl = 'https://github.com/jrsoftware/issrc/releases/download/is-7_1_0/innosetup-7.1.0-x64.exe'
$IsSha256 = '0362a383ed217d4c4239b5933866dd96d3eb2102737da92f80f6057a4b40df2f'
$AppId = '{C0349225-2BE4-45F1-BA50-194AA43E02C6}_is1'   # = AppId in packaging\latentroom.iss
$Name = 'Latentroom'   # the shortcuts' and Installed apps' name
if ($Isolated) { $AppId, $Name = '{115D9C91-470E-455E-BE2E-52129DBBABEF}_is1', 'LatentroomQA' }
$Guid = $AppId.Substring(1, 36)
$Uninst = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall'
$UninstKey = "$Uninst\$AppId"

function Step([string]$name, [scriptblock]$run) {
    Write-Host "== $name" -ForegroundColor Cyan
    & $run
    if ($LASTEXITCODE) { throw "$name failed (exit code $LASTEXITCODE)" }
}

function Check([bool]$ok, [string]$what) {
    if (-not $ok) { throw "install test: $what" }
    Write-Host "   ok: $what"
}

function Wait-Gone([string]$path) {
    for ($i = 0; $i -lt 60 -and (Test-Path $path); $i++) { Start-Sleep -Milliseconds 500 }
    -not (Test-Path $path)
}

# The install test's own uninstall entry and shortcuts, and build\install-test, if still there.
function Get-Leftovers {
    @($UninstKey, $test) + ($lnk | ForEach-Object { "$_\$Name.lnk" }) | Where-Object { Test-Path $_ }
}

# Runs whatever happened in the install test: its uninstaller when it is still there, then
# removes what is left. Only an entry or shortcut that points into build\install-test\app is
# removed, so a real Latentroom is never touched (also without -Isolated).
function Remove-InstallTest {
    $ErrorActionPreference = 'Continue'   # remove as much as possible; never hide the test's own error
    if (Test-Path "$app\unins000.exe") {
        Start-Process "$app\unins000.exe" -Wait -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'
        $null = Wait-Gone $app
    }
    $inApp = { param($p) "$p".StartsWith("$app\", [StringComparison]::OrdinalIgnoreCase) }
    if ((Test-Path $UninstKey) -and (& $inApp (Get-ItemProperty $UninstKey).InstallLocation)) { Remove-Item $UninstKey -Recurse -Force }
    $shell = New-Object -ComObject WScript.Shell
    foreach ($f in Get-Leftovers | Where-Object { $_ -like '*.lnk' }) {
        if (& $inApp $shell.CreateShortcut($f).TargetPath) { Remove-Item $f -Force }
    }
    if (Test-Path $test) { Remove-Item $test -Recurse -Force }
    $left = @(Get-Leftovers)
    if ($left) { Write-Warning "install test could not remove (remove by hand): $($left -join ', ')" }
}

if (Test-Path $test) { Remove-Item $test -Recurse -Force }
$ver = (Select-String -Path 'latentroom\__init__.py' -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
Write-Host "Latentroom $ver"

Step 'lint' { uv run ruff check . }
Step 'tests' { uv run pytest -q }
Step 'exe' { uv run --group build pyinstaller packaging\latentroom.spec --distpath dist --workpath build --noconfirm --log-level WARN }
$notices = 'dist\Latentroom\THIRD-PARTY-NOTICES.txt'   # licence texts of what the exe bundles
if (-not (Test-Path $notices) -or (Get-Item $notices).Length -eq 0) { throw "exe: $notices is missing or empty" }
Step 'exe smoke' { uv run python packaging\smoke.py dist\Latentroom\Latentroom.exe }

# Inno Setup: portable copy in tools\ (gitignored), never installed system-wide.
$isDir = Join-Path $root "tools\innosetup-$IsVersion"
$iscc = Join-Path $isDir 'ISCC.exe'
if (-not (Test-Path $iscc)) {
    Write-Host "== fetch Inno Setup $IsVersion" -ForegroundColor Cyan
    $null = New-Item -ItemType Directory -Force 'tools'
    $isExe = Join-Path $root "tools\innosetup-$IsVersion-x64.exe"
    if (-not (Test-Path $isExe)) { Invoke-WebRequest -Uri $IsUrl -OutFile $isExe -UseBasicParsing }
    if ((Get-FileHash $isExe -Algorithm SHA256).Hash -ne $IsSha256) {
        Remove-Item $isExe
        throw "Inno Setup download does not match the pinned sha256: deleted it, try again"
    }
    $sig = Get-AuthenticodeSignature $isExe   # publisher's documented check (jrsoftware.org/isdl-verify.php)
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'O=Pyrsys B\.V\.') {
        throw "Inno Setup installer signature is not a valid 'Pyrsys B.V.' signature"
    }
    $p = Start-Process $isExe -Wait -PassThru -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES',
        '/NORESTART', '/PORTABLE=1', '/CURRENTUSER', '/NOICONS', '/TASKS=""', "/DIR=`"$isDir`""
    if ($p.ExitCode -or -not (Test-Path $iscc)) { throw "Inno Setup portable unpack failed (exit $($p.ExitCode))" }
}

Step 'installer' { & $iscc /Q packaging\latentroom.iss }
$setup = Join-Path $root "dist\Latentroom-Setup-$ver.exe"
$hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
Set-Content -Path "$setup.sha256" -Value "$hash  Latentroom-Setup-$ver.exe" -Encoding Ascii
Write-Host "   sha256 $hash"

Write-Host '== install test' -ForegroundColor Cyan
# By AppId, and by name: a Latentroom installed with another AppId has the same shortcuts.
if (@(Get-ChildItem $Uninst -ErrorAction SilentlyContinue | Get-ItemProperty | Where-Object { $_.PSChildName -eq $AppId -or $_.DisplayName -eq $Name })) {
    throw "$Name is installed on this PC; the install test would replace it. Use -Isolated."
}
$app = Join-Path $test 'app'
$lnk = [Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('DesktopDirectory')
$setupLog = Join-Path $root 'build\install-test-setup.log'   # outside $test: kept after a failed run
try {
    $testSetup = $setup
    if ($Isolated) {  # same script, only the AppId and the names differ
        $iss = Get-Content packaging\latentroom.iss -Raw
        foreach ($r in ('C0349225-2BE4-45F1-BA50-194AA43E02C6', $Guid), ('}\Latentroom"', "}\$Name`""),
                ('UninstallDisplayName=Latentroom', "UninstallDisplayName=$Name")) {
            if (-not $iss.Contains($r[0])) { throw "-Isolated: '$($r[0])' is no longer in latentroom.iss" }
            $iss = $iss.Replace($r[0], $r[1])
        }
        Set-Content packaging\latentroom-qa.iss $iss -Encoding UTF8   # here: its ..\ paths must hold
        try { Step 'isolated installer' { & $iscc /Q "/O$test" '/Fqa-setup' packaging\latentroom-qa.iss } }
        finally { Remove-Item packaging\latentroom-qa.iss }
        $testSetup = "$test\qa-setup.exe"
    }

    $setupArgs = '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER', "/LOG=`"$setupLog`""
    $p = Start-Process $testSetup -Wait -PassThru -ArgumentList ($setupArgs + "/DIR=`"$app`"")
    Check ($p.ExitCode -eq 0 -and (Test-Path "$app\Latentroom.exe")) 'silent install'
    Check ((Get-Item "$app\Latentroom.exe").VersionInfo.ProductVersion -eq $ver) "installed exe is version $ver"
    Check ((Test-Path "$app\LICENSE") -and (Test-Path "$app\THIRD-PARTY-NOTICES.txt") -and (Get-Item "$app\THIRD-PARTY-NOTICES.txt").Length -gt 0) 'LICENSE and THIRD-PARTY-NOTICES.txt next to the exe'
    Check (Test-Path "$($lnk[0])\$Name.lnk") "Start menu shortcut $Name"

    # An update: the same installer again, no /DIR. It must go where the app is (UsePreviousAppDir)
    # and leave no program file of the version it replaces ([InstallDelete] _internal).
    Set-Content "$app\_internal\old.dll" 'x'
    $p = Start-Process $testSetup -Wait -PassThru -ArgumentList $setupArgs
    Check ($p.ExitCode -eq 0 -and (Test-Path "$app\Latentroom.exe")) 'silent update'
    Check (-not (Test-Path "$app\_internal\old.dll")) "the replaced version's program files gone"
    $entries = @(Get-ChildItem $Uninst -ErrorAction SilentlyContinue | Get-ItemProperty | Where-Object { $_.DisplayName -eq $Name })
    Check ($entries.Count -eq 1 -and $entries[0].PSChildName -eq $AppId -and "$($entries[0].InstallLocation)".TrimEnd('\') -eq $app) "one app in Installed apps: $Name, in the folder it was installed to"
    Step 'installed exe smoke' { uv run python packaging\smoke.py "$app\Latentroom.exe" }

    # A user who moved the data folder (non-ASCII name: settings.json stores it as \uXXXX) and
    # has models, an engine, own LoRAs and pictures. The app's own code writes settings.json.
    # the folder is shared with another app, whose models\ and engine\ files stay.
    $data = Join-Path $test "data-$([char]0x00E8)"
    $pics = Join-Path $test 'pictures'
    foreach ($d in 'models', 'engine', 'loras') { $null = New-Item -ItemType Directory -Force "$data\$d" }
    Set-Content "$data\models\other.gguf" 'x'; Set-Content "$data\engine\other-app.exe" 'x'
    Set-Content "$data\loras\test-style-a.safetensors" 'x'; Set-Content "$data\downloads.json" '{}'
    $null = New-Item -ItemType Directory -Force $pics; Set-Content "$pics\a-lighthouse-at-dusk.png" 'x'
    $env:LR_DATA = $data; $env:LR_PICS = $pics
    Step 'write settings.json' {
        uv run python -c "import os; from latentroom import settings as s; s.save(s.Settings(data_dir=os.environ['LR_DATA'], output_dir=os.environ['LR_PICS']))"
    }
    Step 'write Latentroom files + their list' {  # a catalogue model, an unzipped engine, the list
        uv run python -c "from latentroom.engine import catalogue as c, install as i, paths as p; m = next(f for f in c.files().values() if f.kind != 'engine'); (p.models_dir() / m.name).write_text('x'); e = p.engine_dir() / i.ENGINE_RELEASE; e.mkdir(); (e / 'sd-cli.exe').write_text('x'); i.write_own_files()"
    }

    $p = Start-Process "$app\unins000.exe" -Wait -PassThru -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES',
        '/NORESTART', '/DELETEMODELS=1'
    Check ($p.ExitCode -eq 0) 'silent uninstall'
    Check (Wait-Gone $app) 'program folder removed'
    Check (-not (Test-Path $UninstKey)) 'uninstall entry removed'
    Check (-not (Test-Path "$($lnk[0])\$Name.lnk")) 'Start menu shortcut removed'
    Check (((Get-ChildItem "$data\models").Name -join ',') -eq 'other.gguf') "Latentroom's models deleted, another app's kept (/DELETEMODELS=1)"
    Check (((Get-ChildItem "$data\engine").Name -join ',') -eq 'other-app.exe') "Latentroom's engine deleted, another app's file kept"
    Check (-not (Test-Path "$data\downloads.json") -and -not (Test-Path "$data\latentroom-files.txt")) 'download state and the list deleted with them'
    Check (Test-Path "$data\loras\test-style-a.safetensors") 'own LoRA kept'
    Check (Test-Path "$pics\a-lighthouse-at-dusk.png") 'pictures kept'
    Check (Test-Path "$env:LATENTROOM_HOME\settings.json") 'settings kept'
}
finally { Remove-InstallTest }

$exeMB = '{0:N1}' -f ((Get-ChildItem dist\Latentroom -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
$setupMB = '{0:N1}' -f ((Get-Item $setup).Length / 1MB)
Write-Host "BUILD OK  dist\Latentroom\ $exeMB MB  |  $setup $setupMB MB" -ForegroundColor Green
