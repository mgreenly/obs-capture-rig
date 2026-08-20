#!/usr/bin/env python3
"""
Apply the canonical OBS capture-rig config.

Dry-run by default; pass --write to actually change anything.
Refuses to run while OBS is open, because OBS rewrites its config on exit
and would clobber whatever we wrote.

Design notes:
  * The scene collection is PATCHED, not replaced. Scene items reference
    sources by UUID; regenerating the file would break those links.
  * The profile and collection PATHS are read from OBS's user.ini, not
    hardcoded, and their names are checked. Patching a collection OBS is not
    actually using would silently do nothing.
  * Audio devices are resolved BY NAME at apply time. Wave Link's virtual
    devices use GUIDs that regenerate on reinstall, so a hardcoded UID is
    a latent breakage.
  * When a source's capture preset changes resolution, scene items using it
    are rescaled by the inverse ratio so their ON-SCREEN size is preserved.
    Naturally idempotent: the preset only changes once. Items listed in
    FULLSCREEN_ITEMS are reset to 1.0 instead - a full-frame item should
    follow the new resolution, not resist it.
  * One source can appear in several scenes in different roles (Sony ZV1 is a
    PiP in Desktop and full-frame in BigHead), so scene-item policy is keyed on
    (scene, item), never on the source name alone.
  * Capture sources are matched by TYPE, so renaming them in OBS is free -- but
    audio is the exception. There are now two coreaudio sources, and the type no
    longer tells them apart, so AUDIO_SOURCES is keyed on the source NAME and an
    unrecognised audio source is an error rather than a guess.
"""
import configparser, json, os, shutil, subprocess, sys, time, uuid

HOME     = os.path.expanduser("~")
OBSROOT  = os.path.join(HOME, "Library/Application Support/obs-studio")
OBS      = os.path.join(OBSROOT, "basic")
USER_INI = os.path.join(OBSROOT, "user.ini")
PROJ     = os.path.dirname(os.path.abspath(__file__))
BACKUPS  = os.path.join(PROJ, "backups")

WRITE    = "--write" in sys.argv

# The profile and scene collection this config belongs to. Their paths are NOT
# hardcoded: OBS records the ACTIVE ones in user.ini [Basic], and patching an
# inactive collection would look like it succeeded while changing nothing OBS
# will ever load. So resolve, then assert the names match what we expect.
PROFILE_NAME    = "Untitled"
COLLECTION_NAME = "Rig"


def active_paths():
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    if not cp.read(USER_INI):
        sys.exit("cannot read %s - is OBS installed and has it run once?" % USER_INI)
    prof = cp.get("Basic", "ProfileDir",          fallback=None)
    coll = cp.get("Basic", "SceneCollection",     fallback=None)
    file = cp.get("Basic", "SceneCollectionFile", fallback=None)
    if not (prof and coll and file):
        sys.exit("user.ini [Basic] is missing ProfileDir/SceneCollection entries")
    if not file.endswith(".json"):        # OBS < 31 stored the stem, not the file
        file += ".json"
    if (prof, coll) != (PROFILE_NAME, COLLECTION_NAME):
        sys.exit("OBS's active config is profile %r / collection %r, but this repo\n"
                 "describes profile %r / collection %r. Switch OBS back, or update\n"
                 "PROFILE_NAME / COLLECTION_NAME in apply.py."
                 % (prof, coll, PROFILE_NAME, COLLECTION_NAME))
    return os.path.join(OBS, "profiles", prof), os.path.join(OBS, "scenes", file)


PROFILE, SCENE = active_paths()

# ---------------------------------------------------------------- target state

CANVAS_W, CANVAS_H = 3840, 2160
FPS_NUM, FPS_DEN   = 30000, 1001          # 29.97 - matches the ZV-1 via Cam Link

PROFILE_INI = {
    "Output": {"Mode": "Advanced"},
    "Video": {
        "BaseCX": CANVAS_W, "BaseCY": CANVAS_H,
        "OutputCX": CANVAS_W, "OutputCY": CANVAS_H,
        "FPSType": 2, "FPSNum": FPS_NUM, "FPSDen": FPS_DEN,
        "ScaleType": "lanczos",
        "ColorFormat": "NV12", "ColorSpace": "709", "ColorRange": "Partial",
    },
    "AdvOut": {
        "RecType": "Standard",
        "RecFilePath": os.path.join(HOME, "Movies"),
        "RecFormat2": "hybrid_mov",
        "RecEncoder": "com.apple.videotoolbox.videoencoder.ave.hevc",
        "RecAudioEncoder": "ffmpeg_pcm_s24le",
        "RecTracks": 7,          # bitmask: tracks 1 (mic) + 2 (camera) + 3 (raw mic)
        "RecUseRescale": "false",
    },
    "Audio": {"SampleRate": 48000, "ChannelSetup": "Stereo"},
}

# profiles/<name>/recordEncoder.json
#
# OBS serialises ONLY values that differ from the encoder's defaults, which is
# why rate_control and profile are absent: CBR and "main" are already the
# Apple VT HEVC defaults. Setting them explicitly here would be guesswork about
# key names, so we write exactly what OBS itself wrote.
ENCODER_JSON = {"bitrate": 100000, "keyint_sec": 1}

# device_name -> what each capture source should use
SOURCE_PRESETS = {
    "Cam Link 4K":   "AVCaptureSessionPreset3840x2160",
    "Elgato HD60 X": "AVCaptureSessionPreset3840x2160",
}

# OBS source name -> (CoreAudio device name, recording track number).
#
# Devices are resolved BY NAME at apply time; the track number becomes the
# source's `mixers` bitmask, and the union becomes AdvOut.RecTracks.
#
# NOTE: the source NAMED "Wave XLR" is not the Wave XLR. It captures Wave
# Link's Stream Mix, i.e. the mic AFTER Wave Link's processing. The name is the
# mic on the desk; the device is the mix. Do not "fix" it to the literal Wave
# XLR interface -- that bypasses Wave Link and loses every filter.
#
# "Cam Audio" is the ZV-1's own mic, arriving embedded in the same HDMI stream
# as the video and over the same Cam Link USB device, so it carries the video
# path's latency. It is not program audio and never ships: it exists so that
# ./sync.py can measure how far the Wave XLR drifts from the picture. Track 2
# costs ~2.3 Mbps next to 98 Mbps of video, so it stays on permanently as a
# check rather than being wired up only when sync is already suspect.
# "Wave XLR Raw" is the literal interface, ahead of Wave Link entirely. The
# warning above still stands for the PROGRAM source -- pointing track 1 at the
# interface would lose every filter -- but as a third, non-shipping track it is
# the same microphone recorded twice, once processed and once not, which is the
# only way to see what the processing does:
#
#   * Voice Focus suppresses claps as non-voice, so a clapperboard is invisible
#     on track 1. It survives on track 3, so the picture can be calibrated
#     against a real transient WITHOUT turning the processing off.
#   * Track 3 against track 1 is the same signal through two paths differing
#     only by Wave Link, which measures the processing's true latency.
#   * Track 3 against track 2 is two unprocessed mics, so it measures the
#     capture path with no gate or suppression reshaping either envelope.
AUDIO_SOURCES = {
    "Wave XLR":     ("Elgato Wave Link Stream Mix", 1),
    "Cam Audio":    ("Cam Link 4K",                 2),
    "Wave XLR Raw": ("Elgato Wave XLR",             3),
}
PROGRAM_AUDIO_SOURCE = "Wave XLR"

# Scenes that carry the audio items. An audio source is live only in the scenes
# containing it; Thumb is silent on purpose.
AUDIO_ITEM_SCENES = ("Desktop", "BigHead")

# How far the Wave XLR audio must be DELAYED to line up with the picture, in
# milliseconds. Measure with ./sync.py, then record the answer here so it
# survives an apply. 0 means unmeasured, not "verified as zero".
#
# Positive: the mic runs ahead of the picture -> delay the mic, via the source's
# own sync offset. Negative: the mic runs behind, which a mic delay cannot undo,
# so the CAMERA is delayed by the same amount instead with a Video Delay (Async)
# filter. One constant, two mechanisms, because only one of them can fix a
# given sign.
SYNC_OFFSET_MS = 0

# The camera source that a negative SYNC_OFFSET_MS delays. Only the ZV-1 path is
# measured by ./sync.py -- the HD60 X is a different device with its own
# latency, and correcting it would need its own reference.
CAMERA_SOURCE   = "Sony ZV1"
DELAY_FILTER_ID = "async_delay_filter"

# Scenes inside the collection. COLLECTION_NAME above is the collection (the
# file it names); these are the scenes within it.
SCENES = ("Desktop", "Thumb", "BigHead")

# Scene items that must FILL the canvas. When their source's capture resolution
# changes, these reset to scale 1.0 instead of being inverse-scaled -- inverse
# scaling is only right for an item whose on-screen size should be preserved,
# like a PiP. Sony ZV1 is a PiP in Desktop and full-frame in BigHead, so this
# has to be keyed on (scene, item) rather than on the source.
FULLSCREEN_ITEMS = {
    ("Desktop", "HDX 60"),
    ("BigHead", "Sony ZV1"),
    ("Thumb",   "Image"),
}

# Sources apply.py deliberately leaves alone. Thumb's image is swapped by hand
# for every recording, so its file path is transient by design.
UNMANAGED_SOURCE_IDS = ("image_source",)


def preset_w(p):
    """AVCaptureSessionPreset3840x2160 -> 3840"""
    try:
        return int(p.split("Preset")[1].split("x")[0])
    except (IndexError, ValueError):
        return None

changes = []

def note(what):
    changes.append(what)
    print(("  APPLY  " if WRITE else "  would ") + what)

# ---------------------------------------------------------------- safety

def obs_running():
    return subprocess.run(["pgrep", "-x", "OBS"], capture_output=True).returncode == 0

def resolve_audio_uid(name):
    out = subprocess.run([os.path.join(PROJ, "bin/adev")],
                         capture_output=True, text=True).stdout
    lines = [l.strip() for l in out.splitlines()]
    for i, l in enumerate(lines):
        if l.startswith(name + " ") or l.split("   [")[0] == name:
            for nxt in lines[i+1:i+3]:
                if nxt and not nxt.startswith(name):
                    return nxt
    return None

# ---------------------------------------------------------------- builders

# OBS fills in every one of these keys itself on save; writing them here means
# the collection we produce is loadable as-is rather than only after OBS has
# rewritten it once.
def new_audio_source(name, device_uid, track):
    return {
        "prev_ver": 537001986, "name": name, "uuid": str(uuid.uuid4()),
        "id": "coreaudio_input_capture", "versioned_id": "coreaudio_input_capture",
        "settings": {"device_id": device_uid},
        "mixers": 1 << (track - 1), "sync": 0, "flags": 0,
        "volume": 1.0, "balance": 0.5, "enabled": True, "muted": False,
        "push-to-mute": False, "push-to-mute-delay": 0,
        "push-to-talk": False, "push-to-talk-delay": 0,
        "hotkeys": {"libobs.mute": [], "libobs.unmute": [],
                    "libobs.push-to-mute": [], "libobs.push-to-talk": []},
        "deinterlace_mode": 0, "deinterlace_field_order": 0,
        "monitoring_type": 0, "private_settings": {},
    }


def new_audio_item(name, source_uuid, item_id):
    """An audio scene item. It has no picture, so the geometry is all inert --
    it is copied from the existing Wave XLR item so the two are indistinguishable
    to OBS."""
    return {
        "name": name, "source_uuid": source_uuid,
        "visible": True, "locked": False, "rot": 0.0,
        "scale_ref": {"x": float(CANVAS_W), "y": float(CANVAS_H)},
        "align": 5, "bounds_type": 0, "bounds_align": 0, "bounds_crop": False,
        "crop_left": 0, "crop_top": 0, "crop_right": 0, "crop_bottom": 0,
        "id": item_id, "group_item_backup": False,
        "pos": {"x": 0.0, "y": 0.0},
        "pos_rel": {"x": -(CANVAS_W / CANVAS_H), "y": -1.0},
        "scale": {"x": 1.0, "y": 1.0}, "scale_rel": {"x": 1.0, "y": 1.0},
        "bounds": {"x": 0.0, "y": 0.0}, "bounds_rel": {"x": 0.0, "y": 0.0},
        "scale_filter": "disable", "blend_method": "default", "blend_type": "normal",
        "show_transition": {"duration": 300}, "hide_transition": {"duration": 300},
        "private_settings": {},
    }


def new_delay_filter(delay_ms):
    return {
        "prev_ver": 537001986, "name": "Video Delay (Async)", "uuid": str(uuid.uuid4()),
        "id": DELAY_FILTER_ID, "versioned_id": DELAY_FILTER_ID,
        "settings": {"delay_ms": delay_ms},
        "mixers": 0, "sync": 0, "flags": 0, "volume": 1.0, "balance": 0.5,
        "enabled": True, "muted": False,
        "push-to-mute": False, "push-to-mute-delay": 0,
        "push-to-talk": False, "push-to-talk-delay": 0,
        "hotkeys": {}, "deinterlace_mode": 0, "deinterlace_field_order": 0,
        "monitoring_type": 0, "private_settings": {},
    }

# ---------------------------------------------------------------- profile

def patch_profile():
    print("\n[profile] basic.ini")
    p = os.path.join(PROFILE, "basic.ini")
    cp = configparser.ConfigParser()
    cp.optionxform = str                      # OBS keys are case-sensitive
    cp.read(p)
    for sect, kv in PROFILE_INI.items():
        if not cp.has_section(sect):
            cp.add_section(sect)
        for k, v in kv.items():
            v = str(v)
            if cp.get(sect, k, fallback=None) != v:
                note("%s.%s: %s -> %s" % (sect, k, cp.get(sect, k, fallback="(unset)"), v))
                if WRITE:
                    cp.set(sect, k, v)
    if WRITE:
        with open(p, "w") as f:
            cp.write(f, space_around_delimiters=False)

# ---------------------------------------------------------------- scene

def patch_encoder():
    print("\n[profile] recordEncoder.json")
    p = os.path.join(PROFILE, "recordEncoder.json")
    cur = {}
    if os.path.exists(p):
        try:
            cur = json.load(open(p))
        except ValueError:
            pass
    if cur != ENCODER_JSON:
        note("encoder settings: %s -> %s" % (cur or "(absent)", ENCODER_JSON))
        if WRITE:
            json.dump(ENCODER_JSON, open(p, "w"))
    return


def check_layout(d):
    """Fail early if the scenes were renamed out from under us.

    Nothing below matches on a scene name, so a rename is survivable at runtime --
    but a rename we do not know about means FULLSCREEN_ITEMS and the docs are
    stale, and silently applying a stale layout is the worst outcome.
    """
    scenes = {s["name"] for s in d.get("sources", []) if s.get("id") == "scene"}
    missing = sorted(set(SCENES) - scenes)
    extra   = sorted(scenes - set(SCENES))
    if missing or extra:
        sys.exit("scene layout drifted: missing %s, unexpected %s\n"
                 "Update SCENES / FULLSCREEN_ITEMS in apply.py and notes/rig-guide.html."
                 % (missing or "none", extra or "none"))


def ensure_audio_sources(d, uids):
    """Create any missing audio source, and hold every one to its device and
    its track. `mixers` is a bitmask of recording tracks, so track 2 is 0b10 --
    the two sources must not overlap or each track would carry both."""
    by_name = {s["name"]: s for s in d["sources"]}
    for src_name, (dev, track) in AUDIO_SOURCES.items():
        want_mixers = 1 << (track - 1)
        s = by_name.get(src_name)
        if s is None:
            note("source %r (%s) -> created on track %d" % (src_name, dev, track))
            if WRITE:
                d["sources"].append(new_audio_source(src_name, uids[src_name], track))
            continue
        if s.get("id") != "coreaudio_input_capture":
            sys.exit("source %r is a %s, but AUDIO_SOURCES expects an audio input."
                     % (src_name, s.get("id")))
        st = s.setdefault("settings", {})
        if st.get("device_id") != uids[src_name]:
            note("source %r device -> %s" % (src_name, dev))
            if WRITE:
                st["device_id"] = uids[src_name]
        if s.get("mixers") != want_mixers:
            note("source %r tracks: %s -> %d (track %d only)"
                 % (src_name, bin(s.get("mixers", 0)), want_mixers, track))
            if WRITE:
                s["mixers"] = want_mixers


def ensure_audio_items(d):
    """Put every audio source into every scene that should have sound.

    An audio source is live only in the scenes containing it -- this is the same
    trap that made BigHead record silence. A reference track that is missing from
    the scene you actually recorded is worse than no reference track, because the
    file still has two tracks and only one of them has anything on it.
    """
    uuid_of = {s["name"]: s.get("uuid") for s in d["sources"]}
    for s in d["sources"]:
        if s.get("id") != "scene" or s["name"] not in AUDIO_ITEM_SCENES:
            continue
        items = s.setdefault("settings", {}).setdefault("items", [])
        have  = {it.get("name") for it in items}
        for src_name in AUDIO_SOURCES:
            if src_name in have:
                continue
            note("scene %s: add audio item %r" % (s["name"], src_name))
            if WRITE:
                next_id = max([it.get("id", 0) for it in items] or [0]) + 1
                items.append(new_audio_item(src_name, uuid_of[src_name], next_id))


def apply_sync_offset(d):
    """Realise SYNC_OFFSET_MS, whichever direction it points.

    A source's `sync` field is nanoseconds and can only ever hold audio BACK, so
    it fixes a mic that runs ahead of the picture. A mic running behind needs the
    opposite, which means delaying the camera instead. Both are driven from the
    one constant, and each run clears the mechanism it is not using so that
    flipping the sign cannot leave a stale correction behind.
    """
    by_name = {s["name"]: s for s in d["sources"]}

    mic = by_name.get(PROGRAM_AUDIO_SOURCE)
    want_ns = max(SYNC_OFFSET_MS, 0) * 1_000_000
    if mic is not None and mic.get("sync") != want_ns:
        note("source %r sync offset: %+.0f -> %+.0f ms"
             % (PROGRAM_AUDIO_SOURCE, mic.get("sync", 0) / 1e6, want_ns / 1e6))
        if WRITE:
            mic["sync"] = want_ns

    cam = by_name.get(CAMERA_SOURCE)
    if cam is None:
        return
    filters   = cam.setdefault("filters", [])
    want_ms   = max(-SYNC_OFFSET_MS, 0)
    existing  = [f for f in filters if f.get("id") == DELAY_FILTER_ID]
    if want_ms == 0:
        for f in existing:
            note("source %r: remove %s (%s ms)"
                 % (CAMERA_SOURCE, f["name"], f.get("settings", {}).get("delay_ms")))
            if WRITE:
                filters.remove(f)
    elif not existing:
        note("source %r: add Video Delay (Async) %d ms" % (CAMERA_SOURCE, want_ms))
        if WRITE:
            filters.append(new_delay_filter(want_ms))
    else:
        f = existing[0]
        if f.get("settings", {}).get("delay_ms") != want_ms:
            note("source %r: video delay %s -> %d ms"
                 % (CAMERA_SOURCE, f.get("settings", {}).get("delay_ms"), want_ms))
            if WRITE:
                f.setdefault("settings", {})["delay_ms"] = want_ms


def patch_scene():
    print("\n[scene] %s" % os.path.basename(SCENE))
    d = json.load(open(SCENE))
    check_layout(d)

    uids = {}
    for src_name, (dev, _track) in AUDIO_SOURCES.items():
        uid = resolve_audio_uid(dev)
        if not uid:
            print("  !! could not resolve %r for source %r" % (dev, src_name))
            print("     (Wave Link running? Cam Link plugged in and fed a signal?)")
            return None
        print("  resolved %s -> %s" % (dev, uid))
        uids[src_name] = uid

    ensure_audio_sources(d, uids)

    rescale = {}          # source name -> (old_w/new_w, new_w)
    for s in d.get("sources", []):
        st = s.setdefault("settings", {})

        if s.get("id") == "macos-avcapture":
            want = SOURCE_PRESETS.get(st.get("device_name"))
            if want and st.get("preset") != want:
                note("source %r preset: %s -> %s" % (s["name"], st.get("preset"), want))
                old_w, new_w = preset_w(st.get("preset", "")), preset_w(want)
                if old_w and new_w:
                    rescale[s["name"]] = (old_w / new_w, new_w)
                if WRITE:
                    st["preset"] = want

        if s.get("id") == "coreaudio_input_capture" and s["name"] not in AUDIO_SOURCES:
            sys.exit("unknown audio source %r (device %s).\n"
                     "Audio is matched by NAME because two sources now share the\n"
                     "coreaudio type. Add it to AUDIO_SOURCES in apply.py with the\n"
                     "track it belongs on, or delete it in OBS."
                     % (s["name"], st.get("device_id")))

        if s.get("id") in UNMANAGED_SOURCE_IDS:
            f = st.get("file")
            if f and not os.path.exists(f):
                print("  !! source %r references a missing file: %s" % (s["name"], f))

        if s.get("monitoring_type", 0) != 0:
            note("source %r monitoring -> Monitor Off" % s["name"])
            if WRITE:
                s["monitoring_type"] = 0

    ensure_audio_items(d)
    apply_sync_offset(d)

    # The global audio slots stay empty. AuxAudioDevice1 held 'default' (= the
    # HD60 X): a second live audio path, and the cause of the launch hang. All
    # six are checked, not just the one that was populated -- OBS offers them in
    # the same settings pane, so the next one to get filled in by accident would
    # be a different key with the identical failure.
    for slot in ("DesktopAudioDevice1", "DesktopAudioDevice2",
                 "AuxAudioDevice1", "AuxAudioDevice2",
                 "AuxAudioDevice3", "AuxAudioDevice4"):
        if slot in d:
            note("global audio slot %s (device_id=%r) -> removed"
                 % (slot, d[slot].get("settings", {}).get("device_id")))
            if WRITE:
                del d[slot]

    # preserve on-screen size for items whose source resolution changed
    for s in d.get("sources", []):
        if s.get("id") != "scene":
            continue
        scene = s["name"]
        for it in s.get("settings", {}).get("items", []):
            got = rescale.get(it.get("name"))
            if not got:
                continue
            r, new_w = got
            if it.get("bounds_type", 0) != 0:
                print("  .. %s/%r uses bounds; scale left alone" % (scene, it["name"]))
                continue
            sc = it.get("scale", {})
            if (scene, it["name"]) in FULLSCREEN_ITEMS:
                nx = ny = 1.0            # fills the canvas at any source resolution
            else:
                nx, ny = round(sc.get("x", 1) * r, 6), round(sc.get("y", 1) * r, 6)
            if (sc.get("x"), sc.get("y")) == (nx, ny):
                continue
            note("item %s/%r scale: %.4f -> %.4f  (holds %dpx wide on canvas)"
                 % (scene, it["name"], sc.get("x", 1), nx, round(nx * new_w)))
            if WRITE:
                sc["x"], sc["y"] = nx, ny
    return d

# ---------------------------------------------------------------- main

def main():
    if obs_running():
        sys.exit("OBS is running (pid %s). Quit it first - OBS rewrites its\n"
                 "config on exit and would overwrite these changes."
                 % subprocess.run(["pgrep","-x","OBS"],capture_output=True,text=True).stdout.strip())

    print("=== %s ===" % ("APPLYING" if WRITE else "DRY RUN (pass --write to apply)"))

    if WRITE:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest  = os.path.join(BACKUPS, stamp)
        os.makedirs(dest, exist_ok=True)
        shutil.copytree(PROFILE, os.path.join(dest, "profile"))
        shutil.copy2(SCENE, os.path.join(dest, os.path.basename(SCENE)))
        print("backup -> backups/%s" % stamp)

    patch_profile()
    patch_encoder()
    d = patch_scene()
    if WRITE and d is not None:
        json.dump(d, open(SCENE, "w"), indent=4)

    print("\n%d change(s)%s" % (len(changes), "" if WRITE else " pending"))
    if not WRITE and changes:
        print("run:  ./apply.py --write")

main()
