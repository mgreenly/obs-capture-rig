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
"""
import configparser, json, os, shutil, subprocess, sys, time

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
        "RecTracks": 1,
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

AUDIO_SOURCE_DEVICE = "Elgato Wave Link Stream Mix"   # resolved by name

# NOTE: the OBS source carrying this device is NAMED "Wave XLR", which is not
# what it is. It captures Wave Link's Stream Mix, i.e. the mic AFTER Wave Link's
# processing. The name is the mic on the desk; the device is the mix. apply.py
# matches on source id, not name, so the label is free to say whatever is
# convenient -- but do not "fix" the device to the literal Wave XLR interface.

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


def patch_scene():
    print("\n[scene] %s" % os.path.basename(SCENE))
    d = json.load(open(SCENE))
    check_layout(d)

    uid = resolve_audio_uid(AUDIO_SOURCE_DEVICE)
    if not uid:
        print("  !! could not resolve %r - is Wave Link running?" % AUDIO_SOURCE_DEVICE)
        return None
    print("  resolved %s -> %s" % (AUDIO_SOURCE_DEVICE, uid))

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

        if s.get("id") == "coreaudio_input_capture":
            if st.get("device_id") != uid:
                note("source %r device -> %s" % (s["name"], AUDIO_SOURCE_DEVICE))
                if WRITE:
                    st["device_id"] = uid

        if s.get("id") in UNMANAGED_SOURCE_IDS:
            f = st.get("file")
            if f and not os.path.exists(f):
                print("  !! source %r references a missing file: %s" % (s["name"], f))

        if s.get("monitoring_type", 0) != 0:
            note("source %r monitoring -> Monitor Off" % s["name"])
            if WRITE:
                s["monitoring_type"] = 0

    # global Mic/Aux: currently 'default' (= HD60 X) and duplicating the mic path
    if "AuxAudioDevice1" in d:
        note("global Mic/Aux (device_id=%r) -> removed"
             % d["AuxAudioDevice1"].get("settings", {}).get("device_id"))
        if WRITE:
            del d["AuxAudioDevice1"]

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
