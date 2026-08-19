import AVFoundation
import CoreMedia

func fourCC(_ c: FourCharCode) -> String {
    let b = [UInt8((c >> 24) & 255), UInt8((c >> 16) & 255), UInt8((c >> 8) & 255), UInt8(c & 255)]
    return String(bytes: b, encoding: .ascii)?.trimmingCharacters(in: .whitespaces) ?? "?"
}

let types: [AVCaptureDevice.DeviceType] = [.external, .builtInWideAngleCamera]
let ds = AVCaptureDevice.DiscoverySession(deviceTypes: types, mediaType: .video, position: .unspecified)

for dev in ds.devices {
    print("\n=== \(dev.localizedName)  [\(dev.uniqueID)] ===")
    var rows: [(Int32, Int32, String, Double, Double)] = []
    for f in dev.formats {
        let d = CMVideoFormatDescriptionGetDimensions(f.formatDescription)
        let sub = fourCC(CMFormatDescriptionGetMediaSubType(f.formatDescription))
        for r in f.videoSupportedFrameRateRanges {
            rows.append((d.width, d.height, sub, r.minFrameRate, r.maxFrameRate))
        }
    }
    rows.sort { a, b in
        if a.0 != b.0 { return a.0 > b.0 }
        if a.1 != b.1 { return a.1 > b.1 }
        return a.4 > b.4
    }
    for r in rows {
        let fps = r.3 == r.4 ? String(format: "%.2f", r.4)
                             : String(format: "%.2f-%.2f", r.3, r.4)
        print(String(format: "  %5d x %-5d  %-6@  %@ fps", r.0, r.1, r.2 as NSString, fps))
    }
    if rows.isEmpty { print("  (no formats reported)") }
}
