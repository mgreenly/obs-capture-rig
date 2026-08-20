#!/usr/bin/env python3
"""
Measure how far the Wave XLR audio has drifted from the picture.

    ./sync.py                 # the newest recording in ~/Movies
    ./sync.py <file>          # a specific one
    ./sync.py --self-test     # prove the sign convention on a synthetic file

Reads nothing but the recording. Prints the number to put in apply.py's
SYNC_OFFSET_MS.

How it can know
---------------
Track 2 is the ZV-1's own on-camera mic. That audio is embedded in the same
HDMI stream as the video and arrives over the same Cam Link USB device, so it
carries the video path's latency: track 2 is a stand-in for when the picture
lands. Track 1 is the Wave XLR by way of Wave Link, a completely separate
device on a separate clock. Both mics heard the same room, so the delta between
the two tracks is the delta between the two paths -- which is exactly the
amount the good audio misses the picture by.

That makes this a measurement rather than a guess. Nudging an offset in an
editor until it "looks right" is guessing, and lands somewhere inside the
~60 ms window where humans stop noticing rather than on zero.

Design notes:
  * ENVELOPES are correlated, not waveforms. The two mics are metres apart in
    different parts of the room, through different preamps -- their waveforms
    do not resemble each other at all, but their loudness contours do. Envelope
    correlation is also what makes a pure-stdlib implementation fast enough:
    a 1 kHz envelope is 48x less data than the audio.
  * 1 ms RESOLUTION, deliberately. OBS's sync offset field is whole
    milliseconds and the audible threshold is tens of them, so a sub-sample
    answer would be precision this cannot act on. A parabolic fit on the
    correlation peak reports the fractional part for information only.
  * COARSE THEN FINE. A 100 Hz envelope finds the offset anywhere inside a
    second; the 1 kHz envelope then refines it locally. Searching 1000 ms at
    1 ms directly costs 10x more for the same answer.
  * SEVERAL WINDOWS, spread across the take. One window gives an offset; the
    spread across windows is the confidence, and the SLOPE across them is
    sample-clock drift, which a single static offset cannot fix and which no
    single-window measurement can see. Two USB audio devices with no shared
    word clock will always drift somewhat.
  * Windows are chosen by ENERGY within evenly spaced segments. A window over a
    pause correlates noise with noise and produces a confident-looking number
    from nothing.
"""
import argparse, array, json, math, os, shutil, subprocess, sys

MOVIES = os.path.expanduser("~/Movies")
EXTS   = (".mov", ".mp4", ".mkv", ".m4v")

RATE    = 8000     # decode rate; only the envelope is correlated, so this is plenty
ENV_HZ  = 1000     # 1 ms envelope -- matches OBS's offset granularity
DECIM   = 10       # coarse envelope = 100 Hz
COARSE_MS = 1000   # how far out to look before giving up
FINE_MS   = 25     # refinement radius around the coarse peak

# Speed of sound, dry air at 20 C. Only used for the optional geometry
# correction: the camera mic is further from the talker than the desk mic, so it
# hears everything slightly late, and that acoustic delay is not the rig's fault.
SPEED_OF_SOUND = 343.0

# Two methods must agree within this, in ms, before an answer is trusted.
#
# Measured empirically: no per-window score separates a good take from a bad one.
# The near-silent clap take scored 2.0-3.4 sigma of peak prominence and the good
# takes scored 2.0-3.8, so any threshold that passed the good ones passed the bad
# one too. What DID separate them was agreement -- between windows, and between
# methods that fail differently.
#
# The envelope method follows loudness, which Wave Link's compressor and gate
# reshape on track 1 and not on track 2. The onset method follows the rising
# edges of the log envelope, which is gain-independent and largely survives
# compression, but is sparse and prone to locking onto the wrong syllable. They
# are wrong in unrelated ways, so when they land on the same millisecond that is
# real; when they diverge, at least one is lost and neither can be told which.
MAX_METHOD_DELTA = 5.0

# A window further than this from its method's median is discarded before the
# spread is judged. The onset method is sparse and will occasionally lock onto
# the wrong syllable, which is a single wrong window, not a wrong take -- and
# max-minus-min lets that one window veto four that agree. Rejecting outliers
# against the median first is what makes the spread describe the consensus
# rather than the worst straggler.
OUTLIER_MS = 5.0
MIN_INLIERS = 3

# A window has to be long enough that the coarse search's +/-COARSE_MS of slide
# is a small part of it, or the two ends stop overlapping and the correlation is
# computed on whatever is left.
MIN_WINDOW_S = 4.0

# A track this quiet has not really recorded the room, and correlating against it
# produces a confident number from almost nothing. The rig's known-good take sits
# near -30 dBFS RMS.
MIN_RMS_DBFS = -55.0

# How far the windows may disagree before the median stops meaning anything. The
# offset being corrected is tens of ms, so a spread of the same size is not a
# measurement with noise in it -- it is noise.
MAX_SPREAD_MS = 10.0

# Drift is a slope, and a slope needs a long enough baseline to separate from
# scatter. Below this the fit reports the noise, not the clocks.
#
# The real gate is DRIFT_SIGMA below, not this number: a slope is only claimed
# when its rise across the take beats its own residual scatter, which is a test
# a short take fails on its own. This is kept as a floor because a very short
# baseline can pass that test on luck alone.
MIN_DRIFT_S = 60.0

# A fitted line is only called drift when the total rise across the windows is
# this many times the RMS of the residuals about it. Scatter with a chance tilt
# fails; a real clock difference is a straight line and passes easily -- the
# Wave Link path measures a 0.4 ms residual against a 6 ms rise.
DRIFT_SIGMA = 4.0

# How far apart, in ppm, the envelope and onset methods may land before their
# agreement on a drift stops meaning anything. They are wrong in unrelated ways
# (see MAX_METHOD_DELTA), so a slope both of them see is a slope in the clocks
# and not in one method's response to the audio.
DRIFT_METHOD_PPM = 25.0

# ...or this many combined standard errors, whichever is more forgiving. See
# drift_consensus: the two methods pin the slope down to different precisions,
# and a flat ppm limit either rejects a noisy-but-consistent fit or waves through
# two confident fits that genuinely disagree.
DRIFT_METHOD_SIGMA = 3.0

# Windows needed before a line is fitted at all. Two points always fit perfectly
# and three barely constrain a residual, so the significance test above only
# means something once there are several.
MIN_DRIFT_WINDOWS = 5

# Windows to place when a drift measurement is what was asked for. More windows
# than the offset needs: the offset is a median, which converges fast, while a
# slope is only as good as its baseline and its point count.
DRIFT_WINDOWS = 9

# Rig geometry, centimetres from where you sit to each mic. The camera is further
# away and genuinely hears you later; at 34 cm per ms that is real milliseconds
# being blamed on the capture chain. Measure once and fill these in -- None means
# unknown, and the correction is skipped rather than guessed.
CAM_CM = None
MIC_CM = None

# Milliseconds the camera's audio track lags the picture inside OBS. Every
# audio-only measurement is relative to track 2, so it inherits this, and no
# amount of correlating the two audio tracks can reveal it. Measure it once with
# --video and set it here; 0 means "assumed aligned, not verified aligned".
REFERENCE_BIAS_MS = 0.0


def die(msg):
    sys.exit("sync: " + msg)


def run(cmd):
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        tail = "\n".join(p.stderr.decode("utf8", "replace").strip().splitlines()[-15:])
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


def audio_streams(path):
    p = run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
             "-of", "json", path])
    info = json.loads(p.stdout)
    return info, [s for s in info["streams"] if s["codec_type"] == "audio"]


def decode(path, track_index):
    """One audio track, mono, RATE Hz, as signed 16-bit samples."""
    p = run(["ffmpeg", "-v", "error", "-i", path,
             "-map", "0:a:%d" % track_index, "-vn",
             "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"])
    a = array.array("h")
    a.frombytes(p.stdout[:len(p.stdout) // 2 * 2])
    if sys.byteorder == "big":
        a.byteswap()
    return a


# --------------------------------------------------------------- video motion
#
# The one assumption the audio-only measurement cannot check is that track 2 is
# a stand-in for the picture. This checks it against the picture itself.

VID_W, VID_H = 160, 90    # motion needs shape, not detail
VID_RANGE_MS = 500        # how far to search; frame-rate limited anyway


def video_fps(path):
    p = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", path])
    txt = p.stdout.decode().strip()
    num, _, den = txt.partition("/")
    return float(num) / float(den or 1)


def video_motion(path):
    """Per-frame motion energy: mean absolute luma difference between frames.

    A clap is a big movement that stops dead, so motion rises through the
    approach and collapses at contact. That collapse is the event, and it is
    visible at 160x90 -- the picture only has to carry the shape of the gesture,
    which is why this stays cheap on a 4K file.
    """
    p = run(["ffmpeg", "-v", "error", "-i", path,
             "-an", "-vf", "scale=%d:%d,format=gray" % (VID_W, VID_H),
             "-f", "rawvideo", "-"])
    buf, sz = p.stdout, VID_W * VID_H
    out, prev = [], None
    for i in range(0, len(buf) - sz + 1, sz):
        cur = buf[i:i + sz]
        if prev is not None:
            out.append(sum(abs(a - b) for a, b in zip(cur, prev)) / sz)
        prev = cur
    return out


def bin_to_rate(env, src_hz, dst_hz, count):
    """Average a 1 kHz envelope down onto video-frame boundaries, so the audio
    and the motion are sampled on the same grid."""
    out = []
    for i in range(count):
        a = int(i * src_hz / dst_hz)
        b = max(a + 1, int((i + 1) * src_hz / dst_hz))
        chunk = env[a:b]
        out.append(sum(chunk) / len(chunk) if chunk else 0.0)
    return out


def video_offset(path, ref_i=1):
    """Milliseconds the camera's AUDIO track lags the picture.

    This is the bias the audio-only measurement inherits and cannot see. Add it
    to a track-1-vs-track-2 result to get a true audio-vs-picture number.

    Resolution is bounded by the frame rate: one frame is 33 ms at 29.97 fps, so
    this settles the sign and the rough size, not the last few ms. Several claps
    at unrelated points within their frames average that quantisation down.
    """
    fps = video_fps(path)
    motion = video_motion(path)
    if len(motion) < 30:
        return None, "only %d video frames" % len(motion), fps
    aud = onset(envelope(decode(path, ref_i)))
    if not aud:
        return None, "no audio on track %d" % (ref_i + 1), fps

    a = bin_to_rate(aud, ENV_HZ, fps, len(motion))
    # Motion COLLAPSES at contact while audio SPIKES, so the useful video signal
    # is the fall in motion: negative first difference, half-wave rectified.
    m = [max(0.0, motion[i - 1] - motion[i]) for i in range(1, len(motion))]
    a = a[1:]
    lag = int(VID_RANGE_MS * fps / 1000)
    peak, scores = scan(m, a, -lag, lag)
    if peak is None:
        return None, "correlation found nothing", fps
    frac = peak + refine(scores, peak)
    return frac * 1000.0 / fps, None, fps


def rms_dbfs(samples):
    if not len(samples):
        return float("-inf")
    acc = 0
    for v in samples:
        acc += v * v
    r = math.sqrt(acc / len(samples)) / 32768.0
    return 20 * math.log10(r) if r > 0 else float("-inf")


def envelope(samples):
    """Block RMS at ENV_HZ. This is the loudness contour the two mics share."""
    n = RATE // ENV_HZ
    out = []
    for i in range(0, len(samples) - n + 1, n):
        acc = 0
        for j in range(i, i + n):
            v = samples[j]
            acc += v * v
        out.append(math.sqrt(acc / n))
    return out


def onset(env):
    """Rising edges of the log envelope: where energy ARRIVES, not how much.

    The Wave XLR track comes through Wave Link's compressor, gate and Clipguard;
    the camera track is raw. Compression rewrites the loudness contour -- it
    squashes transients and lifts the quiet parts -- so the two tracks' envelopes
    genuinely differ in shape even when perfectly aligned, which drags the
    correlation down and can pull the peak off true.

    Onset strength survives that. A compressor changes how loud an attack is and
    barely changes when it starts, and taking the difference of the LOG envelope
    makes the result independent of gain entirely. Half-wave rectifying keeps
    attacks and discards releases, which is the half a compressor distorts most.
    """
    if not env:
        return []
    floor = max(max(env) * 1e-4, 1e-9)
    lg = [math.log(max(v, floor)) for v in env]
    return [max(0.0, lg[i] - lg[i - 1]) for i in range(1, len(lg))]


def decimate(env, k):
    """Mean over k envelope blocks. Averaging rather than dropping samples keeps
    a transient from vanishing between the ones we would have kept."""
    return [sum(env[i:i + k]) / k for i in range(0, len(env) - k + 1, k)]


def pearson(x, y):
    n = len(x)
    mx = sum(x) / n
    my = sum(y) / n
    num = dx = dy = 0.0
    for i in range(n):
        a = x[i] - mx
        b = y[i] - my
        num += a * b
        dx  += a * a
        dy  += b * b
    if dx <= 0 or dy <= 0:
        return 0.0
    return num / math.sqrt(dx * dy)


def prominence(scores, peak):
    """How far the peak stands above the rest of the searched lags, in sigma."""
    vals = list(scores.values())
    if len(vals) < 8:
        return 0.0
    m  = sum(vals) / len(vals)
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))
    return (scores[peak] - m) / sd if sd > 0 else 0.0


def scan(a, b, lo, hi):
    """Correlation of a[t] against b[t+L] for L in [lo, hi].

    A peak at positive L means b is LATE relative to a: b's copy of an event sits
    L samples further into the file. Everything downstream depends on that, which
    is what --self-test exists to pin down.
    """
    n = min(len(a), len(b))
    scores = {}
    for L in range(lo, hi + 1):
        s = max(0, -L)
        e = min(n, n - L)
        if e - s < 16:
            continue
        scores[L] = pearson(a[s:e], b[s + L:e + L])
    if not scores:
        return None, {}
    return max(scores, key=lambda L: scores[L]), scores


def refine(scores, peak):
    """Parabolic fit through the peak and its neighbours, for the fractional part."""
    y0, y1, y2 = scores.get(peak - 1), scores.get(peak), scores.get(peak + 1)
    if y0 is None or y2 is None:
        return 0.0
    denom = y0 - 2 * y1 + y2
    if denom == 0:
        return 0.0
    d = 0.5 * (y0 - y2) / denom
    return d if -1 < d < 1 else 0.0


def fit_windows(n, count, width_req):
    """Choose a window count and width that actually fit the recording.

    Windows do NOT have to be disjoint -- they are sample points for a trend, not
    a partition of the take -- so the only hard limit is that one window fits.
    Demanding disjoint windows made anything under count*width unmeasurable, which
    is most real takes.

    Preferring disjoint windows when there is room is still worth doing: two
    windows sharing most of their audio agree with each other for reasons that
    have nothing to do with the offset being stable, and the whole point of
    reporting several is that their spread means something.
    """
    total_s = n / ENV_HZ
    if total_s < MIN_WINDOW_S:
        die("recording is %.1f s; the measurement needs at least %.0f s of audio"
            % (total_s, MIN_WINDOW_S))

    width = width_req if width_req else min(10.0, total_s / count)
    width = min(width, total_s)

    # If even one window per segment will not fit, drop windows rather than
    # shrink below the point where a correlation means anything.
    if width < MIN_WINDOW_S:
        width = MIN_WINDOW_S
        count = max(2, int(total_s // MIN_WINDOW_S))
    if width > total_s:
        count, width = 1, total_s

    return count, int(width * ENV_HZ)


def pick_windows(env_a, env_b, count, width_blocks):
    """Windows spread evenly across the take, each nudged onto the loudest audio
    near where it landed.

    Even spacing is what lets the results be read as a trend over time; the local
    search by energy is what keeps a window off a silent stretch without letting
    them all collapse onto the one loudest moment.
    """
    n = min(len(env_a), len(env_b))
    span = n - width_blocks
    if span < 0:
        return []
    if count < 2:
        return [max(0, span // 2)]

    stride = span / (count - 1)
    slack  = int(stride / 2)          # how far each window may hunt from its slot
    step   = max(1, width_blocks // 8)
    out    = []
    for k in range(count):
        centre = int(k * stride)
        s0 = max(0, centre - slack)
        s1 = min(span, centre + slack)
        best, best_e = s0, -1.0
        for s in range(s0, s1 + 1, step):
            # min(), not sum(): a window is only usable if BOTH tracks have
            # signal in it, and a loud track would otherwise carry a silent one.
            e = min(sum(env_a[s:s + width_blocks]), sum(env_b[s:s + width_blocks]))
            if e > best_e:
                best, best_e = s, e
        if best not in out:
            out.append(best)
    return out


def measure(env_ref, env_mic, starts, width):
    """Per window: coarse scan, then a fine scan around what it found."""
    results = []
    for s in starts:
        a = env_ref[s:s + width]
        b = env_mic[s:s + width]
        ca, cb = decimate(a, DECIM), decimate(b, DECIM)
        coarse, cscores = scan(ca, cb, -COARSE_MS // DECIM, COARSE_MS // DECIM)
        if coarse is None:
            continue
        # Prominence comes from the COARSE scan: it searches +/-1 s, so its lag
        # distribution is a fair sample of "wrong alignment". The fine scan only
        # spans 50 ms, all of it close to right, and has no background to speak of.
        prom = prominence(cscores, coarse)
        centre = coarse * DECIM
        peak, scores = scan(a, b, centre - FINE_MS, centre + FINE_MS)
        if peak is None:
            continue
        results.append((s / ENV_HZ, peak + refine(scores, peak), scores[peak], prom))
    return results


def analyse(path, mic_i=0, ref_i=1, count=5, width=0, use_onset=True):
    """The whole measurement, as data rather than output.

    Returns a dict with ok=True and the offset, or ok=False and a reason. It
    returns failures instead of exiting because normalize.py calls this per take
    and has to be able to carry on without a correction -- only the CLI should
    decide that a bad measurement is fatal.
    """
    raw_mic, raw_ref = decode(path, mic_i), decode(path, ref_i)
    lvl_mic, lvl_ref = rms_dbfs(raw_mic), rms_dbfs(raw_ref)
    out = {"ok": False, "lvl_mic": lvl_mic, "lvl_ref": lvl_ref, "results": []}

    for label, lvl, idx in (("Wave XLR", lvl_mic, mic_i), ("camera mic", lvl_ref, ref_i)):
        if lvl < MIN_RMS_DBFS:
            out["reason"] = ("track %d (%s) is %.1f dBFS RMS, far too quiet to "
                             "measure against" % (idx + 1, label, lvl))
            return out

    env_mic, env_ref = envelope(raw_mic), envelope(raw_ref)
    n = min(len(env_mic), len(env_ref))
    if n / ENV_HZ < MIN_WINDOW_S:
        out["reason"] = "only %.1f s of audio; need %.0f s" % (n / ENV_HZ, MIN_WINDOW_S)
        return out

    count, width = fit_windows(n, count, width)
    starts = pick_windows(env_ref, env_mic, count, width)
    if not starts:
        out["reason"] = "could not place a %.1f s window" % (width / ENV_HZ)
        return out

    # Windows are chosen once, on the raw envelope, so both methods see exactly
    # the same audio -- otherwise a disagreement between them could just be a
    # disagreement about where to look.
    # env_ref first: a positive lag then means the MIC track is the later one.
    out["width"], out["overlap"] = width, width * count > n
    ons_mic, ons_ref = onset(env_mic), onset(env_ref)

    for key, a, b in (("env", env_ref, env_mic), ("onset", ons_ref, ons_mic)):
        res = measure(a, b, starts, width)
        if len(res) < MIN_INLIERS:
            out["reason"] = ("only %d usable window(s); need %d"
                             % (len(res), MIN_INLIERS))
            return out
        got, why = consense(res)
        if got is None:
            out["reason"] = "the %s method: %s" % (key, why)
            return out
        out[key] = got

    out["results"] = out["env"]["results"]
    delta = out["env"]["median"] - out["onset"]["median"]
    out["delta"] = delta

    for key, name in (("env", "envelope"), ("onset", "onset")):
        if out[key]["spread"] > MAX_SPREAD_MS:
            out["reason"] = ("the %s method's windows disagree by %.1f ms, over the "
                             "%.0f ms limit" % (name, out[key]["spread"], MAX_SPREAD_MS))
            return out

    if abs(delta) > MAX_METHOD_DELTA:
        out["reason"] = ("the two methods disagree by %.1f ms (envelope %+.1f, onset "
                         "%+.1f), over the %.0f ms limit"
                         % (abs(delta), out["env"]["median"], out["onset"]["median"],
                            MAX_METHOD_DELTA))
        return out

    out["offset"] = (out["env"]["median"] + out["onset"]["median"]) / 2
    out["spread"] = max(out["env"]["spread"], out["onset"]["spread"])
    out["good"]   = [(t, o) for t, o, _r, _p in out["env"]["results"]]
    out["stable"] = True
    # Both corrections come out of the SAME windows, so they cannot disagree
    # about where in the take they looked. The offset is the median, which lands
    # near the middle of the take; the drift is the slope through them.
    out["drift"], out["drift_why"] = drift_consensus(out["env"]["fit"],
                                                     out["onset"]["fit"])
    out["ok"]     = True
    return out


def acoustic_ms(cam_cm, mic_cm):
    """Milliseconds the camera mic lags the desk mic purely from standing further
    away. Not the rig's fault, so not the rig's to correct."""
    if cam_cm is None or mic_cm is None:
        return 0.0
    return (cam_cm - mic_cm) / 100.0 / SPEED_OF_SOUND * 1000.0


def correction_for(path, mic_i=0, ref_i=1, use_onset=True):
    """Milliseconds to DELAY the mic so it lines up with the picture, or None.

    The single entry point for callers that just want the number.
    """
    r = analyse(path, mic_i, ref_i, use_onset=use_onset)
    if not r["ok"]:
        return None, r["reason"]
    return -(r["offset"] + acoustic_ms(CAM_CM, MIC_CM) + REFERENCE_BIAS_MS), None


def analyse_take(path, mic_i=0, ref_i=1, count=DRIFT_WINDOWS):
    """One pass, reported as both corrections a caller can apply.

        {"ok": True,  "offset_ms": ms to delay the mic,
                      "drift_ppm": ppm or None, "why": why there is no ppm}
        {"ok": False, "why": why nothing could be measured}

    The offset is a constant and the drift is a rate, and they are independent
    faults: two devices can start misaligned, run at different rates, or both.
    Correcting only the constant leaves a take drifting around it, which is what
    a single number cannot express no matter how well it is measured.
    """
    r = analyse(path, mic_i, ref_i, count=count)
    if not r["ok"]:
        return {"ok": False, "why": r["reason"]}
    # drift_ppm is the consensus and is None whenever the two methods cannot
    # agree. drift_seen_ppm is the raw observation: the largest slope either
    # method found that beat its own scatter, regardless of whether they agreed
    # on its size. A caller CORRECTING drift wants the consensus; a caller
    # CHECKING that a correction worked wants the observation, because "the
    # methods disagreed" is not evidence that nothing is there.
    fits = [r[k]["fit"] for k in ("env", "onset") if r[k]["fit"]]
    return {"ok": True,
            "offset_ms": -(r["offset"] + acoustic_ms(CAM_CM, MIC_CM) + REFERENCE_BIAS_MS),
            "drift_ppm": r["drift"],
            "drift_seen_ppm": max((abs(f["ppm"]) for f in fits), default=None),
            "why": r["drift_why"],
            "spread": r["spread"]}


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def fit_line(points):
    """Least-squares line through (second, millisecond) points.

    Returns the slope in ms per second of recording -- the clock difference --
    along with the intercept, the RMS of the residuals about the line, and the
    total rise across the baseline.

    The residual is the part that matters. A slope on its own says nothing: any
    handful of scattered points has one. What separates two clocks running at
    different rates from a noisy measurement is that the clocks put the points
    ON a line, so the rise is large compared to what is left over.
    """
    n = len(points)
    if n < MIN_DRIFT_WINDOWS:
        return None
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    den = sum((p[0] - mx) ** 2 for p in points)
    if not den:
        return None
    m = sum((p[0] - mx) * (p[1] - my) for p in points) / den
    b = my - m * mx
    resid = math.sqrt(sum((p[1] - (m * p[0] + b)) ** 2 for p in points) / n)
    span  = max(p[0] for p in points) - min(p[0] for p in points)
    # How well the slope itself is pinned down, in ppm. Scatter hurts it and a
    # long baseline helps it, which is why this is not the same ranking as
    # "which method looks tidier": a method can be noisy per window and still
    # fix the slope well if its windows reach both ends of the take.
    sigma = (resid / math.sqrt(den)) * 1000.0
    return {"slope": m, "intercept": b, "resid": resid, "n": n, "sigma_ppm": sigma,
            "span": span, "total": m * span, "ppm": m * 1000.0}


def theil_sen(points):
    """Median of the pairwise slopes, as a (slope, intercept) seed line.

    Least squares cannot be the FIRST fit here. The onset method occasionally
    locks onto the wrong syllable and reports a window hundreds of milliseconds
    out; one such point tilts a least-squares line far enough that the rejection
    step then throws away the eight good windows and keeps the bad one. This
    estimator ignores it, because moving the median of the pairwise slopes takes
    a majority of the pairs rather than a single extreme value.

    It is only the seed. Once the outliers are gone, least squares on what is
    left is the better estimator and is what actually gets reported.
    """
    n = len(points)
    if n < MIN_DRIFT_WINDOWS:
        return None
    slopes = [(points[j][1] - points[i][1]) / (points[j][0] - points[i][0])
              for i in range(n) for j in range(i + 1, n)
              if points[j][0] != points[i][0]]
    if not slopes:
        return None
    m = median(slopes)
    return m, median([y - m * t for t, y in points])


def consense(res):
    """Reduce one method's per-window offsets to a consensus, a spread and a slope.

    Outliers are rejected against a FITTED LINE, not against the median, once a
    line is justified. On a take long enough to drift the end windows genuinely
    disagree with the median -- that is what drift IS -- and judging them against
    it rejects both ends of every long take, then reports the survivors as a
    huge disagreement. A ten minute take of this rig drifts ~40 ms end to end,
    which the median test would have thrown out as four bad windows and a broken
    take rather than as two clocks.

    The line is only trusted for that when its rise beats its own scatter.
    Otherwise this falls back to the median, which is what a take with no drift
    needs and what a handful of windows can actually support.
    """
    offs = [(t, o) for t, o, _r, _p in res]

    # Seed with a robust line, drop what it cannot explain, then refit properly.
    # The seed is what keeps one wrong window from deciding which windows are
    # wrong; the refit is what makes the reported residual mean something.
    line, seed = None, theil_sen(offs)
    if seed is not None:
        m, b = seed
        for _ in range(2):
            kept = [p for p in offs if abs(p[1] - (m * p[0] + b)) <= OUTLIER_MS]
            if len(kept) < MIN_DRIFT_WINDOWS:
                line = None
                break
            line = fit_line(kept)
            if line is None:
                break
            m, b = line["slope"], line["intercept"]

    drifting = (line is not None
                and line["span"] >= MIN_DRIFT_S
                and abs(line["total"]) >= DRIFT_SIGMA * line["resid"])

    if drifting:
        predict = lambda t: line["slope"] * t + line["intercept"]
    else:
        rough = median([o for _t, o in offs])
        predict = lambda _t: rough

    inlier = [abs(o - predict(t)) <= OUTLIER_MS for t, o in offs]
    keep   = [(t, o) for (t, o), ok in zip(offs, inlier) if ok]
    if len(keep) < MIN_INLIERS:
        return None, ("only %d of %d windows agree within %.0f ms of each other"
                      % (len(keep), len(offs), OUTLIER_MS))

    fit   = fit_line(keep) if drifting else None
    resid = [o - predict(t) for t, o in keep]
    return {"results": res,
            "median": median([o for _t, o in keep]),
            # With a drift present the windows are SUPPOSED to differ across the
            # take, so what has to be tight is the scatter left after the line is
            # taken out. Without one, this is the plain spread.
            "spread": max(resid) - min(resid),
            "fit": fit,
            "inlier": inlier,
            "dropped": len(offs) - len(keep)}, None


def drift_consensus(env_fit, onset_fit):
    """The ppm both methods agree the mic's clock runs at, or None and why not.

    Positive ppm means the mic track is STRETCHED relative to the reference: the
    same event sits progressively later in it as the take goes on.
    """
    if env_fit is None or onset_fit is None:
        return None, ("no slope stands out from the window-to-window scatter, or "
                      "there are too few windows to fit one through")
    span = min(env_fit["span"], onset_fit["span"])
    if span < MIN_DRIFT_S:
        return None, ("the windows only span %.0f s, and a slope needs %.0f s of "
                      "baseline before it can be told from scatter"
                      % (span, MIN_DRIFT_S))
    # Judged against the two fits' own uncertainties, not against a flat ppm
    # limit. A ragged method with a short baseline can be 20 ppm out and still be
    # consistent with a clean one; two tight fits 20 ppm apart are not. The flat
    # limit stays as a floor, because a difference that small does not matter
    # however confident either fit claims to be.
    diff  = abs(env_fit["ppm"] - onset_fit["ppm"])
    sigma = math.sqrt(env_fit["sigma_ppm"] ** 2 + onset_fit["sigma_ppm"] ** 2)
    if diff > DRIFT_METHOD_PPM and diff > DRIFT_METHOD_SIGMA * sigma:
        return None, ("the two methods disagree about it (%+.0f +/- %.0f vs "
                      "%+.0f +/- %.0f ppm), so at least one is following something "
                      "other than the clocks"
                      % (env_fit["ppm"], env_fit["sigma_ppm"],
                         onset_fit["ppm"], onset_fit["sigma_ppm"]))

    # Inverse-variance weighted, so the better-determined fit carries the answer.
    # A plain average lets the worse method drag the result by half the gap
    # between them, which on this rig is the difference between correcting the
    # drift and overcorrecting it by a third.
    ws = [(f["ppm"], 1.0 / max(f["sigma_ppm"], 1e-6) ** 2) for f in (env_fit, onset_fit)]
    return sum(v * w for v, w in ws) / sum(w for _v, w in ws), None


# ------------------------------------------------------------------ self-test

def self_test(tmp):
    """Build a file whose answer is known, and check we report it.

    The sign of a cross-correlation lag is the easiest thing in this script to
    get backwards, and a backwards answer looks entirely plausible -- it is a
    real number of the right magnitude that makes sync worse. So it gets pinned
    to a file with a delay put in on purpose.
    """
    truth = 40    # ms that track 2 lags track 1 by construction
    path = os.path.join(tmp, "synctest.mov")
    # Pink noise through three tremolos at incommensurate rates: a loudness
    # contour with no short repeat, so the correlation peak is unambiguous.
    src = ("anoisesrc=d=30:c=pink:r=48000:a=0.5,"
           "tremolo=f=3.1:d=0.8,tremolo=f=0.71:d=0.9,tremolo=f=1.37:d=0.7")
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", src,
         "-filter_complex", "[0:a]asplit=2[a][b];[b]adelay=%d|%d[d]" % (truth, truth),
         "-map", "[a]", "-map", "[d]", "-c:a", "pcm_s16le", path])

    env1 = envelope(decode(path, 0))
    env2 = envelope(decode(path, 1))
    count, width = fit_windows(min(len(env1), len(env2)), 3, 0)
    starts = pick_windows(env1, env2, count, width)
    res = measure(env1, env2, starts, width)
    got = median([r[1] for r in res])
    print("self-test: track 2 delayed %+d ms by construction, measured %+.1f ms"
          % (truth, got))
    for t, off, r, pr in res:
        print("   %6.1fs  %+7.1f ms   r=%.3f  %.1f sigma" % (t, off, r, pr))
    if abs(got - truth) > 2:
        die("self-test FAILED: expected %+d ms, got %+.1f ms. The sign convention "
            "or the correlation is wrong; do not trust this tool." % (truth, got))
    print("self-test PASSED (positive = track 2 arrives later than track 1)")


# ppm of stretch built into the drift self-test. Large enough that a 1 ms
# envelope resolves it comfortably over the synthetic take, and far larger than
# the ~60 ppm the real rig shows, so a failure means the machinery is wrong
# rather than that the measurement was marginal.
SELF_TEST_PPM = 300.0
SELF_TEST_DRIFT_S = 150

# How close the measurement has to land to the stretch the file actually has.
# The envelope resolves 1 ms and the fit has a ~130 s baseline, so the noise
# floor here is a couple of ppm; this is loose enough not to be flaky and far
# tighter than any error that would matter.
SELF_TEST_TOL_PPM = 15.0


def self_test_drift(tmp):
    """Build a file whose CLOCKS differ by a known amount, and check we report it.

    A drift has the same hazard as an offset and one more: not only can the sign
    come out backwards, the ppm can come out scaled -- ms per second and ppm are
    a factor of 1000 apart, and either reads as a plausible number. A correction
    built on a wrong ppm bends the take instead of straightening it, so the whole
    path from correlation to ppm gets pinned to a file with a stretch put in on
    purpose.
    """
    path = os.path.join(tmp, "drifttest.mov")
    # The MIC track is the one built to run slow, because that is the direction
    # the convention is stated in: positive ppm means the mic's copy of an event
    # slides later as the take goes on. Reinterpreting its samples at a lower
    # rate spreads them out, which is exactly a slow clock.
    #
    # Note this is the opposite mapping to the offset self-test above, which
    # states its answer as "track 2 relative to track 1". Track 1 is the MIC and
    # track 2 is the REFERENCE, so the two conventions are mirror images and
    # writing this test to the wrong one is the mistake it is here to catch.
    rate = 48000.0 / (1.0 + SELF_TEST_PPM / 1e6)
    src = ("anoisesrc=d=%d:c=pink:r=48000:a=0.5,"
           "tremolo=f=3.1:d=0.8,tremolo=f=0.71:d=0.9,tremolo=f=1.37:d=0.7"
           % SELF_TEST_DRIFT_S)
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", src,
         "-filter_complex",
         "[0:a]asplit=2[a][b];[b]asetrate=%.4f,aresample=48000[slow]" % rate,
         "-map", "[slow]", "-map", "[a]", "-c:a", "pcm_s16le", path])

    # What ffmpeg actually built, not what it was asked for. asetrate takes an
    # integer rate and the resampler approximates the ratio, so the realised
    # stretch is a few ppm off the request -- and asserting against the request
    # would either fail on a true measurement or need a tolerance loose enough to
    # hide a real error. The file's own track lengths are the ground truth.
    info, streams = audio_streams(path)
    truth = (float(streams[0]["duration"]) / float(streams[1]["duration"]) - 1) * 1e6

    r = analyse(path, 0, 1, count=DRIFT_WINDOWS)
    if not r["ok"]:
        die("drift self-test FAILED: the synthetic file did not measure at all -- %s"
            % r["reason"])
    got = r.get("drift")
    print("\ndrift self-test: the mic track runs %+.1f ppm slow by construction "
          "(%.3f s against %.3f s), measured %s"
          % (truth, float(streams[0]["duration"]), float(streams[1]["duration"]),
             "%+.1f ppm" % got if got is not None else "nothing"))
    for key in ("env", "onset"):
        f = r[key]["fit"]
        if f:
            print("   %-5s  %+7.1f ppm   %+6.2f ms over %.0f s, residual %.2f ms rms"
                  % (key, f["ppm"], f["total"], f["span"], f["resid"]))
    if got is None:
        die("drift self-test FAILED: a %+.1f ppm stretch was not detected -- %s"
            % (truth, r.get("drift_why")))
    if abs(got - truth) > SELF_TEST_TOL_PPM:
        die("drift self-test FAILED: the file drifts %+.1f ppm, measured %+.1f ppm. "
            "The sign or the scale of the drift measurement is wrong; a correction "
            "built on it would bend the take rather than straighten it."
            % (truth, got))
    print("drift self-test PASSED (positive ppm = the MIC track's clock runs slow, "
          "so its copy of an event slides later)")


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(
        description="Measure the Wave XLR's offset from the picture using the "
                    "camera's own audio track as a reference.")
    ap.add_argument("file", nargs="?", help="recording (default: newest in ~/Movies)")
    ap.add_argument("--mic-track", type=int, default=1,
                    help="1-based track carrying the Wave XLR (default 1)")
    ap.add_argument("--ref-track", type=int, default=2,
                    help="1-based track carrying the camera mic (default 2)")
    ap.add_argument("-w", "--window", type=float, default=0,
                    help="seconds per measurement window (default: fit to the take, "
                         "up to 10)")
    ap.add_argument("-n", "--windows", type=int, default=DRIFT_WINDOWS,
                    help="number of windows across the take (default %d). The offset"
                         " needs far fewer; a drift slope is only as good as its"
                         " point count." % DRIFT_WINDOWS)
    ap.add_argument("--cam-cm", type=float,
                    help="distance from you to the CAMERA, cm")
    ap.add_argument("--mic-cm", type=float,
                    help="distance from you to the WAVE XLR mic, cm")
    ap.add_argument("--video", action="store_true",
                    help="calibrate against the PICTURE: measure how far the camera's "
                         "audio track lags the video. Record a few claps in frame.")
    ap.add_argument("--self-test", action="store_true",
                    help="verify the sign convention on a synthetic file and exit")
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            die("%s not found. Install it with: brew install ffmpeg" % tool)

    if args.video:
        src = os.path.abspath(os.path.expanduser(args.file)) if args.file else newest_recording()
        if not os.path.isfile(src):
            die("no such file: %s" % src)
        print("source   %s" % src)
        off, why, fps = video_offset(src, args.ref_track - 1)
        if off is None:
            die("could not measure against the picture: %s" % why)
        print("fps      %.3f  (one frame = %.1f ms, the resolution limit here)"
              % (fps, 1000.0 / fps))
        print("")
        print("camera audio lags the picture by %+.1f ms" % off)
        print("")
        if abs(off) < 1000.0 / fps / 2:
            print("That is inside half a frame of zero, so track 2 is a fair stand-in")
            print("for the picture and the audio-only measurement needs no correction.")
            print("Set REFERENCE_BIAS_MS = 0 in sync.py (it already is).")
        else:
            print("Track 2 is NOT aligned with the picture. Every audio-only")
            print("measurement inherits this. Anchor them to it with:")
            print("")
            print("    REFERENCE_BIAS_MS = %d      # in sync.py" % round(off))
        return

    if args.self_test:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self_test(tmp)
            self_test_drift(tmp)
        return

    src = os.path.abspath(os.path.expanduser(args.file)) if args.file else newest_recording()
    if not os.path.isfile(src):
        die("no such file: %s" % src)

    info, streams = audio_streams(src)
    dur = float(info["format"].get("duration", 0))
    print("source   %s" % src)
    print("         %.1f s, %d audio track(s)" % (dur, len(streams)))
    if len(streams) < 2:
        die("this recording has %d audio track(s); the measurement needs two.\n"
            "Track 2 is the camera's own mic -- run ./apply.py --write, then\n"
            "record a fresh take. Older takes cannot be measured after the fact."
            % len(streams))

    mic_i, ref_i = args.mic_track - 1, args.ref_track - 1
    for label, i in (("--mic-track", mic_i), ("--ref-track", ref_i)):
        if not 0 <= i < len(streams):
            die("%s %d is out of range (file has %d tracks)"
                % (label, i + 1, len(streams)))

    print("\ndecoding track %d (Wave XLR) and track %d (camera mic)..."
          % (mic_i + 1, ref_i + 1))
    r = analyse(src, mic_i, ref_i, args.windows, args.window)

    print("levels   track %d %.1f dBFS RMS, track %d %.1f dBFS RMS"
          % (mic_i + 1, r["lvl_mic"], ref_i + 1, r["lvl_ref"]))
    if r.get("width"):
        print("windows  %d x %.1f s%s"
              % (len(r.get("results", [])) or args.windows, r["width"] / ENV_HZ,
                 "  (overlapping - short take, treat the spread as optimistic)"
                 if r["overlap"] else ""))
    if "env" in r and "onset" in r:
        print("\n  window    envelope      onset       r")
        for i, ((t, oe, re_, _p), (_t, oo, _r2, _p2)) in enumerate(
                zip(r["env"]["results"], r["onset"]["results"])):
            mark = lambda k: " " if r[k]["inlier"][i] else "*"
            print("  %6.1fs   %+7.1f ms%s  %+7.1f ms%s  %.3f"
                  % (t, oe, mark("env"), oo, mark("onset"), re_))
        dropped = r["env"]["dropped"] + r["onset"]["dropped"]
        if dropped:
            print("            (* %d outlier%s discarded, more than %.0f ms from the "
                  "median)" % (dropped, "" if dropped == 1 else "s", OUTLIER_MS))
        print("\n  median    %+7.1f ms   %+7.1f ms   (differ by %.1f ms)"
              % (r["env"]["median"], r["onset"]["median"], abs(r["delta"])))
        print("  spread    %7.1f ms   %7.1f ms   (of what was kept)"
              % (r["env"]["spread"], r["onset"]["spread"]))

    if not r["ok"]:
        print("\n%s" % ("-" * 60))
        print("NOT USABLE -- %s." % r["reason"])
        print("")
        print("Usually this is the take, not the rig:")
        print("  * Talk continuously for 60+ seconds. Claps alone leave the")
        print("    envelope flat between them and there is nothing to correlate.")
        print("  * Check both tracks are at a sane level with ./bin/level")
        print("  * Keep the room quiet: a second sound source that only one mic")
        print("    hears well pulls the two envelopes apart.")
        sys.exit(1)

    measured, spread, good, stable = r["offset"], r["spread"], r["good"], True

    cam_cm = args.cam_cm if args.cam_cm is not None else CAM_CM
    mic_cm = args.mic_cm if args.mic_cm is not None else MIC_CM
    acoustic = acoustic_ms(cam_cm, mic_cm)

    electrical = measured + acoustic

    print("\nmeasured   %+.1f ms   (two methods agreeing to %.1f ms across %d "
          "windows)" % (measured, abs(r["delta"]), len(good)))
    if acoustic:
        print("acoustic   %+.1f ms   (camera is %.0f cm further away than the mic)"
              % (acoustic, cam_cm - mic_cm))
        print("path delta %+.1f ms" % electrical)
    else:
        print("           (no --cam-cm/--mic-cm given, so the camera mic's extra")
        print("            distance from you is still counted as rig latency;")
        print("            at 34 cm per ms it is worth supplying)")

    # Reported only when both methods find the same slope and each one's rise
    # beats its own scatter. Otherwise the "drift" is a line through noise, and
    # it reads as a hardware problem rather than as a short or ragged take.
    drift = r.get("drift")
    if drift is not None:
        per_min = drift / 1000.0 * 60.0
        print("\n!! drift %+.2f ms per minute (%+.0f ppm)." % (per_min, drift))
        print("   The two devices run on separate clocks with nothing locking them")
        print("   together, so the offset is not one number: it slides by that much")
        print("   for every minute of the take. Over %.1f minutes it moves %+.0f ms."
              % (dur / 60, drift / 1000.0 * dur))
        print("   A single static offset can only be right in the middle of a take")
        print("   like that. normalize.py corrects the rate as well as the constant,")
        print("   which is why it is the thing to run rather than an editor nudge.")
    else:
        print("\n(no drift correction is called for: %s)" % r.get("drift_why", "not measured"))

    print("\n%s" % ("-" * 60))
    if not stable:
        print("NOT USABLE -- the windows do not agree.")
        print("")
        print("%d window%s spread over %.1f ms, against a %.0f ms limit. The offset"
              % (len(good), "" if len(good) == 1 else "s", spread, MAX_SPREAD_MS))
        print("being corrected is itself only tens of ms, so a disagreement this")
        print("large is not a good measurement with noise on it -- there is no")
        print("measurement here yet. The median (%+.1f ms) is not worth applying."
              % measured)
        print("")
        print("Usually this is the take, not the rig:")
        print("  * Talk continuously for 60+ seconds. Claps alone leave the")
        print("    envelope flat between them and there is nothing to correlate.")
        print("  * Check both tracks are at a sane level with ./bin/level")
        print("  * Keep the room quiet: a second sound source that only one mic")
        print("    hears well pulls the two envelopes apart.")
        sys.exit(1)

    if abs(electrical) < 1:
        print("Already in sync (%+.1f ms). Nothing to correct." % electrical)
        return
    if electrical > 0:
        print("The Wave XLR arrives %.0f ms BEHIND the picture." % electrical)
    else:
        print("The Wave XLR arrives %.0f ms AHEAD of the picture." % -electrical)
    print("")
    print("normalize.py measures this per take by default, so there is usually")
    print("nothing to do -- it will find the same number. To pin it instead:")
    print("")
    print("    SYNC_OFFSET_MS = %d        # in normalize.py" % round(-electrical))
    print("")
    print("Pinning is only right if the offset is stable across takes, and it is")
    print("not: separate captures of this rig have landed 11 ms apart, because the")
    print("USB buffers align differently each time OBS starts the sources.")


if __name__ == "__main__":
    main()
