<#
Polls the Hetzner Server Auction feed and sends a Telegram message when a
server whose CPU matches $CpuFilter shows up. Runs until Ctrl+C.
#>

$BotToken = "8960967933:AAEvh4CCjfEaf8_1EgdnNoN9RIjj1pT-j3U"
$ChatId = "6002976198"
$CpuFilter = "EPYC 7502"
$PollSeconds = 30

$ApiUrl = "https://www.hetzner.com/_resources/app/data/app/live_data_sb_EUR.json"

function Send-Telegram {
    param([string]$Token, [string]$ChatId, [string]$Text)
    $uri = "https://api.telegram.org/bot$Token/sendMessage"
    $body = @{ chat_id = $ChatId; text = $Text; parse_mode = "Markdown" } | ConvertTo-Json
    try {
        $result = Invoke-RestMethod -Uri $uri -Method Post -Body $body -ContentType "application/json; charset=utf-8" -ErrorAction Stop
        if (-not $result.ok) {
            Write-Host "  (telegram rejected message: $($result | ConvertTo-Json -Compress))"
        }
    } catch {
        Write-Host "  (telegram send failed: $_)"
    }
}

function Format-Table {
    param([array]$Servers)
    $idW = "ID".Length
    $cpuW = "CPU".Length
    $priceW = "Price".Length
    foreach ($s in $Servers) {
        if ("$($s.id)".Length -gt $idW) { $idW = "$($s.id)".Length }
        if ($s.cpu.Length -gt $cpuW) { $cpuW = $s.cpu.Length }
        $priceStr = "$($s.price) EUR/mo"
        if ($priceStr.Length -gt $priceW) { $priceW = $priceStr.Length }
    }
    $header = "{0} {1} {2}" -f ("ID".PadRight($idW)), ("CPU".PadRight($cpuW)), ("Price".PadRight($priceW))
    $lines = @($header, ("-" * $header.Length))
    foreach ($s in ($Servers | Sort-Object price)) {
        $priceStr = "$($s.price) EUR/mo"
        $lines += "{0} {1} {2}" -f ("$($s.id)".PadRight($idW)), ($s.cpu.PadRight($cpuW)), ($priceStr.PadRight($priceW))
    }
    return ($lines -join "`n")
}

$seenIds = @{}
$cpuFilterLower = $CpuFilter.ToLower()

Write-Host "Watching for CPUs containing: $CpuFilter"
Write-Host "Polling every $PollSeconds seconds`n"

while ($true) {
    try {
        $response = Invoke-RestMethod -Uri $ApiUrl -Headers @{ "User-Agent" = "Mozilla/5.0 (compatible; hetzner-auction-watch/1.0)" } -TimeoutSec 15 -ErrorAction Stop
        $servers = $response.server
    } catch {
        Write-Host "fetch failed: $_"
        Start-Sleep -Seconds $PollSeconds
        continue
    }

    $currentIds = @{}
    $newMatches = @()
    foreach ($s in $servers) {
        if ($s.cpu -and $s.cpu.ToLower().Contains($cpuFilterLower)) {
            $currentIds[$s.id] = $true
            if (-not $seenIds.ContainsKey($s.id)) {
                $newMatches += $s
            }
        }
    }

    if ($newMatches.Count -gt 0) {
        $title = "$($newMatches.Count) matching server$(if ($newMatches.Count -ne 1) { 's' }) found"
        $table = Format-Table -Servers $newMatches
        $codeBlock = '```'
        Write-Host "$title`n$table`n"
        Send-Telegram -Token $BotToken -ChatId $ChatId -Text "*$title*`n$codeBlock`n$table`n$codeBlock"
    }

    $seenIds = $currentIds
    Start-Sleep -Seconds $PollSeconds
}
