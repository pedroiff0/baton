# Instala o Baton para o usuario atual no Windows: copia os arquivos para %LOCALAPPDATA%\Baton,
# liga o modo residente no login e inicia agora. Desfaz com .\uninstall.ps1. Nao precisa de administrador.
$ErrorActionPreference = "Stop"
$src = Split-Path -Parent $MyInvocation.MyCommand.Path
$dest = Join-Path $env:LOCALAPPDATA "Baton"
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $py) { Write-Error "Python 3 nao encontrado. Instale em python.org (com Tcl/Tk) e rode de novo."; exit 1 }
New-Item -ItemType Directory -Force $dest | Out-Null
Copy-Item "$src\baton.py", "$src\residente.py", "$src\relay.py", "$src\monitores.py", "$src\overlay_tk.py", "$src\tray_win.ps1", "$src\LICENSE" $dest -Force
if ($env:BATON_CONVITE) { & $py "$dest\baton.py" --convite $env:BATON_CONVITE }
if (Test-Path "$src\relay.json") { Copy-Item "$src\relay.json" $dest -Force }
& $py "$dest\baton.py" --instalar
& $py "$dest\baton.py" --preparar-offline
$pyw = Join-Path (Split-Path -Parent $py) "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $py }
Start-Process $pyw -ArgumentList "`"$dest\baton.py`" --residente" -WindowStyle Hidden
Write-Host ""
Write-Host "Baton instalado em $dest e iniciado."
Write-Host "Painel (pausar, modo, apps lembrados): http://localhost:8765/painel"
Write-Host "Pausar: python `"$dest\baton.py`" --pausar     Remover: .\uninstall.ps1"
