param(
    [string]$Distro = "Ubuntu",
    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($Distro)) {
    throw "Distro must be a non-empty WSL distribution name."
}

function Quote-ForBash {
    param([string]$Value)
    $replacement = @'
'"'"'
'@.Trim()
    return "'" + $Value.Replace("'", $replacement) + "'"
}

$bashPid = Quote-ForBash -Value "~/.cache/vetinari/vllm-$Port.pid"

# Single-quoted here-string keeps $(...) as Bash syntax — PowerShell must not
# expand it before wsl sees it.  pkill is intentionally removed: the PID file
# provides the definitive process identity, so we kill only that PID and avoid
# matching unrelated processes that happen to share the port number string.
$bashScript = @'
set +e
PIDFILE=PIDFILE_PLACEHOLDER
if [ -f "$PIDFILE" ]; then
  pid=$(cat "$PIDFILE")
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    echo "Stopped vLLM pid $pid on port PORT_PLACEHOLDER"
  fi
  rm -f "$PIDFILE"
else
  echo "No PID file found for port PORT_PLACEHOLDER — vLLM may not be running"
fi
'@

# Substitute the port-specific values after choosing single-quoted heredoc
$pidFilePath = "~/.cache/vetinari/vllm-$Port.pid"
$bashScript = $bashScript.Replace("PIDFILE_PLACEHOLDER", $pidFilePath).Replace("PORT_PLACEHOLDER", $Port)

$quotedBashScript = Quote-ForBash -Value $bashScript
& wsl -d $Distro bash -lc $quotedBashScript | Write-Host
if ($LASTEXITCODE -ne 0) {
    throw "WSL bash script failed with exit code $LASTEXITCODE while stopping vLLM on port $Port."
}
