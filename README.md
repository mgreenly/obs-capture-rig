# OBS capture rig — M4 Mac mini

Canonical config for the 4K30 YouTube recording setup, plus the tools used to
verify it. Everything here is reproducible: `./apply.py --write` restores the
whole OBS config from scratch.

**Full guide:** `notes/rig-guide.html` — open it in a browser. It covers the manual
camera walkthrough (the fragile part), how to ask Claude to re-apply this setup, and a
complete record of hardware, settings and scenes. This README is the condensed version.

## Hardware

| Role | Device | Connection |
|---|---|---|
| Camera | Sony ZV-1 → Cam Link 4K | HDMI → USB, bus `0x02` |
| Screen/console | Elgato HD60 X | HDMI → USB, bus `0x32` |
| Audio | Wave XLR → Wave Link | USB (virtual devices) |
| Control | Stream Deck | USB |

Host: M4 Mac mini, 10 cores, 32 GB, macOS 26.6.2, OBS 32.2.2 (arm64).

## The binding constraint

**No device in this chain can capture 4K60.** The HD60 X tops out at 4K30
(1440p60 / 1080p60 below that); the Cam Link does 4K30 when fed 4K. This is a
capture-side ceiling, not an encoding one — the M4's media engine has ample
headroom. Hence 4K30 everywhere.

Measured, not from spec sheets — see `make probe`.

## Clocks and latencies

Three independent clock domains, none of them locked to any other:

| Domain | Devices on it |
|---|---|
| **Cam Link 4K** | the picture, and the camera's own mic embedded in the same HDMI stream |
| **Wave XLR** | the microphone interface itself |
| **Wave Link** | the virtual devices (Stream Mix, Chat Mix, Personal Mix) |

Everything below was measured on one 99 s take with all three recorded
simultaneously. Sign convention: **negative means that audio arrives ahead of
the camera's audio**, i.e. earlier in the file.

| pairing | constant | rate |
|---|---|---|
| Wave XLR direct vs Cam Link | −143.5 ms | none detectable |
| Wave Link vs Cam Link | −17.9 ms at t=0 | **+66 ppm** |
| Wave Link vs Wave XLR direct | +125.8 ms at t=0 | **+62 ppm** (sample level) |

The three close as a triangle to 0.1 ms, which is the check that no single
measurement is inventing its answer.

What that says about each device:

- **The Wave XLR interface and the Cam Link keep time with each other.** No slope
  is detectable between them across 99 s. Two unrelated USB devices agreeing this
  well is luck rather than design, and it should be re-checked rather than
  assumed if either is replaced.
- **Wave Link is the only thing that drifts.** Its virtual device runs about
  62 ppm slow, which is 3.7 ms per minute. That is ordinary for an unlocked
  crystal — consumer audio clocks are specified to ±50 or ±100 ppm — and it is
  **not** the 29.97-vs-30 pulldown ratio, which is 1000 ppm and would slip a
  full second every 17 minutes.
- **Wave Link costs about 129 ms of latency**, stable to a millisecond within a
  take. That is the whole app path, not the processing: switching Voice Focus and
  the gate off moves the number by 20–30 ms, not by 129.
- **The constant is a property of the capture session, the rate is a property of
  the hardware.** Six takes of this rig landed between −9.8 and −54.2 ms because
  the USB buffers align differently each time OBS starts the sources. The rate
  should be a fixed ratio between two crystals, but that has been measured on
  one take, so `normalize.py` measures it per take rather than pinning it.

Two practical consequences:

- **The Wave XLR presents one input channel.** OBS duplicates it to stereo, so
  track 3 reads as 2 identical channels. The Wave Link virtual devices and the
  Cam Link present two.
- **Device identity is not stable in the same way for both.** The Wave XLR has a
  hardware UID (`AppleUSBAudioEngine:Elgato Systems:Elgato Wave XLR:…`) that
  survives reinstalls; the Wave Link virtual devices use GUIDs that regenerate,
  which is why `apply.py` resolves audio devices **by name** at apply time.

Everything here is measured against the camera's *audio*, which stands in for the
picture. The camera-audio-to-frames bias is still unmeasured and
`REFERENCE_BIAS_MS` is still 0, so a constant error common to all three rows
would be invisible.

## Camera: Sony ZV-1

4K HDMI output is gated behind settings that are easy to miss:

1. **Mode button → Movie.** The red MOVIE record button is *not* the same thing.
   `4K Output Select` stays hidden in Still mode.
2. **MENU → Movie tab → File Format → XAVC S 4K**, Record Setting `30p 100M`.
   Sony's docs call this tab "Camera Settings2"; the camera labels it Movie1/2.
3. **Proxy Recording → Off.** With proxy on, Sony suppresses HDMI output
   entirely and gives no on-screen warning.
4. `Setup → 4K Output Select` — `Memory Card+HDMI` keeps a card backup;
   `HDMI Only(30p)` runs cooler and has no clip-length limit.

What actually blocked it here was **File Format sitting on XAVC S HD**. The
HDMI Resolution setting and the unplug/replug dance were not needed.

Result: `Cam Link 4K → 3840x2160 420v 29.97fps` — and it is the *only* mode the
Cam Link then offers, since it advertises just what it is being fed.

### Camera settings applied
- File Format XAVC S 4K, Record Setting 30p 100M, Proxy Recording Off
- 4K Output Select → Memory Card+HDMI
- HDMI Info. Display → Off, CTRL FOR HDMI → Off (clean feed, no CEC)
- Power Save Start Time → Off

The camera is fully configured. Worth confirming Auto Power OFF Temp. is set to
High as well — `Memory Card+HDMI` is the hottest-running output mode, and the
ZV-1 will stop on thermal cutout mid-take otherwise.

## Scenes

One scene collection, **`Rig`** — that is the *collection* name, and it is what
names `scenes/Rig.json`. Renaming a scene inside it does not move that file;
renaming the collection does. The profile is still called `Untitled`; the two
are independent, and apply.py resolves both from OBS's `user.ini` rather than
hardcoding paths, so renaming either is a one-constant change.

Three scenes:

| Scene | Contains | Role |
|---|---|---|
| `Desktop` | `Wave XLR`, `Cam Audio`, `HDX 60` full-frame, `Sony ZV1` PiP flush right | The working scene |
| `BigHead` | `Wave XLR`, `Cam Audio`, `Sony ZV1` full-frame | Talking head, camera only |
| `Thumb` | `Image` | Thumbnail still, no audio |

`Sony ZV1` and `Wave XLR` are the **same sources** shared between `Desktop` and
`BigHead`, not copies — same `source_uuid`, one camera capture and one audio
capture referenced twice. Change a source's settings and both scenes follow.

**Every scene that should have sound needs its own audio item.** An audio source
is live only in the scenes containing it, and the global Mic/Aux slot is
deliberately empty (it was a second live audio path and the cause of the launch
hang). `Thumb` is silent on purpose; `BigHead` was silent by accident until its
`Wave XLR` item was added. `Cam Audio` is in both sounding scenes for the same
reason — see [A/V sync](#av-sync).

Two things this costs apply.py:

- **`Sony ZV1` has two roles.** It is a PiP in `Desktop` and full-frame in
  `BigHead`, so scene-item scale policy is keyed on `(scene, item)`. Items in
  `FULLSCREEN_ITEMS` snap to scale 1.0 when the capture resolution changes;
  everything else takes the inverse ratio to hold its on-screen size.
- **`Thumb`'s `Image` is deliberately unmanaged.** The file changes every
  recording, so apply.py never writes it — it only warns if the path has gone
  missing.

apply.py refuses to run if the scene names drift from this list, rather than
silently applying a stale layout. If you rename a scene, update `SCENES` and
`FULLSCREEN_ITEMS` in apply.py and this table.

### The `Wave XLR` source is not the Wave XLR

The audio source is **named** `Wave XLR` and captures **Wave Link Stream Mix** —
the mic after Wave Link's processing. The name is the mic on the desk; the
device is the mix. Do not "fix" it to point at the literal Wave XLR interface;
that bypasses Wave Link and loses every filter. apply.py matches this source by
type, not name, so the label is free to say whatever is convenient.

## Tools

    make probe        # all three, in order

- `bin/caps` — every resolution/format/fps each capture device offers.
  The Cam Link's list reflects the **live HDMI signal**, so it doubles as a
  check on what the camera is sending. The HD60 X's list is static.
- `bin/sig` — connection state and format count. Quick before/after check.
- `bin/adev` — CoreAudio input devices with the UIDs OBS stores as `device_id`.
- `bin/probe <file.mov>` — what a finished recording actually contains:
  resolution, frame rate, codec, color tags, audio format, effective bitrate.
  The real check that config intent survived to disk.
- `bin/level <file.mov>` — decodes **every** audio track and reports peak/RMS
  dBFS per channel. `probe` only reports the audio *format*, which a completely
  silent track satisfies just as well as a good one. This answers "did that scene
  actually capture sound", and comparing RMS against a known-good take confirms
  it came through the same path. It exits non-zero if any track is silent, which
  is the quickest check that a take is measurable — a two-track file whose track
  2 recorded nothing looks fine to `probe`.

- `sync.py [file]` — measures how far the Wave XLR audio has drifted from the
  picture, using the camera's own mic on track 2 as the reference. See
  [A/V sync](#av-sync).

- `normalize.py [file]` — lifts a finished recording to YouTube's loudness
  target. Defaults to the newest recording in `~/Movies`, and emits a single
  audio track (`-t` to choose which; track 1, the Wave XLR, by default). See
  [Normalizing for YouTube](#normalizing-for-youtube).

None need camera permission; enumerating formats does not open a session.

**After any camera change, reseat the HDMI cable before trusting the output** —
the Cam Link caches the handshake and keeps reporting stale modes.

## Stream Deck

Stream Deck Mini, 6 keys in a 3x2 grid. Layout:

| | col 0 | col 1 | col 2 |
|---|---|---|---|
| **row 0** | `Desktop` | `Thumb` | `BigHead` |
| **row 1** | Record toggle | — | Next page |

The scene keys light up when their scene is live, and the record key reflects
OBS's actual recording state rather than assuming a press worked.

That state feedback needs **two** plugins, one on each side:

- `com.elgato.obsstudio.sdPlugin` in the Stream Deck app (from the Marketplace).
- `StreamDeckPlugin.plugin` in `~/Library/Application Support/obs-studio/plugins/`,
  installed by the Elgato installer. It ships per-OBS-version variants; the
  `StreamDeckPluginOBS32` one matches OBS 32.

They talk over a private `streamdeck-obs` protocol on `127.0.0.1:28186`, **not
obs-websocket** — obs-websocket stays disabled. Confirm the bridge in the OBS log:

    <StreamDeck> Plugin version 5.5.4.2
    <StreamDeck> [Server] Listening on '127.0.0.1:28186'.

Keys are configured by scene **name**, e.g. `{"scene": "Desktop", "target": "program"}`.
Renaming a scene in OBS therefore breaks its key — the same reason apply.py
refuses to run on scene drift. Rename in both places.

The profile lives at:

    ~/Library/Application Support/com.elgato.StreamDeck/ProfilesV3/
      <device>.sdProfile/Profiles/<page>/manifest.json

**Quit the Stream Deck app before editing that file** — like OBS, it rewrites
its config on exit and will silently undo hand edits.

## apply.py

Dry-run by default. `--write` to apply.

    ./apply.py            # show what would change
    ./apply.py --write    # back up, then apply

Refuses to run while OBS is open — OBS rewrites its config on exit and would
clobber the changes. Backups land in `backups/<timestamp>/`.

Five deliberate design choices:

- **Patches the scene JSON, never replaces it.** Scene items reference sources
  by UUID; a regenerated file would break those links.
- **Resolves audio devices by name.** Wave Link's virtual devices use GUIDs that
  regenerate on reinstall, so a hardcoded UID is a latent breakage.
- **Reads the active profile and collection from `user.ini`.** Paths are not
  hardcoded, and the names are checked against `PROFILE_NAME` /
  `COLLECTION_NAME`. Patching a collection OBS is not actually using would look
  like it worked while changing nothing.
- **Matches sources by type, not by name.** Renaming a *scene* is not, and apply.py stops rather than guess. **Audio is
  the exception**: there are two `coreaudio_input_capture` sources now, so the
  type no longer identifies them and `AUDIO_SOURCES` is keyed on the source
  name. An audio source apply.py does not recognise is an error, not a guess.
- **Only touches scene-item scale when a capture resolution actually changes**,
  so re-running is idempotent rather than halving the PiP each time, and your
  manual layout tweaks survive.

## What it fixes

| | Was | Now |
|---|---|---|
| Output mode | Simple, 6000 Kbps, `RecQuality=Small` | Advanced, HEVC hardware |
| Output resolution | 1920×1080 (4K canvas discarded) | 3840×2160 |
| FPS | 60 integer | 30000/1001 (29.97, matches the ZV-1) |
| `Sony ZV1` source | preset 1920×1080 | preset 3840×2160 |
| Audio source device | the Wave XLR interface direct — **bypassed Wave Link's filtering** | Wave Link Stream Mix |
| Global Mic/Aux | `default` → HD60 X, a second live audio path | removed |
| ZV1 PiP scale | 1.295 (sized for 1920) | ~0.647 (same on-screen size at 3840) |

That table is the historical record of the first apply. The `Desktop` PiP has
since been resized by hand to 0.4865; apply.py leaves manual layout alone.

## Verified end to end

A 13 s test recording, read back with `bin/probe`:

    VIDEO  3840x2160  29.970 fps  codec hvc1
           ColorPrimaries / TransferFunction / YCbCrMatrix: ITU_R_709_2
           FullRangeVideo: 0
    AUDIO  lpcm  48000 Hz  2 ch  24-bit  (x2 tracks)
    153.6 MB / 13.08 s  ->  98.5 Mbps overall

Every intent survived to disk: native 4K, 29.97 to match the ZV-1, HEVC
(`hvc1`), Rec.709 limited range, lossless 24-bit PCM audio, and CBR landing
within 1.5% of the 100 Mbps target.

Audio levels, read back with `bin/level`:

            peak dBFS   RMS dBFS
      ch0     -9.3       -30.3
      ch1     -9.3       -30.3

Both channels identical, since the Wave XLR is a mono mic duplicated to stereo
by Wave Link. Aim for peaks between **-12 and -6 dBFS**.

From the OBS log:

    [VideoToolbox advanced_video_recording: 'hevc']: session created with
    hardware encoding
    Number of lagged frames due to rendering lag/stalls: 1 (0.2%)

One lagged frame at session start, no errors or warnings. Two simultaneous 4K
USB capture streams plus hardware HEVC encoding is comfortably within the M4's
headroom.

## Audio routing

Wave Link owns everything heard and every filter applied. OBS takes **Stream
Mix** as a finished signal and does not process or play it back.

Monitoring stays **off on every OBS source** — Wave Link's Monitor Mix already
feeds the headphones, so OBS monitoring would double the audio and can feed
back into Wave Link.

### Set gain for headroom, not for level

Recording is lossless 24-bit PCM, whose noise floor sits far below anything this
mic and preamp produce. Recording quieter costs nothing, and lifting it in post
is free. Clipping is the one error that cannot be undone, so headroom is what to
optimise for.

An early take peaked at -2.8 dBFS, under 3 dB of headroom. Dropping the gain
~6 dB moved it to -9.3. The peak fell 6.5 dB while RMS fell only 3.7, which
shows the original peak was a transient rather than the speaking level.

Adjust the **Wave XLR's analog gain knob**, not the Wave Link fader. The knob is
the preamp ahead of the converter; a digital fader after the A/D only makes an
already-clipped signal quieter. That the peak moved with the knob is the proof
the ceiling was analog. Clipguard is a backstop, not a substitute.

## A/V sync

    ./sync.py                 # newest recording in ~/Movies
    ./sync.py --self-test     # prove the sign convention before trusting a number
    ./sync.py <file> --cam-cm 150 --mic-cm 25

Every recording carries **three** audio tracks:

| Track | Source | Device | Ships? |
|---|---|---|---|
| 1 | `Wave XLR` | Wave Link Stream Mix | yes — this is the audio |
| 2 | `Cam Audio` | Cam Link 4K | no — sync reference only |
| 3 | `Wave XLR Raw` | Elgato Wave XLR | no — the same mic, unprocessed |

Tracks 2 and 3 cost about 2.3 Mbps each next to 98 Mbps of video, so they stay
on permanently as a check rather than being wired up once sync is already
suspect. macOS lets OBS open the Wave XLR interface while Wave Link is holding
it, so track 3 is free in every sense that matters.

### Why a second track can answer this

Track 2 is the ZV-1's own on-camera mic. That audio is embedded in the **same
HDMI stream as the video** and arrives over the **same Cam Link USB device**, so
it carries the video path's latency — track 2 is a stand-in for when the picture
lands. Track 1 comes from a completely separate device on a separate clock.

Both mics heard the same room, so the delta between the two tracks is the delta
between the two paths, which is the amount the good audio misses the picture by.
That makes the offset a measurement rather than a guess. Nudging audio in an
editor until it "looks right" lands somewhere inside the ±45 ms window where
people stop noticing, not on zero.

`sync.py` correlates the two tracks' **loudness contours**, not their waveforms —
two mics metres apart through different preamps produce waveforms with no
resemblance to each other and loudness contours that match closely.

It does this **two ways and requires them to agree**:

- **Envelope** — block RMS. Follows loudness directly.
- **Onset** — rising edges of the *log* envelope, half-wave rectified. Follows
  when energy arrives rather than how much, so it is gain-independent.

The second exists because Wave Link's compressor, gate and Clipguard reshape
track 1's loudness and leave track 2 untouched, so the two tracks' envelopes
genuinely differ in shape even when perfectly aligned. Onset strength largely
survives compression; it is also sparse, and can lock onto the wrong syllable.
They are wrong in unrelated ways, so agreement between them means something.

Windows more than 5 ms from what their method **predicts for that point in the
take** are discarded before the spread is judged. The onset method will
occasionally lock onto the wrong syllable — one wrong window, not a wrong take —
and max-minus-min lets a single straggler veto four that agree. A real take was
rejected this way: both methods had agreed on −16.1 ms to within 0.1 ms, and one
onset window at −26.4 failed the whole measurement. Discarded windows print with
a `*`.

The prediction is a **fitted line**, not the median, whenever a take is long
enough for one to be justified. On a take that drifts, the end windows genuinely
disagree with the median, because that is what drift is; judging them against it
throws out both ends of every long take and then reports the survivors as a huge
disagreement. A ten minute take of this rig drifts about 40 ms end to end, which
the median test would have called four bad windows and a broken take.

The line is seeded with a **median of pairwise slopes** before least squares gets
near it. One window hundreds of milliseconds out tilts a least-squares line far
enough that the rejection step then discards the good windows and keeps the bad
one; moving a median of pairwise slopes takes a majority of the pairs.

**Agreement is the only quality test, and that is an empirical finding.** Per
window correlation scores do not separate a good take from a bad one — a
near-silent clap take scored 2.0–3.4 sigma of peak prominence while good takes
scored 2.0–3.8. Any threshold that passed the good ones passed the bad one. What
separated them was windows agreeing with each other, and methods agreeing with
each other.

### What the third track settles

Track 3 is the Wave XLR interface directly, ahead of Wave Link entirely. It is
the **same microphone recorded twice**, once processed and once not, which is the
only way to see what the processing does rather than infer it:

- **Track 3 against track 2** is two unprocessed mics, so neither envelope has
  been reshaped by a gate or a compressor. It is the clean version of the
  measurement track 1 can only approximate.
- **Track 3 against track 1** is one signal through two paths differing only by
  Wave Link, which measures the processing's true latency. Because it is
  literally the same waveform, this pair can be correlated at the **sample**
  level rather than on envelopes, so it resolves 0.02 ms instead of 1 ms.
- **Track 3 against the picture** works with a clapperboard. Voice Focus
  suppresses claps as non-voice, so a clap is nearly invisible on track 1; it
  survives on track 3, which makes frame calibration possible **without** turning
  the processing off and changing the thing being measured.

It was added to settle a specific confound. Across six takes, heavier
suppression correlated with a 10–15 ms more negative offset, and there are two
mechanisms that would produce that: genuine lookahead latency, which should be
corrected, and an envelope artefact, which should not. A gate does not move audio
in time; it truncates each phrase's decay while the unprocessed track keeps its
tail, and lookahead opens it slightly before the sound. Both shift the measured
envelope earlier while shifting nothing audible. They could not be separated
because both scale with the same setting.

The answer, once the clean pairing existed, is that **most of that spread was
drift** (below) rather than either mechanism: the six numbers were single
measurements taken at different points in takes of different lengths, on a path
that slides 3.9 ms per minute. Wave Link's own contribution measures **+129 ms**
of latency, stable to a millisecond within a take.

### Using it

1. Record **60+ seconds of continuous speech**, in view of the camera. A few
   claps on top make the peak sharper, but claps *alone* do not work — the
   envelope is flat between them, and flat is what there is nothing to correlate.
2. `./bin/level <file>` — confirm `tracks: 3`, all with signal, and track 1
   somewhere near the usual −30 dBFS RMS. A silent or very quiet track is
   unmeasurable and `probe` will not notice. `normalize.py` runs this for you
   before it does anything else, so this step is only for looking at the numbers
   yourself.
3. `./sync.py` — read the offset, and the drift if the take is long enough.

Step 4 is usually nothing: **`normalize.py` measures each take itself** and
applies the answer. `sync.py` is for looking at the measurement, not for
feeding a constant to anything.

`sync.py` refuses rather than guesses. It stops if either track is below
−55 dBFS RMS, and it stops if the per-window results disagree by more than
10 ms — the offset being corrected is itself only tens of ms, so a spread that
size is not a measurement with noise on it, it is noise. Both failures print
what to change about the take.

Measure the distances and pass `--cam-cm` / `--mic-cm`. The camera is further
from you than the desk mic, so it genuinely hears you later, and at **34 cm per
millisecond** a camera 1.5 m out and a mic at 25 cm is 3.6 ms of pure acoustics
that is not the rig's fault. Without the flags that distance is silently
attributed to the capture chain.

### The offset is per-take, not per-rig

Four measurements of this setup:

| take | Wave Link processing | offset |
|---|---|---|
| 87 s | on | −22.0 ms |
| 17 s | on | −33.2 ms |
| 63 s | **off** | **−54.2 ms** |

The first two differ by 11 ms with nothing changed between them: the USB buffers
align differently each time OBS starts the sources, so the offset belongs to the
**capture session**, not the rig. A pinned constant is tuned to whichever take
happened to be measured and is wrong by up to ~10 ms on all the others.

The third is a controlled check. Wave Link's compressor and Clipguard have
lookahead, which is real latency in the mic path — switching the processing off
should make the mic arrive *earlier* relative to the picture, and it moved
20–30 ms in exactly that direction. The tool predicted a change it was not told
about, which is the best evidence available that it measures something physical.

Since every take carries its own reference track, there is nothing to guess:
**`normalize.py` measures the take it is normalizing.**

### There are two faults, not one

The offset above is a **constant**. It is not the whole problem, and no
measurement of it however careful can be, because the two devices also run at
slightly different **rates**:

| pairing | offset | drift |
|---|---|---|
| track 1 (Wave Link) vs track 2 (camera) | −17.9 ms at t=0 | **+66 ppm** |
| track 3 (Wave XLR direct) vs track 2 | −143.5 ms | none detectable |
| track 3 vs track 1, at sample level | +125.8 ms at t=0 | **+62 ppm** |

Read across a 99 s take, track 1 slid from −16.8 ms to −12.1 ms in a straight
line, residual 0.34 ms rms. The raw path shows no slope at all. **The drift lives
entirely in Wave Link's virtual device**, and the three pairings close as a
triangle to 0.1 ms, which is the check that none of these numbers is an artefact
of one measurement.

62 ppm is **3.9 ms per minute**, about 40 ms across a ten minute take. That is
ordinary: consumer audio clocks are specified to ±50 or ±100 ppm and nothing here
word-locks them. It is **not** the 29.97-vs-30 pulldown ratio, which is 1000 ppm
and would slip a full second every 17 minutes.

What it means in practice is that a single constant, however well measured, is
only right at one instant. Correcting only the constant *centres* the error
instead of removing it, leaving roughly ±20 ms across a long take — which is near
enough to the edge of perceptible to produce "it seems fine but I cannot quite
tell". `normalize.py` corrects the **rate as well**, by resampling, and then
re-measures the constant on the corrected audio.

A ten minute take, corrected both ways, holds to about a millisecond. The 99 s
take above went from −14.7 ms with a 4.7 ms slope to **+0.08 ms with no slope
detectable**, measured on the finished file against the camera reference.

### Where the correction is applied

`SYNC_OFFSET_MS` means *how far the Wave XLR must be delayed to line up with the
picture*. Two places can apply it, and **only one of them may be non-zero** —
both would correct twice, producing a doubled error that looks exactly like the
problem the tools would then be asked to fix again. normalize.py reads apply.py's
value at startup and refuses to run if both are set.

**normalize.py — the default, `"auto"`.** Measures the take, then applies a
post-processing shift after loudnorm so the loudness correction is still computed
on the audio as recorded. The raw take keeps whatever the rig actually did, a
wrong value costs a re-run rather than a re-shoot, and the live and monitoring
paths are untouched. Positive delays the audio with prepended silence; negative
advances it, which has to discard that much from the head of the track since
there is nothing before the start to move forward into.

`-s <ms>` pins a value for one run and `-s 0` disables the correction. **A pinned
value is only valid for the signal path it was measured on** — Wave Link's
processing latency is part of the mic path, so a number measured with the
processing off is wrong by 20–30 ms for a take recorded with it on. `"auto"` has
no such problem: it re-measures from the take's own two tracks, so whatever Wave
Link was doing during that recording is already in the answer. If a
measurement fails, normalize **refuses** rather than quietly shipping an
uncorrected file — that is indistinguishable from a correct one until someone
watches it. Takes recorded before the reference track existed have one track and
must use `-s`.

**apply.py — the OBS-side fix, currently 0.** Corrects at the source, so the raw
files are right too, at the cost of being baked into every take. It needs two
mechanisms because only one can fix a given sign: positive uses the `Wave XLR`
source's own sync offset to hold the mic back; negative delays the camera with a
Video Delay (Async) filter on `Sony ZV1`, since a sync offset cannot pull audio
*earlier*. Each run clears the mechanism it is not using, so flipping the sign
cannot leave a stale correction behind.

### What this does not cover

- **Only the ZV-1 path is measured.** The HD60 X is a different device with its
  own latency, and a negative offset delays the camera only. If the screen
  capture also drifts, that needs its own reference.
- **Drift is corrected on takes long enough to measure it.** Below about a
  minute of baseline a slope cannot be told from scatter, so `normalize.py`
  applies the constant alone and says so. That is the right answer for a short
  take, where 3.9 ms per minute has nowhere to accumulate, but it means a
  *marginal* take gets no rate correction rather than a guessed one.
- **The drift is measured per take, not pinned.** It probably is a hardware
  constant — a ratio between two crystals rather than however the USB buffers
  happened to line up — but "probably" is not measured, and one take pinned from
  another would be corrected wrongly and silently. It has been measured on
  exactly one take so far.
- **Old recordings cannot be measured after the fact** — they have one track.
- **Track 2 stands in for the video, and that is an assumption.** The camera mic
  shares one HDMI stream and one USB cable with the picture, but inside OBS they
  are two separate sources — video through AVFoundation, audio through CoreAudio
  — with independent timestamps. If those two software paths differ, the number
  is off by that difference. A clapperboard against the actual frames is the only
  ground truth; at 29.97 fps one frame is 33 ms, so it confirms sign and rough
  magnitude rather than the last few ms.

### Verifying the sign

    ./sync.py --self-test

Builds a file with a 40 ms delay put in on purpose and checks the tool reports
+40. A cross-correlation lag is the easiest thing here to get backwards, and a
backwards answer is a plausible-looking number of the right magnitude that makes
sync *worse*. Run it once before trusting a real measurement.

It then builds a second file whose **clocks** differ by a known amount and checks
the reported ppm. A drift has the same sign hazard plus one more: ms-per-second
and ppm are a factor of 1000 apart, and either reads as a plausible number. The
test asserts against the stretch the file actually has, read back from its own
track lengths, rather than the stretch ffmpeg was asked for — asetrate takes an
integer rate and the resampler approximates the ratio, so the two differ by a few
ppm, and asserting against the request would need a tolerance loose enough to
hide a real error. It currently recovers a +291.8 ppm stretch as +291.6.

Both conventions are stated in the output because they are mirror images of each
other, and writing a test to the wrong one is a mistake this suite has already
caught once: the offset test reads "track 2 relative to track 1", while the drift
test reads "the mic track relative to the reference", and track 1 is the mic.

`normalize.py` also checks its own work. It applies the rate correction to the
audio alone, re-measures, and **refuses** if a slope is still detectable — a
correction applied backwards leaves a take drifting twice as fast, and nothing
downstream would notice. Feeding it a deliberately negated value produces:

    normalize: the rate correction did not take: -64.5 ppm was measured, and
    189.0 ppm is still detectable after correcting for it.

## Normalizing for YouTube

    ./normalize.py            # newest recording in ~/Movies
    ./normalize.py -n         # measure only, write nothing
    ./normalize.py <file>     # a specific take
    ./normalize.py -t 2       # normalize the camera track instead
    ./normalize.py -s 0       # this take only: no sync correction
    ./normalize.py -d 0       # this take only: no rate correction
    ./normalize.py --no-level # skip the source check (with -s 0 -d 0)

Requires `brew install ffmpeg`. Output lands in `~/Movies/normalized/<name>.mp4`;
the original is never touched, so re-running is free.

Recording for headroom is the right call on disk and leaves takes far too quiet
to upload. YouTube plays everything back at about **-14 LUFS** and only turns
loud uploads *down* — a quiet one is left alone and just sounds weak next to
every other video. `normalize.py` closes that gap once, deliberately, instead of
letting the platform do it by accident.

A real take, start to finish:

    integrated -27.0 LUFS   true peak -9.3 dBTP   range 4.2 LU
    gain +13.0 dB
    -> integrated -14.7 LUFS   true peak -1.0 dBTP   range 4.0 LU

Four choices worth knowing:

- **The source is checked first, with `bin/level`.** Before any decoding work,
  because it costs a second and the passes it guards cost minutes. A silent track
  satisfies every format check there is — ffprobe reports the *format*, and
  silence has the same one — and only reveals itself when a sync correction is
  computed against it, which produces a confident number from nothing. If any
  track recorded nothing, normalize prints the per-track levels and refuses.
  `--no-level` skips it, which is only sensible together with `-s 0 -d 0`.
  Requires `make` to have been run, since `bin/` is not tracked.
- **Two passes, not one.** Single-pass `loudnorm` is a *dynamic* normalizer that
  rides the level as it goes, which pumps audibly on speech. Pass 1 measures,
  pass 2 applies one fixed correction to the whole file.
- **The sync corrections are applied here**, after loudnorm: the rate first,
  then the constant. See
  [Where the correction is applied](#where-the-correction-is-applied). The order
  matters because the resample pivots at t=0 and therefore moves the constant
  too, so the constant is re-measured on rate-corrected audio rather than derived
  from the uncorrected measurement by arithmetic nobody can check.
- **One audio track out.** The source has three; the upload gets track 1, the
  Wave XLR, and the two reference tracks are dropped. The track is selected for the
  *measurement* pass too — measuring one track and encoding another would apply
  a correction computed from the wrong audio and still look like it worked.
- **Video is copied, never re-encoded.** It is already 4K HEVC from the hardware
  encoder; a re-encode would cost quality and an hour to gain nothing. Only the
  audio is touched, PCM to AAC 384k at 48 kHz.
- **A verify pass re-measures the output** and refuses the result if it missed
  the target — on loudness *and* on true peak. `linear=true` applies one fixed
  gain and does not enforce the true-peak ceiling: when the gain a quiet take
  needs would run the peaks over, they go over. A take measured +0.5 dBTP on the
  way out while passing the loudness check. A silent or mangled audio track
  otherwise looks exactly like a success. Both measurement passes decode audio
  only, so three passes over a 4K take stay cheap.
- **Verify also checks the output's audio and video are the same length.** Every
  sync correction is a filter that moves audio in time, and a mistake in one
  changes the track's *length* rather than its level, which every loudness check
  here would pass without comment. This is not hypothetical: `loudnorm` outputs
  **192 kHz**, four times the rig's rate, because that is what it upsamples to
  for true-peak detection. `asetrate` does not resample, it relabels whatever
  rate it is handed, so the first version of the rate correction reinterpreted
  192 kHz audio as 48 kHz and wrote 394 s of audio against 98 s of video. The
  loudness verify looked at it and complained only that it was 1.4 LU quiet.
- **-1 dBTP, not 0.** AAC decodes in the frequency domain and can reconstruct
  samples slightly above the encoded peak; mastering to 0 clips on someone
  else's player rather than on this one.

Landing a little under -14 (the -14.7 above) is expected and correct: at +13 dB
of gain, that -9.3 dBTP peak would have hit +3.7, so the true-peak limiter did
the last 4.7 dB. If a take routinely needs that much limiting, raise the analog
gain knob a few dB rather than asking the limiter for more.

## Open items

- ~~`recordEncoder.json` not written by apply.py~~ — **done**. OBS serialises
  only values that differ from the encoder defaults, so the file it wrote was
  just `{"bitrate":100000,"keyint_sec":1}`. That confirms CBR and profile
  `main` are already the Apple VT HEVC defaults, and gives the real key names,
  so apply.py now writes the file verbatim rather than guessing.
- ~~Verify Mic/Aux is disabled~~ — **confirmed**. Removing the `AuxAudioDevice1`
  key is equivalent to Disabled; the startup log no longer contains
  `[Loaded global audio device]` and OBS did not recreate the key.
- ~~`BigHead` records silence~~ — **fixed**. The scene had no audio item, and an
  audio source is live only in scenes containing it. Added the existing
  `Wave XLR` source (same `source_uuid`, not a second capture) and confirmed on
  disk at -30.3 dBFS RMS, within 0.3 dB of a known-good `Desktop` take.
- ~~Confirm `hybrid_mov` writes two tracks~~ — **done**. A real take reads back
  as `tracks: 2`, both PCM, both with signal. Now three, likewise confirmed.
- ~~Can OBS open the Wave XLR while Wave Link holds it?~~ — **yes**. This was the
  one unverified risk in adding track 3; macOS does not enforce exclusive access
  here. A real take reads back with signal on all three tracks.
- ~~Is the suppression-correlated offset real latency or a gate artefact?~~ —
  **mostly neither**. With the unprocessed track available for comparison, the
  spread across those six takes is dominated by a +62 ppm clock drift and by
  where in each take the single measurement happened to be taken. Wave Link's
  own latency is +129 ms and is stable within a take.
- ~~Drift is unmeasured~~ — **measured and corrected**. +64.5 ppm on a 99 s take,
  residual 0.34 ms rms about the fitted line. `normalize.py` now corrects the
  rate as well as the constant, and the corrected output measures +0.08 ms
  against the camera reference with no slope detectable.
- ~~Measure the sync offset~~ — **superseded**: there is no single offset. It
  varies per capture session (−22, −33, −54 ms), so normalize.py measures each
  take. The shift itself is verified: normalizing with a 22 ms correction and
  correlating the output against the raw read back −22.0 ms at r = 0.99.
- **No measurement has been checked against actual frames.** Every number here
  is measured against the camera's *audio*, which stands in for the picture but
  is a separate OBS source with its own timestamps. A clapperboard take would
  confirm it independently. At 29.97 fps one frame is 33 ms, so it settles the
  sign and rough magnitude, not the last few ms — which is enough, since the
  offsets seen so far span 22–54 ms.
- **The Wave XLR is clipping, and it now blocks normalize.** Takes have peaked
  at 0.0 dBFS (processing off) and −3.7 dBFS (on), against a −12 to −6 target.
  A quiet take with no headroom needs so much gain that the true-peak ceiling
  cannot be held, and the verify pass refuses those. Turn the analog gain knob
  down; see "Set gain for headroom, not for level".
- **Cross-take comparisons of the offset are confounded and should not be
  trusted yet.** Five takes measured −22.0, −33.2, −54.2, −15.4 and −9.8 ms, but
  Wave Link's settings moved between them. The recording's noise floor tracks the
  **Voice Focus** amount — takes at maximum sit near −90 dBFS, reduced ones near
  −70, processing off near −66 — and the offsets cluster by that setting. What
  the clustering means is unclear: more processing should add latency and put the
  mic *further behind*, and the data goes the other way. Either something else
  changed too, or heavy Voice Focus reshapes the envelope enough to bias the
  correlation. Resolving it needs takes that vary one setting at a time.
  Per-take measurement is unaffected either way — each take is measured in the
  state it was recorded in.
- **Drift is still unmeasured, and there is a hint of it.** A 25 s take read
  −13.1 ms in its first windows and −16.2 ms in its last. That is either the
  capture settling after start or ~9 ms per minute of clock drift — over a
  10 minute video the second would be ~90 ms, which matters. One long take tells
  them apart; `sync.py` reports the slope above 120 s.
- **Drift is still unknown.** The 87 s take is too short a baseline; `sync.py`
  needs 120 s+ before it will report a slope. Real takes run ~10 minutes and the
  two devices share no clock, so one 3–5 minute measurement take is worth doing.
- **Input monitoring permission is still denied** (`[hotkeys-cocoa]: No event
  permissions, could not add global hotkeys`). This no longer blocks the Stream
  Deck, which drives OBS over its own plugin rather than hotkeys — it only
  matters if you want keyboard hotkeys working while OBS is backgrounded. Fix in
  System Settings → Privacy & Security → Input Monitoring.

## Recovering a wedged launch

Symptom: Dock icon bounces, no window, no crash. Cause seen here was OBS's main
thread blocked in `AudioDeviceStart` on a capture-card audio device — it
eventually returned after 2m27s.

    sudo killall coreaudiod

Naming the audio device explicitly (rather than `default`) keeps capture-card
audio out of the startup path.
