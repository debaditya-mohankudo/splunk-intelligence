import Foundation
import FoundationModels

var systemPrompt: String? = nil
var args = CommandLine.arguments.dropFirst().makeIterator()
while let arg = args.next() {
    if arg == "--system" {
        systemPrompt = args.next()
    }
}

let inputData = FileHandle.standardInput.readDataToEndOfFile()
guard let userPrompt = String(data: inputData, encoding: .utf8), !userPrompt.isEmpty else {
    FileHandle.standardError.write("Error: empty prompt on stdin\n".data(using: .utf8)!)
    exit(1)
}

if #available(macOS 26.0, *) {
    do {
        let session: LanguageModelSession
        if let sys = systemPrompt, !sys.isEmpty {
            session = LanguageModelSession(instructions: sys)
        } else {
            session = LanguageModelSession()
        }
        let response = try await session.respond(to: userPrompt)
        print(response.content)
    } catch {
        FileHandle.standardError.write("Error: \(error)\n".data(using: .utf8)!)
        exit(1)
    }
} else {
    FileHandle.standardError.write("Error: requires macOS 26.0 or newer\n".data(using: .utf8)!)
    exit(1)
}
