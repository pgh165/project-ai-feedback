# 세션 전용 환경변수 (영구 변경 아님). 작업 전에: . .\env.ps1
$root = "D:\capstone\cache"
foreach ($d in "pip", "uv", "hf", "torch", "tmp") { New-Item -ItemType Directory -Force (Join-Path $root $d) | Out-Null }
$env:PIP_CACHE_DIR = "$root\pip"
$env:UV_CACHE_DIR = "$root\uv"
$env:HF_HOME = "$root\hf"
$env:TORCH_HOME = "$root\torch"
$env:TEMP = "$root\tmp"
$env:TMP = "$root\tmp"
Write-Host "D드라이브 캐시/임시 경로 설정됨: $root (이 세션에만 적용)"
