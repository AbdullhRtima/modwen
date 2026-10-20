// modwen-tap: capture system audio on macOS 14.2+ with a Core Audio process tap.
//
// The first line on stderr is "FORMAT <sampleRate> <channels> TS1". Stdout is a stream of records:
//   UInt32 magic "MWTS" | UInt32 byte count | UInt64 capture time (ns, mach host clock) | interleaved Float32 PCM
// The capture time lets Modwen tell audio that was delayed (e.g. its reader was busy) from audio that was never
// produced (nothing playing), so the system track stays aligned with the microphone.
// Stop with SIGTERM/SIGINT; the tap and its private aggregate device are destroyed on exit.
//
// Build: bash modwen/native/macos/build.sh

import AudioToolbox
import CoreAudio
import Foundation

func fail(_ message: String, _ status: OSStatus = 0) -> Never {
    FileHandle.standardError.write("ERROR \(message) (OSStatus \(status))\n".data(using: .utf8)!)
    exit(1)
}

func getProperty<T>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector, _ value: inout T) -> OSStatus {
    var address = AudioObjectPropertyAddress(
        mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var size = UInt32(MemoryLayout<T>.size)
    return AudioObjectGetPropertyData(object, &address, 0, nil, &size, &value)
}

guard #available(macOS 14.2, *) else { fail("macOS 14.2 or later is required for Core Audio taps") }

// 1. A global stereo tap over every process (nothing excluded), left audible.
let tapDescription = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
tapDescription.uuid = UUID()
tapDescription.name = "Modwen system audio"
tapDescription.isPrivate = true
tapDescription.muteBehavior = .unmuted

var tapID = AudioObjectID(kAudioObjectUnknown)
var status = AudioHardwareCreateProcessTap(tapDescription, &tapID)
if status != noErr { fail("AudioHardwareCreateProcessTap failed; grant System Audio Recording permission", status) }

var format = AudioStreamBasicDescription()
status = getProperty(tapID, kAudioTapPropertyFormat, &format)
if status != noErr { fail("could not read tap format", status) }

// 2. The default output device anchors the aggregate device's clock.
var outputDevice = AudioDeviceID(kAudioObjectUnknown)
status = getProperty(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultSystemOutputDevice, &outputDevice)
if status != noErr { fail("no default output device", status) }
var outputUID: CFString = "" as CFString
status = getProperty(outputDevice, kAudioDevicePropertyDeviceUID, &outputUID)
if status != noErr { fail("could not read output device UID", status) }

// 3. A private aggregate device that contains the tap.
let aggregateUID = UUID().uuidString
let description: [String: Any] = [
    kAudioAggregateDeviceNameKey: "Modwen Tap",
    kAudioAggregateDeviceUIDKey: aggregateUID,
    kAudioAggregateDeviceMainSubDeviceKey: outputUID as String,
    kAudioAggregateDeviceIsPrivateKey: true,
    kAudioAggregateDeviceIsStackedKey: false,
    kAudioAggregateDeviceTapAutoStartKey: true,
    kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID as String]],
    kAudioAggregateDeviceTapListKey: [[
        kAudioSubTapDriftCompensationKey: true,
        kAudioSubTapUIDKey: tapDescription.uuid.uuidString,
    ]],
]
var aggregateID = AudioObjectID(kAudioObjectUnknown)
status = AudioHardwareCreateAggregateDevice(description as CFDictionary, &aggregateID)
if status != noErr { fail("AudioHardwareCreateAggregateDevice failed", status) }

let channels = Int(format.mChannelsPerFrame)
let nonInterleaved = (format.mFormatFlags & kAudioFormatFlagIsNonInterleaved) != 0
let isFloat = (format.mFormatFlags & kAudioFormatFlagIsFloat) != 0 && format.mBitsPerChannel == 32
if !isFloat { fail("unexpected tap sample format (expected Float32)") }
FileHandle.standardError.write("FORMAT \(Int(format.mSampleRate)) \(channels) TS1\n".data(using: .utf8)!)

let writer = DispatchQueue(label: "modwen.tap.writer")
let stdout = FileHandle.standardOutput

// 4. IO proc: copy the tap's input buffers out (interleaving if needed) and write them off the audio thread.
var procID: AudioDeviceIOProcID?
let recordMagic: UInt32 = 0x5354_574D  // "MWTS" little-endian
status = AudioDeviceCreateIOProcIDWithBlock(&procID, aggregateID, nil) { _, inInputData, inInputTime, _, _ in
    let ts = inInputTime.pointee
    let captureNanos: UInt64 = (ts.mFlags.contains(.hostTimeValid)) ? AudioConvertHostTimeToNanos(ts.mHostTime) : 0
    let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inInputData))
    var data: Data
    if nonInterleaved && buffers.count > 1 {
        let frames = Int(buffers[0].mDataByteSize) / MemoryLayout<Float32>.size
        var interleaved = [Float32](repeating: 0, count: frames * buffers.count)
        for (ch, buffer) in buffers.enumerated() {
            guard let src = buffer.mData?.assumingMemoryBound(to: Float32.self) else { continue }
            for i in 0..<frames { interleaved[i * buffers.count + ch] = src[i] }
        }
        data = interleaved.withUnsafeBufferPointer { Data(buffer: $0) }
    } else {
        data = Data()
        for buffer in buffers {
            if let src = buffer.mData { data.append(src.assumingMemoryBound(to: UInt8.self), count: Int(buffer.mDataByteSize)) }
        }
    }
    var header = Data(capacity: 16)
    withUnsafeBytes(of: recordMagic.littleEndian) { header.append(contentsOf: $0) }
    withUnsafeBytes(of: UInt32(data.count).littleEndian) { header.append(contentsOf: $0) }
    withUnsafeBytes(of: captureNanos.littleEndian) { header.append(contentsOf: $0) }
    header.append(data)
    let record = header
    writer.async { stdout.write(record) }
}
if status != noErr { fail("AudioDeviceCreateIOProcIDWithBlock failed", status) }

status = AudioDeviceStart(aggregateID, procID)
if status != noErr { fail("AudioDeviceStart failed", status) }

func cleanup() {
    if let procID = procID {
        AudioDeviceStop(aggregateID, procID)
        AudioDeviceDestroyIOProcID(aggregateID, procID)
    }
    AudioHardwareDestroyAggregateDevice(aggregateID)
    AudioHardwareDestroyProcessTap(tapID)
}

signal(SIGTERM, SIG_IGN)
signal(SIGINT, SIG_IGN)
signal(SIGPIPE, SIG_IGN)
var signalSources: [DispatchSourceSignal] = []
for sig in [SIGTERM, SIGINT, SIGPIPE] {
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler { cleanup(); exit(0) }
    source.resume()
    signalSources.append(source)
}

dispatchMain()
