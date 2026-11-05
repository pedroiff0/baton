<h3 align="center">Baton — Passador de slides pelo celular</h3>

<div align="center">

[![Status](https://img.shields.io/badge/status-active-success.svg)]()
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](/LICENSE)

</div>

---

<p align="center">O celular (iPhone ou Android) controla qualquer app de apresentação no macOS, Windows ou Linux, com laser, foco, caneta, marca-texto, cortina, notas e zoom. Somente biblioteca padrão do Python 3.8+.</p>

## 📝 Sumário

- [⚡ Guia Rápido](#guia-rapido)
- [Sobre](#sobre)
- [Instalação](#instalacao)
- [Uso](#uso)
- [Plataformas](#plataformas)
- [Segurança](#seguranca)
- [Testes](#testes)
- [Contribuição](#contribuicao)

---

## ⚡ Guia Rápido <a name="guia-rapido"></a>

```bash
python3 baton.py                # celular controla o app em primeiro plano
python3 baton.py slides.pdf     # o Baton exibe o PDF no navegador
python3 baton.py --instalar     # inicia com o sistema (--desinstalar remove)
```

No Windows use `py baton.py ...`. Ao iniciar, uma página local mostra um **QR code**: escaneie com o celular. No iPhone, **Compartilhar > Adicionar à Tela de Início**; no Android, **⋮ > Instalar app**.

---

## 🧐 Sobre <a name="sobre"></a>

- **Modo externo** (padrão, sem PDF): envia setas, Home e End ao app em primeiro plano e desenha laser, foco, caneta e marca-texto numa camada transparente por cima de tudo, inclusive em tela cheia.
- **Modo tela** (com PDF): exibe o PDF no navegador com transições, passos dentro de um slide, notas do apresentador, cronômetro com meta, prévia do próximo slide, "Ir para" e zoom.
- **Cortina e efeitos**: preto, branco, teatro, fim com confete e efeitos sonoros sintetizados.
- **Modo residente**: em segundo plano, detecta apresentação em tela cheia e pergunta no celular antes de enviar qualquer tecla. Clicar no aviso do sistema abre a página local.
- **Ajustes** salvos em disco: cor e tamanho do laser, raio e desfoque do foco, espessura da caneta e do marca-texto.

---

## 🔧 Instalação <a name="instalacao"></a>

Requisitos: Python 3.8+. Para controlar de qualquer rede, Tailscale no computador e no celular; sem ele, use `--rede` (mesmo Wi-Fi).

```bash
./install.sh        # macOS e Linux (sem sudo)
.\install.ps1       # Windows (PowerShell, sem administrador)
./uninstall.sh      # ou .\uninstall.ps1
```

Dependências opcionais:
- macOS: `swiftc` (`xcode-select --install`) para a camada de desenho; `terminal-notifier` para o aviso clicável; liberar o **Baton** em Acessibilidade.
- Linux: `xdotool` e `x11-utils` (teclas e detecção), PyGObject + Gtk 3 + pycairo (camada), `notify-send`.
- Windows: Python do python.org (inclui Tk).

---

## 🧭 Uso <a name="uso"></a>

```bash
baton --pausar | --retomar     # sem desinstalar
baton --detectar               # testa a detecção de tela cheia
python3 baton.py slides.pdf --notas slides.notas.md --transicao fade
```

Opções: `--rede` (sem Tailscale: escuta na rede local e usa o IP dela no QR code; sem Tailscale instalado isso já é automático), `--tela N` (monitor do projetor), `--sem-overlay`, `--cursor`, `--proporcao 16:9`, `--novo-token`, `--porta`, `--sem-abrir`.

Painel local (pausar, modo, apps lembrados, início com o sistema, sair): `http://localhost:8765/painel`.

**Visão do apresentador (como no PowerPoint):** passe o arquivo junto com `--externo`. O celular mostra o slide atual, o próximo, o cronômetro e as notas, e continua enviando as teclas ao app em primeiro plano:

```bash
python3 baton.py apresentacao.pptx --externo     # notas lidas do próprio .pptx (converte em PDF com o LibreOffice)
python3 baton.py slides.pdf --externo --notas slides.notas.md
```

A posição é contada pelos toques, então comece a apresentação no slide 1 e use **Ir para** se sair de sincronia (animações dentro do slide contam como avanço). Sem arquivo, o Baton não conhece o conteúdo e não há prévia nem notas.

**Notas de PDF do beamer (`\note{}`):** compile com `\setbeameroption{show notes on second screen=right}`; o PDF sai com páginas largas (slide à esquerda, notas à direita) e o Baton detecta isso sozinho: mostra só o slide na prévia e no modo tela e lê as notas da metade direita (descarta o cabeçalho da página de notas). Funciona só com PDF passado na linha de comando (`baton slides.pdf --externo`); exemplo em `exemplo-notas-beamer.tex`. Notas digitadas no celular têm prioridade sobre as do PDF.

**Notas digitadas no celular (sem arquivo):** no painel de notas, **Editar** grava a nota do slide atual e **Todas** abre um campo para colar as notas inteiras em Markdown. Ficam salvas em `~/.cache/baton/notas-sessao.json` (por apresentação; sem arquivo, numa sessão "livre") e voltam sozinhas quando o Baton reabre. Sem arquivo, o número do slide é a contagem de toques (comece no slide 1). Com arquivo, o que você digita se soma às notas dele.

**Importar notas de um arquivo (sem abrir o arquivo na apresentação):** `baton --importar-notas apresentacao.pptx` (ou `.md`) lê as notas do PowerPoint e as grava na sessão; se o Baton estiver rodando, valem na hora. No painel (`http://localhost:8765/painel`) há o mesmo em "Notas do apresentador > escolher arquivo". A importação substitui as notas da sessão; depois dá para ajustar no celular.

Notas do apresentador em Markdown: `## 5` é o slide 5, `## p12` a página 12 do PDF; aceita `**negrito**`, `*itálico*` e listas. Veja `exemplo.notas.md`.

Atalhos no modo tela: `F` tela cheia, setas/espaço avançam, `B` preto, `W` branco, `T` teatro, `E` fim, `C` limpar, `Q` QR code.

---

## 🖥️ Plataformas <a name="plataformas"></a>

| Sistema | Teclas e mouse | Camada de desenho | Som |
| :-- | :-- | :-- | :-- |
| macOS | CoreGraphics | `overlay.swift` | `afplay` |
| Windows 10/11 | user32 | `overlay_tk.py` | `winsound` |
| Linux (X11/XWayland) | `xdotool` | `overlay_gtk.py` (ou Tk) | `paplay`, `aplay`, `ffplay` |

Wayland nativo não permite enviar teclas: use sessão X11. A camada macOS e o Windows ainda dependem de teste em máquina real.

---

## 🔒 Segurança <a name="seguranca"></a>

Por padrão o servidor escuta apenas em `127.0.0.1` e no IP do Tailscale, nunca na internet. Com `--rede` (ou sem Tailscale) escuta na rede local em HTTP, sem criptografia: use só em Wi-Fi de confiança. O celular precisa do token da URL; o computador local não. Quem tem a URL com o token controla o computador: não compartilhe o QR code. Pausado, nenhum comando é aceito.

---

## 🧪 Testes <a name="testes"></a>

```bash
python3 -m unittest discover -s tests -v
BATON_BACKEND=log BATON_OVERLAY=log python3 baton.py --sem-abrir   # sem enviar teclas
```

Estrutura: `baton.py` (servidor, páginas web, modo externo), `residente.py` (modo residente), `overlay*.{swift,py}` (camadas), `launcher.swift` (`Baton.app` no macOS), `tests/`.

---

## 🤝 Contribuição <a name="contribuicao"></a>

- Abra issues/pull requests neste repositório.
- Mantenha apenas a biblioteca padrão do Python.
- Adicione testes em `tests/` para novo comportamento.

---

**Licença:** MIT. O pdf.js (Apache-2.0) e o qrcode-generator (MIT) são baixados na primeira execução e não fazem parte do repositório.
