$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$petRecord=Join-Path $root '.run\pet.json'
if(Test-Path -LiteralPath $petRecord){
    $r=Get-Content -LiteralPath $petRecord -Raw | ConvertFrom-Json
    $p=Get-Process -Id $r.pid -ErrorAction SilentlyContinue
    if($p -and $p.Path -eq $r.executable -and [math]::Abs(($p.StartTime-[datetime]$r.started).TotalSeconds) -lt 2){Stop-Process -Id $p.Id}
    Remove-Item -LiteralPath $petRecord
}
& (Join-Path $root 'character-agent\scripts\stop.ps1')
