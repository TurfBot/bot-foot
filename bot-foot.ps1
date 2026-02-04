# =========================
# BOT FOOT - Scan 15 minutes (FREE FRIENDLY)
# =========================
# À "mi-temps" (HT ou fin de 1H), le bot :
# 1) envoie un SIGNAL si tes conditions sont réunies
# 2) sinon, si score nul à la pause, envoie "NUL HT - conditions non réunies"
# Anti-spam persistant : notified.json
# =========================

try { chcp 65001 | Out-Null } catch {}
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"

# ======= REMPLIS ICI =======
$API_KEY   = "ee6bfc898ab1b8e6c0efb14ccc219814"
$BOT_TOKEN = "8436143101:AAG9T9Z0AmFYs8js2jvWVG_3OSDBdvTjDto"
$CHAT_ID   = "902332745"
# ===========================

# 15 minutes
$POLL_SECONDS = 900

# Pour éviter de bouffer 96 req/jour, tu peux limiter les heures de fonctionnement :
# Exemple: de 16h à 02h (heure locale PC). Mets $USE_ACTIVE_HOURS = $false si tu veux H24.
$USE_ACTIVE_HOURS = $true
$ACTIVE_START_HOUR = 16
$ACTIVE_END_HOUR = 2   # si < start, ça passe minuit

$STATE_FILE = Join-Path $PSScriptRoot "notified.json"

# Header API-Sports
$headers = New-Object "System.Collections.Generic.Dictionary[[String],[String]]"
$headers.Add("x-apisports-key", $API_KEY)

function In-ActiveHours {
  if (-not $USE_ACTIVE_HOURS) { return $true }
  $h = (Get-Date).Hour
  if ($ACTIVE_START_HOUR -le $ACTIVE_END_HOUR) {
    return ($h -ge $ACTIVE_START_HOUR -and $h -lt $ACTIVE_END_HOUR)
  } else {
    # plage qui traverse minuit
    return ($h -ge $ACTIVE_START_HOUR -or $h -lt $ACTIVE_END_HOUR)
  }
}

function Load-Notified {
  if (Test-Path $STATE_FILE) {
    try {
      $raw = Get-Content $STATE_FILE -Raw
      if ($raw -and $raw.Trim() -ne "") {
        $data = $raw | ConvertFrom-Json
        if ($null -ne $data) {
          return [System.Collections.Generic.HashSet[int]]::new([int[]]$data)
        }
      }
    } catch { }
  }
  return [System.Collections.Generic.HashSet[int]]::new()
}

function Save-Notified($set) {
  try {
    if ($null -eq $set) { return }
    ($set | ForEach-Object { $_ } | ConvertTo-Json) | Set-Content -Encoding UTF8 $STATE_FILE
  } catch {
    Write-Host "[STATE ERROR] $($_.Exception.Message)"
  }
}

$notified = Load-Notified
if ($null -eq $notified) { $notified = [System.Collections.Generic.HashSet[int]]::new() }

function To-Int($v) {
  if ($null -eq $v) { return 0 }
  $s = "$v".Trim().Replace("%","")
  if ($s -eq "") { return 0 }
  try { return [int]$s } catch { return 0 }
}

function StatValue($teamObj, $name) {
  if ($null -eq $teamObj -or $null -eq $teamObj.statistics) { return $null }
  return ($teamObj.statistics | Where-Object { $_.type -eq $name } | Select-Object -First 1).value
}

function Check-Dominance($statsResponse) {
  if ($null -eq $statsResponse -or $statsResponse.Count -lt 2) { return $null }

  $a = $statsResponse[0]
  $b = $statsResponse[1]

  $aPoss = To-Int (StatValue $a "Ball Possession")
  $bPoss = To-Int (StatValue $b "Ball Possession")
  $aSOT  = To-Int (StatValue $a "Shots on Goal")
  $bSOT  = To-Int (StatValue $b "Shots on Goal")
  $aCor  = To-Int (StatValue $a "Corner Kicks")
  $bCor  = To-Int (StatValue $b "Corner Kicks")

  $aName = if ($a.team) { $a.team.name } else { "TeamA" }
  $bName = if ($b.team) { $b.team.name } else { "TeamB" }

  # TES CONDITIONS EXACTES
  if ($aPoss -ge 60 -and ($aSOT - $bSOT) -ge 4 -and $aCor -ge 4) {
    return [PSCustomObject]@{ Winner=$aName; Poss=$aPoss; SOT=$aSOT; OppSOT=$bSOT; Corners=$aCor }
  }
  if ($bPoss -ge 60 -and ($bSOT - $aSOT) -ge 4 -and $bCor -ge 4) {
    return [PSCustomObject]@{ Winner=$bName; Poss=$bPoss; SOT=$bSOT; OppSOT=$aSOT; Corners=$bCor }
  }
  return $null
}

function Send-Telegram($text) {
  $uri = "https://api.telegram.org/bot$BOT_TOKEN/sendMessage"
  $body = @{
    chat_id = $CHAT_ID
    text    = $text
    disable_web_page_preview = $true
  }
  Invoke-RestMethod -Uri $uri -Method Post -Body $body | Out-Null
}

function Is-Halftime-Window($fx) {
  # "HT" sûr
  $short = $null
  if ($fx.fixture -and $fx.fixture.status) { $short = $fx.fixture.status.short }

  # elapsed (peut être null)
  $elapsed = $null
  if ($fx.fixture -and $fx.fixture.status) { $elapsed = $fx.fixture.status.elapsed }
  $e = To-Int $elapsed

  if ($short -match '^\s*HT\s*$') { return $true }

  # Fin de 1ère mi-temps : on attrape le moment où ça passe 44-47 avant HT
  if ($short -match '^\s*1H\s*$' -and $e -ge 44) { return $true }

  return $false
}

Write-Host "Bot démarré. Scan toutes les $POLL_SECONDS secondes (15 min)."
Write-Host "État: $STATE_FILE (déjà notifiés: $($notified.Count))"
if ($USE_ACTIVE_HOURS) { Write-Host "Heures actives: $ACTIVE_START_HOUR h -> $ACTIVE_END_HOUR h" }

while ($true) {
  try {
    if (-not (In-ActiveHours)) {
      Start-Sleep -Seconds $POLL_SECONDS
      continue
    }

    $live = Invoke-RestMethod -Uri "https://v3.football.api-sports.io/fixtures?live=all" -Headers $headers
    if ($null -eq $live -or $null -eq $live.response) {
      Start-Sleep -Seconds $POLL_SECONDS
      continue
    }

    foreach ($fx in $live.response) {
      if ($null -eq $fx -or $null -eq $fx.fixture -or $null -eq $fx.fixture.id) { continue }
      $fixtureId = [int]$fx.fixture.id

      if ($notified -and $notified.Contains($fixtureId)) { continue }
      if (-not (Is-Halftime-Window $fx)) { continue }

      $home = if ($fx.teams -and $fx.teams.home) { $fx.teams.home.name } else { "" }
      $away = if ($fx.teams -and $fx.teams.away) { $fx.teams.away.name } else { "" }
      $scoreHome = 0; $scoreAway = 0
      if ($fx.goals) { $scoreHome = $fx.goals.home; $scoreAway = $fx.goals.away }

      # Récup stats (1 requête de plus)
      $stats = Invoke-RestMethod -Uri "https://v3.football.api-sports.io/fixtures/statistics?fixture=$fixtureId" -Headers $headers

      $dominant = $null
      if ($stats -and $stats.response) { $dominant = Check-Dominance $stats.response }

      if ($null -ne $dominant) {
        $msg = @"
📣 SIGNAL PAUSE : $home vs $away
Équipe dominante : $($dominant.Winner)
Possession : $($dominant.Poss)%
Tirs cadrés : $($dominant.SOT) (adv $($dominant.OppSOT))
Corners : $($dominant.Corners)
Score : $scoreHome - $scoreAway
Fixture ID : $fixtureId
"@
        Send-Telegram $msg
      }
      elseif ($scoreHome -eq $scoreAway) {
        $msg2 = @"
ℹ️ NUL À LA PAUSE : $home vs $away
Score : $scoreHome - $scoreAway
Conditions demandées NON réunies (>=60% possession, +4 tirs cadrés, >=4 corners)
Fixture ID : $fixtureId
"@
        Send-Telegram $msg2
      }

      # Dans tous les cas, on marque traité pour éviter de retomber dessus au scan suivant
      $notified.Add($fixtureId) | Out-Null
      Save-Notified $notified
    }

  } catch {
    Write-Host "⚠️ Problème réseau/API: $($_.Exception.Message)"
    Write-Host "Nouvelle tentative dans 60 secondes."
    Start-Sleep -Seconds 60
  }

  Start-Sleep -Seconds $POLL_SECONDS
}
