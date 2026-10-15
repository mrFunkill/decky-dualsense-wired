<#
.SYNOPSIS
  Build the plugin and push it to a Steam Deck over SSH.

.EXAMPLE
  ./deploy.ps1 -Setup -DeckHost 192.168.1.50   # one time: SSH key + writable plugin dir on the Deck
  ./deploy.ps1                                  # build, upload; Decky live-reloads the plugin
  ./deploy.ps1 -Logs                            # follow the plugin log
  ./deploy.ps1 -Zip                             # only build out/<name>-v<version>.zip
#>
param(
    [string]$DeckHost,
    [string]$DeckUser = "deck",
    [int]$Port = 22,
    [switch]$Setup,
    [switch]$Logs,
    [switch]$Zip,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$package = Get-Content (Join-Path $root "package.json") -Raw | ConvertFrom-Json
$name = $package.name
$version = $package.version
$configPath = Join-Path $root "deck.json"
$keyPath = Join-Path $HOME ".ssh\steamdeck_ed25519"
$remoteDir = "/home/$DeckUser/homebrew/plugins/$name"
$helper = "/etc/$name/deploy.sh"

function Get-HelperInstallCommand {
    # Root-owned helper + sudoers drop-in limited to exactly that helper.
    # "zz-" makes it sort after SteamOS's "wheel" file: the last matching rule wins.
    $sudoers = "/etc/sudoers.d/zz-$name"
    # Second rule: the audio/haptics test utility (root-owned file inside the root-owned plugin dir).
    $rules = "'$DeckUser ALL=(root) NOPASSWD: $helper' " +
        "'$DeckUser ALL=(root) NOPASSWD: /usr/bin/python3 $remoteDir/py_modules/dualsense_audio.py *'"
    return "sed -i 's/\r$//' /tmp/$name-deploy.sh && sudo install -D -o root -g root -m 0755 /tmp/$name-deploy.sh $helper && rm -f /tmp/$name-deploy.sh && " +
        "printf '%s\n' $rules | sudo tee $sudoers.tmp >/dev/null && sudo chmod 0440 $sudoers.tmp && " +
        "sudo visudo -cf $sudoers.tmp && sudo mv $sudoers.tmp $sudoers"
}

# --- connection settings (remembered in deck.json, which is gitignored) -------

if (Test-Path $configPath) {
    $config = Get-Content $configPath -Raw | ConvertFrom-Json
    if (-not $DeckHost) { $DeckHost = $config.host }
    if (-not $PSBoundParameters.ContainsKey("DeckUser") -and $config.user) { $DeckUser = $config.user }
    if (-not $PSBoundParameters.ContainsKey("Port") -and $config.port) { $Port = $config.port }
    $remoteDir = "/home/$DeckUser/homebrew/plugins/$name"
}

function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments, [switch]$Interactive)
    if ($Interactive) {
        # Direct console access, needed for password prompts and tail -F.
        & $Exe @Arguments
    } else {
        # Out-Host keeps tool output out of function return values.
        & $Exe @Arguments | Out-Host
    }
    if ($LASTEXITCODE -ne 0) { throw "$Exe exited with code $LASTEXITCODE" }
}

function Get-SshArgs([switch]$Tty) {
    $a = @("-p", "$Port", "-o", "StrictHostKeyChecking=accept-new")
    if (Test-Path $keyPath) { $a += @("-i", $keyPath) }
    if ($Tty) { $a += "-t" }
    return $a
}

function Invoke-Deck([string]$Command, [switch]$Tty) {
    Invoke-Native ssh ((Get-SshArgs -Tty:$Tty) + @("$DeckUser@$DeckHost", $Command)) -Interactive:$Tty
}

# --- build ------------------------------------------------------------------

function Build-Stage {
    Push-Location $root
    try {
        if (-not $SkipBuild) {
            if (-not (Test-Path "node_modules")) { Invoke-Native pnpm @("install") }
            Invoke-Native pnpm @("run", "build")
        }
        $stageRoot = Join-Path $root "out\stage"
        $stage = Join-Path $stageRoot $name
        if (Test-Path $stageRoot) { Remove-Item $stageRoot -Recurse -Force }
        New-Item -ItemType Directory -Force (Join-Path $stage "dist") | Out-Null
        Copy-Item "dist\index.js" (Join-Path $stage "dist")
        Copy-Item "py_modules" $stage -Recurse
        # Prebuilt Windows helper installed into Proton prefixes (native/helper, built with zig).
        Copy-Item "bin" $stage -Recurse
        Get-ChildItem (Join-Path $stage "py_modules") -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
        Copy-Item "main.py", "package.json", "plugin.json", "README.md", "LICENSE" $stage
        if (Test-Path "defaults") {
            Get-ChildItem "defaults" -Exclude "defaults.txt" | Copy-Item -Destination $stage -Recurse
        }
        return $stage
    } finally {
        Pop-Location
    }
}

# --- commands -----------------------------------------------------------------

if ($Zip) {
    $stage = Build-Stage
    $zipPath = Join-Path $root "out\$name-v$version.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath }
    Compress-Archive -Path $stage -DestinationPath $zipPath
    Write-Host "Created $zipPath"
    return
}

if (-not $DeckHost) {
    throw "Deck address is unknown. Run: ./deploy.ps1 -Setup -DeckHost <ip-or-hostname>"
}

if ($Setup) {
    @{ host = $DeckHost; user = $DeckUser; port = $Port } | ConvertTo-Json | Set-Content $configPath
    Write-Host "Saved connection settings to deck.json"

    if (-not (Test-Path $keyPath)) {
        New-Item -ItemType Directory -Force (Split-Path $keyPath) | Out-Null
        # Empty passphrase so deploys are non-interactive.
        Invoke-Native ssh-keygen @("-t", "ed25519", "-f", $keyPath, "-N", "", "-C", "decky-deploy")
    }
    $pub = (Get-Content "$keyPath.pub" -Raw).Trim()

    Write-Host "Installing the SSH key on the Deck (enter the 'deck' user password)..."
    Invoke-Native ssh @("-p", "$Port", "-o", "StrictHostKeyChecking=accept-new", "$DeckUser@$DeckHost",
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && (grep -qxF '$pub' ~/.ssh/authorized_keys || echo '$pub' >> ~/.ssh/authorized_keys)") -Interactive

    # Decky keeps root plugins owned by root, so updates go through a root-owned
    # helper that the deck user may run via sudo without a password.
    Write-Host "Installing $helper (sudo password may be requested)..."
    Invoke-Native scp ((Get-SshArgs | ForEach-Object { if ($_ -eq "-p") { "-P" } else { $_ } }) +
        @((Join-Path $root "tools\deck-deploy.sh"), "$DeckUser@${DeckHost}:/tmp/$name-deploy.sh"))
    Invoke-Deck (Get-HelperInstallCommand) -Tty
    Write-Host "Setup done. Now run ./deploy.ps1"
    return
}

if ($Logs) {
    Invoke-Deck "tail -n 100 -F /home/$DeckUser/homebrew/logs/$name/*.log" -Tty
    return
}

$stage = Build-Stage
$tarPath = Join-Path $root "out\$name.tar"
Invoke-Native tar @("-cf", $tarPath, "-C", $stage, ".")

Write-Host "Uploading $name v$version to $DeckUser@${DeckHost}:$remoteDir ..."
Invoke-Native scp ((Get-SshArgs | ForEach-Object { if ($_ -eq "-p") { "-P" } else { $_ } }) + @($tarPath, "$DeckUser@${DeckHost}:/tmp/$name.tar"))
# The helper copies the files in place; Decky's live reload restarts the plugin.
# Decky queues live reloads from its file watcher thread without waking its event
# loop, so poke its HTTP server to make the reload happen right away.
Invoke-Deck ("sudo -n $helper || { echo 'Deploy helper is missing, run ./deploy.ps1 -Setup' >&2; exit 1; }; " +
    "sleep 1; curl -s -o /dev/null http://127.0.0.1:1337/frontend/index.js || true")
Write-Host "Done. Decky reloads the plugin automatically."
