# passador

Seu celular como passador de slides, via Tailscale: controla qualquer app no Mac, com laser, foco, caneta, marca-texto, notas do apresentador e zoom. Só biblioteca padrão do Python 3.8+, mais um helper nativo em Swift para desenhar sobre qualquer app.

## Início rápido

```
python3 passador.py                  # modo passador USB: o celular controla o app em primeiro plano
python3 passador.py slides.pdf       # modo tela: o passador mostra o PDF (transições, notas, zoom, passos)
python3 passador.py --instalar       # macOS: liga sozinho no login (modo externo); --desinstalar remove
```

Ao iniciar, o Mac abre uma página com um **QR code**. Escaneie com o iPhone; use **Compartilhar > Adicionar à Tela de Início** para virar um app. O token fica salvo em `~/.cache/passador/token` (`--novo-token` troca), então a página do celular continua valendo depois de reiniciar.

Requisitos no Mac: Tailscale no Mac e no iPhone; Python 3; para o modo externo, **Ajustes > Privacidade e Segurança > Acessibilidade** liberado para o Terminal (ou o Python); para desenhar sobre os apps, as ferramentas de linha de comando (`xcode-select --install`, o `swiftc` compila o `overlay.swift` na primeira execução).

## Modo externo (padrão sem PDF)
O celular envia setas, Home, End e `B` para o app em primeiro plano (PowerPoint, Keynote, Acrobat, Preview, navegador...). O laser, o foco e os desenhos aparecem numa camada transparente nativa por cima de tudo, inclusive de apresentações em tela cheia:
- **Laser**: ponto vermelho. **Foco**: desfoca tudo, menos um círculo em volta do ponteiro; o **raio é ajustável** no celular (controle "Raio do foco" ou gesto de pinça sobre a miniatura) e fica salvo.
- **Caneta** e **Marca** (marca-texto), 5 cores, e **Limpar**. Trocar de slide limpa os desenhos sozinho.
- `--tela N` escolhe a tela do projetor; `--sem-overlay` usa só o cursor do mouse como laser; `--cursor` move o cursor junto com a camada; `--proporcao 16:9` define a área do laser quando não há PDF (o padrão é o formato da tela).
- Se o `swiftc` não existir ou a compilação falhar, o passador avisa e segue com o laser como cursor.

## Personalização (botão "Ajustes" no celular)
Tudo é salvo em `~/.cache/passador/config.json` e vale em todos os modos: **tamanho e cor do laser**; **foco**: raio, intensidade do desfoque e escurecimento fora do círculo; **caneta**: espessura; **marca-texto**: espessura e opacidade. "Restaurar padrão" volta aos valores originais. Sobre outros apps, o desfoque usa o efeito nativo do macOS (a intensidade controla a opacidade dele); no modo tela, o desfoque é real e proporcional.

## Modo tela (com PDF)
O passador mostra o PDF no navegador do Mac (`F` tela cheia, setas/espaço/clique navegam, `B` preto, `C` limpar, `Q` QR code):
- **Transições**: fade, deslizar, zoom (`--transicao`, ou o seletor no celular); sem transição entre os passos de um mesmo slide.
- **Passos dentro de um slide**: com `elementos=progressivo` na classe `iffslides` (ou `\pause`, `\item<2->`), o passador agrupa as páginas do mesmo slide e mostra "slide 5 / 33  passo 2/4".
- **Cronômetro**: começa no primeiro avanço (ou toque no relógio), mostra o tempo decorrido e quanto resta da meta (padrão 50 min, ajustável em Ajustes); amarelo nos últimos 10% e vermelho ao estourar; sincronizado pelo servidor, sobrevive a recarregar a página.
- **Prévia do próximo** slide (ou do próximo passo) ao lado do cronômetro, e **Ir para**: grade de miniaturas dos slides (modo tela) ou número direto.
- **Notas do apresentador** no iPhone, com A−/A+: `--notas slides.notas.md` (ou `slides.notas.md` ao lado do PDF). Formato: `## 5` = slide 5, `## p12` = página 12 do PDF, texto livre com `**negrito**`, `*itálico*` e listas com `-`. Exemplo: `exemplo.notas.md` (slides de e-lixo).
- **Zoom**: toque ou arraste na miniatura, botões − e +, ou pinça; `1x` volta.
- Todas as ferramentas do modo externo, desenhadas sobre o slide.

## Conexão estável
Batimento do servidor a cada 4 s; o celular reabre a conexão sozinho (silêncio de 12 s, volta para a tela, volta a rede); cada comando tem um id e é reenviado até 5 vezes sem pular dois slides; TCP keepalive e `TCP_NODELAY`. Dica: `caffeinate -dis python3 passador.py` evita a suspensão do Mac.

## Segurança
O servidor escuta em `127.0.0.1` e no IP do Tailscale, nunca na internet. O celular precisa do token (`k`); o Mac local não. Quem tem a URL com o token controla o Mac pelo Tailscale: não compartilhe o QR.

## Desenvolvimento
```
python3 -m unittest discover -s tests -v
swiftc -O overlay.swift -o overlay        # compilar o helper à mão
PASSADOR_BACKEND=log PASSADOR_OVERLAY=log python3 passador.py --sem-abrir   # testa sem mexer no Mac
```
Estrutura: `passador.py` (servidor, páginas web e modo externo), `overlay.swift` (camada nativa), `tests/`. O pdf.js e o gerador de QR são baixados para `~/.cache/passador` na primeira execução.

## Projetos parecidos
- [mobslide](https://github.com/thewh1teagle/mobslide) (Tauri, macOS e Windows, QR code, PowerPoint/Keynote/Slides): só avança e volta.
- [DeckTap](https://github.com/Rico00121/decktap) (Node, MIT) e [presento](https://github.com/alxrm/presento) (Go, MIT): controle por setas pela rede local.
- [DeckRocket](https://github.com/jpsim/DeckRocket) (MIT): iPhone como controle do Deckset.
- [Projecteur](https://github.com/gbin/Projecteur) (MIT): laser virtual no Linux para o Logitech Spotlight.
- [ScreenPen](https://github.com/rsusik/screenpen) (GPL-3.0): anotação direta na tela.

O passador junta o que cada um faz separado: controle pelo celular via Tailscale (funciona fora da mesma rede Wi-Fi), laser, foco com desfoque de raio ajustável, caneta e marca-texto sobre qualquer app, notas do apresentador no celular, zoom e passos dentro de um slide.

## Licença
MIT (ver `LICENSE`). O pdf.js (Apache-2.0) e o qrcode-generator (MIT) são baixados na primeira execução e não fazem parte deste repositório.
