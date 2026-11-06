// overlay.swift: camada transparente por cima de qualquer app (macOS).
// Desenha laser, foco (desfoca e escurece tudo menos um circulo), caneta e marca-texto.
// Recebe comandos JSON, um por linha, na entrada padrao (enviados pelo baton.py).
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
//   {"t":"cortina","k":"preto|branco|teatro|fim|off","txt":"Obrigado!","sub":"Perguntas?"}
//   {"t":"confete","seg":4}
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
    var lupaImg: CGImage? = nil
    var lupaArq = ""
    var lupaPonto = CGPoint(x: 0.5, y: 0.5)
    var lupaZ: CGFloat = 2
    var lupaLado: CGFloat = 0.32
    var lupaLigada = false

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
            let rgb = aj.laserCor.usingColorSpace(.deviceRGB) ?? aj.laserCor
            let escuro = 0.299 * rgb.redComponent + 0.587 * rgb.greenComponent + 0.114 * rgb.blueComponent < 0.3
            if escuro {  // cor escura (preto): halo branco por baixo para aparecer em fundo escuro e claro
                let halo = [
                    NSColor.white.withAlphaComponent(0.95).cgColor,
                    NSColor.white.withAlphaComponent(0.8).cgColor,
                    NSColor.white.withAlphaComponent(0.0).cgColor,
                ] as CFArray
                if let gh = CGGradient(colorsSpace: cs, colors: halo, locations: [0.0, 0.6, 1.0]) {
                    g.drawRadialGradient(gh, startCenter: c, startRadius: 0, endCenter: c, endRadius: aj.laserTam * 1.15, options: [])
                }
            }
            let cores = [
                escuro ? aj.laserCor.cgColor : NSColor.white.cgColor,
                aj.laserCor.cgColor,
                aj.laserCor.withAlphaComponent(escuro ? 0.9 : 0.15).cgColor,
                aj.laserCor.withAlphaComponent(0.0).cgColor,
            ] as CFArray
            let locais: [CGFloat] = [0.0, 0.35, escuro ? 0.8 : 0.9, 1.0]
            if let grad = CGGradient(colorsSpace: cs, colors: cores, locations: locais) {
                g.drawRadialGradient(grad, startCenter: c, startRadius: 0, endCenter: c, endRadius: aj.laserTam, options: [])
            }
        }
        if lupaLigada, let img = lupaImg {
            desenharLupa(g, img, w, h)
        }
    }

    // Lupa retangular: recorta da captura do monitor a area em volta do ponto e a desenha ampliada sob o proprio ponto.
    func desenharLupa(_ g: CGContext, _ img: CGImage, _ w: CGFloat, _ h: CGFloat) {
        let lw = lupaLado * w
        let lh = lupaLado * h
        let fw = lupaLado / lupaZ
        let lx = min(max(lupaPonto.x * w - lw / 2, 0), w - lw)
        let ly = min(max(lupaPonto.y * h - lh / 2, 0), h - lh)
        let fx = min(max(lupaPonto.x - fw / 2, 0), 1 - fw)
        let fy = min(max(lupaPonto.y - fw / 2, 0), 1 - fw)
        let iw = CGFloat(img.width)
        let ih = CGFloat(img.height)
        let rect = CGRect(x: lx, y: ly, width: lw, height: lh)
        let cheia = lupaLado >= 0.99  // ampliacao da tela inteira: sem moldura
        if !cheia {
            g.saveGState()
            g.setFillColor(NSColor.black.withAlphaComponent(0.35).cgColor)
            g.fill(CGRect(x: lx - 3, y: ly - 1, width: lw + 6, height: lh + 8))
            g.restoreGState()
        }
        if let corte = img.cropping(to: CGRect(x: fx * iw, y: fy * ih, width: fw * iw, height: fw * ih)) {
            g.saveGState()
            g.clip(to: rect)
            g.translateBy(x: lx, y: ly + lh)  // a vista e invertida (origem em cima): desvira a imagem
            g.scaleBy(x: 1, y: -1)
            g.interpolationQuality = .high
            g.draw(corte, in: CGRect(x: 0, y: 0, width: lw, height: lh))
            g.restoreGState()
        }
        if cheia { return }
        g.saveGState()
        g.setStrokeColor(NSColor.white.cgColor)
        g.setLineWidth(3)
        g.stroke(rect)
        g.setStrokeColor(NSColor.black.withAlphaComponent(0.55).cgColor)
        g.setLineWidth(1)
        g.stroke(rect.insetBy(dx: -2, dy: -2))
        g.restoreGState()
    }
}

struct Particula {
    var x: CGFloat
    var y: CGFloat
    var vx: CGFloat
    var vy: CGFloat
    var rot: CGFloat
    var vr: CGFloat
    var w: CGFloat
    var h: CGFloat
    var cor: NSColor
}

// Confete: particulas com fisica simples, desenhadas por cima de tudo.
final class Confete: NSView {
    var ps: [Particula] = []
    var emitindo = false
    var fimEmissao = Date()
    var relogio: Timer?
    let paleta: [NSColor] = [
        NSColor(calibratedRed: 1.0, green: 0.23, blue: 0.19, alpha: 1.0),
        NSColor(calibratedRed: 1.0, green: 0.84, blue: 0.04, alpha: 1.0),
        NSColor(calibratedRed: 0.19, green: 0.82, blue: 0.35, alpha: 1.0),
        NSColor(calibratedRed: 0.04, green: 0.52, blue: 1.0, alpha: 1.0),
        NSColor(calibratedRed: 0.75, green: 0.35, blue: 0.95, alpha: 1.0),
        NSColor(calibratedRed: 1.0, green: 0.62, blue: 0.04, alpha: 1.0),
    ]

    override var isFlipped: Bool { return true }
    override var isOpaque: Bool { return false }

    func iniciar(_ segundos: Double) {
        emitindo = true
        fimEmissao = Date().addingTimeInterval(segundos)
        if relogio == nil {
            relogio = Timer.scheduledTimer(withTimeInterval: 1.0 / 60.0, repeats: true) { [weak self] _ in
                self?.passo()
            }
        }
    }

    func parar() {
        emitindo = false
        ps.removeAll()
        relogio?.invalidate()
        relogio = nil
        needsDisplay = true
    }

    func nova() -> Particula {
        return Particula(x: CGFloat.random(in: 0...bounds.width), y: -20,
                         vx: CGFloat.random(in: -120...120), vy: CGFloat.random(in: 80...200),
                         rot: CGFloat.random(in: 0...6.28), vr: CGFloat.random(in: -9...9),
                         w: CGFloat.random(in: 8...16), h: CGFloat.random(in: 5...11),
                         cor: paleta[Int.random(in: 0..<paleta.count)])
    }

    func passo() {
        let dt: CGFloat = 1.0 / 60.0
        if emitindo && Date() < fimEmissao {
            for _ in 0..<5 { ps.append(nova()) }
        } else {
            emitindo = false
        }
        for i in ps.indices {
            ps[i].vy = min(ps[i].vy + 260 * dt, 340)
            ps[i].x += (ps[i].vx + sin(ps[i].y / 45) * 40) * dt
            ps[i].y += ps[i].vy * dt
            ps[i].rot += ps[i].vr * dt
        }
        let limite = bounds.height + 30
        ps.removeAll { $0.y > limite }
        if ps.isEmpty && !emitindo {
            relogio?.invalidate()
            relogio = nil
        }
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        guard let g = NSGraphicsContext.current?.cgContext else { return }
        for p in ps {
            g.saveGState()
            g.translateBy(x: p.x, y: p.y)
            g.rotate(by: p.rot)
            g.setFillColor(p.cor.cgColor)
            g.fill(CGRect(x: -p.w / 2, y: -p.h / 2, width: p.w, height: p.h))
            g.restoreGState()
        }
    }
}

// Cortina por cima de tudo: preto, branco, teatro (panos vermelhos) e tela de encerramento.
final class Cortina: NSView {
    let fundo = NSView()
    let esq = NSView()
    let dir = NSView()
    let titulo = NSTextField(labelWithString: "")
    let sub = NSTextField(labelWithString: "")
    let confete: Confete
    var tipo = ""

    override var isFlipped: Bool { return true }

    init(frame: NSRect, confete: Confete) {
        self.confete = confete
        super.init(frame: frame)
        autoresizingMask = [.width, .height]
        fundo.frame = bounds
        fundo.wantsLayer = true
        fundo.alphaValue = 0
        fundo.autoresizingMask = [.width, .height]
        addSubview(fundo)
        for pano in [esq, dir] {
            pano.frame = NSRect(x: -bounds.width, y: 0, width: bounds.width / 2 + 2, height: bounds.height)
            pano.wantsLayer = true
            pano.layer?.addSublayer(gradientePregas(NSRect(origin: NSPoint.zero, size: pano.frame.size)))
            addSubview(pano)
        }
        for rotulo in [titulo, sub] {
            rotulo.alignment = .center
            rotulo.isBezeled = false
            rotulo.drawsBackground = false
            rotulo.isEditable = false
            rotulo.textColor = NSColor.white
            rotulo.alphaValue = 0
            addSubview(rotulo)
        }
        isHidden = true
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) nao suportado") }

    func gradientePregas(_ r: NSRect) -> CAGradientLayer {
        let g = CAGradientLayer()
        let escuro = NSColor(calibratedRed: 0.42, green: 0.04, blue: 0.08, alpha: 1.0).cgColor
        let claro = NSColor(calibratedRed: 0.72, green: 0.12, blue: 0.16, alpha: 1.0).cgColor
        let n = 14
        var cores: [CGColor] = []
        var locais: [NSNumber] = []
        for i in 0...n {
            cores.append(i % 2 == 0 ? escuro : claro)
            locais.append(NSNumber(value: Double(i) / Double(n)))
        }
        g.colors = cores
        g.locations = locais
        g.startPoint = CGPoint(x: 0.0, y: 0.5)
        g.endPoint = CGPoint(x: 1.0, y: 0.5)
        g.frame = r
        return g
    }

    func definir(_ k: String, _ txt: String, _ subtxt: String) {
        let w = bounds.width
        let h = bounds.height
        let anterior = tipo
        let foraEsq = NSRect(x: -w / 2 - 4, y: 0, width: w / 2 + 2, height: h)
        let foraDir = NSRect(x: w + 2, y: 0, width: w / 2 + 2, height: h)

        if k == "" || k == "off" || k == "preto" || k == "branco" || k == "teatro" || k == "fim" {
            tipo = (k == "off") ? "" : k
        } else {
            return
        }

        if tipo == "" {
            NSAnimationContext.runAnimationGroup({ c in
                c.duration = (anterior == "teatro") ? 1.2 : 0.3
                c.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
                fundo.animator().alphaValue = 0
                esq.animator().frame = foraEsq
                dir.animator().frame = foraDir
                titulo.animator().alphaValue = 0
                sub.animator().alphaValue = 0
            }, completionHandler: { [weak self] in
                guard let s = self, s.tipo == "" else { return }
                s.isHidden = true
                s.confete.parar()
            })
            return
        }

        isHidden = false
        let cor: NSColor
        switch tipo {
        case "branco": cor = NSColor.white
        case "fim": cor = NSColor(calibratedRed: 0.03, green: 0.05, blue: 0.12, alpha: 1.0)
        default: cor = NSColor.black
        }
        fundo.layer?.backgroundColor = cor.cgColor

        let tamTitulo = h * 0.11
        titulo.stringValue = txt
        titulo.font = NSFont.boldSystemFont(ofSize: tamTitulo)
        titulo.frame = NSRect(x: 0, y: h * 0.5 - tamTitulo * 1.1, width: w, height: tamTitulo * 1.4)
        let tamSub = h * 0.04
        sub.stringValue = subtxt
        sub.font = NSFont.systemFont(ofSize: tamSub)
        sub.textColor = NSColor(calibratedWhite: 0.85, alpha: 1.0)
        sub.frame = NSRect(x: 0, y: h * 0.5 + tamTitulo * 0.5, width: w, height: tamSub * 1.5)

        if tipo == "teatro" {
            if anterior != "teatro" {
                esq.frame = foraEsq
                dir.frame = foraDir
            }
            NSAnimationContext.runAnimationGroup({ c in
                c.duration = 1.3
                c.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
                fundo.animator().alphaValue = 0
                esq.animator().frame = NSRect(x: 0, y: 0, width: w / 2 + 2, height: h)
                dir.animator().frame = NSRect(x: w / 2 - 2, y: 0, width: w / 2 + 2, height: h)
                titulo.animator().alphaValue = 0
                sub.animator().alphaValue = 0
            }, completionHandler: nil)
        } else {
            esq.frame = foraEsq
            dir.frame = foraDir
            NSAnimationContext.runAnimationGroup({ c in
                c.duration = 0.35
                fundo.animator().alphaValue = 1
                titulo.animator().alphaValue = (tipo == "fim") ? 1 : 0
                sub.animator().alphaValue = (tipo == "fim") ? 1 : 0
            }, completionHandler: nil)
            if tipo == "fim" { confete.iniciar(6) }
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
    let confete: Confete
    let cortina: Cortina
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
        confete = Confete(frame: raiz.bounds)
        confete.autoresizingMask = [.width, .height]
        cortina = Cortina(frame: raiz.bounds, confete: confete)
        raiz.addSubview(quadro)
        raiz.addSubview(cortina)
        raiz.addSubview(confete)
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
        case "lupa":
            let ligada = (msg["on"] as? Bool) ?? false
            if !ligada {
                quadro.lupaLigada = false
                quadro.lupaImg = nil
                quadro.lupaArq = ""
            } else {
                let arq = (msg["img"] as? String) ?? ""
                if arq != quadro.lupaArq {
                    quadro.lupaImg = NSImage(contentsOfFile: arq)?.cgImage(forProposedRect: nil, context: nil, hints: nil)
                    quadro.lupaArq = arq
                }
                quadro.lupaPonto = CGPoint(x: CGFloat((msg["x"] as? Double) ?? 0.5), y: CGFloat((msg["y"] as? Double) ?? 0.5))
                quadro.lupaZ = CGFloat(min(8.0, max(1.1, (msg["z"] as? Double) ?? 2.0)))
                quadro.lupaLado = CGFloat(min(1.0, max(0.1, (msg["w"] as? Double) ?? 1.0)))
                quadro.lupaLigada = quadro.lupaImg != nil
            }
            quadro.needsDisplay = true
        case "cortina":
            cortina.definir((msg["k"] as? String) ?? "off", (msg["txt"] as? String) ?? "", (msg["sub"] as? String) ?? "")
        case "confete":
            confete.iniciar(min(10.0, max(1.0, (msg["seg"] as? Double) ?? 4.0)))
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
