// ocr — read text out of an image using Apple's Vision framework.
//
// The reading half of LifeOS document intake. Vision runs on the Neural Engine:
// no model download, no VRAM, no network, and it is markedly better on bills
// and receipts than a general-purpose vision LLM would be at this size. The
// local LLM then does the understanding, which is what it is actually good at.
//
//   ocr <image-or-pdf> [--json]
//
// Plain output is the recognised text, reading order preserved.
// --json adds per-line confidence and normalised bounding boxes, which the
// extractor uses to tell a confident number from a guessed one.

import Foundation
import Vision
import AppKit
import CoreGraphics

struct Line: Codable {
    let text: String
    let confidence: Float
    let x: Double, y: Double, w: Double, h: Double
}

func cgImages(from path: String) -> [CGImage] {
    let url = URL(fileURLWithPath: path)
    // PDFs: rasterise every page at 2x so small print survives.
    if path.lowercased().hasSuffix(".pdf") {
        guard let doc = CGPDFDocument(url as CFURL) else { return [] }
        var out: [CGImage] = []
        for i in 1...max(doc.numberOfPages, 1) {
            guard let page = doc.page(at: i) else { continue }
            let box = page.getBoxRect(.mediaBox)
            let scale: CGFloat = 2.0
            let w = Int(box.width * scale), h = Int(box.height * scale)
            guard w > 0, h > 0,
                  let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8,
                                      bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                                      bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)
            else { continue }
            ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
            ctx.fill(CGRect(x: 0, y: 0, width: CGFloat(w), height: CGFloat(h)))
            ctx.scaleBy(x: scale, y: scale)
            ctx.drawPDFPage(page)
            if let img = ctx.makeImage() { out.append(img) }
        }
        return out
    }
    guard let img = NSImage(contentsOf: url),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { return [] }
    return [cg]
}

func recognise(_ image: CGImage) -> [Line] {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = true
    req.recognitionLanguages = ["en-US", "en-CA", "fr-CA"]
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    do { try handler.perform([req]) } catch { return [] }
    guard let obs = req.results else { return [] }
    return obs.compactMap { o in
        guard let c = o.topCandidates(1).first else { return nil }
        let b = o.boundingBox
        return Line(text: c.string, confidence: c.confidence,
                    x: Double(b.origin.x), y: Double(b.origin.y),
                    w: Double(b.width), h: Double(b.height))
    }
}

// ---------------------------------------------------------------- main
let args = CommandLine.arguments
guard args.count >= 2 else {
    FileHandle.standardError.write("usage: ocr <image-or-pdf> [--json]\n".data(using: .utf8)!)
    exit(2)
}
let path = args[1]
let wantJSON = args.contains("--json")

guard FileManager.default.fileExists(atPath: path) else {
    FileHandle.standardError.write("ocr: no such file: \(path)\n".data(using: .utf8)!)
    exit(2)
}
let images = cgImages(from: path)
guard !images.isEmpty else {
    FileHandle.standardError.write("ocr: could not decode \(path) as an image or PDF\n".data(using: .utf8)!)
    exit(3)
}

var all: [Line] = []
for img in images { all.append(contentsOf: recognise(img)) }

// Vision returns lines in no guaranteed order. Sort top-to-bottom, then
// left-to-right, so the text reads the way a human would read the page —
// which matters a great deal for the LLM that consumes it next.
all.sort { a, b in
    if abs(a.y - b.y) > 0.012 { return a.y > b.y }
    return a.x < b.x
}

if wantJSON {
    let enc = JSONEncoder()
    enc.outputFormatting = [.prettyPrinted]
    let payload: [String: Any] = [:]
    _ = payload
    struct Out: Codable { let file: String; let pages: Int; let lines: [Line]; let text: String }
    let out = Out(file: path, pages: images.count, lines: all,
                  text: all.map { $0.text }.joined(separator: "\n"))
    if let d = try? enc.encode(out), let s = String(data: d, encoding: .utf8) { print(s) }
} else {
    print(all.map { $0.text }.joined(separator: "\n"))
}
