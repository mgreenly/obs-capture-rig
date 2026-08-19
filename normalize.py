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
  * A VERIFY pass measures the output and fails loudly if it missed the target.
    A silent or mangled audio track otherwise looks exactly like a success.
  * Output is .mp4 with the HEVC tag preserved. YouTube takes .mov, but MP4 is
    the container its docs specify, and remuxing is free.
"""
import argparse, json, os, re, shutil, subprocess, sys

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
    if len(audio) > 1:
        # Silently picking stream 0 would quietly drop a track someone meant to keep.
        die("%s has %d audio tracks; this script handles one. Pick a track with "
            "ffmpeg -map first." % (os.path.basename(path), len(audio)))
    return info, video, audio[0]


def measure(path, label):
    """Pass 1 / verify: run loudnorm in analysis mode and read back its JSON."""
    p = run(["ffmpeg", "-nostdin", "-hide_banner", "-i", path, "-vn",
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


def normalize(src, stats, dst, video, audio):
    """Pass 2: one measured correction, video copied through untouched."""
    af = ("loudnorm=I=%s:TP=%s:LRA=%s"
          ":measured_I=%s:measured_TP=%s:measured_LRA=%s:measured_thresh=%s"
          ":offset=%s:linear=true:print_format=summary"
          % (TARGET_I, TARGET_TP, TARGET_LRA,
             stats["input_i"], stats["input_tp"], stats["input_lra"],
             stats["input_thresh"], stats["target_offset"]))

    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-y", "-i", src,
           "-map", "0:v:0", "-map", "0:a:0",
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
    ap.add_argument("-f", "--force", action="store_true", help="overwrite an existing output")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="measure and report, write nothing")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the verification pass over the output")
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            die("%s not found. Install it with: brew install ffmpeg" % tool)

    src = os.path.abspath(os.path.expanduser(args.file)) if args.file else newest_recording()
    if not os.path.isfile(src):
        die("no such file: %s" % src)

    info, video, audio = probe(src)
    dur = float(info["format"].get("duration", 0))
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
    stats = measure(src, "measurement")
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
    normalize(src, stats, dst, video, audio)

    if not args.no_verify:
        print("verifying...")
        after = measure(dst, "verify")
        got   = float(after["input_i"]) if after["input_i"] != "-inf" else None
        if got is None:
            die("the normalized file measures as silence - %s is not usable" % dst)
        print("         integrated %s LUFS   true peak %s dBTP   range %s LU"
              % (fmt(after, "input_i"), fmt(after, "input_tp"), fmt(after, "input_lra")))
        if abs(got - TARGET_I) > TOLERANCE_I:
            die("output measured %.1f LUFS, more than %.1f LU off the %.1f target.\n"
                "The file is at %s; do not upload it without checking."
                % (got, TOLERANCE_I, TARGET_I, dst))

    print("\nwrote    %s  (%.1f MB)" % (dst, os.path.getsize(dst) / 1e6))


if __name__ == "__main__":
    main()
