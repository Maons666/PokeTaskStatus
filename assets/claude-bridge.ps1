param(
    [ValidateSet('hook', 'statusline')][string]$Mode = 'hook',
    [Parameter(Mandatory=$true)][string]$DataDir
)
# PokeTaskStatus bridge schema 1. No prompts, tool input, output, or credentials
# are persisted. All errors are swallowed so this observer cannot block Claude.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$observed = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
$mutex = $null
$locked = $false
$temporary = $null

function Get-Percent($value) {
    if ($null -eq $value -or $value -is [bool] -or $value -is [string]) { return $null }
    try {
        $number = [double]$value
        if (-not [double]::IsNaN($number) -and -not [double]::IsInfinity($number) -and $number -ge 0 -and $number -le 100) { return $number }
    } catch {}
    return $null
}

function Get-Reset($value) {
    if ($null -eq $value -or $value -is [bool] -or $value -is [string]) { return $null }
    try {
        $number = [double]$value
        if (-not [double]::IsNaN($number) -and -not [double]::IsInfinity($number) -and $number -gt 0 -and $number -le 32503680000) { return $number }
    } catch {}
    return $null
}

try {
    if ([IO.File]::Exists((Join-Path (Join-Path $DataDir 'bridge') '.disabled'))) { exit 0 }
    [Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
    $raw = [Console]::In.ReadToEnd()
    if ($raw.Length -gt 16777216) { exit 0 }
    $payload = $raw | ConvertFrom-Json
    if ($payload.session_id -isnot [string] -or $payload.session_id.Length -lt 1 -or $payload.session_id.Length -gt 256) { exit 0 }
    if ($payload.agent_id) { exit 0 }
    $sessionId = $payload.session_id
    $snapshot = [ordered]@{
        schema = 1
        provider = 'claude'
        kind = $Mode
        session_id = $sessionId
        observed_at = $observed
    }
    if ($payload.cwd -is [string] -and $payload.cwd.Length -le 4096) { $snapshot.cwd = $payload.cwd }
    if ($Mode -eq 'hook') {
        $allowed = @('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'PostToolUseFailure', 'PermissionRequest', 'Notification', 'Stop', 'StopFailure', 'SessionEnd')
        if ($payload.hook_event_name -isnot [string] -or $payload.hook_event_name -notin $allowed) { exit 0 }
        $snapshot.event = $payload.hook_event_name
        if ($payload.hook_event_name -eq 'PostToolUseFailure') { $snapshot.is_interrupt = ($payload.is_interrupt -eq $true) }
        if ($payload.hook_event_name -eq 'Notification') {
            # Explicit input requests map to asking; idle remains idle. Input
            # resolution notifications let the observer resume working promptly.
            $notifications = @('permission_prompt', 'idle_prompt', 'elicitation_dialog', 'elicitation_url_dialog', 'agent_needs_input', 'elicitation_complete', 'elicitation_response')
            if ($payload.notification_type -notin $notifications) { exit 0 }
            $snapshot.notification_type = $payload.notification_type
        }
    } else {
        $snapshot.context_percent = Get-Percent $payload.context_window.used_percentage
        $windows = @()
        # Unicode labels are assembled explicitly so Windows PowerShell 5.1
        # handles this BOM-less UTF-8 source consistently on every system locale.
        $fiveHour = '5 ' + [char]0x5c0f + [char]0x65f6
        $sevenDay = '7 ' + [char]0x5929
        foreach ($entry in @(@('five_hour', $fiveHour), @('seven_day', $sevenDay))) {
            $window = $payload.rate_limits.($entry[0])
            $percent = Get-Percent $window.used_percentage
            if ($null -ne $percent) {
                $windows += [ordered]@{ label=$entry[1]; used_percent=$percent; resets_at=(Get-Reset $window.resets_at) }
            }
        }
        $snapshot.quota = @($windows)
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $digest = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($sessionId)) } finally { $sha.Dispose() }
    $hash = ([BitConverter]::ToString($digest)).Replace('-', '').ToLowerInvariant()
    $bridgeDir = Join-Path ([IO.Path]::GetFullPath($DataDir)) 'bridge'
    [IO.Directory]::CreateDirectory($bridgeDir) | Out-Null
    $destination = Join-Path $bridgeDir ($hash + '.' + $Mode + '.json')
    $mutex = [Threading.Mutex]::new($false, ('Local\PokeTaskStatus_' + $hash + '_' + $Mode))
    try { $locked = $mutex.WaitOne(2000) } catch [Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { exit 0 }
    $prior = $null
    if ([IO.File]::Exists($destination)) {
        try {
            $prior = [IO.File]::ReadAllText($destination) | ConvertFrom-Json
            if ($prior.observed_at -gt $observed) { exit 0 }
        } catch {}
    }
    if ($Mode -eq 'hook') {
        # Retain only turn identifiers and times, never the submitted prompt.
        # This allows one Stop confirmation to be correlated with the log even
        # when a whole subsequent turn occurs between desktop polling ticks.
        $turnId = $null
        $turnStarted = 0.0
        if ($prior.turn_id -is [string] -and $prior.turn_id.Length -le 512) {
            $turnId = $prior.turn_id
            if ($prior.turn_started_at -is [ValueType]) { $turnStarted = Get-Reset $prior.turn_started_at }
        }
        $promptId = $null
        if ($payload.prompt_id -is [string] -and $payload.prompt_id.Length -gt 0 -and $payload.prompt_id.Length -le 256) {
            $promptId = 'prompt:' + $payload.prompt_id
        }
        if ($snapshot.event -eq 'UserPromptSubmit') {
            $turnId = if ($promptId) { $promptId } else { 'claude-hook:' + [Guid]::NewGuid().ToString('N') }
            $turnStarted = $observed
        } elseif ($promptId -and $promptId -ne $turnId) {
            $turnId = $promptId
            $turnStarted = if ($snapshot.event -eq 'Stop') { 0.0 } else { $observed }
        }
        if ($turnId) {
            $snapshot.turn_id = $turnId
            $snapshot.turn_started_at = $turnStarted
        }
        if ($prior.completion_id -is [string] -and $prior.completion_id.Length -le 512) {
            $snapshot.completion_id = $prior.completion_id
            $snapshot.completed_at = Get-Reset $prior.completed_at
            $snapshot.completion_turn_started_at = Get-Reset $prior.completion_turn_started_at
        }
        if ($snapshot.event -eq 'Stop') {
            $completedId = if ($turnId) { $turnId } else { 'claude-hook:unidentified:' + $sessionId }
            if ($snapshot.completion_id -ne $completedId) {
                $snapshot.completion_id = $completedId
                $snapshot.completed_at = $observed
                $snapshot.completion_turn_started_at = $turnStarted
            }
        }
    }
    $temporary = Join-Path $bridgeDir ($hash + '.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $json = $snapshot | ConvertTo-Json -Depth 5 -Compress
    [IO.File]::WriteAllText($temporary, $json, [Text.UTF8Encoding]::new($false))
    if ([IO.File]::Exists($destination)) {
        [IO.File]::Replace($temporary, $destination, [NullString]::Value)
    } else {
        [IO.File]::Move($temporary, $destination)
    }
    $temporary = $null
    if ($Mode -eq 'statusline') {
        $display = @('PokeTask')
        if ($null -ne $snapshot.context_percent) { $display += ('context ' + $snapshot.context_percent + '%') }
        if ($windows.Count -gt 0) { $display += ('quota ' + $windows[0].used_percent + '% used') }
        [Console]::WriteLine(($display -join ' | '))
    }
} catch {
    # Deliberately do not write the input or exception: either may contain text.
} finally {
    if ($temporary -and [IO.File]::Exists($temporary)) { try { [IO.File]::Delete($temporary) } catch {} }
    if ($locked) { try { $mutex.ReleaseMutex() } catch {} }
    if ($mutex) { $mutex.Dispose() }
}
exit 0
