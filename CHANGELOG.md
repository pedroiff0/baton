# Changelog

## Não lançado
- Modo residente (`--residente`): em segundo plano, detecta apresentação em tela cheia (macOS, Windows, Linux X11) e pergunta no celular antes de controlar; sem aceitar, nenhuma tecla é enviada.
- Pausa e retomada (`--pausar`, `--retomar`), painel local `/painel`, apps lembrados (sempre/nunca) e modo (perguntar, sempre, nunca).
- Instaladores `install.sh`/`uninstall.sh` e `install.ps1`/`uninstall.ps1`; o início automático agora usa o modo residente.
- Projeto renomeado de passador para Baton (o cache de `~/.cache/passador` é migrado).
- Windows e Linux: backend de teclas e cursor do Windows (user32), camada Tk (`overlay_tk.py`) e Gtk (`overlay_gtk.py`), som por `winsound`/`paplay`, `--instalar` na inicialização do Windows e no autostart do Linux.
- Android: ícones 192 e 512 para instalar a página como app.
- Cortina (preto, branco, teatro, fim com texto e confete), confete avulso e efeitos sonoros sintetizados (ding, tambor, aplausos, fanfarra, buzina, suspense), painel "Extras" no celular.
- Correção: o Preto no modo externo agora usa a cortina nativa em vez da tecla `B`, que não fazia nada na maioria dos apps.
- Cronômetro com meta, prévia do próximo slide e "Ir para" (grade de miniaturas ou número direto).
- Personalização salva em disco: tamanho e cor do laser, raio, desfoque e escurecimento do foco, espessura da caneta e do marca-texto, opacidade do marca-texto.
- Camada nativa acima de modos de apresentação (nível máximo da janela, painel sem ativação).
- Modo tela: PDF no navegador do Mac com transições, notas do apresentador, zoom, passos dentro de um slide, laser, foco, caneta e marca-texto.
- Modo externo (padrão sem PDF): controla qualquer app com teclas e mouse; camada nativa `overlay.swift` com laser, foco com desfoque, caneta e marca-texto.
- QR code, token persistente, app na tela de início do iPhone, `--instalar` no login do macOS.
- Conexão estável: batimento, reconexão, comandos idempotentes.
