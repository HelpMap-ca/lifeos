// mapcolor — grade a photo, and save the grade so every other photo matches.
//
// The slider is not the feature. Per-photo fiddling is precisely what produces
// the inconsistency map.ca is trying to avoid: twelve photos of one property,
// each adjusted by eye, each slightly different, and the listing looks like a
// collage. So the unit of work here is a PRESET — a few numbers in a JSON file.
// Neutralise one reference shot, save it, apply it to all twelve. They then
// match by construction rather than by care.
//
//   mapcolor <image> [--auto] [--preset FILE] [--save-preset FILE]
//            [--temp N] [--tint N] [--exposure EV] [--contrast N]
//            [--saturation N] [--vibrance N] [--shadows N] [--highlights N]
//            [--out FILE] [--png] [--json]
//
// --auto measures the image's own colour cast and cancels it (grey-world), which
// is the honest version of "auto white balance": it assumes the average of a
// real scene is neutral. Combine with --save-preset to make a house style.
//
// EXIF is carried through unchanged — capture time and compass heading have to
// survive the grade so `mapshot` can still find them downstream.
//
// Offline: Core Image + ImageIO. No network, no model.

import Foundation
import CoreImage
import ImageIO
import UniformTypeIdentifiers
import AppKit

func die(_ m: String) -> Never {
    FileHandle.standardError.write(("mapcolor: " + m + "\n").data(using: .utf8)!)
    exit(1)
}

// MARK: - the grade

/// Everything that defines a look, and nothing that defines one photo.
/// Codable so a grade is a file you can hand to someone.
struct Grade: Codable {
    var temp: Double = 0          // -100 warm  ..  +100 cool
    var tint: Double = 0          // -100 green ..  +100 magenta
    var exposure: Double = 0      // EV
    var contrast: Double = 1.0    // 1 = unchanged
    var saturation: Double = 1.0  // 1 = unchanged
    var vibrance: Double = 0      // -1 .. 1
    var shadows: Double = 0       // 0 .. 1, lifts
    var highlights: Double = 1.0  // 1 = unchanged, lower recovers
    var gainR: Double = 1.0       // grey-world white balance, from --auto
    var gainG: Double = 1.0
    var gainB: Double = 1.0
    var note: String = ""
}

var args = Array(CommandLine.arguments.dropFirst())
func val(_ n: String) -> String? {
    guard let i = args.firstIndex(of: n), i + 1 < args.count else { return nil }
    let v = args[i + 1]; args.removeSubrange(i...(i + 1)); return v
}
func num(_ n: String) -> Double? { val(n).flatMap(Double.init) }
func flag(_ n: String) -> Bool {
    guard let i = args.firstIndex(of: n) else { return false }
    args.remove(at: i); return true
}

let asJSON = flag("--json")
let wantPNG = flag("--png")
let auto = flag("--auto")
let presetIn = val("--preset")
let presetOut = val("--save-preset")
let outPath = val("--out")

var g = Grade()
// A preset is the baseline; explicit flags override it. That ordering lets you
// apply the house style and still nudge one difficult photo.
if let p = presetIn {
    let url = URL(fileURLWithPath: (p as NSString).expandingTildeInPath)
    guard let data = try? Data(contentsOf: url),
          let loaded = try? JSONDecoder().decode(Grade.self, from: data) else {
        die("could not read preset: \(url.path)")
    }
    g = loaded
}
if let v = num("--temp")       { g.temp = v }
if let v = num("--tint")       { g.tint = v }
if let v = num("--exposure")   { g.exposure = v }
if let v = num("--contrast")   { g.contrast = v }
if let v = num("--saturation") { g.saturation = v }
if let v = num("--vibrance")   { g.vibrance = v }
if let v = num("--shadows")    { g.shadows = v }
if let v = num("--highlights") { g.highlights = v }
if let v = val("--note")       { g.note = v }

guard let input = args.first else {
    die("usage: mapcolor <image> [--auto] [--preset F] [--save-preset F] [--temp N] … [--out F]")
}
let inURL = URL(fileURLWithPath: (input as NSString).expandingTildeInPath)
guard FileManager.default.fileExists(atPath: inURL.path) else { die("no such file: \(inURL.path)") }
guard let src = CGImageSourceCreateWithURL(inURL as CFURL, nil),
      let cgIn = CGImageSourceCreateImageAtIndex(src, 0, [kCGImageSourceShouldCacheImmediately: true] as CFDictionary)
else { die("not an image this Mac can read") }

// Carry the original metadata through: heading and capture time must survive.
let srcProps = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [CFString: Any] ?? [:]

let ctx = CIContext(options: [.workingColorSpace: CGColorSpace(name: CGColorSpace.sRGB)!])
var img = CIImage(cgImage: cgIn)
let extent = img.extent

// MARK: - --auto: measure the cast, then cancel it
//
// CIAreaAverage reduces the whole frame to one pixel on the GPU, which is the
// cheap way to ask "what colour is this photo on average?". Grey-world says a
// real scene averages to neutral, so whatever the average ISN'T is the cast.
if auto {
    guard let avg = CIFilter(name: "CIAreaAverage", parameters: [
        kCIInputImageKey: img,
        kCIInputExtentKey: CIVector(cgRect: extent)]).flatMap({ $0.outputImage }) else {
        die("could not measure the image")
    }
    var px = [UInt8](repeating: 0, count: 4)
    ctx.render(avg, toBitmap: &px, rowBytes: 4, bounds: CGRect(x: 0, y: 0, width: 1, height: 1),
               format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB))
    let r = Double(px[0]), gr = Double(px[1]), b = Double(px[2])
    if r > 1, gr > 1, b > 1 {
        // Scale red and blue onto green. Green is the reference because sensors
        // carry twice as many green photosites and it is the least noisy channel.
        g.gainR = gr / r
        g.gainB = gr / b
        g.gainG = 1.0
        // A cast beyond ±40% is almost certainly a genuinely coloured subject
        // (a red barn, a field) rather than a white-balance error. Clamp so
        // --auto cannot turn a real colour into grey.
        g.gainR = min(max(g.gainR, 0.6), 1.4)
        g.gainB = min(max(g.gainB, 0.6), 1.4)
    }
}

// MARK: - apply, in a deliberate order
//
// White balance first (it is a correction), then exposure, then the creative
// controls. Grading before correcting bakes the cast into everything after it.

func apply(_ name: String, _ params: [String: Any]) {
    var p = params; p[kCIInputImageKey] = img
    guard let f = CIFilter(name: name, parameters: p), let out = f.outputImage else { return }
    img = out
}

if g.gainR != 1 || g.gainG != 1 || g.gainB != 1 {
    apply("CIColorMatrix", [
        "inputRVector": CIVector(x: g.gainR, y: 0, z: 0, w: 0),
        "inputGVector": CIVector(x: 0, y: g.gainG, z: 0, w: 0),
        "inputBVector": CIVector(x: 0, y: 0, z: g.gainB, w: 0)])
}
if g.temp != 0 || g.tint != 0 {
    // CITemperatureAndTint maps a stated neutral onto a target neutral. Positive
    // --temp should read as COOLER to a photographer, so the target moves up in
    // Kelvin from 6500 and the image shifts blue.
    apply("CITemperatureAndTint", [
        "inputNeutral": CIVector(x: 6500, y: 0),
        "inputTargetNeutral": CIVector(x: 6500 + g.temp * 25, y: g.tint * 0.6)])
}
if g.exposure != 0 { apply("CIExposureAdjust", [kCIInputEVKey: g.exposure]) }
if g.shadows != 0 || g.highlights != 1 {
    apply("CIHighlightShadowAdjust", [
        "inputShadowAmount": g.shadows,
        "inputHighlightAmount": g.highlights])
}
if g.contrast != 1 || g.saturation != 1 {
    apply("CIColorControls", [
        kCIInputContrastKey: g.contrast,
        kCIInputSaturationKey: g.saturation])
}
if g.vibrance != 0 { apply("CIVibrance", ["inputAmount": g.vibrance]) }

guard let cgOut = ctx.createCGImage(img, from: extent) else { die("could not render the graded image") }

// MARK: - write

let destURL: URL = {
    if let o = outPath { return URL(fileURLWithPath: (o as NSString).expandingTildeInPath) }
    let base = inURL.deletingPathExtension().lastPathComponent
    return inURL.deletingLastPathComponent()
        .appendingPathComponent("\(base)--graded.\(wantPNG ? "png" : "jpg")")
}()
let type = (wantPNG ? UTType.png : UTType.jpeg).identifier as CFString
guard let dest = CGImageDestinationCreateWithURL(destURL as CFURL, type, 1, nil) else {
    die("could not create \(destURL.path)")
}
var props = srcProps
if !wantPNG { props[kCGImageDestinationLossyCompressionQuality] = 0.95 }
CGImageDestinationAddImage(dest, cgOut, props as CFDictionary)
guard CGImageDestinationFinalize(dest) else { die("could not write \(destURL.path)") }

if let p = presetOut {
    let url = URL(fileURLWithPath: (p as NSString).expandingTildeInPath)
    let enc = JSONEncoder(); enc.outputFormatting = [.prettyPrinted, .sortedKeys]
    do { try enc.encode(g).write(to: url) } catch { die("could not save preset: \(error)") }
}

let outBytes = (try? FileManager.default.attributesOfItem(atPath: destURL.path)[.size] as? Int) ?? 0
if asJSON {
    let report: [String: Any] = [
        "ok": true,
        "input": inURL.lastPathComponent,
        "output": destURL.path,
        "bytesOut": outBytes,
        "pixels": "\(cgOut.width)×\(cgOut.height)",
        "auto": auto,
        "presetSaved": presetOut ?? "",
        "grade": ["temp": g.temp, "tint": g.tint, "exposure": g.exposure,
                  "contrast": g.contrast, "saturation": g.saturation,
                  "vibrance": g.vibrance, "shadows": g.shadows,
                  "highlights": g.highlights,
                  "gainR": (g.gainR * 1000).rounded() / 1000,
                  "gainG": (g.gainG * 1000).rounded() / 1000,
                  "gainB": (g.gainB * 1000).rounded() / 1000,
                  "note": g.note],
    ]
    print(String(data: try! JSONSerialization.data(withJSONObject: report,
          options: [.prettyPrinted, .sortedKeys]), encoding: .utf8)!)
} else {
    print("\(inURL.lastPathComponent)  →  \(destURL.lastPathComponent)")
    // Show the gains whenever they are doing something, not only for --auto:
    // applying a preset must not look like a no-op when it is correcting colour.
    if g.gainR != 1 || g.gainG != 1 || g.gainB != 1 {
        print(String(format: "  white bal R×%.3f  G×%.3f  B×%.3f  (%@)",
                     g.gainR, g.gainG, g.gainB, auto ? "measured with --auto" : "from preset"))
    }
    print(String(format: "  grade     temp %+.0f  tint %+.0f  ev %+.2f  contrast %.2f  sat %.2f",
                 g.temp, g.tint, g.exposure, g.contrast, g.saturation))
    if let p = presetOut { print("  preset    saved → \(p)   (apply with --preset)") }
    print("  metadata  carried through — heading and capture time survive the grade")
}
