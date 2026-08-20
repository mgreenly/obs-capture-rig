#!/usr/bin/env python3
"""
Normalize a recording's audio to YouTube's loudness target.

    ./normalize.py               # the newest recording in ~/Movies
    ./normalize.py <file>        # a specific one

Writes a new file to ~/Movies/normalized/ and never touches the original.

Why this exists: the rig records lossless 24-bit PCM with deliberate headroom
(see README, "Set gain for headroom, not for level"). Takes land around -30 dBFS
RMS, which is correct on disk and far too quiet for upload. YouTube plays
everything back at about -14 LUFS: quieter uploads are left alone, so a raw take
just sounds weak next to every other video. This lifts it to that target once,
in one clean gain/limiter stage, instead of letting the platform's loudness
gap do it by accident.

Design notes:
  * TWO-PASS loudnorm. The single-pass form is a dynamic normalizer that rides
    the level as it goes, which pumps on speech. Pass 1 only measures; pass 2
    applies one measured correction across the whole file. This is the only
    reason the measured numbers are threaded through by hand.
  * VIDEO IS COPIED, never re-encoded. The source is 4K HEVC from the hardware
    encoder; a re-encode would cost quality and an hour for nothing. That also
    means this is safe to re-run.
  * Measurement passes decode audio only (-vn). Skipping 4K video decode is
    what keeps three passes over a long take cheap.
  * SYNC IS TWO CORRECTIONS, not one: a constant offset and a rate. The devices
    start misaligned AND run on unlocked crystals ~64 ppm apart, so a single
    number is only right at one instant of a long take. The rate is fixed by
    resampling, the constant by shifting, and both are measured per take.
  * THE SOURCE IS CHECKED FIRST, with bin/level, before any decoding work. A
    silent track passes every format check there is and only reveals itself when
    something tries to measure against it.
  * A VERIFY pass measures the output and fails loudly if it missed the target.
    A silent or mangled audio track otherwise looks exactly like a success.
  * Output is .mp4 with the HEVC tag preserved. YouTube takes .mov, but MP4 is
    the container its docs specify, and remuxing is free.
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile

import sync            # same directory; measures the per-take offset

PROJ    = os.path.dirname(os.path.abspath(__file__))
LEVEL   = os.path.join(PROJ, "bin/level")

MOVIES  = os.path.expanduser("~/Movies")
OUTDIR  = os.path.join(MOVIES, "normalized")
EXTS    = (".mov", ".mp4", ".mkv", ".m4v")

# YouTube normalizes playback to roughly -14 LUFS integrated. -1 dBTP of true-peak
# headroom is the usual allowance for lossy-codec overshoot: AAC is decoded in the
# frequency domain and can reconstruct samples slightly above the encoded peak, so
# mastering to 0 dBTP clips on someone else's player rather than on ours.
TARGET_I   = -14.0
TARGET_TP  = -1.0
TARGET_LRA = 11.0

# Tolerance for the verify pass. loudnorm's linear mode is exact in principle, but
# it re-measures on a different filter graph, so demand "close", not "identical".
TOLERANCE_I = 1.0

# How far above TARGET_TP the output may land. Loudness is a preference; true peak
# is a correctness bound, so this is tighter. linear=true applies one fixed gain
# and does NOT guarantee the true-peak ceiling -- when the required gain would
# push peaks past it, they go past it -- so the output has to be checked rather
# than assumed. Anything at or above 0 dBTP clips on someone else's decoder.
TOLERANCE_TP = 0.5

# How far the output's audio and video durations may differ, in seconds. The
# corrections legitimately move audio by tens of milliseconds; anything past this
# is a filter operating on a different sample rate than it thinks, not a sync
# offset. Generous enough never to fire on a real correction, tight enough that
# it cannot miss one.
MAX_DURATION_SKEW_S = 0.5

# How far the Wave XLR audio must be DELAYED to line up with the picture, in
# milliseconds. "auto" measures it from the take's own reference track; a number
# pins it; 0 disables.
#
# AUTO IS THE DEFAULT because the offset is not a property of the rig. Two
# captures of this setup measured -22 ms and -33 ms: the USB buffers align
# differently each time OBS starts the sources, so it is a property of the
# capture SESSION. A pinned constant is tuned to whichever take happened to be
# measured and is wrong by up to ~10 ms on every other one. Every take carries
# its own reference track, so there is no reason to guess.
#
# The correction lives HERE rather than in OBS on purpose. It is post-processing:
# the raw take keeps whatever the rig actually did, a wrong value costs a re-run
# instead of a re-shoot, and the live/monitoring path is left alone. apply.py has
# a SYNC_OFFSET_MS of its own for the OBS-side fix -- setting BOTH would correct
# twice, so only one may be non-zero and that is checked at startup.
SYNC_OFFSET_MS = "auto"

# Parts per million the Wave XLR track's clock runs slow against the camera's,
# or "auto" to measure it from the take, or 0 to leave the rate alone.
#
# This is a SECOND, independent fault, and the constant above cannot express it.
# The two devices have their own crystals with nothing locking them together, so
# beyond starting misaligned they run at slightly different rates and the offset
# slides for the whole take. This rig measures about +64 ppm, which is 3.9 ms per
# minute: nothing on a 30 second take, and about 40 ms end to end on the ten
# minute ones. Correcting only the constant centres that error instead of
# removing it, leaving roughly +/-20 ms that no amount of better measurement can
# reach -- which is exactly the "it seems fine but I cannot quite tell" symptom.
#
# AUTO for the same reason the offset is auto: it is measured, not guessed, and
# every take carries the reference track it needs. Unlike the offset this one
# probably IS a hardware constant -- a ratio between two crystals rather than
# however the USB buffers happened to line up -- but "probably" is not measured,
# and the take that disagrees would be corrected wrongly and silently.
#
# 62 ppm is ordinary. Consumer audio clocks are specified to +/-50 or +/-100 ppm,
# and nothing here word-locks them. It is NOT the 29.97-vs-30 pulldown ratio,
# which is 1000 ppm and would slip a full second every 17 minutes.
DRIFT_PPM = "auto"

# 1-based track carrying the camera's own mic, the sync reference.
REF_TRACK = 2

AUDIO_CODEC   = "aac"
AUDIO_BITRATE = "384k"      # YouTube's recommended stereo bitrate
AUDIO_RATE    = "48000"     # what the rig captures; resampling would be a downgrade


def die(msg):
    sys.exit("normalize: " + msg)


def run(cmd, capture=True):
    """Run ffmpeg/ffprobe. Non-zero exit is fatal - a partial file is worse than none."""
    p = subprocess.run(cmd, capture_output=capture, text=True)
    if p.returncode != 0:
        tail = "\n".join((p.stderr or "").strip().splitlines()[-15:])
        die("%s failed (exit %d)\n%s" % (os.path.basename(cmd[0]), p.returncode, tail))
    return p


def check_levels(path):
    """Refuse a source that did not record what it was supposed to.

    ffprobe reports the audio FORMAT, and a fully silent track satisfies that
    exactly as well as a good one. bin/level decodes the samples and exits
    non-zero if any track is silent, which is the failure that matters here: a
    reference track that recorded nothing looks like an ordinary take right up
    until the sync measurement is computed against silence.

    Checked FIRST, before the loudnorm passes and before the sync measurement,
    because those are minutes of decoding and this is a second. The earliest
    point a defect is detectable is the right place to detect it.
    """
    if not os.path.exists(LEVEL):
        die("%s is not built, so the source cannot be checked.\nRun:  make\n"
            "(or pass --no-level to skip the check)" % os.path.relpath(LEVEL, PROJ))
    p = subprocess.run([LEVEL, path], capture_output=True, text=True)
    if p.returncode != 0:
        die("the source did not record cleanly:\n\n%s\n%s\n"
            "A silent track cannot be measured against, and a sync correction\n"
            "computed from silence is a confident number from nothing. Re-record,\n"
            "or pass --no-level with -s 0 -d 0 to normalize the audio only."
            % (p.stdout.strip(), (p.stderr or "").strip()))
    # Only the verdict on the way through; the detail is what matters when it
    # fails, and printing 20 lines of per-channel levels on every good take
    # buries the numbers that do need reading.
    verdict = [l for l in p.stdout.splitlines() if l.strip()]
    print("levels   %s" % (verdict[-1] if verdict else "checked"))


def obs_side_offset():
    """Whatever apply.py is currently applying inside OBS.

    Read out of the source rather than duplicated, because the failure mode is
    silent: two 22 ms corrections produce a 44 ms error that looks exactly like a
    sync problem the tools would then be asked to fix again.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apply.py")
    try:
        m = re.search(r"^SYNC_OFFSET_MS\s*=\s*(-?\d+)", open(path).read(), re.M)
    except OSError:
        return 0
    return int(m.group(1)) if m else 0


def sync_filter(offset_ms):
    """Shift the audio by offset_ms against the video, which is copied untouched.

    Applied AFTER loudnorm so the measured correction is computed on the audio as
    recorded. Delaying is prepended silence; advancing has to discard the head of
    the track, since there is nothing before the start to move forward into.
    """
    if offset_ms > 0:
        return "adelay=%d:all=1" % offset_ms
    return "atrim=start=%.6f,asetpts=PTS-STARTPTS" % (-offset_ms / 1000.0)


def drift_filter(ppm):
    """Undo a clock difference of `ppm` between the audio track and the picture.

    Positive ppm means the audio is STRETCHED: its copy of an event sits
    progressively later as the take goes on, by ppm microseconds per second.
    Reinterpreting the samples at a proportionally HIGHER rate pulls every
    position back by that same proportion, and aresample puts the stream back
    onto the rig's 48 kHz grid.

    A resample, not a time-stretch. The implied pitch change is the same few ppm
    -- 64 ppm is about a thousandth of a semitone -- while a tempo filter would
    have to guess at the audio's content to preserve pitch, and would introduce
    artefacts far more audible than the shift it avoided.

    The correction pivots at the START of the file, so it moves the constant
    offset as well. That is why the constant is re-measured on the corrected
    audio rather than carried over (see drift_probe).

    The leading aresample is NOT redundant. asetrate does not resample, it
    relabels whatever rate it is handed, so the ratio it produces depends
    entirely on what came before it in the chain -- and loudnorm outputs 192 kHz,
    four times the rig's rate, because that is what it upsamples to for true-peak
    detection. Without the leading resample this filter reinterprets 192 kHz
    audio as 48 kHz and stretches the take to four times its length. It did
    exactly that once: the loudness verify passed judgement on the result and
    said nothing, which is why verify now checks duration too.
    """
    rate = int(AUDIO_RATE)
    return ("aresample=%d,asetrate=%.6f,aresample=%d"
            % (rate, rate * (1.0 + ppm / 1e6), rate))


def drift_probe(src, track, ref_track, ppm, tmpdir):
    """Apply the rate correction to the audio alone, so the result can be MEASURED.

    Two things need checking and neither can be predicted honestly:

      * whether the correction went the right way. A sign error here produces a
        file that drifts twice as fast, and nothing downstream would notice --
        the output looks exactly like a success.
      * what constant is left. The resample pivots at t=0, so it moves the offset
        too, and extrapolating the fitted line back to t=0 to guess where it
        landed is precisely the kind of arithmetic this project measures instead.

    So the corrected mic and the untouched reference are written to a scratch
    file, audio only, and handed back to sync.py. Cheap next to the 4K video the
    real pass copies, and it turns both questions into measurements.
    """
    dst = os.path.join(tmpdir, "drift-probe.mov")
    run(["ffmpeg", "-nostdin", "-hide_banner", "-y", "-v", "error", "-i", src,
         "-filter_complex", "[0:a:%d]%s[m]" % (track - 1, drift_filter(ppm)),
         "-map", "[m]", "-map", "0:a:%d" % (ref_track - 1), "-vn",
         "-c:a", "pcm_s16le", "-ar", AUDIO_RATE, dst])
    return dst


def newest_recording():
    if not os.path.isdir(MOVIES):
        die("%s does not exist" % MOVIES)
    files = [os.path.join(MOVIES, f) for f in os.listdir(MOVIES)
             if f.lower().endswith(EXTS) and not f.startswith(".")]
    if not files:
        die("no recordings in %s" % MOVIES)
    return max(files, key=os.path.getmtime)


def probe(path):
    """Stream summary. Also the check that there IS an audio track to normalize."""
    p = run(["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path])
    info   = json.loads(p.stdout)
    video  = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    audio  = [s for s in info["streams"] if s["codec_type"] == "audio"]
    if not audio:
        die("%s has no audio track" % os.path.basename(path))
    return info, video, audio


def measure(path, label, track=1):
    """Pass 1 / verify: run loudnorm in analysis mode and read back its JSON.

    The track is selected here too. Measuring stream 0 and then encoding stream 1
    would apply a correction computed from the wrong audio -- and it would look
    like it worked."""
    p = run(["ffmpeg", "-nostdin", "-hide_banner", "-i", path, "-vn",
             "-map", "0:a:%d" % (track - 1),
             "-af", "loudnorm=I=%s:TP=%s:LRA=%s:print_format=json"
                    % (TARGET_I, TARGET_TP, TARGET_LRA),
             "-f", "null", "-"])
    # loudnorm prints its JSON to stderr, after everything else ffmpeg has to say.
    m = re.search(r"\{[^{}]*input_i[^{}]*\}", p.stderr, re.S)
    if not m:
        die("could not read loudnorm measurements from the %s pass" % label)
    return json.loads(m.group(0))


def fmt(stats, key):
    v = stats.get(key, "")
    return "-inf" if v in ("-inf", "inf", "") else "%.1f" % float(v)


def normalize(src, stats, dst, video, audio, track=1, sync_ms=0, drift_ppm=0.0):
    """Pass 2: the measured corrections, video copied through untouched.

    Filter order is loudnorm, then rate, then constant -- the same order the
    measurements were taken in. The constant was measured on drift-corrected
    audio, so it has to be applied to drift-corrected audio.
    """
    af = ("loudnorm=I=%s:TP=%s:LRA=%s"
          ":measured_I=%s:measured_TP=%s:measured_LRA=%s:measured_thresh=%s"
          ":offset=%s:linear=true:print_format=summary"
          % (TARGET_I, TARGET_TP, TARGET_LRA,
             stats["input_i"], stats["input_tp"], stats["input_lra"],
             stats["input_thresh"], stats["target_offset"]))
    if drift_ppm:
        af += "," + drift_filter(drift_ppm)
    if sync_ms:
        af += "," + sync_filter(sync_ms)

    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-y", "-i", src,
           "-map", "0:v:0", "-map", "0:a:%d" % (track - 1),
           "-c:v", "copy"]
    # HEVC in MP4 needs the hvc1 tag; ffmpeg defaults to hev1, which QuickTime and
    # some upload-side probes will not play. Nothing is re-encoded either way.
    if video and video.get("codec_name") == "hevc":
        cmd += ["-tag:v", "hvc1"]
    cmd += ["-af", af,
            "-c:a", AUDIO_CODEC, "-b:a", AUDIO_BITRATE, "-ar", AUDIO_RATE,
            "-movflags", "+faststart",
            dst]
    run(cmd)


def main():
    ap = argparse.ArgumentParser(
        description="Normalize a recording's audio to YouTube's -14 LUFS target.")
    ap.add_argument("file", nargs="?",
                    help="recording to normalize (default: newest in ~/Movies)")
    ap.add_argument("-o", "--output", help="output path (default: ~/Movies/normalized/<name>.mp4)")
    ap.add_argument("-t", "--track", type=int, default=1, metavar="N",
                    help="1-based audio track to normalize (default 1, the Wave XLR; "
                         "track 2 is the camera sync reference)")
    ap.add_argument("-f", "--force", action="store_true", help="overwrite an existing output")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="measure and report, write nothing")
    ap.add_argument("-s", "--sync-offset", default=None, metavar="MS",
                    help="ms to delay audio against video, or 'auto' to measure it "
                         "from the take (default: %s; 0 disables)" % SYNC_OFFSET_MS)
    ap.add_argument("-d", "--drift", default=None, metavar="PPM",
                    help="parts per million the audio clock runs slow against the "
                         "camera's, or 'auto' to measure it from the take "
                         "(default: %s; 0 leaves the rate alone)" % DRIFT_PPM)
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the verification pass over the output")
    ap.add_argument("--no-level", action="store_true",
                    help="skip the bin/level check on the source. Only sensible "
                         "with -s 0 -d 0, since a silent reference track is exactly "
                         "what makes a sync measurement meaningless")
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            die("%s not found. Install it with: brew install ffmpeg" % tool)

    sync_ms = SYNC_OFFSET_MS if args.sync_offset is None else args.sync_offset
    if sync_ms != "auto":
        try:
            sync_ms = int(sync_ms)
        except (TypeError, ValueError):
            die("--sync-offset takes a whole number of ms, or 'auto'")
    sync_auto = sync_ms == "auto"

    drift_ppm = DRIFT_PPM if args.drift is None else args.drift
    if drift_ppm != "auto":
        try:
            drift_ppm = float(drift_ppm)
        except (TypeError, ValueError):
            die("--drift takes a number of ppm, or 'auto'")
    obs_ms  = obs_side_offset()
    if sync_ms and obs_ms:
        die("both corrections are active: apply.py SYNC_OFFSET_MS=%d and this "
            "script's %d ms.\nThat corrects twice. Zero one of them -- the OBS-side "
            "fix if you want the raw\ntakes to be right, this one if you want it "
            "reversible." % (obs_ms, sync_ms))

    src = os.path.abspath(os.path.expanduser(args.file)) if args.file else newest_recording()
    if not os.path.isfile(src):
        die("no such file: %s" % src)

    if not args.no_level:
        check_levels(src)

    info, video, tracks = probe(src)
    # Recordings carry a second track: the camera's own mic, kept as a sync
    # reference (see sync.py). It is not program audio and must not be what gets
    # normalized, nor ride along into the upload -- so one track is selected
    # explicitly and the rest are dropped here, at the one place that matters.
    if args.track > len(tracks):
        die("--track %d, but %s has %d audio track(s)"
            % (args.track, os.path.basename(src), len(tracks)))
    audio = tracks[args.track - 1]
    if len(tracks) > 1:
        print("tracks   %d present; normalizing track %d, dropping the rest"
              % (len(tracks), args.track))
    dur = float(info["format"].get("duration", 0))

    if sync_auto or drift_ppm == "auto":
        if len(tracks) < REF_TRACK:
            die("'auto' needs the camera reference on track %d, but %s has %d\n"
                "audio track(s). Takes from before the reference track was added\n"
                "cannot be measured after the fact -- use -s 0 -d 0 to normalize\n"
                "without corrections, or -s <ms> -d <ppm> to pin them."
                % (REF_TRACK, os.path.basename(src), len(tracks)))
        print("sync     measuring track %d against the camera on track %d..."
              % (args.track, REF_TRACK))
        # One pass yields both corrections, from the same windows, so they cannot
        # disagree about which part of the take they describe.
        m = sync.analyse_take(src, args.track - 1, REF_TRACK - 1)
        if not m["ok"]:
            # Refusing beats silently shipping an uncorrected file: that is
            # indistinguishable from a correct one until someone watches it.
            die("could not measure the sync offset -- %s.\n\n"
                "Run  ./sync.py '%s'  for the detail. Use -s 0 to normalize with no\n"
                "correction, or -s <ms> to pin one." % (m["why"], src))
        if sync_auto:
            sync_ms = int(round(m["offset_ms"]))
        if drift_ppm == "auto":
            if m["drift_ppm"] is None:
                # Not an error. Most takes are too short for a slope to separate
                # from scatter, and on a short take it does not matter anyway.
                print("drift    not measurable on this take: %s" % m["why"])
                print("         The constant below still applies. On a take long")
                print("         enough to drift, a constant is only right in the")
                print("         middle of it -- check ./sync.py before uploading.")
                drift_ppm = 0.0
            else:
                drift_ppm = m["drift_ppm"]
                print("drift    %+.1f ppm  (%+.2f ms/min, %+.0f ms across this take)"
                      % (drift_ppm, drift_ppm / 1000.0 * 60, drift_ppm / 1000.0 * dur))

    # Applying the rate correction moves the constant, so the constant is
    # re-measured on audio that has already had it applied, rather than derived
    # from the uncorrected measurement by arithmetic nobody can check.
    if drift_ppm and len(tracks) >= REF_TRACK:
        print("         checking it on the audio before committing to it...")
        with tempfile.TemporaryDirectory() as tmp:
            probe_file = drift_probe(src, args.track, REF_TRACK, drift_ppm, tmp)
            after = sync.analyse_take(probe_file, 0, 1)
            if not after["ok"]:
                die("the rate-corrected audio could not be measured -- %s.\n"
                    "Pass -d 0 to normalize without the rate correction."
                    % after["why"])
            # The largest slope EITHER method still sees, not their consensus.
            # A correction applied backwards leaves a take drifting twice as fast,
            # and the two methods then tend to disagree about how fast -- which
            # would read as "no drift measured" and wave the broken file through.
            # The question here is not what the remaining drift is, it is whether
            # there is any sign of one.
            left = after["drift_seen_ppm"]
            if left is not None and left > abs(drift_ppm) / 2.0:
                die("the rate correction did not take: %+.1f ppm was measured, and\n"
                    "%.1f ppm is still detectable after correcting for it. Applying\n"
                    "this would bend the take rather than straighten it.\n\n"
                    "Pass -d 0 to normalize with the constant correction only."
                    % (drift_ppm, left))
            print("         %s; constant on the corrected audio is %+.0f ms"
                  % ("no slope left in either method" if left is None
                     else "%.1f ppm still detectable" % left, after["offset_ms"]))
            if sync_auto:
                sync_ms = int(round(after["offset_ms"]))
    elif drift_ppm:
        print("drift    %+.1f ppm applied UNVERIFIED -- no reference track to check "
              "it against" % drift_ppm)

    if sync_ms:
        print("sync     audio %s %d ms against the picture"
              % ("delayed" if sync_ms > 0 else "advanced", abs(sync_ms)))
    elif obs_ms:
        print("sync     %+d ms, already applied in OBS by apply.py" % obs_ms)
    else:
        print("sync     no correction (SYNC_OFFSET_MS = 0)")
    print("source   %s" % src)
    print("         %s %s / %s %s Hz %sch, %.1f s"
          % (video["codec_name"] if video else "no video",
             "%sx%s" % (video["width"], video["height"]) if video else "",
             audio["codec_name"], audio.get("sample_rate", "?"),
             audio.get("channels", "?"), dur))

    # Resolve and check the destination BEFORE measuring - refusing to overwrite
    # after a full decode pass makes you wait to learn nothing will happen.
    dst = None
    if not args.dry_run:
        dst = os.path.abspath(os.path.expanduser(args.output)) if args.output else \
              os.path.join(OUTDIR, os.path.splitext(os.path.basename(src))[0] + ".mp4")
        if os.path.exists(dst) and not args.force:
            die("output already exists: %s\nuse --force to overwrite" % dst)
        if os.path.abspath(dst) == os.path.abspath(src):
            die("output would overwrite the original")

    print("\nmeasuring...")
    stats = measure(src, "measurement", args.track)
    print("         integrated %s LUFS   true peak %s dBTP   range %s LU"
          % (fmt(stats, "input_i"), fmt(stats, "input_tp"), fmt(stats, "input_lra")))
    print("target   integrated %.1f LUFS   true peak %.1f dBTP" % (TARGET_I, TARGET_TP))
    gain = TARGET_I - float(stats["input_i"]) if stats["input_i"] not in ("-inf",) else None
    if gain is None:
        die("the audio track measures as silence; there is nothing to normalize")
    print("         gain %+.1f dB" % gain)

    if args.dry_run:
        print("\ndry run - nothing written")
        return

    os.makedirs(os.path.dirname(dst), exist_ok=True)

    print("\nnormalizing (video copied, audio re-encoded)...")
    normalize(src, stats, dst, video, audio, args.track, sync_ms, drift_ppm)

    if not args.no_verify:
        print("verifying...")
        after = measure(dst, "verify")   # the output has one track by construction
        got   = float(after["input_i"]) if after["input_i"] != "-inf" else None
        if got is None:
            die("the normalized file measures as silence - %s is not usable" % dst)
        print("         integrated %s LUFS   true peak %s dBTP   range %s LU"
              % (fmt(after, "input_i"), fmt(after, "input_tp"), fmt(after, "input_lra")))
        if abs(got - TARGET_I) > TOLERANCE_I:
            die("output measured %.1f LUFS, more than %.1f LU off the %.1f target.\n"
                "The file is at %s; do not upload it without checking."
                % (got, TOLERANCE_I, TARGET_I, dst))

        # Audio and video must still describe the same span of time. Every
        # correction here is a filter that MOVES audio in time, and a mistake in
        # one of them changes the track's length rather than its level -- which
        # every loudness check in this file would pass without comment.
        vdur = adur = None
        for st in probe(dst)[0]["streams"]:
            d = float(st.get("duration", 0) or 0)
            if st["codec_type"] == "video":
                vdur = d
            elif st["codec_type"] == "audio":
                adur = d
        if vdur and adur and abs(vdur - adur) > MAX_DURATION_SKEW_S:
            die("the output's audio is %.1f s against %.1f s of video, a %.1f s "
                "difference.\nA sync correction changed the track's LENGTH, which "
                "means it was applied to\naudio at a different sample rate than it "
                "assumed. The file is at\n%s and is not usable."
                % (adur, vdur, abs(adur - vdur), dst))

        tp = float(after["input_tp"]) if after["input_tp"] not in ("-inf", "inf") else None
        if tp is not None and tp > TARGET_TP + TOLERANCE_TP:
            die("output true peak is %+.1f dBTP, past the %.1f dBTP ceiling.\n"
                "linear=true applies one fixed gain and will run peaks over the\n"
                "ceiling rather than limit them. This clips on playback.\n\n"
                "The source peaked at %s dBTP and needed %+.1f dB: too much for the\n"
                "headroom it had. Turn the Wave XLR's analog gain DOWN so takes land\n"
                "nearer -12 to -6 dBFS, or pass --no-verify to keep this file anyway.\n"
                "It is at %s."
                % (tp, TARGET_TP, fmt(stats, "input_tp"),
                   float(stats["target_offset"]) + (TARGET_I - float(stats["input_i"])),
                   dst))

    print("\nwrote    %s  (%.1f MB)" % (dst, os.path.getsize(dst) / 1e6))


if __name__ == "__main__":
    main()
