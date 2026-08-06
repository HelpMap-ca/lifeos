// mapmatte — lift the subject off its background.
//
// Uses Vision's foreground-instance mask, the same machinery Photos uses when
// you long-press a subject and it pops out. It runs on the Neural Engine: no
// model download, no VRAM, no network, and it is markedly better at hair and
// foliage edges than any threshold-and-flood approach.
//
//   mapmatte <image> [--all] [--instance N] [--feather PX] [--edge PX]
//            [--background transparent|white|#RRGGBB] [--out FILE] [--json]
//
// The assist controls exist because a mask is never quite right at the edge:
//   --edge   contract (negative) or expand (positive) the mask, in pixels.
//            Contracting by a pixel or two is the usual fix for a bright halo,
//            which is the most common visible failure.
//   --feather softens the cut so a hard mask does not read as a sticker.
//
// Offline: Vision + Core Image.

import Foundation
import Vision
import CoreImage
import ImageIO
import UniformTypeIdentifiers
import AppKit

func die(_ m: String) -> Never {
    FileHandle.standardError.write(("mapmatte: " + m + "\n").data(using: .utf8)!)
    exit(1)
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
let wantAll = flag("--all")
let instancePick = val("--instance").flatMap { Int($0) }
let feather = num("--feather") ?? 0
let edge = num("--edge") ?? 0
let bgSpec = (val("--background") ?? "transparent").lowercased()
let outPath = val("--out")
// A full-resolution RGBA cutout of a 48 MP photo is a ~31 MB PNG — absurd on a
// platform whose whole argument is that data has a cost. Cap the long edge by
// default and make full resolution the explicit choice (--max 0), so the
// responsible output is what you get without thinking about it.
let maxPx = Int(num("--max") ?? 2048)

guard let input = args.first else {
    die("usage: mapmatte <image> [--all] [--feather PX] [--edge PX] [--background white|#RRGGBB] [--out FILE]")
}
let inURL = URL(fileURLWithPath: (input as NSString).expandingTildeInPath)
guard FileManager.default.fileExists(atPath: inURL.path) else { die("no such file: \(inURL.path)") }
guard let src = CGImageSourceCreateWithURL(inURL as CFURL, nil),
      let cgIn = CGImageSourceCreateImageAtIndex(src, 0, [kCGImageSourceShouldCacheImmediately: true] as CFDictionary)
else { die("not an image this Mac can read") }

let inBytes = (try? FileManager.default.attributesOfItem(atPath: inURL.path)[.size] as? Int) ?? 0
let ctx = CIContext(options: [.workingColorSpace: CGColorSpace(name: CGColorSpace.sRGB)!])
let base = CIImage(cgImage: cgIn)
let extent = base.extent

// MARK: - the mask

guard #available(macOS 14.0, *) else { die("needs macOS 14 or later for foreground masking") }

let request = VNGenerateForegroundInstanceMaskRequest()
let handler = VNImageRequestHandler(cgImage: cgIn, options: [:])
do { try handler.perform([request]) } catch { die("Vision failed: \(error.localizedDescription)") }

guard let observation = request.results?.first, !observation.allInstances.isEmpty else {
    die("no distinct subject found — this works on photos with a clear foreground subject, not on flat scenes like a wall or a landscape")
}

let instanceCount = observation.allInstances.count
// Default to every instance Vision found. A single-subject cutout is the
// exception (--instance N), not the rule: a room usually has several.
var chosen: IndexSet = observation.allInstances
if let pick = instancePick {
    guard observation.allInstances.contains(pick) else {
        die("no instance \(pick) — this image has \(instanceCount) (1…\(instanceCount))")
    }
    chosen = IndexSet(integer: pick)
} else if !wantAll, let first = observation.allInstances.first {
    chosen = IndexSet(integer: first)
}

var maskBuffer: CVPixelBuffer
do {
    maskBuffer = try observation.generateScaledMaskForImage(forInstances: chosen, from: handler)
} catch {
    die("could not build the mask: \(error.localizedDescription)")
}
var mask = CIImage(cvPixelBuffer: maskBuffer)
// The mask comes back at the analysis resolution; stretch it onto the photo.
if mask.extent != extent {
    mask = mask.transformed(by: CGAffineTransform(
        scaleX: extent.width / mask.extent.width,
        y: extent.height / mask.extent.height))
}

// MARK: - assist: edge shift, then feather
//
// Order matters. Morphology on an already-blurred mask smears the softness
// instead of moving the boundary, so contract/expand has to come first.
if edge != 0 {
    let r = abs(edge)
    let name = edge < 0 ? "CIMorphologyMinimum" : "CIMorphologyMaximum"
    if let f = CIFilter(name: name, parameters: [kCIInputImageKey: mask, kCIInputRadiusKey: r]),
       let out = f.outputImage { mask = out.cropped(to: extent) }
}
if feather > 0 {
    if let f = CIFilter(name: "CIGaussianBlur", parameters: [kCIInputImageKey: mask, kCIInputRadiusKey: feather]),
       let out = f.outputImage { mask = out.cropped(to: extent) }
}

// MARK: - composite

func parseColor(_ s: String) -> CIColor? {
    if s == "white" { return CIColor(red: 1, green: 1, blue: 1) }
    if s == "black" { return CIColor(red: 0, green: 0, blue: 0) }
    guard s.hasPrefix("#"), s.count == 7,
          let v = Int(s.dropFirst(), radix: 16) else { return nil }
    return CIColor(red: Double((v >> 16) & 255) / 255,
                   green: Double((v >> 8) & 255) / 255,
                   blue: Double(v & 255) / 255)
}

var transparent = bgSpec == "transparent"
var bgColor: CIColor? = nil
if !transparent {
    guard let c = parseColor(bgSpec) else {
        die("unreadable --background '\(bgSpec)' — use transparent, white, black or #RRGGBB")
    }
    bgColor = c
}

// CIBlendWithMask against a clear background yields straight alpha; against a
// colour it flattens. Same filter either way.
let backdrop: CIImage = transparent
    ? CIImage(color: CIColor(red: 0, green: 0, blue: 0, alpha: 0)).cropped(to: extent)
    : CIImage(color: bgColor!).cropped(to: extent)

guard let blend = CIFilter(name: "CIBlendWithMask", parameters: [
    kCIInputImageKey: base,
    kCIInputBackgroundImageKey: backdrop,
    kCIInputMaskImageKey: mask]), let composited = blend.outputImage else {
    die("could not composite")
}

// Transparency needs PNG; a flattened result may as well stay JPEG.
let usePNG = transparent
let destURL: URL = {
    if let o = outPath { return URL(fileURLWithPath: (o as NSString).expandingTildeInPath) }
    let stem = inURL.deletingPathExtension().lastPathComponent
    return inURL.deletingLastPathComponent()
        .appendingPathComponent("\(stem)--cutout.\(usePNG ? "png" : "jpg")")
}()

// Scale after compositing, not before: masking at full resolution keeps the
// edge detail that a downscale would otherwise smear into the background.
var finalImage = composited
var finalExtent = extent
if maxPx > 0, max(extent.width, extent.height) > CGFloat(maxPx) {
    let s = CGFloat(maxPx) / max(extent.width, extent.height)
    finalImage = composited.transformed(by: CGAffineTransform(scaleX: s, y: s))
    finalExtent = finalImage.extent
}
guard let cgOut = ctx.createCGImage(finalImage, from: finalExtent,
        format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!) else {
    die("could not render the cutout")
}
guard let dest = CGImageDestinationCreateWithURL(
        destURL as CFURL, (usePNG ? UTType.png : UTType.jpeg).identifier as CFString, 1, nil) else {
    die("could not create \(destURL.path)")
}
var props: [CFString: Any] = [:]
if !usePNG { props[kCGImageDestinationLossyCompressionQuality] = 0.92 }
CGImageDestinationAddImage(dest, cgOut, props as CFDictionary)
guard CGImageDestinationFinalize(dest) else { die("could not write \(destURL.path)") }

let outBytes = (try? FileManager.default.attributesOfItem(atPath: destURL.path)[.size] as? Int) ?? 0
if asJSON {
    let report: [String: Any] = [
        "ok": true,
        "input": inURL.lastPathComponent,
        "output": destURL.path,
        "instancesFound": instanceCount,
        "instancesUsed": chosen.count,
        "pixels": "\(cgOut.width)×\(cgOut.height)",
        "background": bgSpec,
        "feather": feather,
        "edge": edge,
        "bytesIn": inBytes,
        "bytesOut": outBytes,
        "format": usePNG ? "png" : "jpeg",
    ]
    print(String(data: try! JSONSerialization.data(withJSONObject: report,
          options: [.prettyPrinted, .sortedKeys]), encoding: .utf8)!)
} else {
    print("\(inURL.lastPathComponent)  →  \(destURL.lastPathComponent)")
    print("  subjects  \(instanceCount) found, \(chosen.count) used\(wantAll || instancePick != nil ? "" : "  (--all to keep them all)")")
    print("  edge      \(edge == 0 ? "unshifted" : (edge < 0 ? "contracted \(Int(-edge))px" : "expanded \(Int(edge))px"))\(feather > 0 ? ", feathered \(Int(feather))px" : "")")
    print("  output    \(usePNG ? "PNG with alpha" : "flattened on \(bgSpec)") · \(outBytes / 1024) KB")
}
