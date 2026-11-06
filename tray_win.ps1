# tray_win.ps1: icone do Baton na bandeja do Windows.
# Conversa com o Baton em 127.0.0.1 (mesma API do painel): pausar/retomar, escolher a apresentacao,
# mostrar o QR code, abrir o painel e sair. Iniciado pelo baton.py no modo residente.
param([int]$Porta = 8765)
$ErrorActionPreference = "Continue"
trap { Write-Output ("erro: " + $_.Exception.Message + " @ " + $_.InvocationInfo.PositionMessage); continue }
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$base = "http://127.0.0.1:$Porta"
$script:estado = $null
$script:falhas = 0

function Get-Estado {
    try { return Invoke-RestMethod -Uri "$base/painel/estado" -TimeoutSec 2 } catch { return $null }
}
function Send-Acao($nome) {
    try { Invoke-RestMethod -Uri "$base/painel/acao" -Method Post -ContentType "application/json" `
            -Body (@{ acao = $nome; valor = $null } | ConvertTo-Json) -TimeoutSec 5 | Out-Null } catch {}
}
function New-Icone($cor, $pausado) {
    $bmp = New-Object System.Drawing.Bitmap 32, 32
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = "AntiAlias"
    $g.FillEllipse((New-Object System.Drawing.SolidBrush $cor), 2, 2, 28, 28)
    $branco = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::White)
    if ($pausado) {
        $g.FillRectangle($branco, 10, 9, 4, 14); $g.FillRectangle($branco, 18, 9, 4, 14)
    } else {
        $g.FillPolygon($branco, @((New-Object System.Drawing.Point 12, 9), (New-Object System.Drawing.Point 12, 23), (New-Object System.Drawing.Point 24, 16)))
    }
    $g.Dispose()
    return [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
}
$icAtivo = New-Icone ([System.Drawing.Color]::FromArgb(10, 132, 255)) $false
$icPausa = New-Icone ([System.Drawing.Color]::FromArgb(120, 120, 128)) $true
$icAviso = New-Icone ([System.Drawing.Color]::FromArgb(255, 159, 10)) $false

$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = $icAtivo
$notify.Text = "Baton"
$notify.Visible = $true
$notify.ShowBalloonTip(5000, "Baton", "Icone do Baton na bandeja. Se nao aparecer, clique na seta ^ perto do relogio e arraste-o para a barra.", "Info")
$menu = New-Object System.Windows.Forms.ContextMenuStrip
$notify.ContextMenuStrip = $menu

function Enviar-Deck($caminho) {
    $nome = [System.Uri]::EscapeDataString([System.IO.Path]::GetFileName($caminho))
    try {
        $r = Invoke-RestMethod -Uri "$base/painel/deck?nome=$nome" -Method Post -InFile $caminho `
            -ContentType "application/octet-stream" -TimeoutSec 330
        $t = if ($r.deck) { "$($r.deck.nome) ($($r.deck.paginas) paginas)" } else { "notas importadas" }
        $notify.ShowBalloonTip(4000, "Baton", "Pronto: $t", "Info")
    } catch {
        $msg = $_.ErrorDetails.Message
        $notify.ShowBalloonTip(6000, "Baton", "Falhou: $msg", "Warning")
    }
}
function Escolher-Deck {
    $dlg = New-Object System.Windows.Forms.OpenFileDialog
    $dlg.Title = "Escolher apresentacao"
    $dlg.Filter = "Apresentacoes (*.pdf;*.pptx;*.ppt;*.odp;*.md)|*.pdf;*.pptx;*.ppt;*.odp;*.md;*.txt|Todos|*.*"
    if ($dlg.ShowDialog() -eq "OK") {
        $notify.ShowBalloonTip(3000, "Baton", "Carregando a apresentacao...", "Info")
        Enviar-Deck $dlg.FileName
    }
}
function Montar-Menu($est) {
    $menu.Items.Clear()
    if ($null -eq $est) { $texto = "Baton: sem resposta"; $notify.Icon = $icAviso }
    elseif (-not $est.ativo) { $texto = "Baton: pausado"; $notify.Icon = $icPausa }
    elseif ($est.pedido) { $texto = "Baton: apresentacao detectada ($($est.pedido.app))"; $notify.Icon = $icAviso }
    elseif ($est.autorizado) { $texto = "Baton: controlando"; $notify.Icon = $icAtivo }
    else { $texto = "Baton: em espera"; $notify.Icon = $icAtivo }
    $notify.Text = $texto.Substring(0, [Math]::Min(63, $texto.Length))
    $t = $menu.Items.Add($texto); $t.Enabled = $false
    $menu.Items.Add("-") | Out-Null
    if ($null -ne $est) {
        $p = $menu.Items.Add($(if ($est.ativo) { "Pausar" } else { "Retomar" }))
        $p.add_Click({ Send-Acao $(if ($script:estado.ativo) { "pausar" } else { "retomar" }) })
    }
    $menu.Items.Add("Escolher apresentacao...").add_Click({ Escolher-Deck })
    if ($est -and $est.deck) {
        $menu.Items.Add("Remover `"$($est.deck.nome)`"").add_Click({ Send-Acao "remover_deck" })
    }
    $menu.Items.Add("-") | Out-Null
    $menu.Items.Add("Mostrar QR code").add_Click({ Start-Process "http://localhost:$Porta/" })
    $menu.Items.Add("Abrir painel").add_Click({ Start-Process "http://localhost:$Porta/painel" })
    $menu.Items.Add("-") | Out-Null
    $menu.Items.Add("Sair do Baton").add_Click({ Send-Acao "sair"; $notify.Visible = $false; [System.Windows.Forms.Application]::Exit() })
}

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 2000
$timer.add_Tick({
    $script:estado = Get-Estado
    if ($null -eq $script:estado) { $script:falhas++ } else { $script:falhas = 0 }
    if ($script:falhas -ge 3) { $notify.Visible = $false; [System.Windows.Forms.Application]::Exit(); return }
    if (-not $menu.Visible) { Montar-Menu $script:estado }
})
$script:estado = Get-Estado
Montar-Menu $script:estado
$timer.Start()
[System.Windows.Forms.Application]::Run()
$notify.Dispose()
