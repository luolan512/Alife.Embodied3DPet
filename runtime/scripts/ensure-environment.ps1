param([string]$PythonExe='',[switch]$CheckOnly)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'python-environment.ps1')
function Get-MissingEnvironment {
    $missing=New-Object System.Collections.Generic.List[string]
    $selection=Resolve-PetPython $PythonExe
    if(-not $selection){$missing.Add('Python environment')}else{
        # Keep inherited settings from another Python installation out of this check.
        $oldHome=$env:PYTHONHOME;$oldPath=$env:PYTHONPATH
        try{
            Set-PetPythonEnvironment $selection
            & $selection.executable (Join-Path $PSScriptRoot 'check-python.py') | ForEach-Object {Write-Host $_}
            if($LASTEXITCODE -ne 0){$missing.Add('Python dependencies')}
        }catch{$missing.Add('Python dependencies')}
        finally{$env:PYTHONHOME=$oldHome;$env:PYTHONPATH=$oldPath}
    }
    foreach($relative in @('character-agent\dist\desktop\CharacterDesktopPet.exe','character-agent\dist\web\pet.html','kimodo.cpp\build\cpu\kmd-encode-server.exe')){
        if(-not(Test-Path -LiteralPath (Join-Path $root $relative))){$missing.Add($relative)}
    }
    $manifest=Get-Content -LiteralPath (Join-Path $root 'ASSET-MANIFEST.json') -Raw | ConvertFrom-Json
    foreach($asset in $manifest.assets){
        if($asset.path -like 'models/ardy/ARDY-Core-RP-20FPS-Horizon40/*' -or $asset.path -in @('kimodo.cpp/Llama-3-Kimodo-Q4_K.gguf','kimodo.cpp/tokenizer.gguf')){
            $file=Get-Item -LiteralPath (Join-Path $root $asset.path) -ErrorAction SilentlyContinue
            if(-not $file -or $file.Length -ne $asset.bytes){$missing.Add($asset.path)}
        }
    }
    $desktopRuntime=$false
    if(Get-Command dotnet -ErrorAction SilentlyContinue){$desktopRuntime=[bool](@(& dotnet --list-runtimes) -match '^Microsoft.WindowsDesktop.App 8\.')}
    if(-not $desktopRuntime){$missing.Add('.NET 8 Desktop Runtime')}
    if(-not(Test-Path -LiteralPath (Join-Path $env:WINDIR 'System32\msvcp140.dll'))){$missing.Add('VC++ Runtime')}
    $webview=$false
    foreach($key in @('HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}','HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}')){
        if((Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue).pv){$webview=$true}
    }
    if(-not $webview){$missing.Add('WebView2 Runtime')}
    return $missing.ToArray()
}
Write-Host 'Checking desktop pet environment...'
$missing=@(Get-MissingEnvironment)
if($CheckOnly){
    [pscustomobject]@{ready=($missing.Count -eq 0);missing=$missing} | ConvertTo-Json
    if($missing.Count){throw 'Environment is incomplete.'}
    return
}
if($missing.Count){
    throw ('Missing components: '+($missing -join ', ')+'. Run install.cmd first. Startup does not install or download anything.')
}
Write-Host 'Environment ready.'
