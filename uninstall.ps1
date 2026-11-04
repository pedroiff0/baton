# Remove o Baton do usuario atual no Windows: inicio automatico, processo e arquivos.
$dest = Join-Path $env:LOCALAPPDATA "Baton"
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if ((Test-Path "$dest\baton.py") -and $py) { & $py "$dest\baton.py" --desinstalar }
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*Baton*baton.py*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Remove-Item -Recurse -Force $dest -ErrorAction SilentlyContinue
Write-Host "Baton removido. Preferencias e token ficam em $env:USERPROFILE\.cache\baton (apague a pasta se quiser)."
