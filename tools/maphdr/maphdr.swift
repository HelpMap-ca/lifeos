// maphdr — merge bracketed exposures into one photo that holds both ends.
//
// The single worst problem in property photography: a room with a window. Expose
// for the room and the window is a white hole; expose for the window and the
// room is a cave. A phone's own HDR guesses; brackets do not have to.
//
//   maphdr <img1> <img2> [img3 …] [--strength N] [--shadows N] [--highlights N]
//          [--contrast-weight N] [--exposure-weight N] [--smooth N]
//          [--no-align] [--max PX] [--out FILE] [--json]
//
// METHOD, stated plainly because it matters:
//
// This is **exposure fusion** (Mertens et al.), not tone-mapped HDR. It never
// builds a radiance map and never tone-maps one back down — it picks, per pixel,
// which of your frames looked best there, and blends. That is why the result
// looks like a photograph rather than the crunchy "HDR look": there is no tone
// curve to overdo.
//
// Weights per pixel per frame:
//   well-exposedness — a parabola peaking at mid-grey, so blown and blocked
//                      pixels score ~0 and correctly-exposed ones score 1
//   local contrast   — |detail| via a high-pass, so the frame that actually
//                      resolved texture there wins over a flat one
//
// Mertens blends those through a Laplacian pyramid. This uses the documented
// simplification instead: **blur the weight maps** before normalising. The
// pyramid exists to stop low-frequency weight changes producing banding and
// halos; a wide blur on the weights buys most of that for a fraction of the
// code. The tradeoff is slightly less local punch, which `--strength` and the
// shadow/highlight controls can put back. Honest cost, stated up front.
//
// Offline: Vision (alignment) + Core Image. No network, no model.

import Foundation
import Vision
import CoreImage
import ImageIO
import UniformTypeIdentifiers
import AppKit

func die(_ m: String) -> Never {
    FileHandle.standardError.write(("maphdr: " + m + "\n").data(using: .utf8)!)
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
let noAlign = flag("--no-align")
let outPath = val("--out")
let strength = min(max(num("--strength") ?? 1.0, 0), 1)
let shadows = min(max(num("--shadows") ?? 0, 0), 1)
let highlights = min(max(num("--highlights") ?? 1, 0), 1)
let wContrast = max(num("--contrast-weight") ?? 1.0, 0)
let wExposure = max(num("--exposure-weight") ?? 1.0, 0)
let smoothPx = max(num("--smooth") ?? 24, 0)
let maxPx = Int(num("--max") ?? 2400)

let inputs = args.filter { !$0.hasPrefix("--") }
guard inputs.count >= 2 else {
    die("give at least two bracketed frames of the same scene — one exposed for the bright part, one for the dark")
}

// MARK: - load, at a common size

// Everything happens at one resolution. Brackets from the same camera match, but
// a mixed set would otherwise silently misalign, and a full-res 5-frame fusion
// is a lot of memory for no visible gain at map scale.
var frames: [CGImage] = []
var srcProps: [CFString: Any] = [:]
for (i, p) in inputs.enumerated() {
    let url = URL(fileURLWithPath: (p as NSString).expandingTildeInPath)
    guard FileManager.default.fileExists(atPath: url.path) else { die("no such file: \(url.path)") }
    guard let s = CGImageSourceCreateWithURL(url as CFURL, nil) else { die("not an image: \(url.path)") }
    if i == 0 {
        srcProps = CGImageSourceCopyPropertiesAtIndex(s, 0, nil) as? [CFString: Any] ?? [:]
    }
    let opts: [CFString: Any] = [
        kCGImageSourceCreateThumbnailFromImageAlways: true,
        kCGImageSourceCreateThumbnailWithTransform: true,
        kCGImageSourceThumbnailMaxPixelSize: maxPx > 0 ? maxPx : 8192,
        kCGImageSourceShouldCacheImmediately: true,
    ]
    guard let img = CGImageSourceCreateThumbnailAtIndex(s, 0, opts as CFDictionary) else {
        die("could not decode \(url.lastPathComponent)")
    }
    frames.append(img)
}
guard frames.dropFirst().allSatisfy({ $0.width == frames[0].width && $0.height == frames[0].height }) else {
    die("the frames are not the same shape once scaled — are they really the same shot?")
}

// Float working space: the weight maths sums and divides values that must be
// allowed past 1.0 before normalisation. In an 8-bit space they would clamp and
// the bright frame would quietly dominate.
let ctx = CIContext(options: [.workingFormat: CIFormat.RGBAh,
                              .workingColorSpace: CGColorSpace(name: CGColorSpace.sRGB)!])
var images = frames.map { CIImage(cgImage: $0) }
let extent = images[0].extent

// MARK: - align
//
// Hand-held brackets drift by a few pixels between frames, and unaligned fusion
// shows it as coloured fringing on every edge. Vision's translational
// registration is the right tool: bracket drift is overwhelmingly translation.
var shifts: [String] = []
var aligned = 0
if !noAlign {
    for i in 1..<images.count {
        let req = VNTranslationalImageRegistrationRequest(targetedCGImage: frames[i], options: [:])
        let handler = VNImageRequestHandler(cgImage: frames[0], options: [:])
        do {
            try handler.perform([req])
            if let obs = req.results?.first as? VNImageTranslationAlignmentObservation {
                let t = obs.alignmentTransform
                // Sanity gate. Registration keys off structure, and a heavily
                // over-exposed frame has almost none — it reported a 213 px
                // shift between two frames that were geometrically identical.
                // Real bracket drift is a hand tremor: a few pixels. Anything
                // past 4% of the frame is the matcher failing, and applying it
                // would be far worse than leaving the frame where it is.
                let limit = max(extent.width, extent.height) * 0.04
                if abs(t.tx) > limit || abs(t.ty) > limit {
                    shifts.append(String(format: "rejected %+.0f,%+.0f", -t.tx, -t.ty))
                } else if abs(t.tx) > 0.01 || abs(t.ty) > 0.01 {
                    images[i] = images[i].transformed(
                        by: CGAffineTransform(translationX: -t.tx, y: -t.ty)).cropped(to: extent)
                    aligned += 1
                    shifts.append(String(format: "%+.1f,%+.1f", -t.tx, -t.ty))
                } else {
                    shifts.append("0,0")
                }
            }
        } catch {
            shifts.append("failed")     // an unalignable frame still fuses
        }
    }
}

// MARK: - weights

func filt(_ name: String, _ p: [String: Any]) -> CIImage? {
    CIFilter(name: name, parameters: p)?.outputImage
}
/// Luminance in all three channels — the weight maps are greyscale by nature.
func luma(_ img: CIImage) -> CIImage {
    filt("CIColorMatrix", [
        kCIInputImageKey: img,
        "inputRVector": CIVector(x: 0.2126, y: 0.7152, z: 0.0722, w: 0),
        "inputGVector": CIVector(x: 0.2126, y: 0.7152, z: 0.0722, w: 0),
        "inputBVector": CIVector(x: 0.2126, y: 0.7152, z: 0.0722, w: 0),
        "inputAVector": CIVector(x: 0, y: 0, z: 0, w: 1)]) ?? img
}
/// Force alpha back to 1.
///
/// This is load-bearing, not hygiene. CIAdditionCompositing adds the alpha
/// channel along with the colour, so summing three opaque weight maps yields
/// alpha = 3. Core Image then treats the result as premultiplied, the
/// normalising divide operates on nonsense, and the render clips to white —
/// which is exactly what the first build produced. Every accumulation step has
/// to put alpha back.
func opaque(_ img: CIImage) -> CIImage {
    filt("CIColorMatrix", [
        kCIInputImageKey: img,
        "inputAVector": CIVector(x: 0, y: 0, z: 0, w: 0),
        "inputBiasVector": CIVector(x: 0, y: 0, z: 0, w: 1)]) ?? img
}
func add(_ a: CIImage, _ b: CIImage) -> CIImage {
    opaque(filt("CIAdditionCompositing", [kCIInputImageKey: a, kCIInputBackgroundImageKey: b]) ?? a)
}
/// |a - b| built from two clamping subtractions. CISubtractBlendMode floors at
/// zero, so each direction keeps one half of the signed difference and the sum
/// is the magnitude — no custom kernel, no signed intermediate to lose.
func absDiff(_ a: CIImage, _ b: CIImage) -> CIImage {
    let ab = filt("CISubtractBlendMode", [kCIInputImageKey: a, kCIInputBackgroundImageKey: b]) ?? a
    let ba = filt("CISubtractBlendMode", [kCIInputImageKey: b, kCIInputBackgroundImageKey: a]) ?? a
    return add(ab, ba)
}

var weights: [CIImage] = []
for img in images {
    let g = luma(img)

    // Well-exposedness: 4x(1-x) — a parabola peaking at mid-grey, zero at both
    // clipped ends. CIColorPolynomial evaluates c0 + c1x + c2x² + c3x³ per
    // channel, so this is exact rather than approximated.
    var wexp = filt("CIColorPolynomial", [
        kCIInputImageKey: g,
        "inputRedCoefficients":   CIVector(x: 0, y: 4, z: -4, w: 0),
        "inputGreenCoefficients": CIVector(x: 0, y: 4, z: -4, w: 0),
        "inputBlueCoefficients":  CIVector(x: 0, y: 4, z: -4, w: 0)]) ?? g
    if wExposure != 1 {
        wexp = filt("CIGammaAdjust", [kCIInputImageKey: wexp, "inputPower": 1.0 / max(wExposure, 0.01)]) ?? wexp
    }

    // Local contrast: high-pass magnitude. A frame that resolved texture here
    // should beat one that rendered the same area as flat grey.
    // clampedToExtent before every blur. Without it CIGaussianBlur samples
    // transparent black past the border, which drags the weight map down at
    // the edges — and after normalisation that reads as a vignette burned into
    // the fused photo. It is very visible and easy to mistake for lens falloff.
    let blurred = filt("CIGaussianBlur", [
        kCIInputImageKey: g.clampedToExtent(), kCIInputRadiusKey: 2.0])?
        .cropped(to: extent) ?? g
    var wcon = absDiff(g, blurred)
    // Lift it off zero: pure high-pass is ~0 across smooth walls, which would
    // make the weight there depend on nothing at all.
    wcon = filt("CIColorMatrix", [
        kCIInputImageKey: wcon,
        "inputBiasVector": CIVector(x: 0.08, y: 0.08, z: 0.08, w: 0)]) ?? wcon
    if wContrast != 1 {
        wcon = filt("CIGammaAdjust", [kCIInputImageKey: wcon, "inputPower": 1.0 / max(wContrast, 0.01)]) ?? wcon
    }

    var w = filt("CIMultiplyCompositing", [kCIInputImageKey: wexp, kCIInputBackgroundImageKey: wcon]) ?? wexp
    // The blur that stands in for the Laplacian pyramid.
    if smoothPx > 0 {
        w = filt("CIGaussianBlur", [
            kCIInputImageKey: w.clampedToExtent(), kCIInputRadiusKey: smoothPx])?
            .cropped(to: extent) ?? w
    }
    // Epsilon so the normalising divide can never be 0/0 in a region every
    // frame considers hopeless.
    w = filt("CIColorMatrix", [
        kCIInputImageKey: w,
        "inputBiasVector": CIVector(x: 0.001, y: 0.001, z: 0.001, w: 0)]) ?? w
    weights.append(w)
}

// MARK: - normalise and blend

var total = weights[0]
for w in weights.dropFirst() { total = add(w, total) }

var fused: CIImage? = nil
for (img, w) in zip(images, weights) {
    // CIDivideBlendMode divides the BACKDROP by the SOURCE, so backdrop=w and
    // source=total gives w / total — the normalised weight, in [0,1].
    guard let normRaw = filt("CIDivideBlendMode", [
            kCIInputImageKey: total, kCIInputBackgroundImageKey: w]) else { continue }
    let norm = opaque(normRaw)
    guard let partRaw = filt("CIMultiplyCompositing", [
            kCIInputImageKey: norm, kCIInputBackgroundImageKey: img]) else { continue }
    let part = opaque(partRaw)
    fused = fused.map { add(part, $0) } ?? part
}
guard var result = fused else { die("fusion produced nothing") }
result = result.cropped(to: extent)

// MARK: - controls

// --strength dials the fusion back toward the middle frame, which is the honest
// "less HDR" control: at 0 you get your original exposure back, unchanged.
let reference = images[images.count / 2]
if strength < 1 {
    let a = filt("CIColorMatrix", [kCIInputImageKey: result,
        "inputAVector": CIVector(x: 0, y: 0, z: 0, w: strength)]) ?? result
    result = filt("CISourceOverCompositing", [
        kCIInputImageKey: a, kCIInputBackgroundImageKey: reference]) ?? result
}
if shadows != 0 || highlights != 1 {
    result = filt("CIHighlightShadowAdjust", [
        kCIInputImageKey: result,
        "inputShadowAmount": shadows,
        "inputHighlightAmount": highlights]) ?? result
}

guard let cgOut = ctx.createCGImage(result, from: extent) else { die("could not render the fused image") }

// MARK: - write

let firstURL = URL(fileURLWithPath: (inputs[0] as NSString).expandingTildeInPath)
let destURL: URL = {
    if let o = outPath { return URL(fileURLWithPath: (o as NSString).expandingTildeInPath) }
    let stem = firstURL.deletingPathExtension().lastPathComponent
    return firstURL.deletingLastPathComponent().appendingPathComponent("\(stem)--hdr.jpg")
}()
guard let dest = CGImageDestinationCreateWithURL(destURL as CFURL, UTType.jpeg.identifier as CFString, 1, nil)
else { die("could not create \(destURL.path)") }
var props = srcProps                       // keep capture time + heading
props[kCGImageDestinationLossyCompressionQuality] = 0.94
CGImageDestinationAddImage(dest, cgOut, props as CFDictionary)
guard CGImageDestinationFinalize(dest) else { die("could not write \(destURL.path)") }

let outBytes = (try? FileManager.default.attributesOfItem(atPath: destURL.path)[.size] as? Int) ?? 0
if asJSON {
    let report: [String: Any] = [
        "ok": true,
        "inputs": inputs.map { URL(fileURLWithPath: $0).lastPathComponent },
        "frames": images.count,
        "output": destURL.path,
        "pixels": "\(cgOut.width)×\(cgOut.height)",
        "bytesOut": outBytes,
        "aligned": aligned,
        "shifts": shifts,
        "strength": strength, "shadows": shadows, "highlights": highlights,
        "smooth": smoothPx,
        "method": "exposure fusion (well-exposedness × local contrast, smoothed weights)",
    ]
    print(String(data: try! JSONSerialization.data(withJSONObject: report,
          options: [.prettyPrinted, .sortedKeys]), encoding: .utf8)!)
} else {
    print("\(images.count) frames  →  \(destURL.lastPathComponent)")
    print("  aligned   \(noAlign ? "skipped (--no-align)" : "\(aligned) of \(images.count - 1) frame(s) shifted\(shifts.isEmpty ? "" : "  [" + shifts.joined(separator: "  ") + "]")")")
    print(String(format: "  fusion    strength %.2f · smooth %.0fpx · shadows %.2f · highlights %.2f",
                 strength, smoothPx, shadows, highlights))
    print("  output    \(cgOut.width)×\(cgOut.height) · \(outBytes / 1024) KB")
    print("  method    exposure fusion — no radiance map, no tone curve, so no crunchy HDR look")
}
