"""
Video Interview Analyzer
Processes uploaded interview video to detect per-person:
  - Emotions, eye contact, head pose, posture, speaking time
Produces: score cards, emotion timeline, observations, annotated video.

Uses MediaPipe Tasks API (mediapipe ≥ 0.10). Models are auto-downloaded
once to .mediapipe_models/ in the project directory.
"""

import os
import cv2
import numpy as np
import tempfile
import time
import math
import urllib.request
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from collections import defaultdict

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

# ── Model download URLs ───────────────────────────────────────────────────────

_MODEL_DIR = Path(__file__).parent / ".mediapipe_models"

_FACE_LANDMARKER_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_landmarker"
    "/face_landmarker/float16/1/face_landmarker.task"
)
_POSE_LANDMARKER_URL = (
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker"
    "/pose_landmarker_lite/float16/1/pose_landmarker_lite.task"
)


def _ensure_model(url: str, filename: str) -> str:
    _MODEL_DIR.mkdir(parents=True, exist_ok=True)
    path = _MODEL_DIR / filename
    if not path.exists():
        print(f"[VideoAnalyzer] Downloading {filename}…")
        urllib.request.urlretrieve(url, str(path))
        print(f"[VideoAnalyzer] Downloaded {filename} ({path.stat().st_size // 1024} KB)")
    return str(path)


# ── Constants ─────────────────────────────────────────────────────────────────

ROLE_CANDIDATE   = "Candidate"
ROLE_INTERVIEWER = "Interviewer"

EMOTION_COLORS_HTML = {
    "happy":     "#22c55e",
    "confident": "#3b82f6",
    "neutral":   "#94a3b8",
    "nervous":   "#f59e0b",
    "surprised": "#a855f7",
    "angry":     "#ef4444",
    "disgusted": "#6366f1",
}

EMOTION_COLORS_BGR = {
    "happy":     (50, 205, 50),
    "confident": (255, 144, 30),
    "neutral":   (180, 180, 180),
    "nervous":   (0, 165, 255),
    "surprised": (200, 0, 200),
    "angry":     (60, 20, 220),
    "disgusted": (130, 0, 130),
}

ROLE_COLORS_BGR = {
    "Candidate":     (255, 120, 0),
    "Interviewer 1": (0, 180, 0),
    "Interviewer 2": (220, 0, 220),
    "Interviewer 3": (200, 200, 0),
    "Interviewer 4": (0, 200, 200),
    "Unknown":       (128, 128, 128),
}


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class FaceFrame:
    person_id:     int
    timestamp:     float
    bbox:          Tuple[int, int, int, int]
    emotion:       str               = "neutral"
    emotion_probs: Dict[str, float]  = field(default_factory=dict)
    eye_contact:   bool              = False
    yaw:           float             = 0.0
    pitch:         float             = 0.0
    posture:       str               = "unknown"
    is_speaking:   bool              = False

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
    emotion_distribution: Dict[str, float] = field(default_factory=dict)
    dominant_emotion:     str  = "neutral"
    appeared_at_second:   float = 0.0

@dataclass
class VideoAnalysisResult:
    persons:              Dict[int, PersonScore] = field(default_factory=dict)
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


# ── Centroid face tracker ─────────────────────────────────────────────────────

class _CentroidTracker:
    def __init__(self, max_gone=45, max_dist=160):
        self.next_id  = 0
        self._cents   = {}
        self._bboxes  = {}
        self._gone    = {}
        self.max_gone = max_gone
        self.max_dist = max_dist

    def update(self, bboxes: List[Tuple]) -> Dict[int, Tuple]:
        if not bboxes:
            for oid in list(self._gone):
                self._gone[oid] += 1
                if self._gone[oid] > self.max_gone:
                    self._cents.pop(oid, None)
                    self._bboxes.pop(oid, None)
                    del self._gone[oid]
            return {}

        nc = [(x + w // 2, y + h // 2) for x, y, w, h in bboxes]

        if not self._cents:
            r = {}
            for c, b in zip(nc, bboxes):
                self._cents[self.next_id]  = c
                self._bboxes[self.next_id] = b
                self._gone[self.next_id]   = 0
                r[self.next_id] = b
                self.next_id += 1
            return r

        oids = list(self._cents.keys())
        oc   = [self._cents[o] for o in oids]
        D    = np.array([[((a[0]-b[0])**2 + (a[1]-b[1])**2)**0.5 for b in nc] for a in oc])
        rows = D.min(axis=1).argsort()
        cols = D.argmin(axis=1)[rows]
        ur, uc, r = set(), set(), {}

        for rr, cc in zip(rows, cols):
            if rr in ur or cc in uc or D[rr, cc] > self.max_dist:
                continue
            oid = oids[rr]
            self._cents[oid]  = nc[cc]
            self._bboxes[oid] = bboxes[cc]
            self._gone[oid]   = 0
            r[oid] = bboxes[cc]
            ur.add(rr); uc.add(cc)

        for rr in set(range(len(oids))) - ur:
            oid = oids[rr]
            self._gone[oid] += 1
            if self._gone[oid] > self.max_gone:
                self._cents.pop(oid, None); self._bboxes.pop(oid, None); del self._gone[oid]

        for cc in set(range(len(bboxes))) - uc:
            self._cents[self.next_id]  = nc[cc]
            self._bboxes[self.next_id] = bboxes[cc]
            self._gone[self.next_id]   = 0
            r[self.next_id] = bboxes[cc]
            self.next_id += 1
        return r


# ── Main VideoAnalyzer ────────────────────────────────────────────────────────

class VideoAnalyzer:
    """Main interface for video interview analysis."""

    # ── Public: scan faces ────────────────────────────────────────────────────

    def scan_faces(self, video_path: str, progress_cb=None) -> Tuple[Dict[int, str], float]:
        """Quick pass — detect unique faces. Returns ({id: thumb_path}, duration)."""
        if not _HAS_MP:
            return {}, 0.0
        try:
            fl_path = _ensure_model(_FACE_LANDMARKER_URL, "face_landmarker.task")
        except Exception as e:
            print(f"[VideoAnalyzer] model download failed: {e}")
            return {}, 0.0

        cap  = cv2.VideoCapture(video_path)
        fps  = cap.get(cv2.CAP_PROP_FPS) or 30
        tot  = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        dur  = tot / fps

        opts = _mp_vision.FaceLandmarkerOptions(
            base_options=_mp_tasks.BaseOptions(model_asset_path=fl_path),
            num_faces=5,
            output_face_blendshapes=False,
            output_facial_transformation_matrixes=False,
            min_face_detection_confidence=0.4,
            min_face_presence_confidence=0.4,
            min_tracking_confidence=0.4,
        )
        landmarker = _mp_vision.FaceLandmarker.create_from_options(opts)
        trk    = _CentroidTracker(max_gone=int(fps * 3), max_dist=180)
        thumbs: Dict[int, str] = {}
        interval = max(1, int(fps * 2))
        idx = 0

        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break
            if idx % interval == 0:
                h, w = frame.shape[:2]
                rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mpi   = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                res   = landmarker.detect(mpi)
                bbs   = self._lm_to_bboxes(res, h, w)
                tracked = trk.update(bbs)
                for pid, (x, y, fw, fh) in tracked.items():
                    if pid not in thumbs:
                        crop = frame[y:y+fh, x:x+fw]
                        if crop.size > 0:
                            p = tempfile.mktemp(suffix=f"_thumb{pid}.jpg")
                            cv2.imwrite(p, cv2.resize(crop, (120, 160)))
                            thumbs[pid] = p
                if progress_cb:
                    progress_cb(min(0.3, idx / tot * 0.35))
            idx += 1

        cap.release(); landmarker.close()
        return thumbs, dur

    # ── Public: full analysis ─────────────────────────────────────────────────

    def analyze_video(
        self,
        video_path: str,
        role_map: Dict[int, str],
        sample_fps: float = 1.0,
        progress_cb=None,
    ) -> VideoAnalysisResult:
        if not _HAS_MP:
            r = VideoAnalysisResult()
            r.error = "mediapipe is not installed. Run: pip install mediapipe opencv-python"
            return r
        try:
            return self._run(video_path, role_map, sample_fps, progress_cb)
        except Exception as exc:
            import traceback
            r = VideoAnalysisResult()
            r.error = f"Analysis failed: {exc}\n{traceback.format_exc()}"
            return r

    def _run(self, video_path, role_map, sample_fps, progress_cb):
        fl_path = _ensure_model(_FACE_LANDMARKER_URL, "face_landmarker.task")
        try:
            pl_path = _ensure_model(_POSE_LANDMARKER_URL, "pose_landmarker_lite.task")
            _use_pose = True
        except Exception:
            _use_pose = False

        cap  = cv2.VideoCapture(video_path)
        fps  = cap.get(cv2.CAP_PROP_FPS) or 30
        tot  = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        dur  = tot / fps
        ivl  = max(1, int(fps / sample_fps))

        fl_opts = _mp_vision.FaceLandmarkerOptions(
            base_options=_mp_tasks.BaseOptions(model_asset_path=fl_path),
            num_faces=5,
            output_face_blendshapes=True,
            output_facial_transformation_matrixes=True,
            min_face_detection_confidence=0.4,
            min_face_presence_confidence=0.4,
            min_tracking_confidence=0.4,
        )
        face_lm = _mp_vision.FaceLandmarker.create_from_options(fl_opts)

        pose_lm = None
        if _use_pose:
            pl_opts = _mp_vision.PoseLandmarkerOptions(
                base_options=_mp_tasks.BaseOptions(model_asset_path=pl_path),
                num_poses=4,
                min_pose_detection_confidence=0.4,
                min_pose_presence_confidence=0.4,
                min_tracking_confidence=0.4,
            )
            pose_lm = _mp_vision.PoseLandmarker.create_from_options(pl_opts)

        trk        = _CentroidTracker(max_gone=int(fps * 3), max_dist=200)
        all_frames: List[List[FaceFrame]] = []
        first_seen: Dict[int, float]      = {}

        idx = 0
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break
            if idx % ivl == 0:
                ts  = idx / fps
                ffs = self._proc(frame, ts, trk, face_lm, pose_lm, first_seen)
                all_frames.append(ffs)
                if progress_cb:
                    progress_cb(min(0.72, 0.05 + idx / tot * 0.67))
            idx += 1

        cap.release(); face_lm.close()
        if pose_lm:
            pose_lm.close()

        if progress_cb:
            progress_cb(0.78)

        result = self._aggregate(all_frames, role_map, dur, first_seen)

        if progress_cb:
            progress_cb(0.85)

        result.annotated_video_path = self._annotated_video(
            video_path, all_frames, role_map, fps
        )
        if progress_cb:
            progress_cb(1.0)
        return result

    # ── Frame helpers ─────────────────────────────────────────────────────────

    def _lm_to_bboxes(self, res, h, w) -> List[Tuple[int, int, int, int]]:
        bbs = []
        if not res or not res.face_landmarks:
            return bbs
        for face in res.face_landmarks:
            xs = [lm.x * w for lm in face]
            ys = [lm.y * h for lm in face]
            x1, y1 = max(0, int(min(xs) - 10)), max(0, int(min(ys) - 20))
            x2, y2 = min(w, int(max(xs) + 10)), min(h, int(max(ys) + 10))
            fw, fh = x2 - x1, y2 - y1
            if fw > 20 and fh > 20:
                bbs.append((x1, y1, fw, fh))
        return bbs

    def _proc(self, frame, ts, trk, face_lm, pose_lm, first_seen) -> List[FaceFrame]:
        h, w  = frame.shape[:2]
        rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mpi   = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

        fl_res = face_lm.detect(mpi)
        bbs    = self._lm_to_bboxes(fl_res, h, w)
        tracked = trk.update(bbs)

        for pid in tracked:
            if pid not in first_seen:
                first_seen[pid] = ts

        posture = self._posture(pose_lm, mpi) if pose_lm else "unknown"
        out = []

        for face_idx, (pid, bbox) in enumerate(tracked.items()):
            x, y, fw, fh = bbox

            # Match face_idx from landmark list
            lm_face  = None
            if fl_res and fl_res.face_landmarks and face_idx < len(fl_res.face_landmarks):
                lm_face = fl_res.face_landmarks[face_idx]

            bs_face  = None
            if fl_res and fl_res.face_blendshapes and face_idx < len(fl_res.face_blendshapes):
                bs_face = fl_res.face_blendshapes[face_idx]

            tm_face  = None
            if (fl_res and fl_res.facial_transformation_matrixes
                    and face_idx < len(fl_res.facial_transformation_matrixes)):
                tm_face = fl_res.facial_transformation_matrixes[face_idx]

            # Head pose
            yaw, pitch = self._head_pose_tm(tm_face) if tm_face else (0.0, 0.0)
            eye_contact = abs(yaw) < 20 and abs(pitch) < 20

            # Speaking from blendshapes or fallback
            jaw_open = 0.0
            if bs_face:
                jaw_open = self._bs_val(bs_face, "jawOpen")
            is_speaking = jaw_open > 0.18

            # Emotion
            crop = frame[y:y+fh, x:x+fw]
            emotion, eprobs = self._emotion(crop, bs_face)

            out.append(FaceFrame(
                person_id=pid, timestamp=ts, bbox=bbox,
                emotion=emotion, emotion_probs=eprobs,
                eye_contact=eye_contact, yaw=yaw, pitch=pitch,
                posture=posture, is_speaking=is_speaking,
            ))
        return out

    # ── Head pose from facial transformation matrix ───────────────────────────

    def _head_pose_tm(self, tm) -> Tuple[float, float]:
        try:
            data = list(tm.data)
            if len(data) < 16:
                return 0.0, 0.0
            M  = np.array(data, dtype=float).reshape(4, 4)
            R  = M[:3, :3]
            sy = math.sqrt(R[0,0]**2 + R[1,0]**2)
            if sy > 1e-6:
                pitch = math.degrees(math.atan2(R[2,1], R[2,2]))
                yaw   = math.degrees(math.atan2(-R[2,0], sy))
            else:
                pitch = math.degrees(math.atan2(-R[1,2], R[1,1]))
                yaw   = math.degrees(math.atan2(-R[2,0], sy))
            return float(yaw), float(pitch)
        except Exception:
            return 0.0, 0.0

    # ── Blendshape utilities ──────────────────────────────────────────────────

    def _bs_val(self, blendshapes, name: str) -> float:
        for bs in blendshapes:
            if bs.category_name == name:
                return float(bs.score)
        return 0.0

    def _emotion(self, crop, blendshapes) -> Tuple[str, Dict[str, float]]:
        """Detect emotion: DeepFace → FER → blendshape heuristic."""
        if crop is not None and crop.size > 0 and crop.shape[0] >= 20:
            if _HAS_DEEPFACE:
                try:
                    res = _DeepFace.analyze(
                        crop, actions=["emotion"], enforce_detection=False, silent=True
                    )
                    raw = (res[0] if isinstance(res, list) else res)["emotion"]
                    dom = max(raw, key=raw.get)
                    return self._map_emo(dom), {self._map_emo(k): v for k, v in raw.items()}
                except Exception:
                    pass

            if _HAS_FER:
                try:
                    dets = _fer_detector.detect_emotions(crop)
                    if dets:
                        raw = dets[0]["emotions"]
                        dom = max(raw, key=raw.get)
                        return self._map_emo(dom), {self._map_emo(k): v for k, v in raw.items()}
                except Exception:
                    pass

        if blendshapes:
            return self._emo_from_blendshapes(blendshapes)

        return "neutral", {"neutral": 1.0}

    def _map_emo(self, raw: str) -> str:
        return {
            "angry": "angry", "disgust": "disgusted", "fear": "nervous",
            "happy": "happy", "sad": "nervous", "surprise": "surprised",
            "neutral": "neutral",
        }.get(raw.lower(), "neutral")

    def _emo_from_blendshapes(self, bs) -> Tuple[str, Dict[str, float]]:
        """Derive emotion from MediaPipe blendshape scores."""
        smile = (self._bs_val(bs, "mouthSmileLeft") + self._bs_val(bs, "mouthSmileRight")) / 2
        frown = (self._bs_val(bs, "mouthFrownLeft") + self._bs_val(bs, "mouthFrownRight")) / 2
        brow_d= (self._bs_val(bs, "browDownLeft")   + self._bs_val(bs, "browDownRight"))   / 2
        brow_u= (self._bs_val(bs, "browOuterUpLeft") + self._bs_val(bs, "browOuterUpRight")) / 2
        eye_w = (self._bs_val(bs, "eyeWideLeft")    + self._bs_val(bs, "eyeWideRight"))    / 2
        jaw   = self._bs_val(bs, "jawOpen")
        sneer = (self._bs_val(bs, "noseSneerLeft")  + self._bs_val(bs, "noseSneerRight"))  / 2

        scores = {
            "happy":     smile * 0.7 + (1 - brow_d) * 0.3,
            "surprised": brow_u * 0.6 + eye_w * 0.4,
            "angry":     brow_d * 0.6 + frown * 0.4,
            "disgusted": sneer  * 0.7 + brow_d * 0.3,
            "nervous":   frown  * 0.5 + eye_w  * 0.5,
            "neutral":   max(0.0, 1 - smile - frown - brow_d - brow_u - sneer),
        }
        dom = max(scores, key=scores.get)
        return dom, scores

    # ── Posture from Tasks API pose ───────────────────────────────────────────

    def _posture(self, pose_lm, mpi) -> str:
        try:
            res = pose_lm.detect(mpi)
            if not res or not res.pose_landmarks:
                return "unknown"
            lm = res.pose_landmarks[0]
            ls, rs, nose = lm[11], lm[12], lm[0]
            if ls.visibility < 0.4 or rs.visibility < 0.4:
                return "unknown"
            sh_x = (ls.x + rs.x) / 2
            sh_y = (ls.y + rs.y) / 2
            if nose.x - sh_x > 0.06:
                return "leaning_forward"
            if sh_y - nose.y < 0.14:
                return "slouched"
            return "upright"
        except Exception:
            return "unknown"

    # ── Aggregation ───────────────────────────────────────────────────────────

    def _aggregate(self, all_frames, role_map, duration, first_seen) -> VideoAnalysisResult:
        pf: Dict[int, List[FaceFrame]] = defaultdict(list)
        for ffs in all_frames:
            for ff in ffs:
                pf[ff.person_id].append(ff)

        tot_speak = sum(sum(1 for f in ffs if f.is_speaking) for ffs in pf.values())
        result    = VideoAnalysisResult(duration_seconds=duration, person_count=len(pf))

        for pid, ffs in pf.items():
            if not ffs:
                continue
            role = role_map.get(pid, "Unknown")
            ps   = self._score_person(pid, role, ffs, tot_speak)
            ps.appeared_at_second = first_seen.get(pid, 0.0)
            result.persons[pid] = ps

        if len(result.persons) >= 2:
            result.rapport_score      = self._rapport(pf)
            result.talk_balance_score = self._talk_balance(pf)
        else:
            result.rapport_score      = 50.0
            result.talk_balance_score = 50.0

        c_speak = sum(
            sum(1 for f in ffs if f.is_speaking)
            for pid, ffs in pf.items() if role_map.get(pid) == ROLE_CANDIDATE
        )
        result.candidate_talk_pct = round(c_speak / tot_speak * 100, 1) if tot_speak else 0

        cands = [p for p in result.persons.values() if p.role == ROLE_CANDIDATE]
        c_ov  = (sum(
            [p.confidence, p.composure, p.eye_contact, p.engagement, p.energy]
        ) / 5 if cands else 0)
        result.overall_score = round(
            c_ov * 0.5 + result.rapport_score * 0.3 + result.talk_balance_score * 0.2, 1
        )
        result.timeline_data = self._build_timeline(all_frames, role_map)
        result.observations  = self._observations(result, pf, role_map)
        return result

    def _score_person(self, pid, role, ffs, tot_speak) -> PersonScore:
        n         = len(ffs)
        emo_cnt   = defaultdict(int)
        ec_cnt    = sum(1 for f in ffs if f.eye_contact)
        spk_cnt   = sum(1 for f in ffs if f.is_speaking)
        up_cnt    = sum(1 for f in ffs if f.posture in ("upright", "leaning_forward"))
        pos_emo   = sum(1 for f in ffs if f.emotion in ("happy", "neutral"))
        nerv_cnt  = sum(1 for f in ffs if f.emotion == "nervous")
        for f in ffs:
            emo_cnt[f.emotion] += 1

        ec_r  = ec_cnt  / n
        pos_r = pos_emo / n
        nerv_r= nerv_cnt / n
        up_r  = up_cnt  / n
        spk_r = spk_cnt / n
        tlk   = spk_cnt / tot_speak * 100 if tot_speak else 0

        n_d   = len([k for k, v in emo_cnt.items() if v / n > 0.05])
        e_var = min(1.0, n_d / 5)
        energy= min(100.0, (e_var * 0.5 + min(1.0, spk_r * 1.5) * 0.5) * 100)

        conf  = min(100.0, (pos_r*0.4 + ec_r*0.3 + up_r*0.3) * 100)
        comp  = min(100.0, max(0.0, (1 - nerv_r * 1.5) * 100))
        ec    = ec_r * 100
        eng   = min(100.0, (ec_r*0.4 + up_r*0.35 + energy/100*0.25) * 100)
        rec   = min(100.0, (ec_r*0.5 + pos_r*0.5) * 100)

        is_iv = role not in (ROLE_CANDIDATE, "Unknown", "")
        ov    = round((rec + eng) / 2, 1) if is_iv else round(
            (conf + comp + ec + eng + energy) / 5, 1
        )
        dom = max(emo_cnt, key=emo_cnt.get) if emo_cnt else "neutral"

        return PersonScore(
            person_id=pid, role=role,
            confidence=round(conf, 1), composure=round(comp, 1),
            eye_contact=round(ec, 1),  engagement=round(eng, 1),
            energy=round(energy, 1),   receptiveness=round(rec, 1),
            overall=ov, talk_time_pct=round(tlk, 1),
            emotion_distribution={k: round(v/n, 3) for k, v in emo_cnt.items()},
            dominant_emotion=dom,
        )

    def _rapport(self, pf) -> float:
        ids = list(pf.keys())
        if len(ids) < 2:
            return 50.0
        a = {f.timestamp: f for f in pf[ids[0]]}
        b = {f.timestamp: f for f in pf[ids[1]]}
        sh = set(a) & set(b)
        if not sh:
            return 50.0
        em = sum(1 for t in sh if a[t].emotion == b[t].emotion) / len(sh)
        ec = sum(1 for t in sh if a[t].eye_contact and b[t].eye_contact) / len(sh)
        return round(min(100, (em*0.4 + ec*0.6) * 100), 1)

    def _talk_balance(self, pf) -> float:
        tot = {pid: sum(1 for f in ffs if f.is_speaking) for pid, ffs in pf.items()}
        g   = sum(tot.values())
        if g == 0:
            return 50.0
        n   = len(tot)
        dev = sum(abs(v/g - 1/n) for v in tot.values()) / 2
        return round(max(0, min(100, (1 - dev) * 100)), 1)

    def _build_timeline(self, all_frames, role_map) -> List[dict]:
        md: Dict[int, Dict[int, List[str]]] = defaultdict(lambda: defaultdict(list))
        for ffs in all_frames:
            for ff in ffs:
                md[int(ff.timestamp // 60)][ff.person_id].append(ff.emotion)
        tl = []
        for m in sorted(md.keys()):
            entry = {"minute": m, "persons": {}}
            for pid, emos in md[m].items():
                dom = max(set(emos), key=emos.count)
                entry["persons"][pid] = {
                    "dominant_emotion": dom,
                    "role": role_map.get(pid, "Unknown"),
                    "counts": {e: emos.count(e) for e in set(emos)},
                }
            tl.append(entry)
        return tl

    def _observations(self, result, pf, role_map, n=4) -> List[str]:
        obs = []
        cid = next(
            (pid for pid, p in result.persons.items() if p.role == ROLE_CANDIDATE), None
        )
        if cid:
            ffs  = pf[cid]
            half = len(ffs) // 2
            h1, h2 = ffs[:half], ffs[half:]
            if h1 and h2:
                p1 = sum(1 for f in h1 if f.emotion in ("happy","neutral")) / len(h1)
                p2 = sum(1 for f in h2 if f.emotion in ("happy","neutral")) / len(h2)
                if p2 > p1 + 0.15:
                    obs.append("Candidate grew more confident in the second half of the interview.")
                elif p1 > p2 + 0.15:
                    obs.append("Candidate appeared more confident early on — composure dipped toward the end.")

            ec = result.persons[cid].eye_contact
            if ec < 50:
                obs.append(
                    f"Candidate's eye contact was limited ({ec:.0f}%) — "
                    f"maintaining a more direct gaze would project greater confidence."
                )
            elif ec > 75:
                obs.append(
                    f"Candidate maintained strong eye contact ({ec:.0f}%), "
                    f"projecting engagement and confidence."
                )

            nv = sum(1 for f in ffs if f.emotion == "nervous") / max(1, len(ffs)) * 100
            if nv > 30:
                obs.append(
                    f"Nervousness was visible {nv:.0f}% of the time — "
                    f"breathing exercises or mock interviews could help."
                )

        if result.talk_balance_score < 40:
            obs.append("Talk balance was uneven — one party dominated the conversation.")
        elif result.talk_balance_score > 68:
            obs.append("Conversation was well-balanced with both parties contributing roughly equally.")

        if result.rapport_score > 68:
            obs.append("Good rapport was established — participants showed aligned emotional responses.")
        elif result.rapport_score < 42:
            obs.append("Rapport appeared limited — less mutual eye contact and emotional mirroring than ideal.")

        late = [p for p in result.persons.values() if p.appeared_at_second > 30]
        for lp in late:
            m, s = int(lp.appeared_at_second // 60), int(lp.appeared_at_second % 60)
            obs.append(f"{lp.role} joined late (~{m}m {s}s into the session).")

        return obs[:n]

    # ── Annotated video ───────────────────────────────────────────────────────

    def _annotated_video(self, video_path, all_frames, role_map, fps) -> Optional[str]:
        try:
            cap   = cv2.VideoCapture(video_path)
            w     = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h     = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            out_p = tempfile.mktemp(suffix="_annotated.mp4")
            out   = cv2.VideoWriter(out_p, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))

            lut: Dict[int, List[FaceFrame]] = {}
            for ffs in all_frames:
                if ffs:
                    lut[int(ffs[0].timestamp * fps)] = ffs

            idx  = 0
            last: List[FaceFrame] = []
            si   = max(1, int(fps))

            while cap.isOpened():
                ret, frame = cap.read()
                if not ret:
                    break
                snap  = (idx // si) * si
                faces = lut.get(snap, last)
                if faces:
                    last = faces
                out.write(self._draw(frame, last, role_map))
                idx += 1

            cap.release(); out.release()
            return out_p
        except Exception as e:
            print(f"[VideoAnalyzer] annotated video error: {e}")
            return None

    def _draw(self, frame, faces, role_map) -> np.ndarray:
        for ff in faces:
            x, y, fw, fh = ff.bbox
            role  = role_map.get(ff.person_id, "Unknown")
            color = ROLE_COLORS_BGR.get(role, (128, 128, 128))
            thick = 3 if ff.is_speaking else 2
            cv2.rectangle(frame, (x, y), (x+fw, y+fh), color, thick)
            label = f"{role}: {ff.emotion}"
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.52, 1)
            cv2.rectangle(frame, (x, max(0, y-th-10)), (x+tw+8, y), color, -1)
            cv2.putText(frame, label, (x+4, max(th, y-4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.52, (255,255,255), 1, cv2.LINE_AA)
            if ff.eye_contact:
                cv2.putText(frame, "EC", (x+fw-32, y+20),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.48, (50, 255, 50), 2)
            if ff.is_speaking:
                cv2.rectangle(frame, (x-3, y-3), (x+fw+3, y+fh+3), (0, 255, 80), 2)
        return frame

    # ── HTML score cards ──────────────────────────────────────────────────────

    def render_score_cards_html(self, result: VideoAnalysisResult) -> str:
        if result.error:
            return (
                f'<div style="color:#ef4444;padding:16px;font-family:monospace;'
                f'white-space:pre-wrap;">{result.error[:800]}</div>'
            )

        SC = lambda v: (
            "#166534" if v >= 80 else
            "#1d4ed8" if v >= 65 else
            "#92400e" if v >= 50 else "#991b1b"
        )
        BAR = lambda v, c: (
            f'<div style="background:#e2e8f0;border-radius:4px;height:6px;margin-top:4px;">'
            f'<div style="background:{c};height:6px;border-radius:4px;width:{v:.0f}%;"></div></div>'
        )
        MET = lambda lbl, v, c: (
            f'<div style="margin-bottom:10px;">'
            f'<div style="display:flex;justify-content:space-between;font-size:0.8em;">'
            f'<span style="color:#64748b;">{lbl}</span>'
            f'<span style="font-weight:700;color:{c};">{v:.0f}%</span></div>'
            + BAR(v, c) + '</div>'
        )

        cands = [p for p in result.persons.values() if p.role == ROLE_CANDIDATE]
        ivrs  = [p for p in result.persons.values()
                 if p.role not in (ROLE_CANDIDATE, "Unknown", "")]

        html = '<div style="font-family:system-ui,sans-serif;padding:4px 0;">'

        # Overall banner
        ov = result.overall_score
        oc = SC(ov)
        html += (
            f'<div style="background:{oc};border-radius:16px;padding:18px 24px;'
            f'margin-bottom:20px;display:flex;align-items:center;gap:20px;">'
            f'<div style="background:rgba(255,255,255,0.18);border-radius:12px;'
            f'padding:10px 18px;text-align:center;min-width:78px;">'
            f'<div style="font-size:2.5em;font-weight:900;color:#fff;line-height:1;">{ov:.0f}</div>'
            f'<div style="font-size:0.68em;font-weight:700;color:rgba(255,255,255,.75);'
            f'text-transform:uppercase;letter-spacing:.08em;">/ 100</div></div>'
            f'<div><div style="font-size:0.72em;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:.1em;color:rgba(255,255,255,.7);margin-bottom:4px;">'
            f'Overall Session Score</div>'
            f'<div style="font-size:1.1em;font-weight:800;color:#fff;">'
            f'{result.person_count} participant{"s" if result.person_count!=1 else ""} · '
            f'{int(result.duration_seconds//60)}m {int(result.duration_seconds%60)}s</div></div></div>'
        )

        # Candidate card
        for p in cands:
            c = SC(p.overall)
            html += (
                f'<div style="border:2px solid {c};border-radius:14px;padding:18px 20px;'
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
                + MET("Energy Level", p.energy,      SC(p.energy))
                + '</div>'
            )

        # Interviewer cards
        for p in ivrs:
            c = SC(p.overall)
            html += (
                f'<div style="border:2px solid {c};border-radius:14px;padding:18px 20px;'
                f'margin-bottom:16px;background:var(--ta-card-bg,#f8fafc);">'
                f'<div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;">'
                f'<div style="background:{c};border-radius:10px;padding:6px 14px;'
                f'color:#fff;font-weight:800;font-size:0.9em;">{p.role}</div>'
                f'<div style="font-size:1.4em;font-weight:900;color:{c};">{p.overall:.0f}%</div>'
                f'<div style="font-size:0.78em;color:#64748b;margin-left:auto;">'
                f'Talk: {p.talk_time_pct:.0f}% · Mood: {p.dominant_emotion}</div></div>'
                + MET("Receptiveness", p.receptiveness, SC(p.receptiveness))
                + MET("Engagement",    p.engagement,    SC(p.engagement))
                + '</div>'
            )

        # Interaction panel
        ct = result.candidate_talk_pct
        it = round(100 - ct, 1)
        html += (
            '<div style="border:2px solid #3b82f6;border-radius:14px;padding:18px 20px;'
            'margin-bottom:16px;background:var(--ta-card-bg,#f8fafc);">'
            '<div style="font-weight:800;color:#1d4ed8;margin-bottom:12px;font-size:0.95em;">'
            'Interaction</div>'
            + MET("Rapport",      result.rapport_score,     SC(result.rapport_score))
            + MET("Talk Balance", result.talk_balance_score, SC(result.talk_balance_score))
            + f'<div style="font-size:0.78em;color:#64748b;margin-top:6px;">'
              f'Candidate spoke {ct:.0f}% · Interviewer(s) {it:.0f}%</div>'
            '</div>'
        )

        # Observations
        if result.observations:
            html += (
                '<div style="border:1px solid #e2e8f0;border-radius:14px;padding:18px 20px;'
                'margin-bottom:16px;background:var(--ta-card-bg,#f8fafc);">'
                '<div style="font-weight:800;color:#475569;margin-bottom:12px;font-size:0.95em;">'
                'Key Observations</div><ul style="margin:0;padding-left:18px;">'
            )
            for o in result.observations:
                html += f'<li style="color:#374151;font-size:0.88em;margin-bottom:8px;">{o}</li>'
            html += '</ul></div>'

        html += '</div>'
        return html

    # ── Plotly timeline ───────────────────────────────────────────────────────

    def render_timeline_figure(self, result: VideoAnalysisResult):
        try:
            import plotly.graph_objects as go
        except ImportError:
            return None
        if not result.timeline_data:
            return None

        # Collect persons
        all_pids: Dict[int, str] = {}
        for entry in result.timeline_data:
            for pid_raw, data in entry["persons"].items():
                pid = int(pid_raw)
                if pid not in all_pids:
                    all_pids[pid] = data.get("role", "Unknown")

        minutes = [e["minute"] for e in result.timeline_data]
        fig = go.Figure()

        for pid, role in all_pids.items():
            emo_seq, col_seq, hover = [], [], []
            for entry in result.timeline_data:
                pdata = entry["persons"].get(pid) or entry["persons"].get(str(pid))
                if pdata:
                    emo    = pdata["dominant_emotion"]
                    counts = pdata.get("counts", {})
                    total  = sum(counts.values()) or 1
                    detail = ", ".join(
                        f"{e}: {c/total*100:.0f}%"
                        for e, c in sorted(counts.items(), key=lambda x: -x[1])
                    )
                else:
                    emo    = "none"
                    detail = "not in frame"
                emo_seq.append(emo)
                col_seq.append(EMOTION_COLORS_HTML.get(emo, "#94a3b8"))
                hover.append(f"Minute {entry['minute']}<br>{role}<br><b>{emo}</b><br>{detail}")

            fig.add_trace(go.Scatter(
                x=minutes, y=[role] * len(minutes),
                mode="markers",
                marker=dict(size=24, color=col_seq, symbol="square",
                            line=dict(width=1, color="rgba(0,0,0,0.15)")),
                text=hover, hoverinfo="text", name=role,
            ))

        # Emotion legend
        seen = set()
        for entry in result.timeline_data:
            for pd in entry["persons"].values():
                if isinstance(pd, dict):
                    seen.add(pd.get("dominant_emotion", "neutral"))
        for emo in seen:
            fig.add_trace(go.Scatter(
                x=[None], y=[None], mode="markers",
                marker=dict(size=12, color=EMOTION_COLORS_HTML.get(emo, "#94a3b8"), symbol="square"),
                name=emo, showlegend=True,
            ))

        fig.update_layout(
            title="Emotion Timeline (per minute)",
            xaxis=dict(title="Minute", dtick=1, gridcolor="#e2e8f0"),
            yaxis=dict(title=""),
            plot_bgcolor="white", paper_bgcolor="white",
            height=max(200, 120 + len(all_pids) * 70),
            margin=dict(l=10, r=10, t=40, b=30),
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
        )
        return fig
