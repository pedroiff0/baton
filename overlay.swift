// overlay.swift: camada transparente por cima de qualquer app (macOS).
// Desenha laser, foco (desfoca e escurece tudo menos um circulo), caneta e marca-texto.
// Recebe comandos JSON, um por linha, na entrada padrao (enviados pelo passador.py).
//
// Compilar:  swiftc -O overlay.swift -o overlay
// Usar:      ./overlay [--tela N] [--nivel N]
//            --tela N   indice da tela (0 = principal)
//            --nivel N  nivel da janela (padrao: o maximo do sistema, acima de apresentacoes em tela cheia)
//
// Mensagens (x e y normalizados de 0 a 1 em relacao a tela, origem no canto superior esquerdo):
//   {"t":"laser","x":0.5,"y":0.5,"on":true,"m":"laser","r":0.12}   m = "laser" ou "foco"; r = raio do foco
//   {"t":"draw","id":"a1","hl":false,"c":"#ff3b30","p":[[0.1,0.1],[0.2,0.2]]}
//   {"t":"cfg","laser_tam":24,"laser_cor":"#ff2d2d","foco_blur":100,"foco_escuro":25,
//    "pen_larg":3.5,"hl_larg":14,"hl_alpha":38}
//   {"t":"clear"}   {"t":"quit"}
import Cocoa

func corHex(_ s: String) -> NSColor {
    var h = s
    if h.hasPrefix("#") { h.removeFirst() }
    guard h.count == 6, let v = UInt32(h, radix: 16) else { return NSColor.red }
    let r = CGFloat((v >> 16) & 0xFF) / 255.0
    let g = CGFloat((v >> 8) & 0xFF) / 255.0
    let b = CGFloat(v & 0xFF) / 255.0
    return NSColor(calibratedRed: r, green: g, blue: b, alpha: 1.0)
}

struct Traco {
    var marca: Bool
    var cor: NSColor
    var pontos: [CGPoint]
}

// Ajustes de personalizacao (chegam em mensagens "cfg").
struct Ajustes {
    var laserTam: CGFloat = 24        // raio do laser, em pontos
    var laserCor: NSColor = NSColor(calibratedRed: 1.0, green: 0.18, blue: 0.18, alpha: 1.0)
    var focoBlur: CGFloat = 100       // 0 a 100: intensidade do desfoque
    var focoEscuro: CGFloat = 25      // 0 a 90: escurecimento fora do circulo, em %
    var penLarg: CGFloat = 3.5        // milesimos da largura da tela
    var hlLarg: CGFloat = 14          // milesimos da largura da tela
    var hlAlpha: CGFloat = 38         // opacidade do marca-texto, em %
}

final class Quadro: NSView {
    var tracos: [String: Traco] = [:]
    var ordem: [String] = []
    var laserLigado = false
    var laserPonto = CGPoint(x: 0.5, y: 0.5)
    var aj = Ajustes()

    override var isFlipped: Bool { return true }
    override var isOpaque: Bool { return false }

    override func draw(_ dirtyRect: NSRect) {
        guard let g = NSGraphicsContext.current?.cgContext else { return }
        let w = bounds.width
        let h = bounds.height
        for id in ordem {
            guard let t = tracos[id], !t.pontos.isEmpty else { continue }
            g.saveGState()
            g.setLineCap(.round)
            g.setLineJoin(.round)
            g.setLineWidth(t.marca ? max(8.0, w * aj.hlLarg / 1000.0) : max(2.0, w * aj.penLarg / 1000.0))
            g.setStrokeColor(t.cor.withAlphaComponent(t.marca ? aj.hlAlpha / 100.0 : 1.0).cgColor)
            g.beginPath()
            g.move(to: CGPoint(x: t.pontos[0].x * w, y: t.pontos[0].y * h))
            if t.pontos.count == 1 {
                g.addLine(to: CGPoint(x: t.pontos[0].x * w + 0.1, y: t.pontos[0].y * h))
            }
            for p in t.pontos.dropFirst() {
                g.addLine(to: CGPoint(x: p.x * w, y: p.y * h))
            }
            g.strokePath()
            g.restoreGState()
        }
        if laserLigado {
            let c = CGPoint(x: laserPonto.x * w, y: laserPonto.y * h)
            let cs = CGColorSpaceCreateDeviceRGB()
            let cores = [
                NSColor.white.cgColor,
                aj.laserCor.cgColor,
                aj.laserCor.withAlphaComponent(0.0).cgColor,
            ] as CFArray
            let locais: [CGFloat] = [0.0, 0.35, 1.0]
            if let grad = CGGradient(colorsSpace: cs, colors: cores, locations: locais) {
                g.drawRadialGradient(grad, startCenter: c, startRadius: 0, endCenter: c, endRadius: aj.laserTam, options: [])
            }
        }
    }
}

final class Camada {
    let janela: NSPanel
    let quadro: Quadro
    let desfoque: NSVisualEffectView
    let escuro: NSView
    let mascaraEscuro = CALayer()
    let tamanho: NSSize
    var focoLigado = false

    init(indiceTela: Int, nivel: Int) {
        let telas = NSScreen.screens
        let tela = (indiceTela >= 0 && indiceTela < telas.count) ? telas[indiceTela] : (NSScreen.main ?? telas[0])
        tamanho = tela.frame.size
        // Painel sem ativacao: aparece por cima de apps em tela cheia e de modos de apresentacao
        janela = NSPanel(contentRect: tela.frame, styleMask: [.borderless, .nonactivatingPanel],
                         backing: .buffered, defer: false)
        janela.isOpaque = false
        janela.backgroundColor = NSColor.clear
        janela.hasShadow = false
        janela.ignoresMouseEvents = true
        janela.isFloatingPanel = true
        janela.hidesOnDeactivate = false
        janela.becomesKeyOnlyIfNeeded = true
        janela.worksWhenModal = true
        janela.level = NSWindow.Level(rawValue: nivel)
        janela.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]

        let raiz = NSView(frame: NSRect(origin: NSPoint.zero, size: tela.frame.size))
        desfoque = NSVisualEffectView(frame: raiz.bounds)
        desfoque.material = .hudWindow
        desfoque.blendingMode = .behindWindow
        desfoque.state = .active
        desfoque.autoresizingMask = [.width, .height]
        desfoque.isHidden = true

        escuro = NSView(frame: raiz.bounds)
        escuro.wantsLayer = true
        escuro.autoresizingMask = [.width, .height]
        escuro.isHidden = true
        mascaraEscuro.frame = raiz.bounds
        mascaraEscuro.contentsGravity = .resize
        escuro.layer?.mask = mascaraEscuro

        quadro = Quadro(frame: raiz.bounds)
        quadro.autoresizingMask = [.width, .height]
        raiz.addSubview(desfoque)
        raiz.addSubview(escuro)
        raiz.addSubview(quadro)
        janela.contentView = raiz
        janela.setFrame(tela.frame, display: true)
        janela.orderFrontRegardless()
        aplicarAjustes()
    }

    // Reafirma a janela no topo: alguns apps trazem a propria janela para a frente ao iniciar a apresentacao.
    func manterNoTopo() {
        Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            self?.janela.orderFrontRegardless()
        }
    }

    func aplicarAjustes() {
        let a = quadro.aj
        desfoque.alphaValue = max(0.0, min(1.0, a.focoBlur / 100.0))
        escuro.layer?.backgroundColor = NSColor.black.withAlphaComponent(max(0.0, min(0.9, a.focoEscuro / 100.0))).cgColor
        atualizarVisibilidade()
        quadro.needsDisplay = true
    }

    func atualizarVisibilidade() {
        desfoque.isHidden = !(focoLigado && quadro.aj.focoBlur > 0.5)
        escuro.isHidden = !(focoLigado && quadro.aj.focoEscuro > 0.5)
    }

    // Mascara do foco: opaca em tudo, transparente (com borda suave) em volta do ponteiro.
    func atualizarMascara(_ centro: CGPoint, raio: CGFloat) {
        let largura: CGFloat = 480.0
        let altura: CGFloat = max(1.0, (largura * tamanho.height / tamanho.width).rounded())
        let img = NSImage(size: NSSize(width: largura, height: altura), flipped: true) { rect in
            guard let g = NSGraphicsContext.current?.cgContext else { return false }
            g.setFillColor(NSColor.black.cgColor)
            g.fill(rect)
            g.setBlendMode(.destinationOut)
            let c = CGPoint(x: centro.x * largura, y: centro.y * altura)
            let r1 = largura * raio
            let r0 = r1 * 0.65
            let cs = CGColorSpaceCreateDeviceRGB()
            let cores = [
                NSColor.black.cgColor,
                NSColor.black.withAlphaComponent(0.0).cgColor,
            ] as CFArray
            let locais: [CGFloat] = [0.0, 1.0]
            if let grad = CGGradient(colorsSpace: cs, colors: cores, locations: locais) {
                g.drawRadialGradient(grad, startCenter: c, startRadius: r0, endCenter: c, endRadius: r1,
                                     options: [.drawsBeforeStartLocation])
            }
            return true
        }
        desfoque.maskImage = img
        if let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) {
            CATransaction.begin()
            CATransaction.setDisableActions(true)
            mascaraEscuro.frame = escuro.bounds
            mascaraEscuro.contents = cg
            CATransaction.commit()
        }
    }

    func numero(_ msg: [String: Any], _ chave: String, _ atual: CGFloat, _ lo: Double, _ hi: Double) -> CGFloat {
        guard let v = msg[chave] as? Double else { return atual }
        return CGFloat(min(hi, max(lo, v)))
    }

    func tratar(_ msg: [String: Any]) {
        guard let t = msg["t"] as? String else { return }
        switch t {
        case "laser":
            let x = CGFloat((msg["x"] as? Double) ?? 0.5)
            let y = CGFloat((msg["y"] as? Double) ?? 0.5)
            let ligado = (msg["on"] as? Bool) ?? false
            let modo = (msg["m"] as? String) ?? "laser"
            let raio = CGFloat(min(0.35, max(0.03, (msg["r"] as? Double) ?? 0.12)))
            quadro.laserPonto = CGPoint(x: x, y: y)
            if modo == "foco" {
                quadro.laserLigado = false
                focoLigado = ligado
                if ligado { atualizarMascara(CGPoint(x: x, y: y), raio: raio) }
            } else {
                focoLigado = false
                quadro.laserLigado = ligado
            }
            atualizarVisibilidade()
            quadro.needsDisplay = true
        case "draw":
            let id = (msg["id"] as? String) ?? "?"
            let marca = (msg["hl"] as? Bool) ?? false
            let cor = corHex((msg["c"] as? String) ?? "#ff3b30")
            var novos: [CGPoint] = []
            if let lista = msg["p"] as? [[Double]] {
                for par in lista where par.count >= 2 {
                    novos.append(CGPoint(x: CGFloat(par[0]), y: CGFloat(par[1])))
                }
            }
            if var existente = quadro.tracos[id] {
                existente.pontos.append(contentsOf: novos)
                quadro.tracos[id] = existente
            } else {
                quadro.tracos[id] = Traco(marca: marca, cor: cor, pontos: novos)
                quadro.ordem.append(id)
            }
            quadro.needsDisplay = true
        case "cfg":
            var a = quadro.aj
            a.laserTam = numero(msg, "laser_tam", a.laserTam, 6, 120)
            if let c = msg["laser_cor"] as? String { a.laserCor = corHex(c) }
            a.focoBlur = numero(msg, "foco_blur", a.focoBlur, 0, 100)
            a.focoEscuro = numero(msg, "foco_escuro", a.focoEscuro, 0, 90)
            a.penLarg = numero(msg, "pen_larg", a.penLarg, 0.5, 20)
            a.hlLarg = numero(msg, "hl_larg", a.hlLarg, 3, 60)
            a.hlAlpha = numero(msg, "hl_alpha", a.hlAlpha, 5, 90)
            quadro.aj = a
            aplicarAjustes()
        case "clear":
            quadro.tracos.removeAll()
            quadro.ordem.removeAll()
            quadro.needsDisplay = true
        case "quit":
            NSApp.terminate(nil)
        default:
            break
        }
    }
}

var indice = 0
var nivelJanela = Int(CGWindowLevelForKey(.maximumWindow))
let args = CommandLine.arguments
if let i = args.firstIndex(of: "--tela"), i + 1 < args.count, let n = Int(args[i + 1]) {
    indice = n
}
if let i = args.firstIndex(of: "--nivel"), i + 1 < args.count, let n = Int(args[i + 1]) {
    nivelJanela = n
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let camada = Camada(indiceTela: indice, nivel: nivelJanela)
camada.manterNoTopo()

DispatchQueue.global(qos: .userInteractive).async {
    while let linha = readLine() {
        guard let dados = linha.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: dados),
              let msg = obj as? [String: Any] else { continue }
        DispatchQueue.main.async { camada.tratar(msg) }
    }
    DispatchQueue.main.async { NSApp.terminate(nil) }  // pai fechou a entrada: encerra
}

app.run()
