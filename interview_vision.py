#!/usr/bin/env python3
"""
interview_vision.py — Video-based interview analysis using OpenCV, MediaPipe, DeepFace.

Analyzes an interview video and returns per-person emotion timelines,
body language scores, eye contact scores, speaking time ratios, and observations.
"""

import os
import math
import tempfile
import traceback
from typing import Callable, Optional

# ── Optional heavy imports (fail gracefully) ──────────────────────────────────
try:
    import cv2
    _CV2_OK = True
except ImportError:
    _CV2_OK = False

try:
    import mediapipe as mp
    _MP_OK = True
except ImportError:
    _MP_OK = False

_DF_OK = False
_DeepFace = None

def _get_deepface():
    """Lazy import DeepFace to avoid import-time crashes."""
    global _DF_OK, _DeepFace
    if _DeepFace is not None:
        return _DeepFace
    try:
        from deepface import DeepFace as _df
        _DeepFace = _df
        _DF_OK = True
        return _DeepFace
    except Exception:
        _DF_OK = False
        return None

# ── Constants ─────────────────────────────────────────────────────────────────
SAMPLE_INTERVAL_SEC = 2          # sample 1 frame every N seconds
MOUTH_OPEN_THRESHOLD = 0.04      # lip aspect ratio to consider "talking"
EYE_GAZE_THRESHOLD = 0.35        # normalised iris deviation to consider "looking away"
PERSON_COLORS = [
    (0, 200, 80),    # green  — person 0
    (30, 120, 255),  # blue   — person 1
    (220, 80, 220),  # purple — person 2
    (255, 160, 0),   # orange — person 3
]
EMOTION_COLORS_BGR = {
    "happy":     (60, 200, 80),
    "neutral":   (160, 160, 160),
    "surprised": (0, 200, 230),
    "fear":      (0, 80, 220),
    "sad":       (200, 100, 60),
    "angry":     (30, 30, 220),
    "disgusted": (100, 30, 180),
}

# MediaPipe landmark indices for mouth/eyes
# (all indices from the 468-point face mesh)
_UPPER_LIP = 13
_LOWER_LIP = 14
_LEFT_EYE_INNER  = 133
_LEFT_EYE_OUTER  = 33
_LEFT_IRIS_CTR   = 468
_RIGHT_EYE_INNER = 362
_RIGHT_EYE_OUTER = 263
_RIGHT_IRIS_CTR  = 473
_NOSE_TIP        = 1
_CHIN            = 152
_LEFT_SHOULDER   = 11
_RIGHT_SHOULDER  = 12


# ── Core analysis function ────────────────────────────────────────────────────

def analyze_interview_video(
    video_path: str,
    roles: dict,                    # {0: "Candidate", 1: "Interviewer 1", …}
    on_progress: Optional[Callable] = None,
) -> dict:
    """
    Analyze an interview video and return a structured result dict.

    Parameters
    ----------
    video_path : str
        Path to the input video file.
    roles : dict
        Mapping from person index (int) to role label (str).
        e.g. {0: "Candidate", 1: "Interviewer 1"}
    on_progress : callable, optional
        Called with (pct: float, msg: str) as analysis progresses.

    Returns
    -------
    dict
        See module docstring for full schema.
    """
    if not _CV2_OK:
        raise RuntimeError("opencv-python is not installed. Run: pip install opencv-contrib-python-headless")
    if not _MP_OK:
        raise RuntimeError("mediapipe is not installed. Run: pip install mediapipe")

    def _prog(pct: float, msg: str):
        if on_progress:
            try:
                on_progress(pct, msg)
            except Exception:
                pass

    _prog(0.0, "Opening video…")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"Cannot open video: {video_path}")

    fps        = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration_secs = total_frames / fps
    sample_step   = max(1, int(fps * SAMPLE_INTERVAL_SEC))

    _prog(2.0, f"Video: {duration_secs:.0f}s @ {fps:.1f}fps — sampling every {SAMPLE_INTERVAL_SEC}s")

    # ── MediaPipe setup ───────────────────────────────────────────────────────
    mp_face_mesh  = mp.solutions.face_mesh
    mp_face_det   = mp.solutions.face_detection
    mp_pose       = mp.solutions.pose

    face_mesh_sol  = mp_face_mesh.FaceMesh(
        static_image_mode=True,
        max_num_faces=5,
        refine_landmarks=True,       # enables iris landmarks 468-477
        min_detection_confidence=0.4,
    )
    face_det_sol   = mp_face_det.FaceDetection(
        model_selection=1,
        min_detection_confidence=0.4,
    )
    pose_sol       = mp_pose.Pose(
        static_image_mode=True,
        min_detection_confidence=0.4,
    )

    # ── Per-person accumulators ───────────────────────────────────────────────
    # Will be keyed by person_id (assigned by x-position of face bbox)
    person_data: dict[int, dict] = {}

    def _ensure_person(pid: int):
        if pid not in person_data:
            person_data[pid] = {
                "first_seen":       None,
                "frames_seen":      0,
                "emotions":         [],          # list of str per sampled frame
                "emotions_timeline": [],         # [{"t": float, "emotion": str, "score": float}]
                "eye_contact_frames": 0,
                "talking_frames":   0,
                "upright_frames":   0,
                "total_frames":     0,
                # half-split for confidence trend
                "first_half_confidence_frames": 0,
                "second_half_confidence_frames": 0,
                "first_half_total":  0,
                "second_half_total": 0,
            }

    # ── Main frame loop ───────────────────────────────────────────────────────
    frame_idx    = 0
    sampled      = 0
    total_samples = max(1, int(total_frames / sample_step))

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % sample_step == 0:
            t_sec = frame_idx / fps
            pct   = 5.0 + 85.0 * (frame_idx / max(total_frames, 1))
            _prog(pct, f"Analysing frame at {t_sec:.1f}s…")

            _process_frame(
                frame, t_sec, duration_secs,
                face_det_sol, face_mesh_sol, pose_sol,
                person_data,
            )
            sampled += 1

        frame_idx += 1

    cap.release()
    face_mesh_sol.close()
    face_det_sol.close()
    pose_sol.close()

    _prog(90.0, "Computing scores…")

    # ── Build result ──────────────────────────────────────────────────────────
    persons_result: dict[int, dict] = {}
    total_talk_frames = sum(
        d["talking_frames"] for d in person_data.values()
    ) or 1

    for pid in sorted(person_data.keys()):
        d    = person_data[pid]
        tot  = max(d["total_frames"], 1)
        role = roles.get(pid, f"Person {pid}")

        emotions_list = d["emotions"]
        if emotions_list:
            from collections import Counter
            dominant_emotion = Counter(emotions_list).most_common(1)[0][0]
        else:
            dominant_emotion = "neutral"

        eye_pct     = 100.0 * d["eye_contact_frames"] / tot
        upright_pct = 100.0 * d["upright_frames"]      / tot
        talk_pct    = 100.0 * d["talking_frames"]       / total_talk_frames

        # Confidence-positive emotions
        conf_pos    = sum(1 for e in emotions_list if e in ("happy", "neutral", "surprised"))
        conf_neg    = sum(1 for e in emotions_list if e in ("fear", "sad"))
        composure_pos = sum(1 for e in emotions_list if e in ("neutral", "happy"))
        angry_frames  = sum(1 for e in emotions_list if e in ("angry", "disgusted", "fear"))

        conf_emo_pct = 100.0 * conf_pos / max(len(emotions_list), 1)
        composure    = 100.0 * composure_pos / max(len(emotions_list), 1)

        # Energy = variance proxy: % of non-neutral frames
        energy_frames = sum(1 for e in emotions_list if e != "neutral")
        energy        = min(100, int(100.0 * energy_frames / max(len(emotions_list), 1) * 2.5))

        confidence    = int(0.4 * conf_emo_pct + 0.35 * eye_pct + 0.25 * upright_pct)
        engagement    = int(0.5 * upright_pct + 0.5 * eye_pct)

        # Candidate vs interviewer have different score sets
        if role.lower().startswith("candidate"):
            scores = {
                "confidence": int(confidence),
                "composure":  int(composure),
                "eye_contact": int(eye_pct),
                "engagement":  int(engagement),
                "energy":      int(energy),
            }
        else:
            scores = {
                "receptiveness": int(composure),
                "engagement":    int(engagement),
            }

        # Confidence trend (first half vs second half)
        fh_tot  = max(d["first_half_total"],  1)
        sh_tot  = max(d["second_half_total"], 1)
        fh_conf = 100.0 * d["first_half_confidence_frames"]  / fh_tot
        sh_conf = 100.0 * d["second_half_confidence_frames"] / sh_tot

        persons_result[pid] = {
            "role":              role,
            "emotions_timeline": d["emotions_timeline"],
            "scores":            scores,
            "dominant_emotion":  dominant_emotion,
            "talk_pct":          int(talk_pct),
            "_fh_conf":          fh_conf,
            "_sh_conf":          sh_conf,
            "_eye_pct":          eye_pct,
            "_confidence":       confidence,
        }

    # ── Interaction metrics ───────────────────────────────────────────────────
    candidate_talk = 0
    for pid, d in person_data.items():
        role = roles.get(pid, "")
        if role.lower().startswith("candidate"):
            candidate_talk = d["talking_frames"]

    talk_balance = int(100.0 * candidate_talk / total_talk_frames)
    # Rapport: average of all engagement scores
    all_eng = [v["scores"].get("engagement", v["scores"].get("receptiveness", 70))
               for v in persons_result.values()]
    rapport  = int(sum(all_eng) / max(len(all_eng), 1))
    overall  = int(0.5 * rapport + 0.3 * talk_balance + 0.2 * 70)  # base 70

    interaction = {
        "rapport":      min(100, rapport),
        "talk_balance": min(100, talk_balance),
        "overall":      min(100, overall),
    }

    # ── Observations ─────────────────────────────────────────────────────────
    observations = _build_observations(persons_result, interaction, roles)

    _prog(95.0, "Analysis complete.")

    return {
        "persons":       persons_result,
        "interaction":   interaction,
        "observations":  observations,
        "duration_secs": duration_secs,
    }


# ── Frame processor ───────────────────────────────────────────────────────────

def _process_frame(
    frame, t_sec: float, duration_secs: float,
    face_det_sol, face_mesh_sol, pose_sol,
    person_data: dict,
):
    """Process a single sampled frame, updating person_data in place."""
    h, w = frame.shape[:2]
    rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    is_first_half = (t_sec < duration_secs / 2)

    # ── Detect faces ─────────────────────────────────────────────────────────
    try:
        det_result = face_det_sol.process(rgb)
    except Exception:
        return

    if not det_result.detections:
        return

    # Sort faces left-to-right to assign consistent person IDs
    faces = []
    for det in det_result.detections:
        bb = det.location_data.relative_bounding_box
        cx = bb.xmin + bb.width / 2
        faces.append({
            "cx": cx,
            "x":  int(bb.xmin * w),
            "y":  int(bb.ymin * h),
            "fw": int(bb.width  * w),
            "fh": int(bb.height * h),
        })
    faces.sort(key=lambda f: f["cx"])

    # ── Pose: shoulder alignment ──────────────────────────────────────────────
    upright_by_x: dict = {}
    try:
        pose_result = pose_sol.process(rgb)
        if pose_result.pose_landmarks:
            lm = pose_result.pose_landmarks.landmark
            mp_pose = mp.solutions.pose
            ls = lm[mp_pose.PoseLandmark.LEFT_SHOULDER]
            rs = lm[mp_pose.PoseLandmark.RIGHT_SHOULDER]
            # shoulder tilt < 15 degrees = upright
            tilt_deg = abs(math.degrees(math.atan2(abs(ls.y - rs.y), abs(ls.x - rs.x))))
            is_upright = tilt_deg < 15.0
            # Assign to the face nearest horizontally to mid-shoulder x
            mid_x = (ls.x + rs.x) / 2
            for i, f in enumerate(faces):
                upright_by_x[i] = is_upright
    except Exception:
        pass

    # ── Face mesh: eyes & mouth ───────────────────────────────────────────────
    try:
        mesh_result = face_mesh_sol.process(rgb)
        mesh_faces  = mesh_result.multi_face_landmarks or []
    except Exception:
        mesh_faces = []

    # Sort mesh results by face centre x (to match detection order)
    def _mesh_cx(flms):
        xs = [lm.x for lm in flms.landmark]
        return sum(xs) / len(xs)
    mesh_faces_sorted = sorted(mesh_faces, key=_mesh_cx)

    # ── DeepFace emotion per face ─────────────────────────────────────────────
    face_emotions: list[tuple[str, float]] = []
    for f in faces:
        emotion, score = "neutral", 0.5
        try:
            x1 = max(0, f["x"])
            y1 = max(0, f["y"])
            x2 = min(w, x1 + f["fw"])
            y2 = min(h, y1 + f["fh"])
            DeepFace = _get_deepface()
            if x2 > x1 and y2 > y1 and DeepFace is not None:
                crop  = frame[y1:y2, x1:x2]
                result = DeepFace.analyze(
                    crop,
                    actions=["emotion"],
                    enforce_detection=False,
                    silent=True,
                )
                if isinstance(result, list):
                    result = result[0]
                emotion = result.get("dominant_emotion", "neutral").lower()
                emo_scores = result.get("emotion", {})
                score   = emo_scores.get(emotion, 50) / 100.0
        except Exception:
            pass
        face_emotions.append((emotion, score))

    # ── Update person records ─────────────────────────────────────────────────
    for i, f in enumerate(faces):
        pid = i
        _ensure_person(pid, person_data)
        d   = person_data[pid]

        if d["first_seen"] is None:
            d["first_seen"] = t_sec

        d["total_frames"] += 1
        d["frames_seen"]  += 1

        # Emotion
        emotion, score = face_emotions[i] if i < len(face_emotions) else ("neutral", 0.5)
        d["emotions"].append(emotion)
        d["emotions_timeline"].append({"t": t_sec, "emotion": emotion, "score": score})

        # Upright posture
        if upright_by_x.get(i, True):
            d["upright_frames"] += 1

        # Confidence trend
        if is_first_half:
            d["first_half_total"] += 1
            if emotion in ("happy", "neutral", "surprised"):
                d["first_half_confidence_frames"] += 1
        else:
            d["second_half_total"] += 1
            if emotion in ("happy", "neutral", "surprised"):
                d["second_half_confidence_frames"] += 1

        # Eye contact & talking via face mesh
        if i < len(mesh_faces_sorted):
            flm = mesh_faces_sorted[i].landmark
            eye_contact = _calc_eye_contact(flm)
            talking     = _calc_talking(flm)
        else:
            eye_contact = False
            talking     = False

        if eye_contact:
            d["eye_contact_frames"] += 1
        if talking:
            d["talking_frames"] += 1


def _ensure_person(pid: int, person_data: dict):
    if pid not in person_data:
        person_data[pid] = {
            "first_seen":       None,
            "frames_seen":      0,
            "emotions":         [],
            "emotions_timeline": [],
            "eye_contact_frames": 0,
            "talking_frames":   0,
            "upright_frames":   0,
            "total_frames":     0,
            "first_half_confidence_frames": 0,
            "second_half_confidence_frames": 0,
            "first_half_total":  0,
            "second_half_total": 0,
        }


def _calc_eye_contact(landmarks) -> bool:
    """Return True if gaze appears centred (eye contact with camera)."""
    try:
        # For left eye: iris centre vs eye corners
        l_inner  = landmarks[_LEFT_EYE_INNER]
        l_outer  = landmarks[_LEFT_EYE_OUTER]
        r_inner  = landmarks[_RIGHT_EYE_INNER]
        r_outer  = landmarks[_RIGHT_EYE_OUTER]

        # Iris landmarks only present when refine_landmarks=True
        if len(landmarks) > 468:
            l_iris   = landmarks[_LEFT_IRIS_CTR]
            r_iris   = landmarks[_RIGHT_IRIS_CTR]

            l_width  = abs(l_outer.x - l_inner.x) or 0.001
            r_width  = abs(r_outer.x - r_inner.x) or 0.001

            l_dev    = abs(l_iris.x - (l_inner.x + l_outer.x) / 2) / l_width
            r_dev    = abs(r_iris.x - (r_inner.x + r_outer.x) / 2) / r_width

            return l_dev < EYE_GAZE_THRESHOLD and r_dev < EYE_GAZE_THRESHOLD
    except Exception:
        pass
    return True   # default: assume eye contact if we can't compute


def _calc_talking(landmarks) -> bool:
    """Return True if mouth aspect ratio indicates the person is speaking."""
    try:
        upper = landmarks[_UPPER_LIP]
        lower = landmarks[_LOWER_LIP]
        mouth_open = abs(lower.y - upper.y)
        return mouth_open > MOUTH_OPEN_THRESHOLD
    except Exception:
        return False


# ── Observations ──────────────────────────────────────────────────────────────

def _build_observations(persons_result: dict, interaction: dict, roles: dict) -> list[str]:
    obs = []

    for pid, p in persons_result.items():
        role = p["role"]
        fh   = p.get("_fh_conf", 50)
        sh   = p.get("_sh_conf", 50)
        eye  = p.get("_eye_pct", 100)
        conf = p.get("_confidence", 70)
        talk = p.get("talk_pct", 50)
        dom  = p.get("dominant_emotion", "neutral")

        if sh - fh >= 10:
            obs.append(f"{role} showed increased confidence in the second half of the interview.")
        if eye < 50:
            obs.append(f"{role}: limited eye contact detected throughout the session.")
        if role.lower().startswith("candidate") and talk < 35:
            obs.append("Candidate spoke less than expected — consider prompting more elaborated answers.")
        if dom == "fear":
            obs.append(f"Signs of nervousness detected in {role} throughout the session.")
        if dom == "happy":
            obs.append(f"{role} appeared consistently positive and enthusiastic.")
        if dom == "angry" or dom == "disgusted":
            obs.append(f"Tension or discomfort detected in {role}'s expressions.")

    rapport = interaction.get("rapport", 70)
    if rapport > 75:
        obs.append("Strong rapport between participants was evident throughout.")
    elif rapport < 45:
        obs.append("Low interaction rapport — participants may have been disengaged.")

    if not obs:
        obs.append("Analysis complete. No significant behavioural patterns detected.")

    return obs


# ── Annotated video writer ─────────────────────────────────────────────────────

def write_annotated_video(
    video_path: str,
    result: dict,
    output_path: Optional[str] = None,
    on_progress: Optional[Callable] = None,
) -> str:
    """
    Write a copy of the video with overlaid bounding boxes, emotion labels,
    role labels, and confidence score bars.

    Returns the path to the output file.
    """
    if not _CV2_OK:
        raise RuntimeError("opencv-python is not installed.")

    if output_path is None:
        tmp  = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        output_path = tmp.name
        tmp.close()

    def _prog(pct, msg):
        if on_progress:
            try:
                on_progress(pct, msg)
            except Exception:
                pass

    cap  = cv2.VideoCapture(video_path)
    fps  = cap.get(cv2.CAP_PROP_FPS) or 25.0
    w    = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h    = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out    = cv2.VideoWriter(output_path, fourcc, fps, (w, h))

    # Build a frame-indexed lookup from the emotions_timeline
    persons  = result.get("persons", {})
    # Build per-second lookup {pid: {t_floor: (emotion, score)}}
    timeline_lookup: dict[int, dict[int, tuple]] = {}
    for pid, p in persons.items():
        timeline_lookup[pid] = {}
        for entry in p.get("emotions_timeline", []):
            t_key = int(entry["t"])
            timeline_lookup[pid][t_key] = (entry["emotion"], entry["score"])

    mp_face_det   = mp.solutions.face_detection
    face_det_sol  = mp_face_det.FaceDetection(model_selection=1, min_detection_confidence=0.4)

    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        t_sec = frame_idx / fps
        t_key = int(t_sec)
        pct   = 10.0 + 85.0 * frame_idx / max(total_frames, 1)
        if frame_idx % 30 == 0:
            _prog(pct, f"Writing annotated frame {frame_idx}/{total_frames}…")

        rgb  = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        try:
            det_result = face_det_sol.process(rgb)
        except Exception:
            det_result = None

        if det_result and det_result.detections:
            faces = []
            for det in det_result.detections:
                bb = det.location_data.relative_bounding_box
                cx = bb.xmin + bb.width / 2
                faces.append({
                    "cx": cx,
                    "x":  int(bb.xmin * w),
                    "y":  int(bb.ymin * h),
                    "fw": int(bb.width  * w),
                    "fh": int(bb.height * h),
                })
            faces.sort(key=lambda f: f["cx"])

            for i, f in enumerate(faces):
                pid   = i
                color = PERSON_COLORS[pid % len(PERSON_COLORS)]
                x1, y1 = max(0, f["x"]), max(0, f["y"])
                x2, y2 = min(w, x1 + f["fw"]), min(h, y1 + f["fh"])

                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)

                # Emotion & role labels
                emotion, score = timeline_lookup.get(pid, {}).get(t_key, ("neutral", 0.5))
                role    = persons.get(pid, {}).get("role", f"Person {pid}")

                label_y = max(y1 - 10, 12)
                cv2.putText(frame, emotion.capitalize(), (x1, label_y),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA)
                cv2.putText(frame, role, (x1, y2 + 18),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)

                # Mini confidence bar inside box
                bar_w  = max(0, x2 - x1 - 4)
                bar_h  = 5
                bar_x1 = x1 + 2
                bar_y1 = y2 - 8
                fill_w = int(bar_w * score)
                cv2.rectangle(frame, (bar_x1, bar_y1), (bar_x1 + bar_w, bar_y1 + bar_h),
                              (60, 60, 60), -1)
                if fill_w > 0:
                    cv2.rectangle(frame, (bar_x1, bar_y1), (bar_x1 + fill_w, bar_y1 + bar_h),
                                  color, -1)

        out.write(frame)
        frame_idx += 1

    cap.release()
    out.release()
    face_det_sol.close()
    _prog(100.0, "Annotated video written.")
    return output_path
