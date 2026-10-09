# Shared interpreter selection. External environments are never modified by startup.
function Resolve-PetPython([string]$PythonExe='') {
    $root=Split-Path -Parent $PSScriptRoot
    $external=$PythonExe
    if(-not $external){$external=$env:CHARACTER_PYTHON}
    $record=Join-Path $root '.run\python.json'
    if(-not $external -and (Test-Path -LiteralPath $record)){$external=(Get-Content -LiteralPath $record -Raw | ConvertFrom-Json).executable}
    if($external){
        if(-not(Test-Path -LiteralPath $external -PathType Leaf)){throw "Python executable not found: $external. Use install.cmd -PythonExe PATH to select another environment."}
        return [pscustomobject]@{executable=(Resolve-Path -LiteralPath $external).Path;external=$true}
    }
    foreach($relative in @('runtime\python\python.exe','runtime\python\Scripts\python.exe')){
        $candidate=Join-Path $root $relative
        if(Test-Path -LiteralPath $candidate){return [pscustomobject]@{executable=$candidate;external=$false}}
    }
    return $null
}
function Set-PetPythonEnvironment($Selection) {
    $root=Split-Path -Parent $PSScriptRoot
    if(-not $Selection.external -and -not(Test-Path -LiteralPath (Join-Path $root 'runtime\python\pyvenv.cfg'))){$env:PYTHONHOME=Join-Path $root 'runtime\python'}
    else{Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue}
    $env:PYTHONPATH=Join-Path $root 'ardy'
    $env:PYTHONNOUSERSITE='1'
}
