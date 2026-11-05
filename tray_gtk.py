#!/usr/bin/env python3
"""tray_gtk.py: icone do Baton na bandeja (Linux).

Conversa com o Baton em 127.0.0.1 (mesma API do painel): pausar/retomar, escolher a apresentacao,
mostrar o QR code, abrir o painel e sair. Usa AyatanaAppIndicator3/AppIndicator3 quando existir
(no GNOME precisa da extensao "AppIndicator"), senao Gtk.StatusIcon. Sem Gtk ou sem bandeja, sai com
codigo 3 e o Baton segue funcionando sem icone.
Uso: tray_gtk.py [--porta 8765]
"""
import importlib
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

EXTENSOES = ("pdf", "pptx", "ppt", "odp", "md", "txt")


def estado(porta, timeout=2):
    """Estado do painel (dict) ou None se o Baton nao responde."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{porta}/painel/estado", timeout=timeout) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return None


def acao(porta, nome, valor=None):
    rq = urllib.request.Request(f"http://127.0.0.1:{porta}/painel/acao", method="POST",
                                data=json.dumps({"acao": nome, "valor": valor}).encode(),
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(rq, timeout=5) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return None


def enviar_deck(porta, caminho):
    """Envia o arquivo escolhido para o painel. Devolve (ok, mensagem)."""
    try:
        dados = open(caminho, "rb").read()
    except OSError as e:
        return False, f"nao consegui ler o arquivo: {e}"
    rq = urllib.request.Request(f"http://127.0.0.1:{porta}/painel/deck?nome=" + urllib.parse.quote(os.path.basename(caminho)),
                                data=dados, method="POST")
    try:
        with urllib.request.urlopen(rq, timeout=330) as r:
            j = json.loads(r.read())
        d = j.get("deck")
        return True, f"{d['nome']} ({d['paginas']} paginas)" if d else "notas importadas"
    except urllib.error.HTTPError as e:
        try:
            return False, json.loads(e.read()).get("erro", str(e))
        except ValueError:
            return False, str(e)
    except OSError as e:
        return False, str(e)


def rotulo(est):
    """Texto da linha de status e nome do icone para um estado do painel."""
    if est is None:
        return "Baton: sem resposta", "dialog-error-symbolic"
    if est.get("acessibilidade") is False:
        return "Baton: permissao de teclas pendente", "dialog-warning-symbolic"
    if not est.get("ativo"):
        return "Baton: pausado", "media-playback-pause-symbolic"
    if est.get("pedido"):
        return f"Baton: apresentacao detectada ({est['pedido'].get('app', '?')})", "dialog-question-symbolic"
    return ("Baton: controlando" if est.get("autorizado") else "Baton: em espera"), "emblem-ok-symbolic"


def main():
    porta = 8765
    if "--porta" in sys.argv:
        try:
            porta = int(sys.argv[sys.argv.index("--porta") + 1])
        except (ValueError, IndexError):
            pass
    os.environ.setdefault("GDK_BACKEND", "x11")
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import GLib, Gtk
    except Exception as e:
        print(f"tray_gtk: Gtk indisponivel ({e})", file=sys.stderr)
        return 3
    if not Gtk.init_check()[0]:  # sem sessao grafica (SSH, servidor): sem bandeja
        print("tray_gtk: sem display", file=sys.stderr)
        return 3
    indicador = None
    for nome in ("AyatanaAppIndicator3", "AppIndicator3"):
        try:
            gi.require_version(nome, "0.1")
            indicador = importlib.import_module(f"gi.repository.{nome}")
            break
        except Exception:
            continue
    menu = Gtk.Menu()
    icone = {"obj": None, "usa_indicador": False}
    if indicador:
        ind = indicador.Indicator.new("baton", "emblem-ok-symbolic", indicador.IndicatorCategory.APPLICATION_STATUS)
        ind.set_status(indicador.IndicatorStatus.ACTIVE)
        ind.set_menu(menu)
        icone.update(obj=ind, usa_indicador=True)
    else:
        si = Gtk.StatusIcon.new_from_icon_name("emblem-ok-symbolic")
        si.set_tooltip_text("Baton")
        si.connect("popup-menu", lambda i, b, t: menu.popup(None, None, Gtk.StatusIcon.position_menu, i, b, t))
        icone["obj"] = si

    def abrir(caminho):
        webbrowser.open(f"http://localhost:{porta}{caminho}")

    def notificar(texto):
        try:
            import subprocess
            subprocess.Popen(["notify-send", "-a", "Baton", "Baton", texto])
        except OSError:
            pass

    def escolher(_):
        dlg = Gtk.FileChooserDialog(title="Escolher apresentacao", action=Gtk.FileChooserAction.OPEN)
        dlg.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Abrir", Gtk.ResponseType.OK)
        filtro = Gtk.FileFilter()
        filtro.set_name("Apresentacoes (PDF, PPTX, ODP, MD)")
        for e in EXTENSOES:
            filtro.add_pattern(f"*.{e}")
            filtro.add_pattern(f"*.{e.upper()}")
        dlg.add_filter(filtro)
        caminho = dlg.get_filename() if dlg.run() == Gtk.ResponseType.OK else None
        dlg.destroy()
        if not caminho:
            return
        notificar("Carregando a apresentacao...")

        def enviar():
            ok, msg = enviar_deck(porta, caminho)
            notificar(("Pronto: " if ok else "Falhou: ") + msg)
        threading.Thread(target=enviar, daemon=True).start()

    def montar(est):
        for filho in menu.get_children():
            menu.remove(filho)
        texto, nome_icone = rotulo(est)

        def item(rot, fn=None, ativo=True):
            it = Gtk.MenuItem(label=rot)
            it.set_sensitive(ativo and fn is not None)
            if fn:
                it.connect("activate", fn)
            menu.append(it)
            return it
        item(texto)
        menu.append(Gtk.SeparatorMenuItem())
        if est is not None:
            item("Retomar" if not est.get("ativo") else "Pausar",
                 lambda _: threading.Thread(target=acao, args=(porta, "retomar" if not est.get("ativo") else "pausar"), daemon=True).start())
        item("Escolher apresentacao...", escolher)
        if est and est.get("deck"):
            item(f"Remover \"{est['deck']['nome']}\"", lambda _: threading.Thread(target=acao, args=(porta, "remover_deck"), daemon=True).start())
        menu.append(Gtk.SeparatorMenuItem())
        item("Mostrar QR code", lambda _: abrir("/"))
        item("Abrir painel", lambda _: abrir("/painel"))
        menu.append(Gtk.SeparatorMenuItem())
        item("Sair do Baton", lambda _: (acao(porta, "sair"), Gtk.main_quit()))
        menu.show_all()
        if icone["usa_indicador"]:
            icone["obj"].set_icon_full(nome_icone, texto)
        else:
            icone["obj"].set_from_icon_name(nome_icone)
            icone["obj"].set_tooltip_text(texto)

    sem_resposta = {"n": 0}

    def atualizar():
        def trabalho():
            est = estado(porta)
            sem_resposta["n"] = 0 if est else sem_resposta["n"] + 1
            if sem_resposta["n"] >= 3:  # o Baton encerrou: a bandeja sai junto
                GLib.idle_add(Gtk.main_quit)
                return
            GLib.idle_add(montar, est)
        threading.Thread(target=trabalho, daemon=True).start()
        return True

    montar(estado(porta))
    GLib.timeout_add_seconds(2, atualizar)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
