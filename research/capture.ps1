<#
.SYNOPSIS
  Record Bluetooth traffic of the TP-Link USB adapter (only that device) with USBPcap.

.DESCRIPTION
  USBPcapCMD re-launches itself elevated in the background (UAC prompt) and keeps
  writing until it is stopped, so recording is a start / stop pair of commands.

.EXAMPLE
  ./capture.ps1 01-baseline     # start: writes captures/01-baseline.pcap
  ./capture.ps1 -Stop           # stop the running capture
#>
param(
    [Parameter(Position = 0)][string]$Name,
    [switch]$Stop,
    # Root hub and device address of the TP-Link adapter (USBPcapCMD --extcap-config).
    [string]$Hub = "\\.\USBPcap1",
    [string]$Device = "2"
)

$usbpcap = "C:\Program Files\USBPcap\USBPcapCMD.exe"
$outDir = Join-Path $PSScriptRoot "captures"

if ($Stop) {
    $running = Get-Process USBPcapCMD -ErrorAction SilentlyContinue
    if (-not $running) {
        Write-Host "No capture is running."
        return
    }
    # The capture process is elevated, so stopping it needs elevation too.
    Start-Process taskkill -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList @("/F", "/IM", "USBPcapCMD.exe")
    Start-Sleep -Milliseconds 500
    if (Get-Process USBPcapCMD -ErrorAction SilentlyContinue) {
        throw "USBPcapCMD is still running. End USBPcapCMD.exe in Task Manager (Details tab)."
    }
    Write-Host "Capture stopped."
    Get-ChildItem $outDir -Filter *.pcap | Sort-Object LastWriteTime | Select-Object -Last 1 |
        ForEach-Object { Write-Host ("Last file: {0} ({1} KB)" -f $_.Name, [math]::Round($_.Length / 1KB)) }
    return
}

if (-not $Name) { throw "Give a capture name, e.g. ./capture.ps1 01-baseline" }
if (Get-Process USBPcapCMD -ErrorAction SilentlyContinue) {
    throw "A capture is already running; stop it first with ./capture.ps1 -Stop"
}
New-Item -ItemType Directory -Force $outDir | Out-Null
$out = Join-Path $outDir "$Name.pcap"
if (Test-Path $out) { throw "$out already exists; choose another name or delete it" }

# Descriptors let Wireshark recognise the adapter as Bluetooth HCI and decode L2CAP/HID.
Start-Process $usbpcap -WindowStyle Hidden -ArgumentList @(
    "-d", $Hub, "--devices", $Device, "--inject-descriptors", "-s", "65535", "-o", "`"$out`"")
Write-Host "Recording to $out (confirm the UAC prompt)."
Write-Host "Turn the controller ON now, do the scenario, then run: ./capture.ps1 -Stop"
