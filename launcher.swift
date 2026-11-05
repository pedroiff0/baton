// launcher.swift: miolo do Baton.app. O macOS mostra o nome do aplicativo (Baton) na lista de
// Acessibilidade e Automacao, em vez de "python3.x". Ele pede a permissao de Acessibilidade ao
// iniciar e executa o Python do Baton como processo filho (a permissao vale para os filhos).
//
// Compilar:  swiftc -O launcher.swift -o Baton.app/Contents/MacOS/Baton
// Usar:      Baton <python> <baton.py> [argumentos...]
import ApplicationServices
import Foundation

let argumentos = Array(CommandLine.arguments.dropFirst())

// Baton.app --pedir-acesso: so pede a permissao de Acessibilidade (o app passa a aparecer na lista) e sai.
if argumentos.first == "--pedir-acesso" {
    let opcoes = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
    let ok = AXIsProcessTrustedWithOptions(opcoes)
    print(ok ? "Acessibilidade: liberada" : "Acessibilidade: pendente, ative o Baton nos Ajustes")
    Thread.sleep(forTimeInterval: 1.5)
    exit(ok ? 0 : 1)
}
guard argumentos.count >= 2 else {
    FileHandle.standardError.write("uso: Baton <python> <baton.py> [argumentos...]\n".data(using: .utf8)!)
    exit(2)
}

let opcoes = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
_ = AXIsProcessTrustedWithOptions(opcoes)

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
filho.waitUntilExit()
exit(filho.terminationStatus)
