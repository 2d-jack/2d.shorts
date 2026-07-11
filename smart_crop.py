#!/usr/bin/env python3
"""
smart_crop.py

Converts a horizontal clip to 9:16 vertical. Two reframing modes:

  - "smart_crop" (default): crops in on the subject.
      Primary path: shells out to `verthor` (KazKozDev/auto-vertical-reframe)
      if installed -- scene-aware, YOLOv11 + MediaPipe, smoothed camera path.
          git clone https://github.com/KazKozDev/auto-vertical-reframe.git
          cd auto-vertical-reframe && pip install -e .
      Fallback (no extra install needed): continuous Haar-cascade face
      tracking -- samples face position every 0.5s across the WHOLE clip
      (not just once), interpolates between samples, and smooths with an
      EMA so the crop window actually pans to follow the subject instead of
      sitting at one fixed position for the entire clip.

  - "blur_letterbox": doesn't crop anything out. Keeps the full original
    frame visible, scaled to fit the width, with the empty top/bottom space
    filled by a blurred, zoomed-in copy of the same footage. Good for
    gameplay/screen-recordings or anything where losing part of the frame
    isn't acceptable.

No external API calls either way -- everything here is local.
"""

import argparse
import os
import shutil
import subprocess

import cv2

os.environ.setdefault("GLOG_minloglevel", "2")  # silence mediapipe/absl init noise

try:
    import mediapipe as _mp
    _MEDIAPIPE_AVAILABLE = True
except ImportError:
    _MEDIAPIPE_AVAILABLE = False


class _FaceDetector:
    """Wraps mediapipe's face detector (confirmed far more robust than Haar
    cascade to head angle, lighting, and scale -- a 25-degree head turn that
    Haar cascade missed outright was caught correctly, and it runs roughly
    14x faster) with a graceful fallback to Haar cascade if mediapipe isn't
    installed, or if something goes wrong with it at runtime.

    Runs BOTH of mediapipe's model variants per frame and unions the results:
    model_selection=0 (short-range) scored noticeably higher confidence on
    close-up faces in testing -- the common case for talking-head footage --
    while model_selection=1 (full-range) catches smaller/farther faces that
    0 misses. Using only one or the other left real single-face scenes
    occasionally undetected; using both closes most of that gap.

    On any frame where mediapipe finds nothing, Haar (frontal + profile,
    both mirror directions) gets a second chance -- profiles and tilted
    heads are mediapipe's main misses and Haar recovers a useful share of
    them, at zero extra cost on frames mediapipe already handled.

    Exposes one method, detect(frame) -> [(x, y, w, h), ...] in absolute
    pixel coordinates, so every caller in this file is detector-agnostic."""

    def __init__(self):
        self.backend = "haar"
        self._mp_detectors = []
        self._frontal = None
        self._profile = None
        self._haar_ok = True
        if _MEDIAPIPE_AVAILABLE:
            try:
                self._mp_detectors = [
                    _mp.solutions.face_detection.FaceDetection(
                        model_selection=0, min_detection_confidence=0.4
                    ),
                    _mp.solutions.face_detection.FaceDetection(
                        model_selection=1, min_detection_confidence=0.4
                    ),
                ]
                self.backend = "mediapipe"
            except Exception:
                self._mp_detectors = []

    def _haar_cascades(self):
        if self._frontal is None:
            frontal_xml = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            profile_xml = cv2.data.haarcascades + "haarcascade_profileface.xml"
            self._frontal = cv2.CascadeClassifier(frontal_xml)
            self._profile = cv2.CascadeClassifier(profile_xml)
            # OpenCV 5.x wheels stopped bundling the cascade XMLs, and a
            # CascadeClassifier constructs "successfully" even when its file
            # is missing -- detectMultiScale then aborts the whole clip.
            # Treat unusable cascades as "Haar found no faces" instead.
            if self._frontal.empty() or self._profile.empty():
                self._haar_ok = False
                print("    (Haar cascade data missing from this OpenCV "
                      "install -- skipping Haar face detection)")
        return self._frontal, self._profile

    def _detect_haar(self, frame):
        frontal, profile = self._haar_cascades()
        if not self._haar_ok:
            return []
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = list(frontal.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40)))
        if not faces:
            faces = list(profile.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40)))
        if not faces:
            # The profile cascade only knows left-facing profiles -- run it on
            # the mirrored frame too so a head turned the other way still counts.
            flipped = cv2.flip(gray, 1)
            full_w = gray.shape[1]
            faces = [(full_w - x - fw, y, fw, fh)
                     for x, y, fw, fh in profile.detectMultiScale(
                         flipped, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))]
        return [tuple(map(int, f)) for f in faces]

    def detect(self, frame):
        h, w = frame.shape[:2]
        boxes = []
        if self.backend == "mediapipe":
            try:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                for det in self._mp_detectors:
                    result = det.process(rgb)
                    if result.detections:
                        for d in result.detections:
                            box = d.location_data.relative_bounding_box
                            # mediapipe can report boxes that hang partially
                            # outside the frame (negative origin) -- clamp
                            x = max(0, int(box.xmin * w))
                            y = max(0, int(box.ymin * h))
                            bw = min(int(box.width * w), w - x)
                            bh = min(int(box.height * h), h - y)
                            if bw > 0 and bh > 0:
                                boxes.append((x, y, bw, bh))
            except Exception:
                # Runtime failure -- fall back to Haar for the rest of this
                # run rather than crashing the whole pipeline over it.
                self.backend = "haar"
                boxes = []
        if not boxes:
            # Second chance on frames mediapipe reports empty: profiles,
            # tilted heads, and partly occluded faces are its main misses,
            # and Haar frontal+profile recovers a useful share of them.
            # Frames where mediapipe already found a face pay nothing extra.
            boxes = self._detect_haar(frame)
        return _dedupe_face_boxes(boxes, merge_distance=max(60, w * 0.05))


def has_verthor() -> bool:
    return shutil.which("verthor") is not None


def crop_with_verthor(input_path: str, output_path: str, preset: str = "talking_head"):
    cmd = ["verthor", input_path, output_path, "--preset", preset]
    subprocess.run(cmd, check=True)


def _probe_dimensions(video_path: str):
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0", video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    out = result.stdout.strip()
    parts = out.split(",")
    if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
        raise RuntimeError(
            f"Couldn't read video dimensions from {video_path!r}.\n"
            f"  ffprobe stdout: {result.stdout!r}\n"
            f"  ffprobe stderr: {result.stderr!r}\n"
            f"This almost always means the clip is empty or corrupt -- most often "
            f"because its start/end timestamps fell outside the source video's "
            f"actual duration (e.g. the LLM picked a highlight past the end of "
            f"the video). Check the clip with: ffprobe -v error -show_entries "
            f"format=duration {video_path}"
        )
    w, h = parts
    return int(w), int(h)


def _sample_face_positions(video_path: str, fps: float, total_frames: int, interval_sec: float = 0.25,
                            t_start: float = 0.0, t_end: float = None, detector=None):
    """Samples face x-position every `interval_sec` within [t_start, t_end)
    (defaults to the whole clip), so we have a track over time instead of a
    single snapshot. Returns [(t, center_x_or_None), ...] with absolute
    timestamps.

    When more than one face is detected, sticks with whichever one is
    closest to the previously tracked position rather than picking the
    largest -- picking "largest" independently each sample is what causes
    the crop to flip back and forth between two people (or a face vs. a
    false-positive) whenever their detected sizes happen to swap rank
    between samples."""
    cap = cv2.VideoCapture(video_path)
    if detector is None:
        detector = _FaceDetector()
    duration = (total_frames / fps) if fps else 0.0
    if t_end is None:
        t_end = duration

    # Decode sequentially and detect on every Nth frame instead of seeking
    # to each sample time -- each seek forces a decode from the previous
    # keyframe anyway, so seek-per-sample re-decoded most of the clip many
    # times over. One linear pass is both faster and frame-exact.
    step = max(1, int(round(interval_sec * fps))) if fps else 1
    start_frame = int(t_start * fps)
    end_frame = int(t_end * fps)
    if start_frame > 0:
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    samples = []
    last_known = None
    fidx = start_frame
    while fidx < end_frame:
        ok, frame = cap.read()
        if not ok:
            break
        if (fidx - start_frame) % step == 0:
            t = fidx / fps
            faces = detector.detect(frame)
            if faces:
                if last_known is None:
                    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
                else:
                    x, y, w, h = min(faces, key=lambda f: abs((f[0] + f[2] / 2) - last_known))
                center = x + w / 2
                last_known = center
                samples.append((t, center))
            else:
                samples.append((t, None))
        fidx += 1
    cap.release()
    return samples


def _median_filter_samples(samples, window=3):
    """Replaces each detected x with the median of itself and its nearest
    detected neighbors, rejecting single-sample detection noise (a head turn
    or lighting blip giving one bad box) before it ever reaches the
    smoother. None entries (no detection at that sample) pass through
    unchanged and aren't counted as neighbors."""
    xs = [x for _, x in samples]
    half = window // 2
    filtered = []
    for i, (t, x) in enumerate(samples):
        if x is None:
            filtered.append((t, None))
            continue
        neighborhood = [xs[j] for j in range(max(0, i - half), min(len(xs), i + half + 1))
                         if xs[j] is not None]
        neighborhood.sort()
        filtered.append((t, neighborhood[len(neighborhood) // 2]))
    return filtered


def _make_interpolator(samples):
    """Builds a function t -> x that linearly interpolates between the known
    (non-None) sample points. Returns None if no face was ever detected."""
    known = [(t, x) for t, x in samples if x is not None]
    if not known:
        return None

    def interp(t):
        if t <= known[0][0]:
            return known[0][1]
        if t >= known[-1][0]:
            return known[-1][1]
        for (t0, x0), (t1, x1) in zip(known, known[1:]):
            if t0 <= t <= t1:
                if t1 == t0:
                    return x0
                frac = (t - t0) / (t1 - t0)
                return x0 + frac * (x1 - x0)
        return known[-1][1]

    return interp


def _open_ffmpeg_sink(output_path: str, frame_w: int, frame_h: int, fps: float,
                       audio_source: str = None, vf: str = None):
    """Opens an ffmpeg process that consumes raw BGR frames on stdin and
    encodes them with libx264 in a single pass, muxing audio from
    `audio_source` in the same command. Replaces the old
    cv2.VideoWriter('mp4v') -> temp file -> second ffmpeg re-encode flow,
    which cost a full extra encode AND a visible generation-loss quality
    hit from the lossy mp4v intermediate."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{frame_w}x{frame_h}", "-r", f"{fps:.6f}",
        "-i", "pipe:0",
    ]
    if audio_source:
        cmd += ["-i", audio_source, "-map", "0:v:0", "-map", "1:a:0?",
                "-c:a", "aac", "-shortest"]
    if vf:
        cmd += ["-vf", vf]
    cmd += [
        "-c:v", "libx264", "-crf", "19", "-pix_fmt", "yuv420p",
        "-loglevel", "error",
        output_path,
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


def _close_ffmpeg_sink(proc):
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg encoder exited with an error while writing frames")


def crop_fallback(input_path: str, output_path: str, smoothing_alpha: float = 0.25,
                   canvas_w: int = 1080, canvas_h: int = 1920):
    """9:16 crop that actually tracks the subject across the clip: samples
    face position every 0.5s, interpolates between samples, and applies an
    exponential moving average so the crop pans smoothly instead of jumping
    or sitting frozen on one early position. Final output is scaled to
    canvas_w x canvas_h so it can sit next to blur_letterbox segments in a
    concatenated hybrid clip without a resolution mismatch."""
    src_w, src_h = _probe_dimensions(input_path)
    target_w = int(src_h * 9 / 16)

    if target_w > src_w:
        # Source is already narrower than 9:16 -- pad instead of crop.
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-vf", (f"scale={src_w}:-2,pad={src_h * 9 // 16}:{src_h}:(ow-iw)/2:0:black,"
                    f"scale={canvas_w}:{canvas_h}"),
            "-c:v", "libx264", "-c:a", "copy", "-loglevel", "error",
            output_path,
        ]
        subprocess.run(cmd, check=True)
        return

    cap = cv2.VideoCapture(input_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()

    detector = _FaceDetector()
    samples = _sample_face_positions(input_path, fps, total_frames, detector=detector)
    samples = _median_filter_samples(samples)
    interp = _make_interpolator(samples)

    cap = cv2.VideoCapture(input_path)
    sink = _open_ffmpeg_sink(output_path, target_w, src_h, fps,
                              audio_source=input_path, vf=f"scale={canvas_w}:{canvas_h}")

    ema_x = None
    frame_idx = 0
    DEAD_ZONE_PX = 10  # ignore movement smaller than this -- treats minor
                       # detection jitter as noise instead of real movement
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        t = frame_idx / fps
        raw_x = interp(t) if interp else src_w / 2
        if ema_x is None:
            ema_x = raw_x
        elif abs(raw_x - ema_x) > DEAD_ZONE_PX:
            ema_x = smoothing_alpha * raw_x + (1 - smoothing_alpha) * ema_x

        crop_x = int(ema_x - target_w / 2)
        crop_x = max(0, min(crop_x, src_w - target_w))
        sink.stdin.write(frame[:, crop_x:crop_x + target_w].tobytes())
        frame_idx += 1
    cap.release()
    _close_ffmpeg_sink(sink)


def blur_letterbox(input_path: str, output_path: str, target_w: int = 1080,
                    target_h: int = 1920, blur_sigma: int = 20):
    """No cropping at all: scales the full frame to fit the width and fills
    the empty top/bottom with a blurred, zoomed-in copy of the same footage."""
    filter_complex = (
        f"[0:v]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
        f"crop={target_w}:{target_h},gblur=sigma={blur_sigma}[bg];"
        f"[0:v]scale={target_w}:-2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[outv]"
    )
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "0:a:0?",
        "-c:v", "libx264", "-c:a", "aac",
        "-loglevel", "error",
        output_path,
    ]
    subprocess.run(cmd, check=True)


def detect_scenes(video_path: str, diff_threshold: float = 22.0, min_scene_sec: float = 0.6):
    """Lightweight hard-cut detector: downsamples each frame, takes the mean
    absolute pixel difference from the previous frame, and calls it a cut
    when that jumps above `diff_threshold`. No extra dependency -- if
    PySceneDetect is installed it's more accurate (proper HSV-based content
    detection), so this prefers that when available.

    Returns a list of (start_sec, end_sec) covering the whole video."""
    try:
        from scenedetect import open_video, SceneManager
        from scenedetect.detectors import ContentDetector

        video = open_video(video_path)
        sm = SceneManager()
        sm.add_detector(ContentDetector())
        sm.detect_scenes(video)
        scene_list = sm.get_scene_list()
        if scene_list:
            return [(s.seconds, e.seconds) for s, e in scene_list]
    except ImportError:
        pass  # fall through to the homegrown detector below

    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    prev_gray = None
    cut_times = [0.0]
    frame_idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        small = cv2.resize(frame, (160, 90))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        if prev_gray is not None:
            score = float(cv2.absdiff(gray, prev_gray).mean())
            if score > diff_threshold:
                cut_times.append(frame_idx / fps)
        prev_gray = gray
        frame_idx += 1
    cap.release()

    duration = (total_frames / fps) if fps else cut_times[-1]
    cut_times.append(duration)

    scenes = []
    for i in range(len(cut_times) - 1):
        s, e = cut_times[i], cut_times[i + 1]
        if e - s < min_scene_sec and scenes:
            scenes[-1] = (scenes[-1][0], e)  # merge tiny slivers into the previous scene
        elif e > s:
            scenes.append((s, e))
    return scenes or [(0.0, duration)]


def _dedupe_face_boxes(faces, merge_distance=60):
    """Merges near-duplicate detections of the same face. Haar cascade
    occasionally reports two overlapping boxes for one real face at
    slightly different scales -- left uncorrected, that single face gets
    miscounted as two people."""
    boxes = list(faces)
    if len(boxes) <= 1:
        return boxes
    centers = [(x + w / 2, y + h / 2) for x, y, w, h in boxes]
    used = [False] * len(boxes)
    merged = []
    for i in range(len(boxes)):
        if used[i]:
            continue
        group = [boxes[i]]
        used[i] = True
        for j in range(i + 1, len(boxes)):
            if used[j]:
                continue
            dx, dy = centers[i][0] - centers[j][0], centers[i][1] - centers[j][1]
            if (dx * dx + dy * dy) ** 0.5 < merge_distance:
                group.append(boxes[j])
                used[j] = True
        merged.append(max(group, key=lambda b: b[2] * b[3]))
    return merged


def _is_genuinely_multi_person(faces, size_ratio_threshold=0.4):
    """A second, much-smaller detection alongside a real face is usually
    noise -- a gesturing hand near the face, a skin-toned object, or two
    ensembled models reporting slightly different box sizes for the SAME
    face -- rather than an actual second person. Real distinct people tend
    to have comparably-sized faces (similar distance from camera); this
    only counts as multi-person if at least two detections are within
    `size_ratio_threshold` of each other in area."""
    if len(faces) <= 1:
        return False
    areas = sorted((w * h for x, y, w, h in faces), reverse=True)
    return areas[1] >= areas[0] * size_ratio_threshold


def scene_has_reliable_face(video_path: str, scene_start: float, scene_end: float,
                             fps: float, detector, samples: int = 18,
                             min_detection_ratio: float = 0.15,
                             max_faces_for_single_subject: int = 1) -> bool:
    """Samples frames within [scene_start, scene_end] and returns True only
    if there's a SINGLE reliable subject worth cropping into.

    Two ways this returns False:
    - No face detected reliably -- B-roll/gameplay/wide-shot with nobody in it.
    - Multiple people detected at once -- a crowd, a stadium, a group shot.
      Cropping into "whichever face happened to be tracked" there looks
      arbitrary, so this sends those to the full-frame blurred-letterbox
      treatment instead.

    Single-frame face detection is noisy in both directions -- it can miss a
    real face (one bad sample shouldn't write off an otherwise-clear single
    subject) or double-report one face as two boxes (which would wrongly
    read as "multiple people"). This dedupes near-duplicate boxes within each
    frame, then decides "single vs multi-person" by majority vote across all
    samples where a face was found at all, rather than a single average that
    one noisy frame can skew either direction."""
    cap = cv2.VideoCapture(video_path)
    duration = max(scene_end - scene_start, 0.01)
    hits, total, multi_face_hits = 0, 0, 0
    for i in range(samples):
        t = scene_start + duration * (i + 0.5) / samples
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(t * fps))
        ok, frame = cap.read()
        if not ok:
            continue
        faces = detector.detect(frame)
        faces = _dedupe_face_boxes(faces)
        total += 1
        if len(faces) > 0:
            hits += 1
            if len(faces) > max_faces_for_single_subject and _is_genuinely_multi_person(faces):
                multi_face_hits += 1
    cap.release()

    if total == 0 or (hits / total) < min_detection_ratio:
        return False
    # majority vote across samples where a face was found at all -- robust
    # against one noisy frame swinging an average either direction
    return (multi_face_hits / hits) <= 0.5


def _blur_letterbox_frame(frame, canvas_w: int, canvas_h: int, blur_ksize: int = 61):
    """Pure-OpenCV equivalent of blur_letterbox(), for use inside a per-frame
    loop: blurred cover-scaled background + sharp fit-scaled foreground,
    composited onto one canvas_w x canvas_h frame."""
    src_h, src_w = frame.shape[:2]

    scale = max(canvas_w / src_w, canvas_h / src_h)
    bg = cv2.resize(frame, (int(src_w * scale) + 2, int(src_h * scale) + 2))
    bh, bw = bg.shape[:2]
    x0 = max(0, (bw - canvas_w) // 2)
    y0 = max(0, (bh - canvas_h) // 2)
    bg = bg[y0:y0 + canvas_h, x0:x0 + canvas_w]
    bg = cv2.GaussianBlur(bg, (blur_ksize, blur_ksize), 0)

    fg_w = canvas_w
    fg_h = max(1, int(round(src_h * (canvas_w / src_w))))
    fg = cv2.resize(frame, (fg_w, fg_h))

    out = bg
    if fg_h <= canvas_h:
        y_off = (canvas_h - fg_h) // 2
        out[y_off:y_off + fg_h, 0:fg_w] = fg
    else:
        crop_top = (fg_h - canvas_h) // 2
        out = fg[crop_top:crop_top + canvas_h, 0:fg_w]
    return out


def _build_frame_plan(input_path: str, fps: float, total_frames: int, merged_spans,
                       target_w: int, src_w: int, detector, smoothing_alpha: float = 0.25):
    """For every frame index, decides ('crop', x) or ('blur', None). Smoothing
    resets at each span boundary on purpose -- a hard cut should never blend
    crop position with an unrelated scene's position."""
    plan = [("blur", None)] * total_frames

    for s, e, has_face in merged_spans:
        start_f = max(0, int(s * fps))
        end_f = min(total_frames, int(e * fps))
        if end_f <= start_f:
            continue

        if not has_face or target_w > src_w:
            for fidx in range(start_f, end_f):
                plan[fidx] = ("blur", None)
            continue

        samples = _sample_face_positions(input_path, fps, total_frames,
                                          interval_sec=0.25, t_start=s, t_end=e, detector=detector)
        samples = _median_filter_samples(samples)
        interp = _make_interpolator(samples)
        ema_x = None
        DEAD_ZONE_PX = 10  # ignore movement smaller than this -- treats minor
                           # detection jitter as noise instead of real movement
        for fidx in range(start_f, end_f):
            t = fidx / fps
            raw_x = interp(t) if interp else src_w / 2
            if ema_x is None:
                ema_x = raw_x
            elif abs(raw_x - ema_x) > DEAD_ZONE_PX:
                ema_x = smoothing_alpha * raw_x + (1 - smoothing_alpha) * ema_x
            crop_x = int(ema_x - target_w / 2)
            crop_x = max(0, min(crop_x, src_w - target_w))
            plan[fidx] = ("crop", crop_x)

    return plan


def reframe_hybrid(input_path: str, output_path: str, canvas_w: int = 1080, canvas_h: int = 1920):
    """Splits the clip into scenes, decides crop-vs-blur independently for
    each scene, then renders the WHOLE clip in a single read/write pass using
    a precomputed per-frame plan -- no per-scene ffmpeg extraction, no concat
    step, so there's no segment boundary for video/audio duration to drift at.
    (An earlier version extracted+re-encoded each scene separately and
    concatenated them; on footage with many short/flashy scenes -- gameplay,
    B-roll-heavy cuts -- that multiplied subprocess overhead and let small
    per-segment timing mismatches compound into visibly broken playback.)"""
    src_w, src_h = _probe_dimensions(input_path)
    target_w = int(src_h * 9 / 16)

    cap = cv2.VideoCapture(input_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()

    detector = _FaceDetector()
    scenes = detect_scenes(input_path)

    decisions = [(s, e, scene_has_reliable_face(input_path, s, e, fps, detector)) for s, e in scenes]
    # Merge adjacent BLUR scenes only. Face scenes must stay split at every
    # cut: merging two consecutive face scenes into one span made the crop
    # smoothing pan across the hard cut between them, so for a moment after
    # each cut the camera was still travelling from the previous shot's
    # position and the new subject sat half out of frame.
    merged = []
    for s, e, hf in decisions:
        if merged and not hf and merged[-1][2] == hf:
            merged[-1] = (merged[-1][0], e, hf)
        else:
            merged.append((s, e, hf))

    plan = _build_frame_plan(input_path, fps, total_frames, merged, target_w, src_w, detector)

    cap = cv2.VideoCapture(input_path)
    # Audio muxed straight from the ORIGINAL, untouched track in the same
    # single-pass encode -- no segment-boundary trims anywhere, so nothing
    # to drift.
    sink = _open_ffmpeg_sink(output_path, canvas_w, canvas_h, fps, audio_source=input_path)

    fidx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if fidx < len(plan):
            action, val = plan[fidx]
        elif plan:
            # The container's frame-count metadata undercounted; keep the
            # last planned treatment instead of flashing to blur at the end.
            action, val = plan[-1]
        else:
            action, val = "blur", None
        if action == "crop":
            out_frame = cv2.resize(frame[:, val:val + target_w], (canvas_w, canvas_h))
        else:
            out_frame = _blur_letterbox_frame(frame, canvas_w, canvas_h)
        sink.stdin.write(out_frame.tobytes())
        fidx += 1
    cap.release()
    _close_ffmpeg_sink(sink)

    return len(merged)


def reframe(input_path: str, output_path: str, preset: str = "talking_head", mode: str = "hybrid"):
    """High-level entry point used by make_shorts.py.
    mode="hybrid" (default): splits the clip into scenes and picks crop-vs-blur
        per scene -- fixes both the frozen-crop-after-cut problem and the
        B-roll/gameplay-getting-cropped problem.
    mode="smart_crop": one crop treatment for the whole clip (verthor if
        installed, else tracked face-crop) -- use this if you know the clip
        is one continuous talking-head shot with no cutaways.
    mode="blur_letterbox": no cropping anywhere, blurred-fill letterbox for
        the entire clip.
    """
    if mode == "hybrid":
        n_spans = reframe_hybrid(input_path, output_path)
        return f"hybrid ({n_spans} span{'s' if n_spans != 1 else ''})"
    if mode == "blur_letterbox":
        blur_letterbox(input_path, output_path)
        return "blur-letterbox"
    if has_verthor():
        crop_with_verthor(input_path, output_path, preset)
        return "verthor"
    crop_fallback(input_path, output_path)
    return "fallback-face-crop-tracked"


def main():
    parser = argparse.ArgumentParser(description="Reframe a horizontal clip to 9:16 vertical.")
    parser.add_argument("input_path")
    parser.add_argument("output_path")
    parser.add_argument("--preset", default="talking_head")
    parser.add_argument("--mode", choices=["hybrid", "smart_crop", "blur_letterbox"], default="hybrid")
    args = parser.parse_args()

    method = reframe(args.input_path, args.output_path, args.preset, args.mode)
    print(f"Reframed using: {method} -> {args.output_path}")


if __name__ == "__main__":
    main()
