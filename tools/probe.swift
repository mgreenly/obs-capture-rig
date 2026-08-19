// Verify a finished recording actually matches the intended config.
import AVFoundation
import Foundation

func fourCC(_ c: FourCharCode) -> String {
    let b = [UInt8((c >> 24) & 255), UInt8((c >> 16) & 255), UInt8((c >> 8) & 255), UInt8(c & 255)]
    return String(bytes: b, encoding: .ascii) ?? "?"
}

let args = CommandLine.arguments.dropFirst()
guard let path = args.first else {
    FileHandle.standardError.write("usage: probe <file.mov>\n".data(using: .utf8)!)
    exit(2)
}

let asset = AVURLAsset(url: URL(fileURLWithPath: path))
let sem = DispatchSemaphore(value: 0)

Task {
    do {
        let dur = try await asset.load(.duration)
        print("file:     \((path as NSString).lastPathComponent)")
        print("duration: \(String(format: "%.2f", CMTimeGetSeconds(dur))) s")

        let attrs = try FileManager.default.attributesOfItem(atPath: path)
        let bytes = (attrs[.size] as? NSNumber)?.doubleValue ?? 0
        let secs  = max(CMTimeGetSeconds(dur), 0.001)
        print("size:     \(String(format: "%.1f", bytes / 1_048_576)) MB"
            + "   (\(String(format: "%.1f", bytes * 8 / secs / 1_000_000)) Mbps overall)")

        for t in try await asset.loadTracks(withMediaType: .video) {
            let size = try await t.load(.naturalSize)
            let fps  = try await t.load(.nominalFrameRate)
            let fds  = try await t.load(.formatDescriptions)
            let codec = fds.first.map { fourCC(CMFormatDescriptionGetMediaSubType($0)) } ?? "?"
            print("\nVIDEO  \(Int(size.width))x\(Int(size.height))  \(String(format: "%.3f", fps)) fps  codec \(codec)")
            if let fd = fds.first,
               let ext = CMFormatDescriptionGetExtensions(fd) as? [String: Any] {
                for k in ["CVImageBufferColorPrimaries", "CVImageBufferTransferFunction",
                          "CVImageBufferYCbCrMatrix", "FullRangeVideo"] {
                    if let v = ext[k] { print("       \(k): \(v)") }
                }
            }
        }
        for t in try await asset.loadTracks(withMediaType: .audio) {
            let fds = try await t.load(.formatDescriptions)
            guard let fd = fds.first else { continue }
            let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(fd)?.pointee
            print("\nAUDIO  \(fourCC(CMFormatDescriptionGetMediaSubType(fd)))"
                + "  \(Int(asbd?.mSampleRate ?? 0)) Hz"
                + "  \(asbd?.mChannelsPerFrame ?? 0) ch"
                + "  \(asbd?.mBitsPerChannel ?? 0)-bit")
        }
    } catch {
        print("error: \(error)")
    }
    sem.signal()
}
sem.wait()
