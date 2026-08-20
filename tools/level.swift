// Measure what is actually ON a recording's audio tracks.
//
// probe reports the audio FORMAT, which a fully silent track satisfies just as
// well as a good one. This decodes the samples and reports peak/RMS per channel,
// so "did this scene actually capture sound" has a real answer.
//
// EVERY track is reported, not just the first. Recordings carry three: track 1
// is the Wave XLR through Wave Link, track 2 the camera's own mic, and track 3
// the Wave XLR interface direct. The last two are sync references for sync.py.
// Reporting only track 1 would hide exactly the failure that makes a sync
// measurement impossible -- a reference track that recorded nothing.
import AVFoundation
import Foundation

let args = CommandLine.arguments.dropFirst()
guard let path = args.first else {
    FileHandle.standardError.write("usage: level <file.mov>\n".data(using: .utf8)!)
    exit(2)
}

func dbfs(_ v: Double) -> String {
    v <= 0.0000001 ? "  -inf" : String(format: "%6.1f", 20 * log10(v))
}

let asset = AVURLAsset(url: URL(fileURLWithPath: path))
let sem = DispatchSemaphore(value: 0)

Task {
    do {
        let tracks = try await asset.loadTracks(withMediaType: .audio)
        guard !tracks.isEmpty else {
            print("no audio track"); exit(1)
        }

        print("file:   \((path as NSString).lastPathComponent)")
        print("tracks: \(tracks.count)")
        var anySignal = false
        var anySilent = false

        // One reader per track. AVAssetReader is single-use and a shared one would
        // interleave the tracks' sample buffers, so each gets its own pass.
        for (idx, track) in tracks.enumerated() {
        let reader = try AVAssetReader(asset: asset)
        // Decode to canonical float so peak/RMS are independent of source bit depth.
        let out = AVAssetReaderTrackOutput(track: track, outputSettings: [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVLinearPCMBitDepthKey: 32,
            AVLinearPCMIsFloatKey: true,
            AVLinearPCMIsNonInterleaved: false,
            AVLinearPCMIsBigEndianKey: false,
        ])
        reader.add(out)
        reader.startReading()

        var chans = 0
        var peak = [Double](), sumsq = [Double](), n = 0

        while let sb = out.copyNextSampleBuffer() {
            guard let bb = CMSampleBufferGetDataBuffer(sb) else { continue }
            if chans == 0,
               let fd = CMSampleBufferGetFormatDescription(sb),
               let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(fd) {
                chans = Int(asbd.pointee.mChannelsPerFrame)
                peak  = [Double](repeating: 0, count: chans)
                sumsq = [Double](repeating: 0, count: chans)
            }
            var len = 0, ptr: UnsafeMutablePointer<Int8>?
            CMBlockBufferGetDataPointer(bb, atOffset: 0, lengthAtOffsetOut: nil,
                                        totalLengthOut: &len, dataPointerOut: &ptr)
            guard let p = ptr, chans > 0 else { continue }
            p.withMemoryRebound(to: Float.self, capacity: len / 4) { f in
                let frames = (len / 4) / chans
                for i in 0..<frames {
                    for c in 0..<chans {
                        let v = Double(abs(f[i * chans + c]))
                        if v > peak[c] { peak[c] = v }
                        sumsq[c] += v * v
                    }
                }
                n += frames
            }
        }

        print("\ntrack \(idx + 1):  \(n) frames, \(chans) channels")
        guard n > 0 else {
            print("  NO SAMPLES DECODED")
            anySilent = true
            continue
        }
        print("          peak dBFS   RMS dBFS")
        for c in 0..<chans {
            print("    ch\(c)   \(dbfs(peak[c]))      \(dbfs((sumsq[c] / Double(n)).squareRoot()))")
        }
        let loudest = peak.max() ?? 0
        if loudest <= 0.0000001 {
            print("    SILENT — no signal on any channel")
            anySilent = true
        } else {
            anySignal = true
        }
        }

        print("")
        if !anySignal {
            print("SILENT — no signal on any track")
            exit(1)
        }
        // A silent track among live ones is the interesting case: the take is not
        // obviously broken, but something that was meant to record did not.
        print(anySilent ? "signal present, but at least one track is SILENT"
                        : "signal present on all \(tracks.count) track(s)")
        exit(anySilent ? 1 : 0)
    } catch {
        FileHandle.standardError.write("error: \(error)\n".data(using: .utf8)!)
        exit(1)
    }
}
sem.wait()
