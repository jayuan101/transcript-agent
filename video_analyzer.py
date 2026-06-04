"""
Video Interview Analyzer  — v2.0
Uploaded video + live webcam analysis.

Per-person detection: emotion, eye contact, head pose, posture, body language,
speaking time. Cultural scoring: American Interview Standard vs Indian-to-American
adaptation. Score cards, emotion timeline, body-language badges, annotated video.

Uses MediaPipe Tasks API (mediapipe ≥ 0.10).
Models auto-download once to .mediapipe_models/ in project root.
"""

from __future__ import annotations

import cv2
import math
import os
import tempfile
import time
import urllib.request
import numpy as np
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import mediapipe as mp
    from mediapipe.tasks import python as _mp_tasks
    from mediapipe.tasks.python import vision as _mp_vision
    _HAS_MP = True
except ImportError:
    _HAS_MP = False

try:
    from deepface import DeepFace as _DeepFace
    _HAS_DEEPFACE = True
except Exception:
    _HAS_DEEPFACE = False

try:
    from fer import FER as _FERLib
    _fer_detector = _FERLib(mtcnn=False)
    _HAS_FER = True
except Exception:
    _HAS_FER = False

# ── Model URLs & cache ────────────────────────────────────────────────────────

_MODEL_DIR = Path(__file__).parent / ".mediapipe_models"
_FACE_LANDMARKER_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_landmarker"
    "/face_landmarker/float16/1/face_landmarker.task"
)
_POSE_LANDMARKER_URL = (
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker"
    "/pose_landmarker_lite/float16/1/pose_landmarker_lite.task"
)


def _ensure_model(url: str, fname: str) -> str:
    _MODEL_DIR.mkdir(parents=True, exist_ok=True)
    p = _MODEL_DIR / fname
    if not p.exists():
        print(f"[VideoAnalyzer] Downloading {fname}…")
        urllib.request.urlretrieve(url, str(p))
        print(f"[VideoAnalyzer] {fname} ready ({p.stat().st_size // 1024} KB)")
    return str(p)


# ── Colour tables ─────────────────────────────────────────────────────────────

EMO_HTML = {
    "happy": "#22c55e", "confident": "#3b82f6", "neutral": "#94a3b8",
    "nervous": "#f59e0b", "surprised": "#a855f7",
    "angry": "#ef4444",  "disgusted": "#6366f1",
}
EMO_BGR = {
    "happy": (50, 205, 50), "confident": (255, 144, 30), "neutral": (180, 180, 180),
    "nervous": (0, 165, 255), "surprised": (200, 0, 200),
    "angry": (60, 20, 220),  "disgusted": (130, 0, 130),
}
ROLE_BGR = {
    "Candidate": (255, 120, 0), "Interviewer 1": (0, 180, 0),
    "Interviewer 2": (220, 0, 220), "Interviewer 3": (200, 200, 0),
    "Interviewer 4": (0, 200, 200), "Unknown": (128, 128, 128),
}
BL_BGR = {
    "OPEN": (50, 205, 50), "ENGAGED": (0, 165, 255),
    "TENSE": (0, 140, 255), "CLOSED": (60, 20, 220), "NEUTRAL": (128, 128, 128),
}
BL_HTML = {
    "OPEN": "#22c55e", "ENGAGED": "#3b82f6",
    "TENSE": "#f59e0b", "CLOSED": "#ef4444", "NEUTRAL": "#94a3b8",
}


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class BodyLanguageSummary:
    arms_crossed:        bool  = False
    forward_lean:        bool  = False
    leaning_back:        bool  = False
    shoulders_raised:    bool  = False
    head_nod:            bool  = False
    head_tilt_deg:       float = 0.0
    open_score:          float = 50.0   # 0-100
    body_language_label: str   = "NEUTRAL"


@dataclass
class FaceFrame:
    person_id:      int
    timestamp:      float
    bbox:           Tuple[int, int, int, int]
    emotion:        str                = "neutral"
    emotion_probs:  Dict[str, float]   = field(default_factory=dict)
    eye_contact:    bool               = False
    yaw:            float              = 0.0
    pitch:          float              = 0.0
    roll:           float              = 0.0
    posture:        str                = "unknown"
    is_speaking:    bool               = False
    body_language:  Optional[BodyLanguageSummary] = None


@dataclass
class CulturalStyleScore:
    american_score:      float = 0.0
    adaptation_score:    float = 0.0
    american_tips:       List[str] = field(default_factory=list)
    adaptation_tips:     List[str] = field(default_factory=list)
    ec_pct:              float = 0.0
    head_wobble_count:   int   = 0
    open_body_pct:       float = 0.0
    forward_lean_pct:    float = 0.0


@dataclass
class PersonScore:
    person_id:            int
    role:                 str
    confidence:           float = 0.0
    composure:            float = 0.0
    eye_contact:          float = 0.0
    engagement:           float = 0.0
    energy:               float = 0.0
    receptiveness:        float = 0.0
    overall:              float = 0.0
    talk_time_pct:        float = 0.0
    open_body_pct:        float = 0.0
    arm_crossed_pct:      float = 0.0
    forward_lean_pct:     float = 0.0
    emotion_distribution: Dict[str, float] = field(default_factory=dict)
    dominant_emotion:     str   = "neutral"
    appeared_at_second:   float = 0.0
    cultural:             Optional[CulturalStyleScore] = None


@dataclass
class LiveScoreSnapshot:
    confidence:      float = 0.0
    eye_contact:     float = 0.0
    engagement:      float = 0.0
    composure:       float = 0.0
    open_body_pct:   float = 0.0
    dominant_emotion: str  = "neutral"
    body_label:      str   = "NEUTRAL"
    cultural:        Optional[CulturalStyleScore] = None
    frame_count:     int   = 0
    elapsed_sec:     float = 0.0


@dataclass
class VideoAnalysisResult:
    persons:              Dict[int, PersonScore]   = field(default_factory=dict)
    rapport_score:        float = 0.0
    talk_balance_score:   float = 0.0
    overall_score:        float = 0.0
    candidate_talk_pct:   float = 0.0
    observations:         List[str]  = field(default_factory=list)
    timeline_data:        List[dict] = field(default_factory=list)
    annotated_video_path: Optional[str] = None
    face_thumbnails:      Dict[int, str] = field(default_factory=dict)
    duration_seconds:     float = 0.0
    person_count:         int   = 0
    error:                str   = ""


# ── Face centroid tracker ─────────────────────────────────────────────────────

class _CentroidTracker:
    def __init__(self, max_gone: int = 45, max_dist: int = 160):
        self.next_id  = 0
        self._c: Dict[int, tuple]  = {}  # id → centroid
        self._b: Dict[int, tuple]  = {}  # id → bbox
        self._g: Dict[int, int]    = {}  # id → frames_gone
        self.max_gone = max_gone
        self.max_dist = max_dist

    def update(self, bboxes: List[Tuple]) -> Dict[int, Tuple]:
        if not bboxes:
            for oid in list(self._g):
                self._g[oid] += 1
                if self._g[oid] > self.max_gone:
                    self._c.pop(oid, None); self._b.pop(oid, None); del self._g[oid]
            return {}

        nc = [(x + w // 2, y + h // 2) for x, y, w, h in bboxes]
        if not self._c:
            r = {}
            for c, b in zip(nc, bboxes):
                self._c[self.next_id] = c; self._b[self.next_id] = b
                self._g[self.next_id] = 0; r[self.next_id] = b; self.next_id += 1
            return r

        oids = list(self._c.keys())
        oc   = [self._c[o] for o in oids]
        D    = np.array([[((a[0]-b[0])**2+(a[1]-b[1])**2)**.5 for b in nc] for a in oc])
        rows = D.min(axis=1).argsort(); cols = D.argmin(axis=1)[rows]
        ur, uc, r = set(), set(), {}
        for rr, cc in zip(rows, cols):
            if rr in ur or cc in uc or D[rr, cc] > self.max_dist: continue
            oid = oids[rr]
            self._c[oid] = nc[cc]; self._b[oid] = bboxes[cc]; self._g[oid] = 0
            r[oid] = bboxes[cc]; ur.add(rr); uc.add(cc)
        for rr in set(range(len(oids))) - ur:
            oid = oids[rr]; self._g[oid] += 1
            if self._g[oid] > self.max_gone:
                self._c.pop(oid, None); self._b.pop(oid, None); del self._g[oid]
        for cc in set(range(len(bboxes))) - uc:
            self._c[self.next_id] = nc[cc]; self._b[self.next_id] = bboxes[cc]
            self._g[self.next_id] = 0; r[self.next_id] = bboxes[cc]; self.next_id += 1
        return r


# ── Main VideoAnalyzer ────────────────────────────────────────────────────────

class VideoAnalyzer:

    # ── Public API: uploaded-video pipeline ───────────────────────────────────

    def scan_faces(self, video_path: str, progress_cb=None) -> Tuple[Dict[int, str], float]:
        if not _HAS_MP: return {}, 0.0
        try: fl = _ensure_model(_FACE_LANDMARKER_URL, "face_landmarker.task")
        except Exception as e: print(f"[VA] model error: {e}"); return {}, 0.0

        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        tot = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        dur = tot / fps

        opts = _mp_vision.FaceLandmarkerOptions(
            base_options=_mp_tasks.BaseOptions(model_asset_path=fl),
            num_faces=5, output_face_blendshapes=False,
            output_facial_transformation_matrixes=False,
            min_face_detection_confidence=0.4, min_face_presence_confidence=0.4,
            min_tracking_confidence=0.4,
        )
        lm  = _mp_vision.FaceLandmarker.create_from_options(opts)
        trk = _CentroidTracker(max_gone=int(fps * 3), max_dist=180)
        thumbs: Dict[int, str] = {}
        ivl = max(1, int(fps * 2))
        idx = 0
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret: break
            if idx % ivl == 0:
                h, w = frame.shape[:2]
                rgb  = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mpi  = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                res  = lm.detect(mpi)
                bbs  = self._lm_to_bboxes(res, h, w)
                for pid, (x, y, fw, fh) in trk.update(bbs).items():
                    if pid not in thumbs:
                        crop = frame[y:y+fh, x:x+fw]
                        if crop.size > 0:
                            p = tempfile.mktemp(suffix=f"_t{pid}.jpg")
                            cv2.imwrite(p, cv2.resize(crop, (120, 160)))
                            thumbs[pid] = p
                if progress_cb: progress_cb(min(0.3, idx / tot * 0.35))
            idx += 1
        cap.release(); lm.close()
        return thumbs, dur

    def analyze_video(
        self,
        video_path: str,
        role_map: Dict[int, str],
        sample_fps: float = 1.0,
        progress_cb=None,
        cultural_mode: str = "both",
    ) -> VideoAnalysisResult:
        if not _HAS_MP:
            r = VideoAnalysisResult()
            r.error = "mediapipe not installed. Run: pip install mediapipe opencv-python"
            return r
        try:
            return self._run_upload(video_path, role_map, sample_fps, progress_cb, cultural_mode)
        except Exception as exc:
            import traceback
            r = VideoAnalysisResult()
            r.error = f"Analysis failed: {exc}\n{traceback.format_exc()}"
            return r

    def _run_upload(self, video_path, role_map, sample_fps, progress_cb, cultural_mode):
        fl   = _ensure_model(_FACE_LANDMARKER_URL, "face_landmarker.task")
        try:   pl = _ensure_model(_POSE_LANDMARKER_URL, "pose_landmarker_lite.task")
        except: pl = None

        cap  = cv2.VideoCapture(video_path)
        fps  = cap.get(cv2.CAP_PROP_FPS) or 30
        tot  = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        dur  = tot / fps
        ivl  = max(1, int(fps / sample_fps))

        fl_opts = _mp_vision.FaceLandmarkerOptions(
            base_options=_mp_tasks.BaseOptions(model_asset_path=fl), num_faces=5,
            output_face_blendshapes=True, output_facial_transformation_matrixes=True,
            min_face_detection_confidence=0.4, min_face_presence_confidence=0.4,
            min_tracking_confidence=0.4,
        )
        face_lm = _mp_vision.FaceLandmarker.create_from_options(fl_opts)
        pose_lm = None
        if pl:
            pose_lm = _mp_vision.PoseLandmarker.create_from_options(
                _mp_vision.PoseLandmarkerOptions(
                    base_options=_mp_tasks.BaseOptions(model_asset_path=pl), num_poses=4,
                    min_pose_detection_confidence=0.4, min_pose_presence_confidence=0.4,
                    min_tracking_confidence=0.4,
                )
            )

        trk        = _CentroidTracker(max_gone=int(fps * 3), max_dist=200)
        pitch_hist = deque(maxlen=8)
        all_frames: List[List[FaceFrame]] = []
        first_seen: Dict[int, float] = {}
        idx = 0
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret: break
            if idx % ivl == 0:
                ffs = self._proc(frame, idx / fps, trk, face_lm, pose_lm, first_seen, pitch_hist)
                all_frames.append(ffs)
                if progress_cb: progress_cb(min(0.72, 0.05 + idx / tot * 0.67))
            idx += 1
        cap.release(); face_lm.close()
        if pose_lm: pose_lm.close()
        if progress_cb: progress_cb(0.78)

        result = self._aggregate(all_frames, role_map, dur, first_seen, cultural_mode)
        if progress_cb: progress_cb(0.85)
        result.annotated_video_path = self._make_annotated_video(video_path, all_frames, role_map, fps)
        if progress_cb: progress_cb(1.0)
        return result

    # ── Live session factory ──────────────────────────────────────────────────

    def create_live_session(self) -> "LiveAnalysisSession":
        return LiveAnalysisSession(self)

    # ── Frame processing helpers ──────────────────────────────────────────────

    def _lm_to_bboxes(self, res, h, w) -> List[Tuple]:
        bbs = []
        if not (res and res.face_landmarks): return bbs
        for face in res.face_landmarks:
            xs = [lm.x * w for lm in face]; ys = [lm.y * h for lm in face]
            x1, y1 = max(0, int(min(xs)-10)), max(0, int(min(ys)-20))
            x2, y2 = min(w, int(max(xs)+10)), min(h, int(max(ys)+10))
            if x2-x1 > 20 and y2-y1 > 20: bbs.append((x1, y1, x2-x1, y2-y1))
        return bbs

    def _proc(self, frame, ts, trk, face_lm, pose_lm, first_seen, pitch_hist) -> List[FaceFrame]:
        h, w  = frame.shape[:2]
        rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mpi   = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        fl_r  = face_lm.detect(mpi)
        bbs   = self._lm_to_bboxes(fl_r, h, w)
        trkd  = trk.update(bbs)
        for pid in trkd:
            if pid not in first_seen: first_seen[pid] = ts

        pose_res = pose_lm.detect(mpi) if pose_lm else None

        out = []
        for fi, (pid, bbox) in enumerate(trkd.items()):
            x, y, fw, fh = bbox
            bs  = fl_r.face_blendshapes[fi] if (fl_r and fl_r.face_blendshapes and fi < len(fl_r.face_blendshapes)) else None
            tm  = fl_r.facial_transformation_matrixes[fi] if (fl_r and fl_r.facial_transformation_matrixes and fi < len(fl_r.facial_transformation_matrixes)) else None

            yaw, pitch, roll = self._head_pose_angles(tm) if tm else (0.0, 0.0, 0.0)
            pitch_hist.append(pitch)
            ec   = abs(yaw) < 20 and abs(pitch) < 20
            jaw  = self._bs_val(bs, "jawOpen") if bs else 0.0
            crop = frame[y:y+fh, x:x+fw]
            emo, eprobs = self._emotion(crop, bs)

            bl = self._body_language(pose_res, pitch_hist, roll) if pose_res else BodyLanguageSummary()
            posture = bl.body_language_label.lower() if bl.body_language_label != "NEUTRAL" else "upright"

            out.append(FaceFrame(
                person_id=pid, timestamp=ts, bbox=bbox,
                emotion=emo, emotion_probs=eprobs,
                eye_contact=ec, yaw=yaw, pitch=pitch, roll=roll,
                posture=posture, is_speaking=(jaw > 0.18),
                body_language=bl,
            ))
        return out

    def _head_pose_angles(self, tm) -> Tuple[float, float, float]:
        try:
            data = list(tm.data)
            if len(data) < 16: return 0.0, 0.0, 0.0
            R  = np.array(data, dtype=float).reshape(4, 4)[:3, :3]
            sy = math.sqrt(R[0,0]**2 + R[1,0]**2)
            if sy > 1e-6:
                pitch = math.degrees(math.atan2(R[2,1], R[2,2]))
                yaw   = math.degrees(math.atan2(-R[2,0], sy))
                roll  = math.degrees(math.atan2(R[1,0], R[0,0]))
            else:
                pitch = math.degrees(math.atan2(-R[1,2], R[1,1]))
                yaw   = math.degrees(math.atan2(-R[2,0], sy))
                roll  = 0.0
            return float(yaw), float(pitch), float(roll)
        except Exception:
            return 0.0, 0.0, 0.0

    def _bs_val(self, bs, name: str) -> float:
        if not bs: return 0.0
        for b in bs:
            if b.category_name == name: return float(b.score)
        return 0.0

    def _emotion(self, crop, bs) -> Tuple[str, Dict[str, float]]:
        if crop is not None and crop.size > 0 and crop.shape[0] >= 20:
            if _HAS_DEEPFACE:
                try:
                    res = _DeepFace.analyze(crop, actions=["emotion"], enforce_detection=False, silent=True)
                    raw = (res[0] if isinstance(res, list) else res)["emotion"]
                    dom = max(raw, key=raw.get)
                    return self._map_emo(dom), {self._map_emo(k): v for k, v in raw.items()}
                except Exception: pass
            if _HAS_FER:
                try:
                    dets = _fer_detector.detect_emotions(crop)
                    if dets:
                        raw = dets[0]["emotions"]; dom = max(raw, key=raw.get)
                        return self._map_emo(dom), {self._map_emo(k): v for k, v in raw.items()}
                except Exception: pass
        if bs: return self._emo_from_bs(bs)
        return "neutral", {"neutral": 1.0}

    def _map_emo(self, raw: str) -> str:
        return {"angry":"angry","disgust":"disgusted","fear":"nervous",
                "happy":"happy","sad":"nervous","surprise":"surprised",
                "neutral":"neutral"}.get(raw.lower(), "neutral")

    def _emo_from_bs(self, bs) -> Tuple[str, Dict[str, float]]:
        g  = self._bs_val
        smile = (g(bs,"mouthSmileLeft") + g(bs,"mouthSmileRight")) / 2
        frown = (g(bs,"mouthFrownLeft") + g(bs,"mouthFrownRight")) / 2
        bd    = (g(bs,"browDownLeft")   + g(bs,"browDownRight"))   / 2
        bu    = (g(bs,"browOuterUpLeft")+ g(bs,"browOuterUpRight")) / 2
        ew    = (g(bs,"eyeWideLeft")    + g(bs,"eyeWideRight"))    / 2
        sn    = (g(bs,"noseSneerLeft")  + g(bs,"noseSneerRight"))  / 2
        sc = {
            "happy":     smile * 0.7 + (1-bd)*0.3,
            "surprised": bu   * 0.6 + ew * 0.4,
            "angry":     bd   * 0.6 + frown * 0.4,
            "disgusted": sn   * 0.7 + bd  * 0.3,
            "nervous":   frown* 0.5 + ew  * 0.5,
            "neutral":   max(0.0, 1 - smile - frown - bd - bu - sn),
        }
        dom = max(sc, key=sc.get)
        return dom, sc

    # ── Body language ─────────────────────────────────────────────────────────

    def _body_language(
        self, pose_res, pitch_hist: deque, face_roll_deg: float = 0.0
    ) -> BodyLanguageSummary:
        if not (pose_res and pose_res.pose_landmarks):
            return BodyLanguageSummary()
        try:
            lm = pose_res.pose_landmarks[0]
            # Visibility gate: nose(0), l-shoulder(11), r-shoulder(12), l-wrist(15), r-wrist(16)
            if any(lm[i].visibility < 0.4 for i in [0, 11, 12]):
                return BodyLanguageSummary()

            nose = lm[0]; ls = lm[11]; rs = lm[12]
            sh_cx = (ls.x + rs.x) / 2

            # Arm crossing — only if wrists visible
            arms_crossed = False
            if lm[15].visibility > 0.3 and lm[16].visibility > 0.3:
                lw, rw = lm[15], lm[16]
                arms_crossed = (lw.x > sh_cx + 0.05) and (rw.x < sh_cx - 0.05)

            # Lean
            forward_lean = (nose.x - sh_cx) > 0.06
            leaning_back = (sh_cx - nose.x) > 0.06

            # Shoulder tension
            shoulders_raised = (ls.y < 0.30 and rs.y < 0.30)

            # Head nod from pitch history
            head_nod = False
            if len(pitch_hist) >= 6:
                recent = list(pitch_hist)[-6:]
                head_nod = any(p > 10 for p in recent) and any(p < -10 for p in recent)

            # Head tilt: use face roll if available, else shoulder line proxy
            if abs(face_roll_deg) > 0.1:
                head_tilt = face_roll_deg
            else:
                head_tilt = math.degrees(math.atan2(ls.y - rs.y, rs.x - ls.x))

            # Open-score composite
            open_score = (
                25 * (not arms_crossed) +
                25 * forward_lean +
                20 * (not shoulders_raised) +
                15 * (not head_nod) +
                15 * (abs(head_tilt) < 10)
            )

            if   open_score >= 75: label = "OPEN"
            elif open_score >= 55: label = "ENGAGED"
            elif open_score >= 35: label = "TENSE"
            else:                  label = "CLOSED"

            return BodyLanguageSummary(
                arms_crossed=arms_crossed, forward_lean=forward_lean,
                leaning_back=leaning_back, shoulders_raised=shoulders_raised,
                head_nod=head_nod, head_tilt_deg=float(head_tilt),
                open_score=float(open_score), body_language_label=label,
            )
        except Exception:
            return BodyLanguageSummary()

    # ── Posture string from pose (simple version for backward compat) ─────────

    def _posture(self, pose_lm, mpi) -> str:
        try:
            res = pose_lm.detect(mpi)
            if not (res and res.pose_landmarks): return "unknown"
            lm = res.pose_landmarks[0]
            ls, rs, nose = lm[11], lm[12], lm[0]
            if ls.visibility < 0.4 or rs.visibility < 0.4: return "unknown"
            sh_x = (ls.x + rs.x) / 2
            if nose.x - sh_x > 0.06:   return "leaning_forward"
            if sh_x - nose.x > 0.06:   return "slouched"
            if (ls.y + rs.y) / 2 < 0.30: return "tense"
            return "upright"
        except Exception:
            return "unknown"

    # ── Cultural scoring ──────────────────────────────────────────────────────

    def score_cultural(
        self,
        face_frames: List[FaceFrame],
        bl_list: List[BodyLanguageSummary],
    ) -> CulturalStyleScore:
        n   = len(face_frames) or 1
        nb  = len(bl_list)    or 1

        ec_pct   = sum(1 for f in face_frames if f.eye_contact) / n * 100
        fwd_pct  = sum(1 for b in bl_list if b.forward_lean) / nb * 100
        open_pct = sum(1 for b in bl_list if b.open_score >= 55) / nb * 100
        cross_pct= sum(1 for b in bl_list if b.arms_crossed)     / nb * 100
        nod_cnt  = sum(1 for b in bl_list if b.head_nod)
        pos_pct  = sum(1 for f in face_frames if f.emotion in ("happy","neutral","confident")) / n * 100
        yaws     = [f.yaw for f in face_frames]
        yaw_stab = max(0.0, 100 - float(np.std(yaws) if yaws else 0) * 2.5)

        # ── American score ────────────────────────────────────────────
        am = (ec_pct*0.30 + fwd_pct*0.20 + open_pct*0.20 + pos_pct*0.20 + yaw_stab*0.10)
        american_score = min(100.0, am)

        # ── Indian → American adaptation score ───────────────────────
        ec_adapt   = min(100.0, max(0.0, (ec_pct - 30) / 40 * 100))
        wobble_r   = nod_cnt / n * 100
        wobble_a   = max(0.0, 100 - wobble_r * 3)
        posture_a  = open_pct * 0.5 + fwd_pct * 0.5
        conf_a     = pos_pct
        adap = ec_adapt*0.35 + wobble_a*0.25 + posture_a*0.25 + conf_a*0.15
        adaptation_score = min(100.0, adap)

        # ── American tips (conditional) ───────────────────────────────
        am_tips = []
        if ec_pct < 70:
            am_tips.append(
                f"Increase eye contact (currently {ec_pct:.0f}%, target 70%+) — "
                "direct gaze signals confidence to American interviewers."
            )
        if fwd_pct < 40:
            am_tips.append(
                "Lean slightly forward — it projects engagement and enthusiasm "
                "in Western interview culture."
            )
        if open_pct < 60:
            am_tips.append(
                "Open your body posture — uncrossed arms and an upright stance "
                "convey confidence."
            )
        if pos_pct < 65:
            am_tips.append(
                "Show more positive facial expression — a calm, confident look "
                "improves how interviewers perceive you."
            )
        if yaw_stab < 60:
            am_tips.append(
                "Keep your gaze steadier — frequent eye drift can appear evasive "
                "to American interviewers."
            )

        # ── Indian → American adaptation tips (conditional) ──────────
        ia_tips = []
        if nod_cnt > 3:
            ia_tips.append(
                "Reduce the Indian head-wobble/nod: in American culture it can "
                "read as uncertainty rather than agreement. Use a deliberate single "
                "downward nod or a verbal 'Yes, exactly' instead."
            )
        if ec_pct < 55:
            ia_tips.append(
                f"Eye contact ({ec_pct:.0f}%) is below the American professional "
                "norm. In many Indian contexts a lowered gaze shows respect — but "
                "here it may read as a lack of confidence. Gradually hold eye contact "
                "for 3–5 seconds at a time."
            )
        if cross_pct > 20:
            ia_tips.append(
                f"Arms were crossed {cross_pct:.0f}% of the time. American interviewers "
                "read this as defensive or closed-off — rest your hands in your lap "
                "or on the desk instead."
            )
        if pos_pct < 60:
            ia_tips.append(
                "Express enthusiasm more visibly — Indian cultural norms toward "
                "humility can suppress positive expression that American interviewers "
                "actively look for."
            )
        if fwd_pct < 30:
            ia_tips.append(
                "Add a mild forward lean — formal upright posture is respected in "
                "Indian settings but can read as disengaged to American interviewers "
                "who expect visible enthusiasm."
            )

        return CulturalStyleScore(
            american_score=round(american_score, 1),
            adaptation_score=round(adaptation_score, 1),
            american_tips=am_tips[:5],
            adaptation_tips=ia_tips[:5],
            ec_pct=round(ec_pct, 1),
            head_wobble_count=nod_cnt,
            open_body_pct=round(open_pct, 1),
            forward_lean_pct=round(fwd_pct, 1),
        )

    # ── Aggregation ───────────────────────────────────────────────────────────

    def _aggregate(self, all_frames, role_map, duration, first_seen, cultural_mode="both") -> VideoAnalysisResult:
        pf: Dict[int, List[FaceFrame]] = defaultdict(list)
        for ffs in all_frames:
            for ff in ffs: pf[ff.person_id].append(ff)

        tot_sp = sum(sum(1 for f in ffs if f.is_speaking) for ffs in pf.values())
        result = VideoAnalysisResult(duration_seconds=duration, person_count=len(pf))

        for pid, ffs in pf.items():
            if not ffs: continue
            role = role_map.get(pid, "Unknown")
            ps   = self._score_person(pid, role, ffs, tot_sp, cultural_mode)
            ps.appeared_at_second = first_seen.get(pid, 0.0)
            result.persons[pid] = ps

        if len(result.persons) >= 2:
            result.rapport_score      = self._rapport(pf)
            result.talk_balance_score = self._talk_balance(pf)
        else:
            result.rapport_score = result.talk_balance_score = 50.0

        cs = sum(sum(1 for f in ffs if f.is_speaking)
                 for pid, ffs in pf.items() if role_map.get(pid) == "Candidate")
        result.candidate_talk_pct = round(cs / tot_sp * 100, 1) if tot_sp else 0

        cands = [p for p in result.persons.values() if p.role == "Candidate"]
        c_ov  = sum([p.confidence,p.composure,p.eye_contact,p.engagement,p.energy])/5 if cands else 0
        result.overall_score = round(c_ov*0.5 + result.rapport_score*0.3 + result.talk_balance_score*0.2, 1)
        result.timeline_data = self._build_timeline(all_frames, role_map)
        result.observations  = self._observations(result, pf, role_map)
        return result

    def _score_person(self, pid, role, ffs, tot_sp, cultural_mode) -> PersonScore:
        n       = len(ffs)
        emo_cnt = defaultdict(int)
        for f in ffs: emo_cnt[f.emotion] += 1

        ec_r  = sum(1 for f in ffs if f.eye_contact) / n
        pos_r = sum(1 for f in ffs if f.emotion in ("happy","neutral")) / n
        nerv_r= sum(1 for f in ffs if f.emotion == "nervous") / n
        spk   = sum(1 for f in ffs if f.is_speaking)
        up_r  = sum(1 for f in ffs if f.posture in ("upright","leaning_forward","open","engaged")) / n
        tlk   = spk / tot_sp * 100 if tot_sp else 0

        # Body language aggregates
        bls       = [f.body_language for f in ffs if f.body_language]
        open_pct  = sum(1 for b in bls if b.open_score >= 55) / max(1, len(bls)) * 100 if bls else 50
        cross_pct = sum(1 for b in bls if b.arms_crossed)     / max(1, len(bls)) * 100 if bls else 0
        fwd_pct   = sum(1 for b in bls if b.forward_lean)     / max(1, len(bls)) * 100 if bls else 0

        n_d   = len([k for k,v in emo_cnt.items() if v/n > 0.05])
        energy= min(100.0, (min(1.0,n_d/5)*0.5 + min(1.0,spk/n*1.5)*0.5)*100)
        conf  = min(100.0, (pos_r*0.35 + ec_r*0.30 + up_r*0.20 + open_pct/100*0.15)*100)
        comp  = min(100.0, max(0.0, (1-nerv_r*1.5)*100))
        ec    = ec_r * 100
        eng   = min(100.0, (ec_r*0.35 + up_r*0.30 + open_pct/100*0.20 + energy/100*0.15)*100)
        rec   = min(100.0, (ec_r*0.5 + pos_r*0.5)*100)

        is_iv = role not in ("Candidate","Unknown","")
        ov    = round((rec+eng)/2,1) if is_iv else round((conf+comp+ec+eng+energy)/5,1)
        dom   = max(emo_cnt, key=emo_cnt.get) if emo_cnt else "neutral"

        # Cultural scoring (candidate only)
        cultural = None
        if not is_iv and cultural_mode != "none":
            cultural = self.score_cultural(ffs, bls)

        return PersonScore(
            person_id=pid, role=role,
            confidence=round(conf,1), composure=round(comp,1),
            eye_contact=round(ec,1),  engagement=round(eng,1),
            energy=round(energy,1),   receptiveness=round(rec,1),
            overall=ov, talk_time_pct=round(tlk,1),
            open_body_pct=round(open_pct,1), arm_crossed_pct=round(cross_pct,1),
            forward_lean_pct=round(fwd_pct,1),
            emotion_distribution={k:round(v/n,3) for k,v in emo_cnt.items()},
            dominant_emotion=dom, cultural=cultural,
        )

    def _rapport(self, pf) -> float:
        ids = list(pf.keys())
        if len(ids) < 2: return 50.0
        a = {f.timestamp: f for f in pf[ids[0]]}
        b = {f.timestamp: f for f in pf[ids[1]]}
        sh = set(a) & set(b)
        if not sh: return 50.0
        em = sum(1 for t in sh if a[t].emotion == b[t].emotion) / len(sh)
        ec = sum(1 for t in sh if a[t].eye_contact and b[t].eye_contact) / len(sh)
        return round(min(100,(em*0.4+ec*0.6)*100), 1)

    def _talk_balance(self, pf) -> float:
        tot = {pid: sum(1 for f in ffs if f.is_speaking) for pid,ffs in pf.items()}
        g   = sum(tot.values())
        if g == 0: return 50.0
        n   = len(tot)
        dev = sum(abs(v/g - 1/n) for v in tot.values()) / 2
        return round(max(0, min(100,(1-dev)*100)), 1)

    def _build_timeline(self, all_frames, role_map) -> List[dict]:
        md: Dict[int, Dict[int, List[str]]] = defaultdict(lambda: defaultdict(list))
        for ffs in all_frames:
            for ff in ffs: md[int(ff.timestamp//60)][ff.person_id].append(ff.emotion)
        tl = []
        for m in sorted(md.keys()):
            e = {"minute": m, "persons": {}}
            for pid, emos in md[m].items():
                dom = max(set(emos), key=emos.count)
                e["persons"][pid] = {"dominant_emotion":dom,
                                     "role":role_map.get(pid,"Unknown"),
                                     "counts":{emo:emos.count(emo) for emo in set(emos)}}
            tl.append(e)
        return tl

    def _observations(self, result, pf, role_map, n=4) -> List[str]:
        obs = []
        cid = next((pid for pid,p in result.persons.items() if p.role=="Candidate"), None)
        if cid:
            ffs = pf[cid]; half = len(ffs)//2
            h1, h2 = ffs[:half], ffs[half:]
            if h1 and h2:
                p1 = sum(1 for f in h1 if f.emotion in ("happy","neutral")) / len(h1)
                p2 = sum(1 for f in h2 if f.emotion in ("happy","neutral")) / len(h2)
                if p2 > p1 + 0.15:
                    obs.append("Candidate grew more confident in the second half of the interview.")
                elif p1 > p2 + 0.15:
                    obs.append("Candidate appeared more confident early on — energy dipped toward the end.")

            ec = result.persons[cid].eye_contact
            if ec < 50:
                obs.append(f"Candidate's eye contact was limited ({ec:.0f}%) — stronger direct gaze would project confidence.")
            elif ec > 75:
                obs.append(f"Candidate maintained strong eye contact ({ec:.0f}%), projecting engagement.")

            nv = sum(1 for f in ffs if f.emotion=="nervous") / max(1,len(ffs)) * 100
            if nv > 30:
                obs.append(f"Nervousness was visible {nv:.0f}% of the time — mock interviews could help.")

            # Body language observation
            bls = [f.body_language for f in ffs if f.body_language]
            if bls:
                cross_pct = sum(1 for b in bls if b.arms_crossed) / len(bls) * 100
                if cross_pct > 25:
                    obs.append(f"Arms were crossed {cross_pct:.0f}% of the time — open body language conveys more confidence.")

        if result.talk_balance_score < 40:
            obs.append("Talk balance was uneven — one party dominated the conversation.")
        elif result.talk_balance_score > 68:
            obs.append("Conversation was well-balanced with both parties contributing roughly equally.")

        if result.rapport_score > 68:
            obs.append("Good rapport — participants showed aligned emotional responses.")
        elif result.rapport_score < 42:
            obs.append("Rapport appeared limited — less mutual eye contact and emotional mirroring than ideal.")

        late = [p for p in result.persons.values() if p.appeared_at_second > 30]
        for lp in late:
            m, s = int(lp.appeared_at_second//60), int(lp.appeared_at_second%60)
            obs.append(f"{lp.role} joined late (~{m}m {s}s into the session).")

        return obs[:n]

    # ── Drawing helpers ───────────────────────────────────────────────────────

    def _draw(self, frame: np.ndarray, faces: List[FaceFrame], role_map: Dict) -> np.ndarray:
        for ff in faces:
            x, y, fw, fh = ff.bbox
            role  = role_map.get(ff.person_id, "Unknown")
            color = ROLE_BGR.get(role, (128,128,128))
            thick = 3 if ff.is_speaking else 2
            cv2.rectangle(frame, (x,y), (x+fw,y+fh), color, thick)
            label = f"{role}: {ff.emotion}"
            (tw,th),_ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.52, 1)
            cv2.rectangle(frame, (x,max(0,y-th-10)), (x+tw+8,y), color, -1)
            cv2.putText(frame, label, (x+4,max(th,y-4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.52, (255,255,255), 1, cv2.LINE_AA)
            if ff.eye_contact:
                cv2.putText(frame,"EC",(x+fw-32,y+20),cv2.FONT_HERSHEY_SIMPLEX,0.48,(50,255,50),2)
            if ff.is_speaking:
                cv2.rectangle(frame,(x-3,y-3),(x+fw+3,y+fh+3),(0,255,80),2)
            # Body language sub-label
            if ff.body_language and ff.body_language.body_language_label != "NEUTRAL":
                bl_col = BL_BGR.get(ff.body_language.body_language_label, (128,128,128))
                bl_txt = ff.body_language.body_language_label
                cv2.putText(frame, bl_txt, (x, y+fh+18),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.48, bl_col, 2, cv2.LINE_AA)
        return frame

    def _draw_body_language_badge(
        self, frame: np.ndarray, bl: BodyLanguageSummary, pos: Tuple[int,int] = (10, 10)
    ) -> np.ndarray:
        label = bl.body_language_label if bl else "NEUTRAL"
        color = BL_BGR.get(label, (128,128,128))
        (tw,th),_ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        x, y = pos
        cv2.rectangle(frame, (x,y), (x+tw+16,y+th+12), color, -1)
        cv2.putText(frame, label, (x+8,y+th+4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,255,255), 2, cv2.LINE_AA)
        # Arm-crossed indicator
        if bl and bl.arms_crossed:
            cv2.putText(frame, "ARMS X", (x,y+th+30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (60,20,220), 2)
        return frame

    # ── Annotated video ───────────────────────────────────────────────────────

    def _make_annotated_video(self, video_path, all_frames, role_map, fps) -> Optional[str]:
        try:
            cap  = cv2.VideoCapture(video_path)
            w    = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h    = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            out_p= tempfile.mktemp(suffix="_annotated.mp4")
            out  = cv2.VideoWriter(out_p, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w,h))
            lut: Dict[int, List[FaceFrame]] = {}
            for ffs in all_frames:
                if ffs: lut[int(ffs[0].timestamp * fps)] = ffs
            idx  = 0; last: List[FaceFrame] = []; si = max(1,int(fps))
            while cap.isOpened():
                ret, frame = cap.read()
                if not ret: break
                snap = (idx//si)*si
                faces = lut.get(snap, last)
                if faces: last = faces
                bl = last[0].body_language if last else BodyLanguageSummary()
                frame = self._draw(frame, last, role_map)
                frame = self._draw_body_language_badge(frame, bl)
                out.write(frame); idx += 1
            cap.release(); out.release()
            return out_p
        except Exception as e:
            print(f"[VA] annotated video error: {e}"); return None

    # ── HTML renderers ────────────────────────────────────────────────────────

    def render_score_cards_html(self, result: VideoAnalysisResult, cultural_mode="both") -> str:
        if result.error:
            return f'<div style="color:#ef4444;padding:16px;white-space:pre-wrap;">{result.error[:800]}</div>'

        SC  = lambda v: "#166534" if v>=80 else "#1d4ed8" if v>=65 else "#92400e" if v>=50 else "#991b1b"
        BAR = lambda v,c: (f'<div style="background:#e2e8f0;border-radius:4px;height:6px;margin-top:4px;">'
                           f'<div style="background:{c};height:6px;border-radius:4px;width:{v:.0f}%;"></div></div>')
        MET = lambda lbl,v,c: (f'<div style="margin-bottom:10px;">'
                                f'<div style="display:flex;justify-content:space-between;font-size:0.8em;">'
                                f'<span style="color:#64748b;">{lbl}</span>'
                                f'<span style="font-weight:700;color:{c};">{v:.0f}%</span></div>'
                                + BAR(v,c) + '</div>')

        cands = [p for p in result.persons.values() if p.role == "Candidate"]
        ivrs  = [p for p in result.persons.values() if p.role not in ("Candidate","Unknown","")]
        html  = '<div style="font-family:system-ui,sans-serif;padding:4px 0;">'

        # Overall banner
        ov = result.overall_score; oc = SC(ov)
        html += (f'<div style="background:{oc};border-radius:16px;padding:18px 24px;'
                 f'margin-bottom:20px;display:flex;align-items:center;gap:20px;">'
                 f'<div style="background:rgba(255,255,255,0.18);border-radius:12px;'
                 f'padding:10px 18px;text-align:center;min-width:78px;">'
                 f'<div style="font-size:2.5em;font-weight:900;color:#fff;line-height:1;">{ov:.0f}</div>'
                 f'<div style="font-size:0.68em;font-weight:700;color:rgba(255,255,255,.75);'
                 f'text-transform:uppercase;letter-spacing:.08em;">/ 100</div></div>'
                 f'<div><div style="font-size:0.72em;font-weight:700;text-transform:uppercase;'
                 f'letter-spacing:.1em;color:rgba(255,255,255,.7);margin-bottom:4px;">Overall Session Score</div>'
                 f'<div style="font-size:1.1em;font-weight:800;color:#fff;">'
                 f'{result.person_count} participant{"s" if result.person_count!=1 else ""} · '
                 f'{int(result.duration_seconds//60)}m {int(result.duration_seconds%60)}s</div></div></div>')

        # Candidate card
        for p in cands:
            c = SC(p.overall)
            html += (f'<div style="border:2px solid {c};border-radius:14px;padding:18px 20px;'
                     f'margin-bottom:16px;background:var(--ta-card-bg,#f8fafc);">'
                     f'<div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;">'
                     f'<div style="background:{c};border-radius:10px;padding:6px 14px;'
                     f'color:#fff;font-weight:800;font-size:0.9em;">Candidate</div>'
                     f'<div style="font-size:1.4em;font-weight:900;color:{c};">{p.overall:.0f}%</div>'
                     f'<div style="font-size:0.78em;color:#64748b;margin-left:auto;">'
                     f'Talk: {p.talk_time_pct:.0f}% · Mood: {p.dominant_emotion}</div></div>'
                     + MET("Confidence",   p.confidence,  SC(p.confidence))
                     + MET("Composure",    p.composure,   SC(p.composure))
                     + MET("Eye Contact",  p.eye_contact, SC(p.eye_contact))
                     + MET("Engagement",   p.engagement,  SC(p.engagement))
                     + MET("Energy Level", p.energy,      SC(p.energy)))
            # Body language sub-section
            bl_col = BL_HTML.get(("OPEN" if p.open_body_pct > 60 else "CLOSED" if p.arm_crossed_pct > 25 else "NEUTRAL"), "#94a3b8")
            html += (f'<div style="margin-top:10px;padding-top:10px;border-top:1px solid #e2e8f0;">'
                     f'<div style="font-size:0.78em;font-weight:700;color:#475569;margin-bottom:6px;">Body Language</div>'
                     + MET("Open Posture",   p.open_body_pct,    SC(p.open_body_pct))
                     + MET("Forward Lean",   p.forward_lean_pct, SC(p.forward_lean_pct))
                     + f'<div style="font-size:0.75em;color:#64748b;">'
                     f'Arms crossed: {p.arm_crossed_pct:.0f}% of session</div></div>')
            html += '</div>'

        # Interviewer cards
        for p in ivrs:
            c = SC(p.overall)
            html += (f'<div style="border:2px solid {c};border-radius:14px;padding:18px 20px;'
                     f'margin-bottom:16px;background:var(--ta-card-bg,#f8fafc);">'
                     f'<div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;">'
                     f'<div style="background:{c};border-radius:10px;padding:6px 14px;'
                     f'color:#fff;font-weight:800;font-size:0.9em;">{p.role}</div>'
                     f'<div style="font-size:1.4em;font-weight:900;color:{c};">{p.overall:.0f}%</div>'
                     f'<div style="font-size:0.78em;color:#64748b;margin-left:auto;">'
                     f'Talk: {p.talk_time_pct:.0f}% · Mood: {p.dominant_emotion}</div></div>'
                     + MET("Receptiveness", p.receptiveness, SC(p.receptiveness))
                     + MET("Engagement",    p.engagement,    SC(p.engagement))
                     + '</div>')

        # Interaction
        ct = result.candidate_talk_pct; it = round(100-ct,1)
        html += (f'<div style="border:2px solid #3b82f6;border-radius:14px;padding:18px 20px;'
                 f'margin-bottom:16px;background:var(--ta-card-bg,#f8fafc);">'
                 f'<div style="font-weight:800;color:#1d4ed8;margin-bottom:12px;">Interaction</div>'
                 + MET("Rapport",      result.rapport_score,     SC(result.rapport_score))
                 + MET("Talk Balance", result.talk_balance_score, SC(result.talk_balance_score))
                 + f'<div style="font-size:0.78em;color:#64748b;margin-top:6px;">'
                 f'Candidate {ct:.0f}% · Interviewer(s) {it:.0f}%</div></div>')

        # Observations
        if result.observations:
            html += ('<div style="border:1px solid #e2e8f0;border-radius:14px;padding:18px 20px;'
                     'margin-bottom:16px;background:var(--ta-card-bg,#f8fafc);">'
                     '<div style="font-weight:800;color:#475569;margin-bottom:12px;">Key Observations</div>'
                     '<ul style="margin:0;padding-left:18px;">')
            for o in result.observations:
                html += f'<li style="color:#374151;font-size:0.88em;margin-bottom:8px;">{o}</li>'
            html += '</ul></div>'

        # Cultural panels (candidate only)
        for p in cands:
            if p.cultural and cultural_mode != "none":
                html += self.render_cultural_comparison_html(p.cultural, cultural_mode)

        html += '</div>'
        return html

    def render_cultural_comparison_html(
        self, score: CulturalStyleScore, mode: str = "both"
    ) -> str:
        SC  = lambda v: "#166534" if v>=80 else "#1d4ed8" if v>=65 else "#92400e" if v>=50 else "#991b1b"
        BAR = lambda v,c: (f'<div style="background:#e2e8f0;border-radius:4px;height:6px;margin-top:4px;">'
                           f'<div style="background:{c};height:6px;border-radius:4px;width:{v:.0f}%;"></div></div>')

        def _tips_html(tips):
            if not tips: return '<p style="color:#94a3b8;font-size:0.82em;">No tips — great work!</p>'
            return ''.join(
                f'<div style="display:flex;gap:8px;margin-bottom:8px;">'
                f'<span style="color:#f59e0b;flex-shrink:0;">▶</span>'
                f'<span style="font-size:0.82em;color:#374151;">{t}</span></div>'
                for t in tips
            )

        am_col  = SC(score.american_score)
        ad_col  = SC(score.adaptation_score)
        grid    = "1fr 1fr" if mode == "both" else "1fr"

        html = (f'<div style="margin-top:16px;margin-bottom:16px;">'
                f'<div style="font-weight:800;color:#475569;margin-bottom:12px;">'
                f'Cultural Style Analysis</div>'
                f'<div style="display:grid;grid-template-columns:{grid};gap:16px;">')

        if mode in ("both", "american"):
            html += (f'<div style="border:2px solid {am_col};border-radius:14px;'
                     f'padding:16px;background:var(--ta-card-bg,#f8fafc);">'
                     f'<div style="font-weight:800;color:{am_col};margin-bottom:8px;font-size:0.9em;">'
                     f'American Interview Standard</div>'
                     f'<div style="font-size:2em;font-weight:900;color:{am_col};">{score.american_score:.0f}</div>'
                     f'<div style="font-size:0.68em;color:#64748b;margin-bottom:10px;">/ 100</div>'
                     + BAR(score.american_score, am_col)
                     + f'<div style="margin-top:12px;font-size:0.78em;font-weight:700;color:#475569;'
                     f'margin-bottom:6px;">Coaching Tips</div>'
                     + _tips_html(score.american_tips) + '</div>')

        if mode in ("both", "indian_to_american"):
            html += (f'<div style="border:2px solid {ad_col};border-radius:14px;'
                     f'padding:16px;background:var(--ta-card-bg,#f8fafc);">'
                     f'<div style="font-weight:800;color:{ad_col};margin-bottom:8px;font-size:0.9em;">'
                     f'Indian → American Adaptation</div>'
                     f'<div style="font-size:2em;font-weight:900;color:{ad_col};">{score.adaptation_score:.0f}</div>'
                     f'<div style="font-size:0.68em;color:#64748b;margin-bottom:10px;">/ 100</div>'
                     + BAR(score.adaptation_score, ad_col)
                     + f'<div style="margin-top:12px;font-size:0.78em;font-weight:700;color:#475569;'
                     f'margin-bottom:6px;">Adaptation Tips</div>'
                     + _tips_html(score.adaptation_tips) + '</div>')

        html += '</div></div>'
        return html

    def render_live_score_html(self, snap: LiveScoreSnapshot, cultural_mode: str = "both") -> str:
        SC  = lambda v: "#166534" if v>=80 else "#1d4ed8" if v>=65 else "#92400e" if v>=50 else "#991b1b"
        BAR = lambda v,c: (f'<div style="background:#e2e8f0;border-radius:4px;height:5px;margin-top:3px;">'
                           f'<div style="background:{c};height:5px;border-radius:4px;width:{v:.0f}%;"></div></div>')
        MET = lambda lbl,v,c: (f'<div style="margin-bottom:8px;">'
                                f'<div style="display:flex;justify-content:space-between;font-size:0.78em;">'
                                f'<span style="color:#64748b;">{lbl}</span>'
                                f'<span style="font-weight:700;color:{c};">{v:.0f}%</span></div>'
                                + BAR(v,c) + '</div>')

        el_m = int(snap.elapsed_sec // 60); el_s = int(snap.elapsed_sec % 60)
        bl_c = BL_HTML.get(snap.body_label, "#94a3b8")
        emo_c= EMO_HTML.get(snap.dominant_emotion, "#94a3b8")

        html = (f'<div style="font-family:system-ui,sans-serif;padding:4px 0;">'
                # Header row
                f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap;">'
                f'<span style="background:#dc2626;color:#fff;border-radius:50%;width:10px;height:10px;'
                f'display:inline-block;animation:pulse 1s infinite;"></span>'
                f'<span style="font-size:0.78em;font-weight:700;color:#374151;">LIVE · {el_m}:{el_s:02d}</span>'
                f'<span style="background:{bl_c};color:#fff;border-radius:20px;padding:2px 10px;'
                f'font-size:0.72em;font-weight:700;">{snap.body_label}</span>'
                f'<span style="background:{emo_c};color:#fff;border-radius:20px;padding:2px 10px;'
                f'font-size:0.72em;font-weight:700;">{snap.dominant_emotion}</span>'
                f'</div>'
                # Score bars
                + MET("Confidence",   snap.confidence,   SC(snap.confidence))
                + MET("Eye Contact",  snap.eye_contact,  SC(snap.eye_contact))
                + MET("Engagement",   snap.engagement,   SC(snap.engagement))
                + MET("Composure",    snap.composure,    SC(snap.composure))
                + MET("Open Posture", snap.open_body_pct,SC(snap.open_body_pct))
                + f'<div style="font-size:0.72em;color:#94a3b8;margin-top:4px;">'
                f'{snap.frame_count} frames analyzed</div>')

        if snap.cultural and cultural_mode != "none":
            html += self.render_cultural_comparison_html(snap.cultural, cultural_mode)

        html += '</div>'
        return html

    def render_timeline_figure(self, result: VideoAnalysisResult):
        try: import plotly.graph_objects as go
        except ImportError: return None
        if not result.timeline_data: return None

        all_pids: Dict[int, str] = {}
        for e in result.timeline_data:
            for pid_raw, d in e["persons"].items():
                pid = int(pid_raw)
                if pid not in all_pids: all_pids[pid] = d.get("role","Unknown")

        mins = [e["minute"] for e in result.timeline_data]
        fig  = go.Figure()

        for pid, role in all_pids.items():
            ec, cc, hov = [], [], []
            for e in result.timeline_data:
                pd = e["persons"].get(pid) or e["persons"].get(str(pid))
                if pd:
                    emo = pd["dominant_emotion"]; cnt = pd.get("counts",{})
                    tot = sum(cnt.values()) or 1
                    detail = ", ".join(f"{k}: {v/tot*100:.0f}%" for k,v in sorted(cnt.items(),key=lambda x:-x[1]))
                else:
                    emo = "none"; detail = "not in frame"
                ec.append(emo); cc.append(EMO_HTML.get(emo,"#94a3b8"))
                hov.append(f"Minute {e['minute']}<br>{role}<br><b>{emo}</b><br>{detail}")

            fig.add_trace(go.Scatter(
                x=mins, y=[role]*len(mins), mode="markers",
                marker=dict(size=22, color=cc, symbol="square",
                            line=dict(width=1,color="rgba(0,0,0,0.15)")),
                text=hov, hoverinfo="text", name=role,
            ))

        seen = set()
        for e in result.timeline_data:
            for d in e["persons"].values():
                if isinstance(d, dict): seen.add(d.get("dominant_emotion","neutral"))
        for emo in seen:
            fig.add_trace(go.Scatter(
                x=[None], y=[None], mode="markers",
                marker=dict(size=12,color=EMO_HTML.get(emo,"#94a3b8"),symbol="square"),
                name=emo, showlegend=True,
            ))

        fig.update_layout(
            title="Emotion Timeline (per minute)",
            xaxis=dict(title="Minute", dtick=1, gridcolor="#e2e8f0"),
            yaxis=dict(title=""),
            plot_bgcolor="white", paper_bgcolor="white",
            height=max(200,120+len(all_pids)*70),
            margin=dict(l=10,r=10,t=40,b=30),
            legend=dict(orientation="h",yanchor="bottom",y=1.02),
        )
        return fig


# ── Live Analysis Session ─────────────────────────────────────────────────────

class LiveAnalysisSession:
    """
    Holds per-user MediaPipe models + state for a live webcam session.
    Stored in gr.State — one instance per Gradio browser session.
    Models are lazy-initialized on the first analysis frame.
    """

    ANALYZE_EVERY = 3     # process every 3rd webcam frame
    SCORE_REFRESH = 15    # refresh score panel every 15 analyzed frames (~5s at 3fps)

    def __init__(self, analyzer: VideoAnalyzer):
        self._va          = analyzer
        self._face_lm     = None
        self._pose_lm     = None
        self._tracker     = None
        self._pitch_hist  = None
        self._initialized = False
        self._frame_n     = 0   # total frames received
        self._analyzed_n  = 0   # frames actually analyzed
        self._since_score = 0   # analyzed frames since last score refresh
        self._face_buf:   List[FaceFrame]           = []  # rolling 30-frame buffer
        self._bl_buf:     List[BodyLanguageSummary] = []
        self._last_bgr:   Optional[np.ndarray]      = None
        self._snap:       Optional[LiveScoreSnapshot] = None
        self._start_time  = time.time()
        self._role_map:   Dict[int, str] = {}

    def set_roles(self, role_map: Dict[int, str]):
        self._role_map = role_map

    def _init_models(self) -> bool:
        if self._initialized: return True
        try:
            fl = _ensure_model(_FACE_LANDMARKER_URL, "face_landmarker.task")
            fl_opts = _mp_vision.FaceLandmarkerOptions(
                base_options=_mp_tasks.BaseOptions(model_asset_path=fl),
                num_faces=2,    # allow 2 for side-by-side panels
                output_face_blendshapes=True,
                output_facial_transformation_matrixes=True,
                min_face_detection_confidence=0.4,
                min_face_presence_confidence=0.4,
                min_tracking_confidence=0.4,
            )
            self._face_lm = _mp_vision.FaceLandmarker.create_from_options(fl_opts)

            try:
                pl = _ensure_model(_POSE_LANDMARKER_URL, "pose_landmarker_lite.task")
                pl_opts = _mp_vision.PoseLandmarkerOptions(
                    base_options=_mp_tasks.BaseOptions(model_asset_path=pl),
                    num_poses=2,
                    min_pose_detection_confidence=0.4,
                    min_pose_presence_confidence=0.4,
                    min_tracking_confidence=0.4,
                )
                self._pose_lm = _mp_vision.PoseLandmarker.create_from_options(pl_opts)
            except Exception as e:
                print(f"[Live] pose model unavailable: {e}")

            self._tracker    = _CentroidTracker(max_gone=30, max_dist=160)
            self._pitch_hist = deque(maxlen=8)
            self._initialized = True
            return True
        except Exception as e:
            print(f"[Live] model init failed: {e}")
            return False

    def process_frame(
        self, frame_rgb: np.ndarray, cultural_mode: str = "both"
    ) -> Tuple[np.ndarray, Optional[LiveScoreSnapshot]]:
        """
        Called by Gradio .stream() for every incoming webcam frame (RGB).
        Returns (annotated_rgb, snapshot_or_None).
        snapshot is only set every SCORE_REFRESH analyzed frames.
        """
        self._frame_n += 1
        frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)

        # Non-analysis frame — return last annotated
        if self._frame_n % self.ANALYZE_EVERY != 0:
            out = self._last_bgr if self._last_bgr is not None else frame_bgr
            return cv2.cvtColor(out, cv2.COLOR_BGR2RGB), None

        if not _HAS_MP or not self._init_models():
            return frame_rgb, None

        h, w = frame_bgr.shape[:2]
        mpi  = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb)

        # Face landmarks
        fl_r    = self._face_lm.detect(mpi)
        bbs     = self._va._lm_to_bboxes(fl_r, h, w)
        tracked = self._tracker.update(bbs)

        # Pose
        pose_res = self._pose_lm.detect(mpi) if self._pose_lm else None

        new_faces: List[FaceFrame] = []
        new_bls:   List[BodyLanguageSummary] = []

        for fi, (pid, bbox) in enumerate(tracked.items()):
            x, y, fw, fh = bbox
            bs = fl_r.face_blendshapes[fi] if (fl_r and fl_r.face_blendshapes and fi < len(fl_r.face_blendshapes)) else None
            tm = fl_r.facial_transformation_matrixes[fi] if (fl_r and fl_r.facial_transformation_matrixes and fi < len(fl_r.facial_transformation_matrixes)) else None

            yaw, pitch, roll = self._va._head_pose_angles(tm) if tm else (0.0, 0.0, 0.0)
            self._pitch_hist.append(pitch)
            ec  = abs(yaw) < 20 and abs(pitch) < 20
            jaw = self._va._bs_val(bs, "jawOpen") if bs else 0.0
            crop= frame_bgr[y:y+fh, x:x+fw]
            emo, eprobs = self._va._emotion(crop, bs)
            bl  = self._va._body_language(pose_res, self._pitch_hist, roll)

            ff = FaceFrame(
                person_id=pid, timestamp=self._frame_n / 10.0,
                bbox=bbox, emotion=emo, emotion_probs=eprobs,
                eye_contact=ec, yaw=yaw, pitch=pitch, roll=roll,
                posture=bl.body_language_label.lower(), is_speaking=(jaw > 0.18),
                body_language=bl,
            )
            new_faces.append(ff); new_bls.append(bl)

        # Update rolling buffers (keep last 30)
        self._face_buf.extend(new_faces); self._bl_buf.extend(new_bls)
        if len(self._face_buf) > 30:
            self._face_buf = self._face_buf[-30:]
            self._bl_buf   = self._bl_buf[-30:]

        self._analyzed_n  += 1
        self._since_score += 1

        # Draw annotations
        annotated = frame_bgr.copy()
        annotated = self._va._draw(annotated, new_faces, self._role_map)
        if new_bls:
            annotated = self._va._draw_body_language_badge(annotated, new_bls[0])
        self._last_bgr = annotated

        # Refresh scores
        snap = None
        if self._since_score >= self.SCORE_REFRESH and self._face_buf:
            self._since_score = 0
            snap = self._compute_snapshot(cultural_mode)
            self._snap = snap

        return cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB), snap

    def _compute_snapshot(self, cultural_mode: str) -> LiveScoreSnapshot:
        ffs = self._face_buf; bls = self._bl_buf
        n   = len(ffs) or 1;  nb  = len(bls) or 1

        ec_r   = sum(1 for f in ffs if f.eye_contact) / n
        pos_r  = sum(1 for f in ffs if f.emotion in ("happy","neutral")) / n
        nerv_r = sum(1 for f in ffs if f.emotion == "nervous") / n
        up_r   = sum(1 for f in ffs if f.posture in ("upright","open","engaged","leaning_forward")) / n
        open_r = sum(1 for b in bls if b.open_score >= 55) / nb

        conf = min(100.0, (pos_r*0.35 + ec_r*0.30 + up_r*0.20 + open_r*0.15)*100)
        comp = min(100.0, max(0.0, (1-nerv_r*1.5)*100))
        ec   = ec_r * 100
        eng  = min(100.0, (ec_r*0.35 + up_r*0.30 + open_r*0.20 + min(1,n/30)*0.15)*100)

        from collections import Counter
        dom  = Counter(f.emotion for f in ffs).most_common(1)[0][0] if ffs else "neutral"
        bl_l = (bls[-1].body_language_label if bls else "NEUTRAL")
        cult = self._va.score_cultural(ffs, bls) if cultural_mode != "none" and ffs else None

        return LiveScoreSnapshot(
            confidence=round(conf,1), eye_contact=round(ec,1),
            engagement=round(eng,1),  composure=round(comp,1),
            open_body_pct=round(open_r*100,1),
            dominant_emotion=dom, body_label=bl_l,
            cultural=cult, frame_count=self._analyzed_n,
            elapsed_sec=time.time()-self._start_time,
        )

    def close(self):
        try:
            if self._face_lm: self._face_lm.close()
            if self._pose_lm: self._pose_lm.close()
        except Exception: pass
        self._initialized = False
