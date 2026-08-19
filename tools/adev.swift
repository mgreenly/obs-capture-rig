import CoreAudio
import Foundation

func str(_ id: AudioObjectID, _ sel: AudioObjectPropertySelector) -> String {
    var a = AudioObjectPropertyAddress(mSelector: sel,
        mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var s: CFString = "" as CFString
    var sz = UInt32(MemoryLayout<CFString>.size)
    guard AudioObjectGetPropertyData(id, &a, 0, nil, &sz, &s) == noErr else { return "?" }
    return s as String
}
func chans(_ id: AudioObjectID, input: Bool) -> Int {
    var a = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreamConfiguration,
        mScope: input ? kAudioObjectPropertyScopeInput : kAudioObjectPropertyScopeOutput,
        mElement: kAudioObjectPropertyElementMain)
    var sz: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(id, &a, 0, nil, &sz) == noErr, sz > 0 else { return 0 }
    let p = UnsafeMutableRawPointer.allocate(byteCount: Int(sz), alignment: 16)
    defer { p.deallocate() }
    guard AudioObjectGetPropertyData(id, &a, 0, nil, &sz, p) == noErr else { return 0 }
    let abl = UnsafeMutableAudioBufferListPointer(p.assumingMemoryBound(to: AudioBufferList.self))
    return abl.reduce(0) { $0 + Int($1.mNumberChannels) }
}

var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices,
    mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
var size: UInt32 = 0
AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size)
var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids)

print("INPUT-CAPABLE DEVICES  (name / OBS device_id)\n")
for id in ids where chans(id, input: true) > 0 {
    print("  \(str(id, kAudioObjectPropertyName))   [in:\(chans(id, input: true))]")
    print("      \(str(id, kAudioDevicePropertyDeviceUID))\n")
}
