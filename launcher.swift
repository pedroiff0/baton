// launcher.swift: miolo do Baton.app. O macOS mostra o nome do aplicativo (Baton) na lista de
// Acessibilidade e Automacao, em vez de "python3.x". Ele pede a permissao de Acessibilidade ao
// iniciar e executa o Python do Baton como processo filho (a permissao vale para os filhos).
//
// Compilar:  swiftc -O launcher.swift -o Baton.app/Contents/MacOS/Baton
// Usar:      Baton <python> <baton.py> [argumentos...]
import ApplicationServices
import Foundation
#if BANDEJA
import AppKit
#endif


// Pede as duas permissoes que o envio de teclas usa: Acessibilidade (dialogo + entrada na lista) e acesso a eventos
// de teclado sinteticos (CGRequestPostEventAccess). Devolve se a Acessibilidade ja esta liberada.
func pedirAcessos() -> Bool {
    let opcoes = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
    let ok = AXIsProcessTrustedWithOptions(opcoes)
    if #available(macOS 10.15, *) {
        _ = CGRequestPostEventAccess()
    }
    return ok
}

#if BANDEJA
// ---------------------------------------------------------------- icone na barra de menu
// Casca fina sobre a API local do Baton (a mesma do painel): pausar/retomar, escolher a apresentacao,
// QR code, painel e sair. Compilado com -DBANDEJA; se falhar, o baton.py compila sem ela.
final class Bandeja: NSObject {
    let porta: Int
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    var estado: [String: Any]? = nil
    var falhas = 0

    init(porta: Int) {
        self.porta = porta
        super.init()
        item.button?.image = Bandeja.icone(pausado: false, aviso: false)
        montar()
        Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.atualizar() }
        atualizar()
    }

    static func icone(pausado: Bool, aviso: Bool) -> NSImage {
        let img = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { _ in
            NSColor.black.setStroke()
            NSColor.black.setFill()
            let tela = NSBezierPath(roundedRect: NSRect(x: 2, y: 4, width: 14, height: 10), xRadius: 2, yRadius: 2)
            tela.lineWidth = 1.5
            tela.stroke()
            if pausado {
                NSBezierPath(rect: NSRect(x: 6.5, y: 6.5, width: 2, height: 5)).fill()
                NSBezierPath(rect: NSRect(x: 9.5, y: 6.5, width: 2, height: 5)).fill()
            } else {
                let seta = NSBezierPath()
                seta.move(to: NSPoint(x: 7, y: 6))
                seta.line(to: NSPoint(x: 7, y: 12))
                seta.line(to: NSPoint(x: 12, y: 9))
                seta.close()
                seta.fill()
            }
            if aviso {
                NSBezierPath(ovalIn: NSRect(x: 12, y: 12, width: 6, height: 6)).fill()
            }
            return true
        }
        img.isTemplate = true
        return img
    }

    func requisicao(_ caminho: String, metodo: String = "GET", corpo: Data? = nil, tipo: String = "application/json", timeout: TimeInterval = 3) -> URLRequest {
        var rq = URLRequest(url: URL(string: "http://127.0.0.1:\(porta)\(caminho)")!)
        rq.httpMethod = metodo
        rq.timeoutInterval = timeout
        if let corpo = corpo {
            rq.httpBody = corpo
            rq.setValue(tipo, forHTTPHeaderField: "Content-Type")
        }
        return rq
    }

    func json(_ dados: Data?) -> [String: Any]? {
        guard let dados = dados else { return nil }
        return (try? JSONSerialization.jsonObject(with: dados)) as? [String: Any]
    }

    func atualizar() {
        URLSession.shared.dataTask(with: requisicao("/painel/estado", timeout: 2)) { dados, _, _ in
            let novo = self.json(dados)
            DispatchQueue.main.async {
                self.estado = novo
                self.falhas = novo == nil ? self.falhas + 1 : 0
                if self.falhas >= 5 { exit(0) }  // o Baton encerrou
                self.montar()
            }
        }.resume()
    }

    func acao(_ nome: String) {
        let corpo = try? JSONSerialization.data(withJSONObject: ["acao": nome])
        URLSession.shared.dataTask(with: requisicao("/painel/acao", metodo: "POST", corpo: corpo)) { _, _, _ in
            DispatchQueue.main.async { self.atualizar() }
        }.resume()
    }

    func montar() {
        let ativo = (estado?["ativo"] as? Bool) ?? true
        let autorizado = (estado?["autorizado"] as? Bool) ?? false
        let semPermissao = (estado?["acessibilidade"] as? Bool) == false
        let pedido = estado?["pedido"] as? [String: Any]
        var texto = "Baton: em espera"
        if estado == nil { texto = "Baton: sem resposta" }
        else if semPermissao { texto = "Baton: permissao de teclas pendente" }
        else if !ativo { texto = "Baton: pausado" }
        else if let app = pedido?["app"] as? String { texto = "Baton: apresentacao detectada (\(app))" }
        else if autorizado { texto = "Baton: controlando" }
        item.button?.image = Bandeja.icone(pausado: !ativo, aviso: semPermissao || pedido != nil || estado == nil)

        let menu = NSMenu()
        menu.autoenablesItems = false
        let titulo = NSMenuItem(title: texto, action: nil, keyEquivalent: "")
        titulo.isEnabled = false
        menu.addItem(titulo)
        menu.addItem(NSMenuItem.separator())
        if estado != nil {
            menu.addItem(entrada(ativo ? "Pausar" : "Retomar", #selector(alternar)))
        }
        menu.addItem(entrada("Escolher apresentacao...", #selector(escolher)))
        if let deck = estado?["deck"] as? [String: Any], let nome = deck["nome"] as? String {
            menu.addItem(entrada("Remover \"\(nome)\"", #selector(removerDeck)))
        }
        menu.addItem(NSMenuItem.separator())
        menu.addItem(entrada("Mostrar QR code", #selector(abrirQR)))
        menu.addItem(entrada("Abrir painel", #selector(abrirPainel)))
        if semPermissao {
            menu.addItem(entrada("Permissao de teclas...", #selector(pedirPermissao)))
        }
        menu.addItem(NSMenuItem.separator())
        menu.addItem(entrada("Sair do Baton", #selector(sair)))
        item.menu = menu
    }

    func entrada(_ titulo: String, _ acao: Selector) -> NSMenuItem {
        let e = NSMenuItem(title: titulo, action: acao, keyEquivalent: "")
        e.target = self
        e.isEnabled = true
        return e
    }

    @objc func alternar() { acao(((estado?["ativo"] as? Bool) ?? true) ? "pausar" : "retomar") }
    @objc func removerDeck() { acao("remover_deck") }
    @objc func abrirQR() { NSWorkspace.shared.open(URL(string: "http://localhost:\(porta)/")!) }
    @objc func abrirPainel() { NSWorkspace.shared.open(URL(string: "http://localhost:\(porta)/painel")!) }
    @objc func pedirPermissao() {
        _ = pedirAcessos()
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")!)
    }
    @objc func sair() {
        acao("sair")
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { exit(0) }
    }

    @objc func escolher() {
        NSApp.activate(ignoringOtherApps: true)
        let painel = NSOpenPanel()
        painel.title = "Escolher apresentacao"
        painel.allowedFileTypes = ["pdf", "pptx", "ppt", "odp", "md", "txt"]
        painel.canChooseDirectories = false
        painel.allowsMultipleSelection = false
        guard painel.runModal() == .OK, let url = painel.url, let dados = try? Data(contentsOf: url) else { return }
        let nome = url.lastPathComponent.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? "apresentacao.pdf"
        let rq = requisicao("/painel/deck?nome=\(nome)", metodo: "POST", corpo: dados, tipo: "application/octet-stream", timeout: 330)
        URLSession.shared.dataTask(with: rq) { resposta, _, _ in
            let j = self.json(resposta)
            DispatchQueue.main.async {
                if let erro = j?["erro"] as? String {
                    let alerta = NSAlert()
                    alerta.messageText = "Baton nao carregou a apresentacao"
                    alerta.informativeText = erro
                    alerta.runModal()
                }
                self.atualizar()
            }
        }.resume()
    }
}
#endif

let argumentos = Array(CommandLine.arguments.dropFirst())

#if BANDEJA
// Vira um aplicativo de verdade (sem Dock) antes de pedir a permissao: o macOS so lista na Acessibilidade
// quem esta registrado no WindowServer; um executavel sem AppKit pode nem aparecer na lista.
let aplicativo = NSApplication.shared
aplicativo.setActivationPolicy(.accessory)
#endif

// Baton.app --pedir-acesso: so pede a permissao de Acessibilidade (o app passa a aparecer na lista) e sai.
if argumentos.first == "--pedir-acesso" {
    let ok = pedirAcessos()
    print(ok ? "Acessibilidade: liberada" : "Acessibilidade: pendente, ative o Baton nos Ajustes")
    #if BANDEJA
    RunLoop.main.run(until: Date().addingTimeInterval(4))
    #else
    Thread.sleep(forTimeInterval: 1.5)
    #endif
    exit(ok ? 0 : 1)
}
guard argumentos.count >= 2 else {
    FileHandle.standardError.write("uso: Baton <python> <baton.py> [argumentos...]\n".data(using: .utf8)!)
    exit(2)
}

#if !BANDEJA
_ = pedirAcessos()
#endif

let filho = Process()
filho.executableURL = URL(fileURLWithPath: argumentos[0])
filho.arguments = Array(argumentos.dropFirst())
filho.environment = ProcessInfo.processInfo.environment.merging(["BATON_LAUNCHER": "1"]) { $1 }

// A permissao de Acessibilidade e do Baton.app (nao do Python filho): grava o estado para o Baton avisar
// no painel e no celular quando as teclas estiverem bloqueadas.
let pastaCache = NSHomeDirectory() + "/.cache/baton"
try? FileManager.default.createDirectory(atPath: pastaCache, withIntermediateDirectories: true)
DispatchQueue.global().async {
    while true {
        try? (AXIsProcessTrusted() ? "1" : "0").write(toFile: pastaCache + "/acessibilidade", atomically: true, encoding: .utf8)
        Thread.sleep(forTimeInterval: 2)
    }
}

var fontes: [DispatchSourceSignal] = []
for sinal in [SIGTERM, SIGINT, SIGHUP] {
    signal(sinal, SIG_IGN)
    let fonte = DispatchSource.makeSignalSource(signal: sinal, queue: DispatchQueue.main)
    fonte.setEventHandler {
        if filho.isRunning { filho.terminate() }
        exit(0)
    }
    fonte.resume()
    fontes.append(fonte)
}

do {
    try filho.run()
} catch {
    FileHandle.standardError.write("nao consegui iniciar \(argumentos[0]): \(error)\n".data(using: .utf8)!)
    exit(1)
}
#if BANDEJA
filho.terminationHandler = { p in exit(p.terminationStatus) }
var porta = 8765
if let i = argumentos.firstIndex(of: "--porta"), i + 1 < argumentos.count, let n = Int(argumentos[i + 1]) { porta = n }
let bandeja = Bandeja(porta: porta)
_ = bandeja
// o pedido sai de dentro do laco de eventos do aplicativo (so la o macOS apresenta o dialogo e lista o app)
DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { _ = pedirAcessos() }
aplicativo.run()
#else
filho.waitUntilExit()
exit(filho.terminationStatus)
#endif
