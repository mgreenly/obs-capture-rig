import AVFoundation
import CoreMedia

let ds = AVCaptureDevice.DiscoverySession(
    deviceTypes: [.external, .builtInWideAngleCamera],
    mediaType: .video, position: .unspecified)

for d in ds.devices {
    let dims = d.formats.map { CMVideoFormatDescriptionGetDimensions($0.formatDescription) }
    let maxW = dims.map { $0.width }.max() ?? 0
    let maxH = dims.map { $0.height }.max() ?? 0
    print("""
    \(d.localizedName)
      connected: \(d.isConnected)   suspended: \(d.isSuspended)   in-use: \(d.isInUseByAnotherApplication)
      formats offered: \(d.formats.count)   max: \(maxW)x\(maxH)
    """)
}
