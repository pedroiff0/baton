# Baton

Seu celular (iPhone ou Android) como passador de slides, via Tailscale: controla qualquer app no Mac, Windows ou Linux, com laser, foco, caneta, marca-texto, cortina, notas do apresentador e zoom. Só biblioteca padrão do Python 3.8+; para desenhar sobre os apps há uma camada nativa por sistema (Swift no macOS, Tk no Windows, Gtk no Linux).

## Início rápido

```
python3 baton.py                  # modo passador USB: o celular controla o app em primeiro plano
python3 baton.py slides.pdf       # modo tela: o Baton mostra o PDF (transições, notas, zoom, passos)
python3 baton.py --instalar       # liga sozinho no login (launchd, Inicializar do Windows ou autostart); --desinstalar remove
```

No Windows use `py baton.py ...` (ou `python`).

```
```

Ao iniciar, o computador abre uma página com um **QR code**. Escaneie com o celular. No iPhone, **Compartilhar > Adicionar à Tela de Início**; no Android (Chrome), menu **⋮ > Instalar app** ou **Adicionar à tela inicial**. O token fica salvo em `~/.cache/baton/token` (`--novo-token` troca), então a página do celular continua valendo depois de reiniciar.

## Instalar e rodar em segundo plano

```
./install.sh        # macOS e Linux (sem sudo): copia para ~/.local/share/baton, cria o comando `baton` e liga no login
.\install.ps1       # Windows (PowerShell, sem administrador): copia para %LOCALAPPDATA%\Baton e liga no login
./uninstall.sh      # ou .\uninstall.ps1: remove tudo
```

Instalado, o Baton roda no **modo residente** (`--residente`): fica em espera, sem enviar nenhuma tecla, e vigia a janela em primeiro plano a cada 2 s. Quando um programa abre **em tela cheia** (PowerPoint, Keynote, Impress, leitor de PDF...), ele pergunta **no celular** (se a página estiver aberta): **Controlar**, **Sempre neste app**, **Agora não** ou **Nunca neste app**. Se o celular não estiver conectado, mostra um aviso do sistema com o endereço. Ao sair da tela cheia, volta à espera. Apps de vídeo (VLC, IINA, QuickTime, mpv, Spotify...) são ignorados. No celular, "Controlar mesmo assim" libera o controle sem apresentação detectada.

**Desativar** a qualquer momento, sem desinstalar:
- `baton --pausar` e `baton --retomar` (ou `python baton.py --pausar`);
- o **painel** em `http://localhost:8765/painel` (só abre no próprio computador): pausar/retomar, modo (perguntar, sempre, nunca), apps lembrados, iniciar ou não com o sistema e sair do programa;
- `./uninstall.sh` remove o início automático.

**Permissões no macOS com o nome certo:** o `./install.sh` monta `~/Applications/Baton.app` (um launcher pequeno em Swift, `launcher.swift`) e o início automático passa a rodar por ele. Assim a lista de **Acessibilidade** e **Automação** mostra **Baton**, e não `python3.x`. Ele pede a Acessibilidade ao iniciar; se não aparecer, use **+** e escolha `~/Applications/Baton.app`. Se você já tinha liberado o `python3.x`, pode remover essa entrada. O app não é recompilado nas próximas instalações, para a permissão não se perder.

Pausado, o celular recebe "Baton pausado" e nenhum comando, laser ou desenho é aceito. As escolhas ficam em `~/.cache/baton/estado.json`. A detecção usa `osascript` (macOS, pede permissão de Automação para o Terminal/Python na primeira vez), a API do Windows (janela em primeiro plano cobrindo o monitor) e `xdotool` + `xprop` (Linux, pacote `x11-utils`). Teste com `baton --detectar`. Ainda sem ícone na barra de menu/bandeja e sem binários prontos: precisa do Python 3.

## Plataformas

| Computador que projeta | Teclas e mouse | Camada (laser, foco, caneta, cortina) | Som | Observações |
| :-- | :-- | :-- | :-- | :-- |
| macOS | CoreGraphics (ctypes) | `overlay.swift` (compilado na 1a execução, `xcode-select --install`), com desfoque real | `afplay` | Liberar o Terminal/Python em **Acessibilidade** |
| Windows 10/11 | user32 (ctypes) | `overlay_tk.py` (Tk, vem com o Python do python.org) | `winsound` | Foco só escurece (sem desfoque); marca-texto e borda do foco usam padrão pontilhado |
| Linux (X11 ou XWayland) | `xdotool` | `overlay_gtk.py` (PyGObject + Gtk 3 + pycairo, compositor ligado); sem Gtk cai no Tk | `paplay`, `aplay` ou `ffplay` | Foco só escurece. Wayland nativo não deixa um programa enviar teclas: use sessão X11 |

O celular pode ser **iPhone ou Android** (e qualquer navegador): o controle é uma página web, sem app. Em todos os casos, Tailscale instalado no computador e no celular.

Estado dos testes: o servidor, o controle web e as camadas Gtk e Tk (desenho) foram testados no Linux; o macOS (Swift) e o Windows (teclas, cliques atravessando a janela, transparência) dependem de teste na máquina real.

## Modo externo (padrão sem PDF)
O celular envia setas, Home e End para o app em primeiro plano (PowerPoint, Keynote, Acrobat, Preview, navegador...). O laser, o foco e os desenhos aparecem numa camada transparente nativa por cima de tudo, inclusive de apresentações em tela cheia:
- **Laser**: ponto vermelho. **Foco**: desfoca tudo, menos um círculo em volta do ponteiro; o **raio é ajustável** no celular (controle "Raio do foco" ou gesto de pinça sobre a miniatura) e fica salvo.
- **Caneta** e **Marca** (marca-texto), 5 cores, e **Limpar**. Trocar de slide limpa os desenhos sozinho.
- `--tela N` escolhe a tela do projetor; `--sem-overlay` usa só o cursor do mouse como laser; `--cursor` move o cursor junto com a camada; `--proporcao 16:9` define a área do laser quando não há PDF (o padrão é o formato da tela).
- Se a camada não subir (sem `swiftc`, Gtk ou compositor), o Baton avisa e segue com o laser como cursor (e só o Preto, pela tecla `B` do leitor).

## Personalização (botão "Ajustes" no celular)
Tudo é salvo em `~/.cache/baton/config.json` e vale em todos os modos: **tamanho e cor do laser**; **foco**: raio, intensidade do desfoque e escurecimento fora do círculo; **caneta**: espessura; **marca-texto**: espessura e opacidade. "Restaurar padrão" volta aos valores originais. Sobre outros apps, o desfoque usa o efeito nativo do macOS (a intensidade controla a opacidade dele); no modo tela, o desfoque é real e proporcional.

## Modo tela (com PDF)
O baton mostra o PDF no navegador do Mac (`F` tela cheia, setas/espaço/clique navegam, `B` preto, `W` branco, `T` teatro, `E` fim, `C` limpar, `Q` QR code):
- **Transições**: fade, deslizar, zoom (`--transicao`, ou o seletor no celular); sem transição entre os passos de um mesmo slide.
- **Passos dentro de um slide**: com `elementos=progressivo` na classe `iffslides` (ou `\pause`, `\item<2->`), o Baton agrupa as páginas do mesmo slide e mostra "slide 5 / 33  passo 2/4".
- **Cronômetro**: começa no primeiro avanço (ou toque no relógio), mostra o tempo decorrido e quanto resta da meta (padrão 50 min, ajustável em Ajustes); amarelo nos últimos 10% e vermelho ao estourar; sincronizado pelo servidor, sobrevive a recarregar a página.
- **Prévia do próximo** slide (ou do próximo passo) ao lado do cronômetro, e **Ir para**: grade de miniaturas dos slides (modo tela) ou número direto.
- **Notas do apresentador** no iPhone, com A−/A+: `--notas slides.notas.md` (ou `slides.notas.md` ao lado do PDF). Formato: `## 5` = slide 5, `## p12` = página 12 do PDF, texto livre com `**negrito**`, `*itálico*` e listas com `-`. Exemplo: `exemplo.notas.md` (slides de e-lixo).
- **Zoom**: toque ou arraste na miniatura, botões − e +, ou pinça; `1x` volta.
- Todas as ferramentas do modo externo, desenhadas sobre o slide.

## Cortina, fim e efeitos sonoros (botão "Extras" no celular)
- **Cortina:** Preto, Branco, Teatro (panos vermelhos que fecham e abrem) e Fim (tela de encerramento com o texto que você digitar e confete). Tocar de novo abre a cortina. No modo externo ela é desenhada pela camada nativa, por cima do app; sem a camada, só o Preto funciona (tecla `B` do leitor).
- **Confete:** chuva de confete por cima do slide, por 4 segundos.
- **Efeitos:** Ding, Tambor, Aplausos, Fanfarra, Buzina e Suspense, sintetizados pelo próprio baton (sem arquivos) e tocados no computador que projeta, por `afplay` (macOS), `winsound` (Windows), `paplay`, `aplay` ou `ffplay` (Linux). "Parar som" interrompe o efeito.

## Conexão estável
Batimento do servidor a cada 4 s; o celular reabre a conexão sozinho (silêncio de 12 s, volta para a tela, volta a rede); cada comando tem um id e é reenviado até 5 vezes sem pular dois slides; TCP keepalive e `TCP_NODELAY`. Dica: no Mac, `caffeinate -dis python3 baton.py` evita a suspensão; no Windows e no Linux, desative a suspensão durante a apresentação.

## Segurança
O servidor escuta em `127.0.0.1` e no IP do Tailscale, nunca na internet. O celular precisa do token (`k`); o Mac local não. Quem tem a URL com o token controla o Mac pelo Tailscale: não compartilhe o QR.

## Desenvolvimento
```
python3 -m unittest discover -s tests -v
swiftc -O overlay.swift -o overlay        # macOS: compilar o helper à mão
BATON_BACKEND=log BATON_OVERLAY=log python3 baton.py --sem-abrir   # testa sem mexer no Mac
```
Estrutura: `baton.py` (servidor, páginas web e modo externo), `overlay.swift` (camada macOS), `overlay_tk.py` (Windows), `overlay_gtk.py` (Linux), `tests/`. O pdf.js e o gerador de QR são baixados para `~/.cache/baton` na primeira execução.

## Projetos parecidos
- [mobslide](https://github.com/thewh1teagle/mobslide) (Tauri, macOS e Windows, QR code, PowerPoint/Keynote/Slides): só avança e volta.
- [DeckTap](https://github.com/Rico00121/decktap) (Node, MIT) e [presento](https://github.com/alxrm/presento) (Go, MIT): controle por setas pela rede local.
- [DeckRocket](https://github.com/jpsim/DeckRocket) (MIT): iPhone como controle do Deckset.
- [Projecteur](https://github.com/gbin/Projecteur) (MIT): laser virtual no Linux para o Logitech Spotlight.
- [ScreenPen](https://github.com/rsusik/screenpen) (GPL-3.0): anotação direta na tela.

O baton junta o que cada um faz separado: controle pelo celular via Tailscale (funciona fora da mesma rede Wi-Fi), laser, foco com desfoque de raio ajustável, caneta e marca-texto sobre qualquer app, notas do apresentador no celular, zoom e passos dentro de um slide.

## Licença
MIT (ver `LICENSE`). O pdf.js (Apache-2.0) e o qrcode-generator (MIT) são baixados na primeira execução e não fazem parte deste repositório.
