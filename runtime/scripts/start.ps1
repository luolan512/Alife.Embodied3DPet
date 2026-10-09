param([int]$Port=18196,[string]$Model='ikoma-miru',[switch]$Fullscreen,[switch]$ShowSettings,[switch]$Debug,[switch]$Validate,[switch]$ServerOnly,[string]$PythonExe='',[switch]$CheckOnly,[switch]$NoFace)
$ErrorActionPreference='Stop'
$bundle=Split-Path -Parent $PSScriptRoot
if($Port -lt 1024 -or $Port -gt 65534){throw 'Port must be 1024..65534 (HTTP plus WebSocket).'}
if($Model -notmatch '^[a-zA-Z0-9_-]+$'){throw 'Model ID must contain only letters, digits, underscore or hyphen.'}
& (Join-Path $PSScriptRoot 'ensure-environment.ps1') -PythonExe $PythonExe -CheckOnly:$CheckOnly
if($CheckOnly){return}
. (Join-Path $PSScriptRoot 'python-environment.ps1')
$selection=Resolve-PetPython $PythonExe
$project=Join-Path $bundle 'character-agent'
$python=$selection.executable
$exe=Join-Path $project 'dist\desktop\CharacterDesktopPet.exe'
foreach($file in @($python,$exe,(Join-Path $project 'dist\web\pet.html'),(Join-Path $bundle 'kimodo.cpp\Llama-3-Kimodo-Q4_K.gguf'),(Join-Path $bundle 'kimodo.cpp\tokenizer.gguf'))){
    if(-not(Test-Path -LiteralPath $file)){throw "Missing file: $file. See README.md for setup."}
}

if(-not(Test-Path -LiteralPath (Join-Path $project "data\models\$Model.vrm"))){
    $models=@(Get-ChildItem -LiteralPath (Join-Path $project 'data\models') -Filter '*.vrm' -ErrorAction SilentlyContinue)
    if(-not $models.Count){throw 'Put at least one VRM file in character-agent/data/models/.'}
    $Model=$models[0].BaseName
}
$run=Join-Path $bundle '.run'
New-Item -ItemType Directory -Force -Path $run,(Join-Path $project '.run') | Out-Null
Set-PetPythonEnvironment $selection
$env:CHARACTER_BODY_LLM='0'
$env:PYTHONUNBUFFERED='1'
$env:PYTHONIOENCODING='utf-8'
$env:CHARACTER_PORT="$Port"
$env:CHECKPOINTS_DIR=Join-Path $bundle 'models\ardy'
$env:KIMODO_TEXT_BUNDLE=Join-Path $bundle 'kimodo.cpp\Llama-3-Kimodo-Q4_K.gguf'
$env:KIMODO_ENCODE_SERVER=Join-Path $bundle 'kimodo.cpp\build\cpu\kmd-encode-server.exe'
$env:ARDY_LLM_CONFIG=Join-Path $project 'config\llm.json'
$faceAssets=@('cteg-model.chkpt','bert-tokenizer\vocab.txt','EmoAva\src\data\generic_model.pkl','EmoAva\dataset\stage1_mean.npy')
$missingFace=@($faceAssets | Where-Object {-not(Test-Path -LiteralPath (Join-Path $project ".cache\face-audit\$_"))})
$env:CHARACTER_FACE_ENABLED=if($NoFace -or $missingFace.Count){'0'}else{'1'}
if($missingFace.Count){Write-Host 'CTEG assets are not installed; dynamic face generation is disabled. See README.'}
$env:HF_HUB_OFFLINE='1'
$env:TRANSFORMERS_OFFLINE='1'
$env:TEMP=Join-Path $run 'temp'
$env:TMP=$env:TEMP
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
$pidFile=Join-Path $project '.run\server.json'
try{$health=Invoke-RestMethod "http://127.0.0.1:$Port/api/v1/health" -TimeoutSec 1}catch{$health=$null}
if($health){
    if(-not(Test-Path -LiteralPath $pidFile)){throw "Port $Port is already used. Choose a different -Port."}
    $saved=Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
    $existing=Get-CimInstance Win32_Process -Filter "ProcessId=$($health.pid)"
    if($health.service -ne 'character-agent' -or $saved.pid -ne $health.pid -or -not $existing -or $existing.ExecutablePath -ne $python){throw 'Existing service does not belong to this bundle.'}
} else {
    foreach($p in @($Port,($Port+1))){if(Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue){throw "Port $p is occupied."}}
    $env:CHARACTER_RUN_ID=(Get-Date -Format 'yyyyMMdd-HHmmss')+'-'+[guid]::NewGuid().ToString('N').Substring(0,8)
    $process=Start-Process -FilePath $python -ArgumentList 'apps\agent-server\main.py' -WorkingDirectory $project -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $run 'server-out.log') -RedirectStandardError (Join-Path $run 'server-error.log')
    @{pid=$process.Id;started=$process.StartTime.ToString('o');port=$Port} | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding UTF8
    for($i=0;$i -lt 120;$i++){
        Start-Sleep -Milliseconds 500
        try{$health=Invoke-RestMethod "http://127.0.0.1:$Port/api/v1/health" -TimeoutSec 1}catch{$health=$null}
        if($health -and $health.pid -eq $process.Id){break}
        if($process.HasExited){throw "Backend exited. See $run\server-error.log"}
    }
    if(-not $health){throw "Backend startup timeout. See $run\server-error.log"}
}
Write-Host "Desktop pet backend: http://127.0.0.1:$Port/"
if($ServerOnly){return}
$existingPet=Get-CimInstance Win32_Process | Where-Object {$_.ExecutablePath -eq $exe} | Select-Object -First 1
if($existingPet){
    $pet=Get-Process -Id $existingPet.ProcessId
    @{pid=$pet.Id;started=$pet.StartTime.ToString('o');executable=$exe} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $run 'pet.json') -Encoding UTF8
    Write-Host 'This bundle desktop pet is already running.'
    return
}
$url="http://127.0.0.1:$Port/pet.html?alife=1&model=$Model"
if($Fullscreen){$url+='&big-head=1'}
if($ShowSettings){$url+='&panel=open'}
$arguments=@('--project',('"'+$project+'"'),'--url',('"'+$url+'"'))
if($Debug){$arguments+='--debug'}
if($Validate){$arguments+='--validate'}
$pet=Start-Process -FilePath $exe -ArgumentList $arguments -WorkingDirectory $project -WindowStyle Hidden -PassThru
@{pid=$pet.Id;started=$pet.StartTime.ToString('o');executable=$exe} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $run 'pet.json') -Encoding UTF8
