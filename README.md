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
| `Desktop` | `Wave XLR`, `HDX 60` full-frame, `Sony ZV1` PiP flush right | The working scene |
| `BigHead` | `Wave XLR`, `Sony ZV1` full-frame | Talking head, camera only |
| `Thumb` | `Image` | Thumbnail still, no audio |

`Sony ZV1` and `Wave XLR` are the **same sources** shared between `Desktop` and
`BigHead`, not copies — same `source_uuid`, one camera capture and one audio
capture referenced twice. Change a source's settings and both scenes follow.

**Every scene that should have sound needs its own audio item.** An audio source
is live only in the scenes containing it, and the global Mic/Aux slot is
deliberately empty (it was a second live audio path and the cause of the launch
hang). `Thumb` is silent on purpose; `BigHead` was silent by accident until its
`Wave XLR` item was added.

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
- `bin/level <file.mov>` — decodes the audio track and reports peak/RMS dBFS per
  channel. `probe` only reports the audio *format*, which a completely silent
  track satisfies just as well as a good one. This answers "did that scene
  actually capture sound", and comparing RMS against a known-good take confirms
  it came through the same path.

- `normalize.py [file]` — lifts a finished recording to YouTube's loudness
  target. Defaults to the newest recording in `~/Movies`. See
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
- **Matches sources by type, not by name.** Renaming a source in OBS is free.
  Renaming a *scene* is not, and apply.py stops rather than guess.
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
    AUDIO  lpcm  48000 Hz  2 ch  24-bit
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

## Normalizing for YouTube

    ./normalize.py            # newest recording in ~/Movies
    ./normalize.py -n         # measure only, write nothing
    ./normalize.py <file>     # a specific take

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

- **Two passes, not one.** Single-pass `loudnorm` is a *dynamic* normalizer that
  rides the level as it goes, which pumps audibly on speech. Pass 1 measures,
  pass 2 applies one fixed correction to the whole file.
- **Video is copied, never re-encoded.** It is already 4K HEVC from the hardware
  encoder; a re-encode would cost quality and an hour to gain nothing. Only the
  audio is touched, PCM to AAC 384k at 48 kHz.
- **A verify pass re-measures the output** and refuses the result if it missed
  the target, because a silent or mangled audio track otherwise looks exactly
  like a success. Both measurement passes decode audio only, so three passes
  over a 4K take stay cheap.
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
