// launcher.swift: miolo do Baton.app. O macOS mostra o nome do aplicativo (Baton) na lista de
// Acessibilidade e Automacao, em vez de "python3.x". Ele pede a permissao de Acessibilidade ao
// iniciar e executa o Python do Baton como processo filho (a permissao vale para os filhos).
//
// Compilar:  swiftc -O launcher.swift -o Baton.app/Contents/MacOS/Baton
// Usar:      Baton <python> <baton.py> [argumentos...]
import ApplicationServices
import Foundation

let argumentos = Array(CommandLine.arguments.dropFirst())
guard argumentos.count >= 2 else {
    FileHandle.standardError.write("uso: Baton <python> <baton.py> [argumentos...]\n".data(using: .utf8)!)
    exit(2)
}

let opcoes = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
_ = AXIsProcessTrustedWithOptions(opcoes)

let filho = Process()
filho.executableURL = URL(fileURLWithPath: argumentos[0])
filho.arguments = Array(argumentos.dropFirst())

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
