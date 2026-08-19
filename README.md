# OBS capture rig — M4 Mac mini

Canonical config for the 4K30 YouTube recording setup, plus the tools used to
verify it. Everything here is reproducible: `./apply.py --write` restores the
whole OBS config from scratch.

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

### Still to do on the camera
- HDMI Info. Display → Off, CTRL FOR HDMI → Off (needs the cable unplugged)
- Power Save Start Time → Off, Auto Power OFF Temp. → High

## Tools

    make probe        # all three, in order

- `bin/caps` — every resolution/format/fps each capture device offers.
  The Cam Link's list reflects the **live HDMI signal**, so it doubles as a
  check on what the camera is sending. The HD60 X's list is static.
- `bin/sig` — connection state and format count. Quick before/after check.
- `bin/adev` — CoreAudio input devices with the UIDs OBS stores as `device_id`.

None need camera permission; enumerating formats does not open a session.

**After any camera change, reseat the HDMI cable before trusting the output** —
the Cam Link caches the handshake and keeps reporting stale modes.

## apply.py

Dry-run by default. `--write` to apply.

    ./apply.py            # show what would change
    ./apply.py --write    # back up, then apply

Refuses to run while OBS is open — OBS rewrites its config on exit and would
clobber the changes. Backups land in `backups/<timestamp>/`.

Three deliberate design choices:

- **Patches the scene JSON, never replaces it.** Scene items reference sources
  by UUID; a regenerated file would break those links.
- **Resolves audio devices by name.** Wave Link's virtual devices use GUIDs that
  regenerate on reinstall, so a hardcoded UID is a latent breakage.
- **Derives scene-item scale from a target on-screen width**, so re-running is
  idempotent rather than halving the PiP each time.

## What it fixes

| | Was | Now |
|---|---|---|
| Output mode | Simple, 6000 Kbps, `RecQuality=Small` | Advanced, HEVC hardware |
| Output resolution | 1920×1080 (4K canvas discarded) | 3840×2160 |
| FPS | 60 integer | 30000/1001 (29.97, matches the ZV-1) |
| `Sony ZV1` source | preset 1920×1080 | preset 3840×2160 |
| Audio source | Wave XLR direct — **bypassed Wave Link's filtering** | Wave Link Stream Mix |
| Global Mic/Aux | `default` → HD60 X, a second live audio path | removed |
| ZV1 PiP scale | 1.295 (sized for 1920) | ~0.647 (same on-screen size at 3840) |

## Audio routing

Wave Link owns everything heard and every filter applied. OBS takes **Stream
Mix** as a finished signal and does not process or play it back.

Monitoring stays **off on every OBS source** — Wave Link's Monitor Mix already
feeds the headphones, so OBS monitoring would double the audio and can feed
back into Wave Link.

## Open items

- `recordEncoder.json` (bitrate, rate control) is **not** written by apply.py.
  The Apple VT encoder's key names are not documented and wrong keys fail
  silently into defaults. Set it once in the OBS UI — CBR, 100000 Kbps,
  keyframe interval 1s, profile `main` — then capture the file OBS writes and
  add it here.
- ~~Verify Mic/Aux is disabled~~ — **confirmed**. Removing the `AuxAudioDevice1`
  key is equivalent to Disabled; the startup log no longer contains
  `[Loaded global audio device]` and OBS did not recreate the key.
- OBS log showed **input monitoring permission denied** — global hotkeys will
  not fire while OBS is backgrounded. Fix in System Settings → Privacy &
  Security → Input Monitoring.

## Recovering a wedged launch

Symptom: Dock icon bounces, no window, no crash. Cause seen here was OBS's main
thread blocked in `AudioDeviceStart` on a capture-card audio device — it
eventually returned after 2m27s.

    sudo killall coreaudiod

Naming the audio device explicitly (rather than `default`) keeps capture-card
audio out of the startup path.
