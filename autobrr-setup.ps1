# ==============================================================================
# autobrr Windows Auto-Installer & Startup Setup
# ==============================================================================

# 1. Enforce Administrator Privileges
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Warning "You must run this script as Administrator. Please right-click PowerShell and select 'Run as Administrator'."
    exit
}

# 2. Define Paths
$InstallDir = "C:\autobrr"
$TaskName = "autobrr_background_service"

# 3. Create Installation Directory
if (-not (Test-Path $InstallDir)) {
    Write-Host "Creating directory at $InstallDir..."
    New-Item -Path $InstallDir -ItemType Directory | Out-Null
}

# 4. Fetch the latest release metadata from GitHub
Write-Host "Querying GitHub for the latest autobrr release..."
$ApiUrl = "https://api.github.com/repos/autobrr/autobrr/releases/latest"
$Release = Invoke-RestMethod -Uri $ApiUrl

# Find the Windows x86_64 asset (checks for both .zip and .tar.gz)
$Asset = $Release.assets | Where-Object { $_.name -match "windows_x86_64" }
if (-not $Asset) {
    Write-Error "Could not find a valid Windows x86_64 release."
    exit
}

$DownloadUrl = $Asset[0].browser_download_url
$FileName = $Asset[0].name
$FilePath = "$env:TEMP\$FileName"

# 5. Download the release archive
Write-Host "Downloading $FileName..."
Invoke-WebRequest -Uri $DownloadUrl -OutFile $FilePath

# 6. Extract the archive to the installation folder
Write-Host "Extracting files to $InstallDir..."
if ($FileName -match "\.zip$") {
    Expand-Archive -Path $FilePath -DestinationPath $InstallDir -Force
} elseif ($FileName -match "\.tar\.gz$") {
    # Windows 10/11 natively supports the tar command
    Push-Location $InstallDir
    tar -xzf $FilePath
    Pop-Location
}

# Cleanup the downloaded archive
Remove-Item $FilePath

# 7. Create a Scheduled Task for Startup Automation
Write-Host "Configuring Windows Task Scheduler to run autobrr on boot..."

# The Action tells it where to run from, and specifies the config directory explicitly
$Action = New-ScheduledTaskAction -Execute "$InstallDir\autobrr.exe" `
                                  -WorkingDirectory $InstallDir `
                                  -Argument "--config $InstallDir"

# The Trigger tells it to start when the system boots
$Trigger = New-ScheduledTaskTrigger -AtStartup

# Running as SYSTEM ensures it runs completely invisibly in the background without requiring user login
$Principal = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest

# Ensure the task runs indefinitely (by default Windows kills tasks after 3 days) and runs on battery
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                                         -DontStopIfGoingOnBatteries `
                                         -StartWhenAvailable `
                                         -DontStopOnIdleEnd `
                                         -ExecutionTimeLimit (New-TimeSpan -Days 0)

# Register the task in Windows
Register-ScheduledTask -TaskName $TaskName `
                       -Action $Action `
                       -Trigger $Trigger `
                       -Principal $Principal `
                       -Settings $Settings `
                       -Force | Out-Null

# 8. Start the service right now
Write-Host "Starting autobrr service..."
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3

# 9. Verify and Output
$TaskStatus = (Get-ScheduledTask -TaskName $TaskName).State
if ($TaskStatus -eq "Running") {
    Write-Host "`n=====================================================================" -ForegroundColor Green
    Write-Host " SUCCESS! autobrr is now installed and running in the background." -ForegroundColor Green
    Write-Host "=====================================================================" -ForegroundColor Green
    Write-Host "Installation Path: $InstallDir"
    Write-Host "Config Location:   $InstallDir\config.toml"
    Write-Host "Web Interface:     http://localhost:7474"
    Write-Host "`nYou can now open your browser and navigate to the Web Interface to finish the setup."
} else {
    Write-Warning "The script finished, but the task doesn't seem to be running. Check Task Scheduler manually."
}