param(
    [string]$Distro = "Ubuntu",
    [string]$Model = "Qwen/Qwen2.5-3B-Instruct",
    # $VllmHost avoids collision with PowerShell's built-in read-only $Host variable.
    [string]$VllmHost = "127.0.0.1",
    [int]$Port = 8000,
    [string]$VenvPath = "~/.vetinari/amw-wsl",
    [string]$NativeModelsDir = (Join-Path $PSScriptRoot "models\native"),
    [ValidateSet("sha256", "sha256_cbor", "xxhash", "xxhash_cbor")]
    [string]$PrefixCachingHashAlgo = "sha256",
    [switch]$DisablePrefixCaching,
    [switch]$EnableFlashInferSampler,
    [string]$CacheSalt = $env:VETINARI_VLLM_CACHE_SALT,
    [int]$StartupTimeoutSeconds = 180,
    [switch]$ForceRestart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($StartupTimeoutSeconds -le 0) {
    throw "StartupTimeoutSeconds must be greater than zero."
}

function Test-VllmEndpoint {
    param([string]$Endpoint)

    try {
        $response = Invoke-WebRequest -Uri "$Endpoint/v1/models" -UseBasicParsing -TimeoutSec 5
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Convert-ToWslPath {
    param([string]$PathValue)

    if ($PathValue -match '^[A-Za-z]:\\') {
        $drive = $PathValue.Substring(0, 1).ToLowerInvariant()
        $rest = $PathValue.Substring(2).Replace('\', '/')
        return "/mnt/$drive$rest"
    }

    return $PathValue.Replace('\', '/')
}

function Quote-ForBash {
    param([string]$Value)
    $replacement = @'
'"'"'
'@.Trim()
    return "'" + $Value.Replace("'", $replacement) + "'"
}

function Invoke-WslBash {
    param([string]$Script)

    $quotedScript = Quote-ForBash -Value $Script
    & wsl -d $Distro bash -lc $quotedScript
    return $LASTEXITCODE
}

$endpointHost = if ($VllmHost -eq "0.0.0.0") { "localhost" } else { $VllmHost }
$endpoint = "http://$endpointHost`:$Port"
$nativeModelsDirForEnv = $NativeModelsDir
if (-not (Test-Path -LiteralPath $nativeModelsDirForEnv)) {
    $nativeModelsDirForEnv = ""
}

$modelTarget = $Model
if (Test-Path -LiteralPath $Model) {
    $modelTarget = Convert-ToWslPath -PathValue (Resolve-Path -LiteralPath $Model).Path
}
elseif ($Model -match '^[A-Za-z]:\\') {
    $modelTarget = Convert-ToWslPath -PathValue $Model
}

$resolvedVenvPath = $VenvPath.Replace('\', '/')
if ($resolvedVenvPath -match '^~(/|$)') {
    $resolvedVenvPath = "`$HOME" + $resolvedVenvPath.Substring(1)
}

$bashModel = Quote-ForBash -Value $modelTarget
$bashHost = Quote-ForBash -Value $VllmHost
$bashVenv = if ($resolvedVenvPath.StartsWith("`$HOME")) { '"' + $resolvedVenvPath + '"' } else { Quote-ForBash -Value $resolvedVenvPath }
$bashPrefixCachingHashAlgo = Quote-ForBash -Value $PrefixCachingHashAlgo
$forceRestartFlag = if ($ForceRestart) { "1" } else { "0" }
$prefixCachingEnabledFlag = if ($DisablePrefixCaching) { "0" } else { "1" }
$flashInferSamplerFlag = if ($EnableFlashInferSampler) { "1" } else { "0" }

$bashScript = @"
set -e
CACHE_DIR="`$HOME/.cache/vetinari"
PID_FILE="`$CACHE_DIR/vllm-$Port.pid"
LOG_FILE="`$CACHE_DIR/vllm-$Port.log"
MARKER="--port $Port"
FORCE_RESTART="$forceRestartFlag"
MODEL=$bashModel
HOST=$bashHost
VENV=$bashVenv
PREFIX_CACHING_ENABLED="$prefixCachingEnabledFlag"
PREFIX_CACHING_HASH_ALGO=$bashPrefixCachingHashAlgo
FLASHINFER_SAMPLER="$flashInferSamplerFlag"

mkdir -p "`$CACHE_DIR"

find_existing_pid() {
  if [ -f "`$PID_FILE" ]; then
    pid=`$(cat "`$PID_FILE" 2>/dev/null || true)
    if [ -n "`$pid" ] && kill -0 "`$pid" 2>/dev/null; then
      echo "`$pid"
      return 0
    fi
    rm -f "`$PID_FILE"
  fi

  pgrep -f -- "`$MARKER" | head -n 1 || true
}

stop_existing() {
  pid=`$(find_existing_pid)
  if [ -z "`$pid" ]; then
    rm -f "`$PID_FILE"
    return 0
  fi

  kill "`$pid" 2>/dev/null || true
  for _ in {1..15}; do
    if ! kill -0 "`$pid" 2>/dev/null; then
      rm -f "`$PID_FILE"
      return 0
    fi
    sleep 1
  done

  kill -9 "`$pid" 2>/dev/null || true
  sleep 1
  if kill -0 "`$pid" 2>/dev/null; then
    echo "Failed to stop existing vLLM process `$pid on port $Port" >&2
    return 1
  fi
  rm -f "`$PID_FILE"
}

if [ "`$FORCE_RESTART" = "1" ]; then
  stop_existing
fi

existing_pid=`$(find_existing_pid)
if [ -n "`$existing_pid" ]; then
  echo "`$existing_pid" > "`$PID_FILE"
  echo "Adopted existing vLLM process on port $Port with pid `$existing_pid"
  echo "Log file: `$LOG_FILE"
  exit 0
fi

if [ ! -f "`$VENV/bin/activate" ]; then
  echo "vLLM virtualenv not found at `$VENV" >&2
  exit 2
fi

. "`$VENV/bin/activate"
CUDA_ROOT=`$(find "`$VENV/lib" -type d -path "*/site-packages/nvidia/*/bin" -print -quit 2>/dev/null | xargs -r dirname)
if [ -n "`$CUDA_ROOT" ] && [ -d "`$CUDA_ROOT/bin" ]; then
  export CUDA_HOME="`$CUDA_ROOT"
  export CUDA_PATH="`$CUDA_ROOT"
  export PATH="`$VENV/bin:`$CUDA_ROOT/bin:`$PATH"
else
  export PATH="`$VENV/bin:`$PATH"
fi
export VLLM_USE_FLASHINFER_SAMPLER="`$FLASHINFER_SAMPLER"

if [ "`$PREFIX_CACHING_ENABLED" = "1" ]; then
  PREFIX_CACHE_ARGS="--enable-prefix-caching --prefix-caching-hash-algo `$PREFIX_CACHING_HASH_ALGO"
else
  PREFIX_CACHE_ARGS="--no-enable-prefix-caching"
fi

nohup vllm serve "`$MODEL" --host "`$HOST" --port $Port `$PREFIX_CACHE_ARGS > "`$LOG_FILE" 2>&1 &
new_pid=`$!
echo "`$new_pid" > "`$PID_FILE"
sleep 1
if ! kill -0 "`$new_pid" 2>/dev/null; then
  rm -f "`$PID_FILE"
  echo "vLLM process exited immediately; see `$LOG_FILE" >&2
  exit 1
fi
echo "Started vLLM on port $Port with pid `$new_pid"
echo "Log file: `$LOG_FILE"
"@

if ($ForceRestart -or -not (Test-VllmEndpoint -Endpoint $endpoint)) {
    $exitCode = Invoke-WslBash -Script $bashScript
    if ($exitCode -ne 0) {
        Write-Error "WSL bash script failed with exit code $exitCode -- vLLM may not have started"
        exit $exitCode
    }

    $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-VllmEndpoint -Endpoint $endpoint) {
            break
        }
        Start-Sleep -Seconds 2
    }
}

if (-not (Test-VllmEndpoint -Endpoint $endpoint)) {
    $cleanupScript = @"
set -e
PID_FILE="`$HOME/.cache/vetinari/vllm-$Port.pid"
if [ -f "`$PID_FILE" ]; then
  pid=`$(cat "`$PID_FILE" 2>/dev/null || true)
  if [ -n "`$pid" ]; then
    kill "`$pid" 2>/dev/null || true
    sleep 1
    if kill -0 "`$pid" 2>/dev/null; then
      kill -9 "`$pid" 2>/dev/null || true
    fi
  fi
  if [ -z "`$pid" ] || ! kill -0 "`$pid" 2>/dev/null; then
    rm -f "`$PID_FILE"
  fi
fi
"@
    Invoke-WslBash -Script $cleanupScript | Out-Null
    Write-Error "vLLM endpoint did not become healthy at $endpoint within $StartupTimeoutSeconds seconds"
    exit 1
}

$env:VETINARI_VLLM_ENDPOINT = $endpoint
$env:VETINARI_VLLM_PREFIX_CACHING_ENABLED = if ($DisablePrefixCaching) { "false" } else { "true" }
$env:VETINARI_VLLM_PREFIX_CACHING_HASH_ALGO = $PrefixCachingHashAlgo
$env:VETINARI_VLLM_FLASHINFER_SAMPLER = if ($EnableFlashInferSampler) { "true" } else { "false" }
if ($CacheSalt) {
    $env:VETINARI_VLLM_CACHE_SALT = $CacheSalt
}
if ($nativeModelsDirForEnv) {
    $env:VETINARI_NATIVE_MODELS_DIR = $nativeModelsDirForEnv
}

Write-Host "vLLM endpoint: $env:VETINARI_VLLM_ENDPOINT"
Write-Host "vLLM prefix caching: $env:VETINARI_VLLM_PREFIX_CACHING_ENABLED ($env:VETINARI_VLLM_PREFIX_CACHING_HASH_ALGO)"
Write-Host "vLLM FlashInfer sampler: $env:VETINARI_VLLM_FLASHINFER_SAMPLER"
if ($env:VETINARI_NATIVE_MODELS_DIR) {
    Write-Host "Native models: $env:VETINARI_NATIVE_MODELS_DIR"
}
Write-Host "Health check: $endpoint/v1/models"
