param([int]$CommunityId = 95098598, [ValidateSet('all','donut')][string]$Filter = 'all')
$ErrorActionPreference = 'Stop'
$targets = @(41120, 30861)
$found = @{}
$seen = [Collections.Generic.HashSet[long]]::new()
$secret = Read-Host 'VK application service key (wall.get)' -AsSecureString
$credential = [pscredential]::new('vk', $secret)
$offset = 0
$total = $null
$missingId = 0
$otherOwner = 0
$duplicates = 0
try {
    do {
        $body = @{
            owner_id = -$CommunityId
            offset = $offset
            count = 100
            filter = $Filter
            v = '5.199'
            access_token = $credential.GetNetworkCredential().Password
        }
        $reply = Invoke-RestMethod -Method Post -Uri 'https://api.vk.com/method/wall.get' -Body $body -TimeoutSec 30
        if ($reply.error) { throw "VK $($reply.error.error_code): $($reply.error.error_msg)" }
        if ($null -eq $reply.response.items) { throw 'Unexpected API response.' }
        if ($null -eq $total) {
            $total = [int]$reply.response.count
            "Filter: $Filter | VK count: $total"
        }
        $items = @($reply.response.items)
        if ($items.Count -eq 0) { break }
        foreach ($post in $items) {
            if ($null -eq $post.id -or $null -eq $post.owner_id) { $missingId++; continue }
            if ($post.owner_id -ne -$CommunityId) { $otherOwner++; continue }
            if (-not $seen.Add([long]$post.id)) { $duplicates++; continue }
            if ($targets -notcontains $post.id -or $found.ContainsKey([string]$post.id)) { continue }
            $found[[string]$post.id] = $true
            "FOUND wall$($post.owner_id)_$($post.id)"
            foreach ($source in @($post) + @($post.copy_history)) {
                if ($null -eq $source) { continue }
                $texts = @([string]$source.text)
                foreach ($a in $source.attachments) {
                    $obj = $a.($a.type)
                    if ($a.type -eq 'link') { $texts += [string]$obj.url }
                    if ($null -ne $obj.id) {
                        "  attachment: $($a.type)$($obj.owner_id)_$($obj.id)"
                    } else { "  attachment: $($a.type)" }
                }
                $decoded = [Net.WebUtility]::UrlDecode(($texts -join ' '))
                [regex]::Matches($decoded, '(?i)(?:doc|video)-?\d+_\d+') |
                    ForEach-Object { $_.Value } | Sort-Object -Unique |
                    ForEach-Object { "  linked ID: $_" }
            }
        }
        $offset += $items.Count
        Write-Progress -Activity 'Reading VK wall' -Status "$offset / $total" -PercentComplete ([Math]::Min(100, 100 * $offset / [Math]::Max(1,$total)))
        if ($found.Count -eq $targets.Count -or $offset -ge $total) { break }
        Start-Sleep -Milliseconds 500
    } while ($true)
    "Read: $offset rows, $($seen.Count) unique; initial count: $total"
    "Missing ID or owner: $missingId | Other owner: $otherOwner | Duplicate: $duplicates"
    foreach ($id in $targets) {
        if (-not $found.ContainsKey([string]$id)) { "NOT RETURNED: wall-$($CommunityId)_$id" }
    }
} catch {
    "STOP at offset $offset : $($_.Exception.Message)"
} finally {
    Write-Progress -Activity 'Reading VK wall' -Completed
    $secret.Dispose()
}
