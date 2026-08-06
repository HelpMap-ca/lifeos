// mapshot — repackage any photo into the map.ca capture standard.
//
// A photo uploaded to map.ca is INFORMATION, not a social post. That single
// sentence decides everything this tool does:
//
//   · Size is a budget, not a preference. A 4 MB phone photo carries maybe
//     150 KB of information at map scale. The rest is bytes the visitor pays
//     for in load time and the platform pays for in storage, forever.
//   · Metadata splits cleanly in two. A photo's *informational* metadata —
//     when it was taken, which way the camera was pointing — is exactly what
//     makes it useful on a map. Its *incidental* metadata — the camera's
//     serial number, the owner's name, the exact GPS of someone's living room
//     — is a privacy liability that no one chose to publish.
//
// So: resize to a byte budget, keep the facts that make the photo evidence,
// and strip everything else — then SAY what was stripped, because the point
// is to teach the habit, not to launder the file silently.
//
//   mapshot <image> [--preset thumb|card|full] [--out FILE] [--keep-gps]
//           [--max PX] [--target-kb N] [--json]
//
// Offline: ImageIO only. No network, no model, no dependency.

import Foundation
import ImageIO
import UniformTypeIdentifiers
import CoreGraphics

// MARK: - presets — "the map site bite"

struct Preset { let name: String; let maxPx: Int; let targetKB: Int }
let PRESETS = [
    // Long edge, and the byte budget it must land under.
    "thumb": Preset(name: "thumb", maxPx: 640,  targetKB: 60),
    "card":  Preset(name: "card",  maxPx: 1280, targetKB: 150),
    "full":  Preset(name: "full",  maxPx: 2048, targetKB: 320),
]

// MARK: - which metadata is information, and which is exhaust

/// Kept: the facts that make a photo usable as evidence on a map.
/// GPSImgDirection is the quiet hero — it is what lets the platform say
/// "this is the view NORTH from here", which is the whole point of a map photo.
let KEEP_EXIF = ["DateTimeOriginal", "DateTimeDigitized", "PixelXDimension",
                 "PixelYDimension", "FocalLenIn35mmFilm", "LensModel"]
let KEEP_GPS  = ["Latitude", "LatitudeRef", "Longitude", "LongitudeRef",
                 "Altitude", "AltitudeRef", "ImgDirection", "ImgDirectionRef",
                 "DestBearing", "DestBearingRef"]
let KEEP_TIFF = ["Make", "Model", "Orientation"]

/// Stripped, and named in the report so the user learns what was in there.
/// These are the tags that identify a PERSON or a DEVICE rather than a scene.
let SENSITIVE: [(key: String, why: String)] = [
    ("BodySerialNumber",   "camera serial number — uniquely identifies your device"),
    ("SerialNumber",       "device serial number"),
    ("LensSerialNumber",   "lens serial number"),
    ("CameraOwnerName",    "camera owner name"),
    ("Artist",             "artist/owner name"),
    ("Copyright",          "copyright holder name"),
    ("Software",           "editing software and version — fingerprints your setup"),
    ("HostComputer",       "the computer that touched the file"),
    ("UserComment",        "free-text comment field"),
    ("ImageDescription",   "embedded description"),
    ("MakerNote",          "vendor blob — often contains serials and face data"),
]

func die(_ msg: String) -> Never {
    FileHandle.standardError.write(("mapshot: " + msg + "\n").data(using: .utf8)!)
    exit(1)
}

// MARK: - args

var args = Array(CommandLine.arguments.dropFirst())
func flagValue(_ name: String) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    let v = args[i + 1]; args.removeSubrange(i...(i + 1)); return v
}
func flag(_ name: String) -> Bool {
    guard let i = args.firstIndex(of: name) else { return false }
    args.remove(at: i); return true
}

let asJSON = flag("--json")
let keepGPS = flag("--keep-gps")
let presetName = flagValue("--preset") ?? "card"
let outPath = flagValue("--out")
let maxOverride = flagValue("--max").flatMap { Int($0) }
let targetOverride = flagValue("--target-kb").flatMap { Int($0) }

guard let preset = PRESETS[presetName] else {
    die("unknown preset '\(presetName)' — use thumb, card or full")
}
guard let input = args.first else {
    die("usage: mapshot <image> [--preset thumb|card|full] [--out FILE] [--keep-gps] [--json]")
}
let maxPx = maxOverride ?? preset.maxPx
let targetBytes = (targetOverride ?? preset.targetKB) * 1024

let inURL = URL(fileURLWithPath: (input as NSString).expandingTildeInPath)
guard FileManager.default.fileExists(atPath: inURL.path) else { die("no such file: \(inURL.path)") }
let originalBytes = (try? FileManager.default.attributesOfItem(atPath: inURL.path)[.size] as? Int) ?? 0

guard let src = CGImageSourceCreateWithURL(inURL as CFURL, nil),
      CGImageSourceGetCount(src) > 0 else { die("not an image this Mac can read: \(inURL.path)") }

let props = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [CFString: Any] ?? [:]
let exif  = props[kCGImagePropertyExifDictionary] as? [CFString: Any] ?? [:]
let gps   = props[kCGImagePropertyGPSDictionary]  as? [CFString: Any] ?? [:]
let tiff  = props[kCGImagePropertyTIFFDictionary] as? [CFString: Any] ?? [:]
let srcW  = props[kCGImagePropertyPixelWidth]  as? Int ?? 0
let srcH  = props[kCGImagePropertyPixelHeight] as? Int ?? 0

// MARK: - what was in there

/// Look for identifying tags across all dictionaries. Reported, never copied.
var stripped: [[String: String]] = []
for probe in SENSITIVE {
    let key = probe.key as CFString
    let found = exif[key] ?? tiff[key] ?? props[key]
    if found != nil {
        stripped.append(["tag": probe.key, "why": probe.why])
    }
}
let hadGPS = gps[kCGImagePropertyGPSLatitude] != nil

// MARK: - resize
//
// CGImageSourceCreateThumbnailAtIndex does the decode and the downsample in one
// pass, so a 48 MP HEIC never has to exist as a full bitmap. kCreateThumbnail-
// WithTransform bakes the EXIF orientation into the pixels, which is why the
// output can safely drop the orientation tag: the image is already upright.
func render(maxPixels: Int) -> CGImage? {
    let opts: [CFString: Any] = [
        kCGImageSourceCreateThumbnailFromImageAlways: true,
        kCGImageSourceCreateThumbnailWithTransform: true,
        kCGImageSourceThumbnailMaxPixelSize: maxPixels,
        kCGImageSourceShouldCacheImmediately: true,
    ]
    return CGImageSourceCreateThumbnailAtIndex(src, 0, opts as CFDictionary)
}
guard var image = render(maxPixels: maxPx) else { die("could not decode/resize the image") }

// MARK: - the metadata we deliberately re-attach

func filtered(_ dict: [CFString: Any], keep: [String]) -> [CFString: Any] {
    var out: [CFString: Any] = [:]
    for k in keep {
        let cf = k as CFString
        if let v = dict[cf] { out[cf] = v }
    }
    return out
}

var outExif = filtered(exif, keep: KEEP_EXIF)

var outGPS = filtered(gps, keep: KEEP_GPS)
if !keepGPS {
    // Heading is what makes a map photo informative; the coordinate is what
    // makes it an address. Direction stays, position goes, unless asked for.
    for k in ["Latitude", "LatitudeRef", "Longitude", "LongitudeRef", "Altitude", "AltitudeRef"] {
        outGPS.removeValue(forKey: k as CFString)
    }
}

var outTIFF = filtered(tiff, keep: KEEP_TIFF)
outTIFF.removeValue(forKey: kCGImagePropertyTIFFOrientation)   // baked into pixels above

// MARK: - encode to a byte budget
//
// Quality is searched, not guessed: the same 1280px cap produces wildly
// different byte counts for a flat wall and a tree canopy, so a fixed quality
// either overshoots the budget or wastes it. Binary search lands close to the
// budget for both.
func encode(_ image: CGImage, quality: Double) -> Data? {
    // Every trial encodes to memory, so searching quality costs no disk I/O.
    let data = NSMutableData()
    guard let d2 = CGImageDestinationCreateWithData(
        data, UTType.jpeg.identifier as CFString, 1, nil) else { return nil }
    var opts: [CFString: Any] = [kCGImageDestinationLossyCompressionQuality: quality]
    // Dimensions must describe the bitmap actually being written — the budget
    // search can shrink it after this dictionary was first built.
    var exifOut = outExif
    exifOut[kCGImagePropertyExifPixelXDimension] = image.width
    exifOut[kCGImagePropertyExifPixelYDimension] = image.height
    if !exifOut.isEmpty { opts[kCGImagePropertyExifDictionary] = exifOut }
    if !outGPS.isEmpty  { opts[kCGImagePropertyGPSDictionary]  = outGPS }
    if !outTIFF.isEmpty { opts[kCGImagePropertyTIFFDictionary] = outTIFF }
    CGImageDestinationAddImage(d2, image, opts as CFDictionary)
    guard CGImageDestinationFinalize(d2) else { return nil }
    return data as Data
}

/// Best encode of one bitmap under the budget, else its smallest possible.
func bestUnderBudget(_ img: CGImage) -> (data: Data, quality: Double)? {
    var lo = 0.30, hi = 0.92
    guard let top = encode(img, quality: hi) else { return nil }
    if top.count <= targetBytes { return (top, hi) }
    var best: (Data, Double)? = nil
    for _ in 0..<8 {
        let mid = (lo + hi) / 2
        guard let trial = encode(img, quality: mid) else { break }
        if trial.count > targetBytes { hi = mid } else { lo = mid; best = (trial, mid) }
    }
    if let b = best { return b }
    guard let floorData = encode(img, quality: 0.30) else { return nil }
    return (floorData, 0.30)          // still over — caller decides what to do
}

// Quality is the first lever, PIXELS are the second. For a detailed photo the
// quality floor still lands well over budget (this 5712×4284 shot bottomed out
// at 234 KB against a 150 KB budget), and pushing quality lower just makes it
// ugly without making it small. Shrinking the long edge is what actually gets
// there, and it keeps the image clean at the size it ends up displayed.
var usedPx = maxPx
var chosen = bestUnderBudget(image)
var shrinks = 0
while let c = chosen, c.data.count > targetBytes, shrinks < 5, usedPx > 320 {
    usedPx = Int(Double(usedPx) * 0.82)
    guard let smaller = render(maxPixels: usedPx) else { break }
    image = smaller
    chosen = bestUnderBudget(image)
    shrinks += 1
}
guard let (outData, usedQuality) = chosen else { die("could not encode JPEG") }

let destURL: URL = {
    if let o = outPath { return URL(fileURLWithPath: (o as NSString).expandingTildeInPath) }
    let base = inURL.deletingPathExtension().lastPathComponent
    return inURL.deletingLastPathComponent()
        .appendingPathComponent("\(base)--map-\(preset.name).jpg")
}()
do { try outData.write(to: destURL) } catch { die("could not write \(destURL.path): \(error)") }

// MARK: - report

func gpsNum(_ k: CFString) -> Double? { gps[k] as? Double }
let heading = gpsNum(kCGImagePropertyGPSImgDirection) ?? gpsNum(kCGImagePropertyGPSDestBearing)
func compass(_ deg: Double) -> String {
    let pts = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"]
    return pts[Int((deg / 22.5).rounded()) % 16]
}

let saved = originalBytes > 0 ? Double(originalBytes - outData.count) / Double(originalBytes) * 100 : 0
var report: [String: Any] = [
    "ok": true,
    "input": inURL.path,
    "output": destURL.path,
    "preset": preset.name,
    "sourcePixels": "\(srcW)×\(srcH)",
    "outputPixels": "\(image.width)×\(image.height)",
    "quality": (usedQuality * 100).rounded() / 100,
    "bytesIn": originalBytes,
    "bytesOut": outData.count,
    "savedPercent": (saved * 10).rounded() / 10,
    "withinBudget": outData.count <= targetBytes,
    "budgetKB": targetBytes / 1024,
    "capturedAt": (exif[kCGImagePropertyExifDateTimeOriginal] as? String) ?? "",
    "camera": [tiff[kCGImagePropertyTIFFMake] as? String,
               tiff[kCGImagePropertyTIFFModel] as? String]
              .compactMap { $0 }.joined(separator: " "),
    "headingDegrees": heading as Any,
    "headingCompass": heading.map(compass) as Any,
    "gpsPresent": hadGPS,
    "gpsKept": hadGPS && keepGPS,
    "strippedCount": stripped.count,
    "stripped": stripped,
]
if hadGPS && !keepGPS {
    report["gpsNote"] = "exact coordinates removed — pass --keep-gps only when the location is meant to be public"
}

if asJSON {
    let data = try! JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
    print(String(data: data, encoding: .utf8)!)
} else {
    func kb(_ n: Int) -> String { String(format: "%.0f KB", Double(n) / 1024) }
    print("\(inURL.lastPathComponent)  →  \(destURL.lastPathComponent)")
    print("  size      \(kb(originalBytes)) → \(kb(outData.count))   " +
          "\(String(format: "%.1f", saved))% smaller" +
          (outData.count <= targetBytes ? "" : "  (over the \(targetBytes/1024) KB budget)"))
    print("  pixels    \(srcW)×\(srcH) → \(image.width)×\(image.height)  [\(preset.name)]")
    if let h = heading { print("  facing    \(compass(h)) (\(Int(h))°)  — kept, it is what makes this a map photo") }
    if let t = exif[kCGImagePropertyExifDateTimeOriginal] as? String { print("  taken     \(t)") }
    if hadGPS { print("  gps       \(keepGPS ? "KEPT (--keep-gps)" : "removed — the coordinate was the property's exact position")") }
    if stripped.isEmpty {
        print("  stripped  nothing identifying was in this file")
    } else {
        print("  stripped  \(stripped.count) identifying tag(s):")
        for s in stripped { print("              · \(s["tag"]!) — \(s["why"]!)") }
    }
}
