#!/usr/bin/env python3
"""
FastAPI Face Recognition Server with Streaming Results, Image Upload, and Auto-Resizing
Features: Image upload with auto-resizing, listing, and streaming face recognition.
"""

from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from insightface.app import FaceAnalysis
import onnxruntime as ort
from numpy.linalg import norm
import numpy as np
import os
import asyncio
from PIL import Image
import io
from fastapi import UploadFile, File, Form, HTTPException, FastAPI
import boto3
from datetime import datetime
import time
import json
from typing import List, Tuple
from models.weblink import WebLinks
from models.folders import Folder, SubFolder
import uuid
import cv2
from sklearn.cluster import DBSCAN
from fastapi import BackgroundTasks

from database import connect_db
from dotenv import load_dotenv
load_dotenv()
from fastapi import BackgroundTasks
from fastapi import Form, HTTPException
# from eventFaceFinder import router as event_router
import gc 
from datetime import datetime
import requests
import bson
from mongoengine.queryset.visitor import Q  
from datetime import datetime, timedelta
import mediapipe as mp
import threading


import psutil
import os

def print_memory(label):
    process = psutil.Process(os.getpid())
    mem = process.memory_full_info()

    rss_mb = mem.rss / (1024 * 1024)
    uss_mb = getattr(mem, "uss", 0) / (1024 * 1024)

    print(f"\n========== {label} ==========")
    print(f"RSS: {rss_mb:.2f} MB | USS: {uss_mb:.2f} MB")
    print("============================\n")


s3_client = boto3.client('s3') 
BUCKET_NAME = "photography-hora"
AWS_REGION = os.getenv("AWS_REGION", "eu-north-1")
S3_BUCKET = os.getenv("S3_BUCKET_NAME", "photography-hora")




def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]

s3 = boto3.client("s3", region_name=AWS_REGION)
from concurrent.futures import ThreadPoolExecutor

EXECUTOR = ThreadPoolExecutor(max_workers=3)



app = FastAPI(title="Face Recognition Server", version="1.0.0")

ORIENTATION_ANGLES = [0, 90, 180, 270]

ORIENTATION_MIN_FACE_SCORE = 0.50

# InsightFace confidence rules
ORIENTATION_HIGH_CONFIDENCE = 0.90
ORIENTATION_MIN_CONFIDENCE = 0.82
ORIENTATION_MIN_MARGIN = 0.12

# MediaPipe confidence rules
MEDIAPIPE_MIN_CONFIDENCE = 0.82
MEDIAPIPE_MIN_MARGIN = 0.10

EXIF_ORIENTATION_TO_ROTATION = {
    1: 0,
    3: 180,
    6: 90,
    8: 270,
}

_face_landmarker = None
_face_landmarker_lock = threading.Lock()


def rotate_for_orientation(image, angle):
    """
    OpenCV rotation.
    """
    if angle == 0:
        return image

    if angle == 90:
        return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)

    if angle == 180:
        return cv2.rotate(image, cv2.ROTATE_180)

    if angle == 270:
        return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)

    return image


def calculate_face_upright_score(face):
    """
    Face landmarks + bounding box dono use karta hai.
    Bounding box se "sir vs daadi" (head-top vs chin) signal
    milta hai jo landmark-noise se zyada robust hai —
    khaaskar tilted/embrace poses ke liye.
    """
    try:
        kps = getattr(face, "kps", None)
        bbox = getattr(face, "bbox", None)

        if kps is None or bbox is None:
            return 0.0

        kps = np.asarray(kps)
        if kps.shape[0] < 5:
            return 0.0

        left_eye = kps[0]
        right_eye = kps[1]
        nose = kps[2]
        left_mouth = kps[3]
        right_mouth = kps[4]

        eye_distance = np.linalg.norm(right_eye - left_eye)
        if eye_distance < 1:
            return 0.0

        x1, y1, x2, y2 = bbox
        box_height = y2 - y1
        if box_height < 1:
            return 0.0

        eye_center_y = (left_eye[1] + right_eye[1]) / 2
        eye_position_ratio = (eye_center_y - y1) / box_height

        head_position_score = float(
            np.clip(1.0 - (eye_position_ratio / 0.5), 0.0, 1.0)
        )
        eye_vertical_difference = abs(left_eye[1] - right_eye[1])
        eye_alignment = 1.0 - min(eye_vertical_difference / eye_distance, 1.0)

        mouth_center_y = (left_mouth[1] + right_mouth[1]) / 2

        nose_offset_ratio = (nose[1] - eye_center_y) / eye_distance
        nose_score = float(np.clip(nose_offset_ratio / 0.3, 0.0, 1.0))

        mouth_offset_ratio = (mouth_center_y - nose[1]) / eye_distance
        mouth_score = float(np.clip(mouth_offset_ratio / 0.3, 0.0, 1.0))

        min_eye_x = min(left_eye[0], right_eye[0])
        max_eye_x = max(left_eye[0], right_eye[0])
        nose_center_score = 1.0 if min_eye_x <= nose[0] <= max_eye_x else 0.0
        score = (
            head_position_score * 0.15 +
            nose_score * 0.30 +
            mouth_score * 0.30 +
            eye_alignment * 0.15 +
            nose_center_score * 0.10
        )

        return float(score)

    except Exception as e:
        print(f"⚠️ Orientation face score error: {e}")
        return 0.0


def score_orientation_angle(image, angle):
    """
    Ek angle ko test karta hai.
    """
    rotated = rotate_for_orientation(image, angle)

    faces = searcher.app.get(rotated)

    valid_faces = []

    for face in faces:
        detection_score = float(getattr(face, "det_score", 0))

        if detection_score < ORIENTATION_MIN_FACE_SCORE:
            continue

        orientation_score = calculate_face_upright_score(face)

        valid_faces.append({
            "detection": detection_score,
            "orientation": orientation_score
        })

    if not valid_faces:
        return {"angle": angle, "score": 0.0, "faces": 0, "weighted_total": 0.0}

    scores = [item["orientation"] for item in valid_faces]
    average_score = float(np.mean(scores))
    weighted_total = float(sum(
        item["detection"] * item["orientation"] for item in valid_faces
    ))

    return {
        "angle": angle,
        "score": average_score,
        "faces": len(valid_faces),
        "weighted_total": weighted_total
    }



def get_face_landmarker():
    """
    MediaPipe model lazy initialize hoga aur memory mein ek hi baar rahega.
    """
    global _face_landmarker

    if _face_landmarker is None:
        with _face_landmarker_lock:
            if _face_landmarker is None:
                model_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "face_landmarker.task"
                )

                if not os.path.exists(model_path):
                    raise FileNotFoundError(
                        f"MediaPipe model not found: {model_path}"
                    )

                BaseOptions = mp.tasks.BaseOptions
                FaceLandmarker = mp.tasks.vision.FaceLandmarker
                FaceLandmarkerOptions = mp.tasks.vision.FaceLandmarkerOptions
                RunningMode = mp.tasks.vision.RunningMode

                options = FaceLandmarkerOptions(
                    base_options=BaseOptions(model_asset_path=model_path),
                    running_mode=RunningMode.IMAGE,
                    num_faces=10,
                    min_face_detection_confidence=0.5,
                    min_face_presence_confidence=0.5,
                    min_tracking_confidence=0.5
                )

                _face_landmarker = FaceLandmarker.create_from_options(
                    options
                )
                print_memory("AFTER MEDIAPIPE MODEL LOAD")

                print("✅ MediaPipe Face Landmarker initialized")

    return _face_landmarker


def calculate_mediapipe_upright_score(landmarks):
    """
    MediaPipe facial landmarks se upright score calculate karta hai.
    Score 0 se 1 ke beech hota hai.
    """

    try:
        # MediaPipe Face Landmarker landmark indices
        left_eye = landmarks[33]
        right_eye = landmarks[263]
        nose = landmarks[1]
        left_mouth = landmarks[61]
        right_mouth = landmarks[291]

        eye_distance = np.sqrt(
            (right_eye.x - left_eye.x) ** 2 +
            (right_eye.y - left_eye.y) ** 2
        )

        if eye_distance < 0.001:
            return 0.0

        eye_center_y = (left_eye.y + right_eye.y) / 2
        mouth_center_y = (left_mouth.y + right_mouth.y) / 2

        eye_alignment = 1.0 - min(
            abs(left_eye.y - right_eye.y) / eye_distance,
            1.0
        )

        nose_score = float(np.clip(
            (nose.y - eye_center_y) / (eye_distance * 0.30),
            0.0,
            1.0
        ))

        mouth_score = float(np.clip(
            (mouth_center_y - nose.y) / (eye_distance * 0.30),
            0.0,
            1.0
        ))

        nose_center_score = (
            1.0
            if min(left_eye.x, right_eye.x) <= nose.x <= max(left_eye.x, right_eye.x)
            else 0.0
        )

        score = (
            eye_alignment * 0.20 +
            nose_score * 0.35 +
            mouth_score * 0.35 +
            nose_center_score * 0.10
        )

        return float(score)

    except Exception as e:
        print(f"⚠️ MediaPipe orientation score error: {e}")
        return 0.0


def score_mediapipe_orientation_angle(image, angle):
    """
    MediaPipe se ek orientation angle test karta hai.
    """

    rotated = rotate_for_orientation(image, angle)

    rgb_image = cv2.cvtColor(rotated, cv2.COLOR_BGR2RGB)

    mp_image = mp.Image(
        image_format=mp.ImageFormat.SRGB,
        data=rgb_image
    )

    landmarker = get_face_landmarker()

    with _face_landmarker_lock:
        result = landmarker.detect(mp_image)
    print_memory("AFTER MEDIAPIPE INFERENCE")

    if not result.face_landmarks:
        return {
            "angle": angle,
            "score": 0.0,
            "faces": 0,
            "weighted_total": 0.0
        }

    scores = [
        calculate_mediapipe_upright_score(face_landmarks)
        for face_landmarks in result.face_landmarks
    ]

    valid_scores = [s for s in scores if s > 0]

    if not valid_scores:
        return {
            "angle": angle,
            "score": 0.0,
            "faces": 0,
            "weighted_total": 0.0
        }

    average_score = float(np.mean(valid_scores))

    return {
        "angle": angle,
        "score": average_score,
        "faces": len(valid_scores),
        "weighted_total": average_score * len(valid_scores)
    }



def detect_image_orientation(original_image, content_bytes=None):

    # STEP 1: EXIF check first
    if content_bytes is not None:
        has_exif, exif_rotation = get_exif_rotation(content_bytes)

        if has_exif:
            print(f"📐 EXIF orientation found: {exif_rotation}°")

            return {
                "rotation": exif_rotation,
                "confidence": 1.0,
                "faces": 0,
                "autoRotated": exif_rotation != 0,
                "reason": "exif_orientation",
                "candidates": []
            }

    try:
        return _detect_image_orientation(
               original_image,
               content_bytes=content_bytes
        )

    finally:
        del original_image



def _detect_image_orientation(image, content_bytes=None):

    candidates = []
    mediapipe_candidates = []
    valid_candidates = []
    valid_mp_candidates = []

    print("\n🔍 INSIGHTFACE ORIENTATION CHECK")

    # =========================================
    # STAGE 1: FIRST CHECK ONLY 0 DEGREE
    # =========================================

    result = score_orientation_angle(image, 0)
    candidates.append(result)

    print(
        f"InsightFace | Angle=0° | "
        f"Score={result['score']:.4f} | "
        f"Faces={result['faces']}"
    )

    # Normal image: no need to check other angles
    if (
        result["faces"] > 0
        and result["score"] >= ORIENTATION_MIN_CONFIDENCE
    ):

        print("✅ Normal orientation detected. Skipping remaining angles.")

        return {
            "rotation": 0,
            "confidence": round(result["score"], 4),
            "faces": result["faces"],
            "autoRotated": False,
            "reason": "orientation_detected",
            "candidates": candidates
        }

    # =========================================
    # STAGE 2: CHECK REMAINING ANGLES ONLY
    # IF 0 DEGREE IS UNCERTAIN
    # =========================================

    print("⚠️ 0° uncertain. Checking remaining angles...")

    for angle in [90, 180, 270]:

        result = score_orientation_angle(image, angle)
        candidates.append(result)

        print(
            f"InsightFace | Angle={angle}° | "
            f"Score={result['score']:.4f} | "
            f"Faces={result['faces']}"
        )

        # High confidence: return immediately
        if (
            result["faces"] > 0
            and result["score"] >= ORIENTATION_HIGH_CONFIDENCE
        ):

            print(f"✅ HIGH CONFIDENCE: {angle}°")

            return {
                "rotation": angle,
                "confidence": round(result["score"], 4),
                "faces": result["faces"],
                "autoRotated": angle != 0,
                "reason": "orientation_detected",
                "candidates": candidates
            }

    # =========================================
    # STAGE 3: CHECK INSIGHTFACE RESULTS
    # =========================================

    valid_candidates = [
        c for c in candidates if c["faces"] > 0
    ]

    valid_candidates.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    if valid_candidates:

        best = valid_candidates[0]

        second_score = (
            valid_candidates[1]["score"]
            if len(valid_candidates) > 1
            else 0.0
        )

        margin = best["score"] - second_score

        if (
            best["score"] >= ORIENTATION_MIN_CONFIDENCE
            and margin >= ORIENTATION_MIN_MARGIN
        ):

            print(
                f"✅ InsightFace CONFIDENT: {best['angle']}°"
            )

            return {
                "rotation": best["angle"],
                "confidence": round(best["score"], 4),
                "faces": best["faces"],
                "autoRotated": best["angle"] != 0,
                "reason": "orientation_detected",
                "candidates": candidates
            }

    # =========================================
    # STAGE 4: MEDIAPIPE FALLBACK
    # =========================================

    print(
        "⚠️ InsightFace uncertain. "
        "Starting MediaPipe fallback..."
    )

    try:

        for angle in ORIENTATION_ANGLES:

            result = score_mediapipe_orientation_angle(
                image,
                angle
            )

            mediapipe_candidates.append(result)

            print(
                f"MediaPipe | Angle={angle}° | "
                f"Score={result['score']:.4f} | "
                f"Faces={result['faces']}"
            )

        valid_mp_candidates = [
            c for c in mediapipe_candidates
            if c["faces"] > 0
        ]

        valid_mp_candidates.sort(
            key=lambda x: x["score"],
            reverse=True
        )

        if valid_mp_candidates:

            best_mp = valid_mp_candidates[0]

            second_mp_score = (
                valid_mp_candidates[1]["score"]
                if len(valid_mp_candidates) > 1
                else 0.0
            )

            mp_margin = best_mp["score"] - second_mp_score

            if (
                best_mp["score"] >= MEDIAPIPE_MIN_CONFIDENCE
                and mp_margin >= MEDIAPIPE_MIN_MARGIN
            ):

                print(
                    f"✅ MediaPipe CONFIDENT: {best_mp['angle']}°"
                )

                return {
                    "rotation": best_mp["angle"],
                    "confidence": round(best_mp["score"], 4),
                    "faces": best_mp["faces"],
                    "autoRotated": best_mp["angle"] != 0,
                    "reason": "orientation_detected",
                    "candidates": candidates
                }

    except Exception as e:

        print(f"⚠️ MediaPipe fallback failed: {e}")

    # =========================================
    # STAGE 5: EXIF FALLBACK IF NO FACE
    # =========================================

    has_exif = False
    exif_rotation = 0

    if content_bytes is not None:

        has_exif, exif_rotation = get_exif_rotation(
            content_bytes
        )

    insight_faces = max(
        (c["faces"] for c in candidates),
        default=0
    )

    mp_faces = max(
        (c["faces"] for c in mediapipe_candidates),
        default=0
    )

    total_detected_faces = max(
        insight_faces,
        mp_faces
    )

    if total_detected_faces == 0:

        if has_exif and exif_rotation != 0:

            print(
                f"📐 No face detected. "
                f"Using EXIF: {exif_rotation}°"
            )

            return {
                "rotation": exif_rotation,
                "confidence": 0,
                "faces": 0,
                "autoRotated": True,
                "reason": "no_face_exif_fallback",
                "candidates": candidates
            }

        return {
            "rotation": 0,
            "confidence": 0,
            "faces": 0,
            "autoRotated": False,
            "reason": "no_face_detected",
            "candidates": candidates
        }

    # =========================================
    # STAGE 6: STILL UNCERTAIN
    # =========================================

    print("⚠️ Both models uncertain. Returning uncertain result.")

    best_confidence = 0.0

    if valid_candidates:
        best_confidence = valid_candidates[0]["score"]

    if valid_mp_candidates:
        best_confidence = max(
            best_confidence,
            valid_mp_candidates[0]["score"]
        )

    return {
        "rotation": 0,
        "confidence": round(best_confidence, 4),
        "faces": total_detected_faces,
        "autoRotated": False,
        "reason": "uncertain",
        "candidates": candidates
    }





def get_exif_rotation(content_bytes):

    """
    Returns (has_tag: bool, rotation: int)
    """

    try:

        img = Image.open(io.BytesIO(content_bytes))
        exif = img.getexif()

        orientation_tag = exif.get(274)

        if orientation_tag is None:
            return False, 0

        # Only supported orientation values
        if orientation_tag not in EXIF_ORIENTATION_TO_ROTATION:
            return False, 0

        rotation = EXIF_ORIENTATION_TO_ROTATION[orientation_tag]

        return True, rotation

    except Exception as e:

        print(f"⚠️ EXIF read error: {e}")

        return False, 0

    finally:

        if "img" in locals():
            img.close()




@app.post("/detect-orientation")
async def detect_orientation_api(file: UploadFile = File(...)):
    try:
        print("\n===================================")
        print("🤖 ORIENTATION API REQUEST")
        print("Filename:", file.filename)
        print("===================================")

        content = await file.read()

        array = np.frombuffer(content, dtype=np.uint8)
        image = cv2.imdecode(
            array,
            cv2.IMREAD_IGNORE_ORIENTATION | cv2.IMREAD_COLOR
        )

        if image is None:
            return {
                "success": False,
                "filename": file.filename,
                "rotation": 0,
                "confidence": 0,
                "autoRotated": False,
                "reason": "invalid_image"
            }

        print("Original image size:", image.shape[1], "x", image.shape[0])

        result = detect_image_orientation(image, content_bytes=content)

        return {
            "success": True,
            "filename": file.filename,
            **result
        }

    except Exception as error:
        print("❌ ORIENTATION API ERROR:", str(error))
        return {
            "success": False,
            "filename": file.filename,
            "rotation": 0,
            "confidence": 0,
            "faces": 0,
            "autoRotated": False,
            "reason": "python_error",
            "error": str(error)
        }




def encode_crop_for_memory(crop, quality=85):
    """
    NumPy image ko compressed JPEG bytes mein convert karta hai.
    """
    if crop is None:
        return None

    success, encoded = cv2.imencode(
        ".jpg",
        crop,
        [int(cv2.IMWRITE_JPEG_QUALITY), quality]
    )

    if not success:
        return None

    return encoded.tobytes()


def decode_crop_from_memory(crop_bytes):
    """
    Compressed JPEG bytes ko wapas NumPy image banata hai.
    """
    if not crop_bytes:
        return None

    image_array = np.frombuffer(
        crop_bytes,
        dtype=np.uint8
    )

    return cv2.imdecode(
        image_array,
        cv2.IMREAD_COLOR
    )



app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# app.include_router(event_router)
        

def delete_subfolder_by_id(subfolder_id: str):
    try:
        result = Folder.objects(
            subFolders___id=subfolder_id
        ).update_one(
            pull__subFolders__id=subfolder_id
        )

        if result == 0:
            print(f"⚠️ SubFolder not found: {subfolder_id}")
        else:
            print(f"🗑️ SubFolder deleted: {subfolder_id}")

    except Exception as e:
        print(f"❌ Failed to delete subfolder: {e}")



class FaceSearcher:
    def __init__(self, min_similarity: float = 0.3):
        self.min_similarity = min_similarity

        session_options = ort.SessionOptions()
        session_options.intra_op_num_threads = 2
        session_options.inter_op_num_threads = 1
        session_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL

        session_options.enable_cpu_mem_arena = False
        session_options.enable_mem_pattern = False

        self.app = FaceAnalysis(
        name="buffalo_s",
        allowed_modules=["detection", "recognition", "landmark_3d_68"],
        providers=["CPUExecutionProvider"],
        sess_options=session_options,
    )
        self.app.prepare(ctx_id=0, det_size=(640, 640))
        print_memory("AFTER INSIGHTFACE LOAD")

    def build_s3_prefix(
        self,
        folder_name: str,
        customer_id: str,
        vendor_id: str | None = None,
    ) -> str:
        return (
            f"{folder_name}"
        )

    async def stream_search_batch(
        self,
        s3_prefix: str,
        reference_embedding: np.ndarray,
        subFolderId: str,
    ):
        loop = asyncio.get_running_loop()

        # 1️⃣ LIST S3 IMAGES
        t0 = time.perf_counter()
        try:
         image_keys = list_s3_images(s3_prefix)
        except Exception as e:
         yield f"data: ❌ S3 ERROR | {str(e)}\n\n"
         return

        print(
            f"[{ts()}] 📂 S3 LIST DONE | "
            f"Images: {len(image_keys)} | "
            f"{time.perf_counter() - t0:.2f}s"
        )

        # Tunables
        BATCH_SIZE = 5
        DOWNLOAD_LIMIT = 4
        SCAN_LIMIT = 3

        download_sem = asyncio.Semaphore(DOWNLOAD_LIMIT)
        scan_sem = asyncio.Semaphore(SCAN_LIMIT) 

         
        batches = [
            image_keys[i : i + BATCH_SIZE]
            for i in range(0, len(image_keys), BATCH_SIZE)
        ]

        print(f"[{ts()}] 🧩 Total batches: {len(batches)}")

        tasks = []
        match_count = 0

        
        for batch_id, keys in enumerate(batches, start=1):

            async def handle_batch(
                batch_id=batch_id,
                keys=keys,
            ):
                # 🔽 DOWNLOAD STAGE
                async with download_sem:
                    print(
                        f"[{ts()}] 📥 Batch-{batch_id} "
                        f"S3 DOWNLOAD START"
                    )

                    s3_tasks = [
                        read_s3_image_async(key, loop)
                        for key in keys
                    ]
                    imgs = await asyncio.gather(*s3_tasks)

                    print(
                        f"[{ts()}] 📥 Batch-{batch_id} "
                        f"DOWNLOAD DONE"
                    )

                batch_imgs = list(zip(keys, imgs))

                
                async with scan_sem:
                    print(
                        f"[{ts()}] 🔍 Batch-{batch_id} "
                        f"SCAN START"
                    )

                    start = time.perf_counter()
                    results = await loop.run_in_executor(
                        EXECUTOR,
                        process_image_batch,
                        self.app,
                        batch_imgs,
                        reference_embedding,
                        self.min_similarity,
                    )

                    print(
                        f"[{ts()}] ✅ Batch-{batch_id} SCAN DONE | "
                        f"{time.perf_counter() - start:.2f}s | "
                        f"Matches NEW ONE -------------------: {len(results)}"
                    )

                return batch_id, results

            tasks.append(asyncio.create_task(handle_batch()))

        
        for coro in asyncio.as_completed(tasks):
            batch_id, results = await coro

            for fname, confidence in results:
                match_count += 1
                WebLinks.objects(thumbnailKey=fname).update_one(
                add_to_set__folderIds = subFolderId
                )
                payload ={
                    "type": "match",
                    "matchNo": match_count,
                    "batch": batch_id,
                    "file": fname,
                    "confidence": float(round(float(confidence), 2))
                }
                yield f"data: {json.dumps(payload)}\n\n"

        complete_payload = {
        "type": "complete",
        "totalMatches": match_count
        }

        yield f"data: {json.dumps(complete_payload)}\n\n"


# -------------------------------------------------
# S3 HELPERS
# -------------------------------------------------
def list_s3_images(prefix: str) -> List[str]:
    """
    Return list of S3 object keys under a prefix
    that are ⁠ .webp ⁠ images and contain ⁠ /thumb_ ⁠.
    """
    keys = []
    paginator = s3.get_paginator("list_objects_v2")

    for page in paginator.paginate(
        Bucket=S3_BUCKET,
        Prefix=prefix,
    ):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            key_lower = key.lower()

            if key_lower.endswith(".webp") and "/thumb_" in key_lower:
                keys.append(key)

    return keys


def read_s3_image(key: str) -> np.ndarray | None:
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        img_bytes = obj["Body"].read()

        pil_img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        return np.array(pil_img)

    except Exception as e:
        print(f"[{ts()}] ⚠️ Failed to read {key}: {e}")
        return None


async def read_s3_image_async(key: str, loop):
    return await loop.run_in_executor(
        EXECUTOR,
        read_s3_image,
        key,
    )


# -------------------------------------------------
# Batch processing
# -------------------------------------------------
def process_image_batch(
    app: FaceAnalysis,
    batch: List[Tuple[str, np.ndarray]],
    reference_embedding: np.ndarray,
    min_similarity: float,
):
    results = []

    for filename, img in batch:
        if img is None:
            continue

        faces = app.get(img)
        for face in faces:
            sim = np.dot(reference_embedding, face.embedding) / (
                norm(reference_embedding) * norm(face.embedding)
            )
            if sim >= min_similarity:
                results.append((filename, sim * 100))
    return results

searcher = FaceSearcher()



@app.on_event("startup")
async def startup_event():
    # MongoDB connect
    connect_db()
    print("✅ MongoDB connected")
    

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "timestamp": ts()}
   
@app.post("/search")
async def search_faces_s3(
    sample_image: UploadFile = File(...),
    folder_name: str = Form(...),
    customer_id: str = Form(...),
    subFolderId: str = Form(...),
    vendor_id: str | None = Form(None),
):
    """
    S3-based face search endpoint.

    - sample_image: image file to search for
    - folder_name: album/folder name in S3
    - customer_id: customer identifier
    - vendor_id: optional vendor identifier
    """

    # -----------------------------
    # Step 1: Process sample image
    # -----------------------------
    try:
        content = await sample_image.read()
        img = Image.open(io.BytesIO(content)).convert("RGB")

        faces = searcher.app.get(np.array(img))
        if not faces:
            raise HTTPException(
                status_code=400,
                detail="No face found in sample image",
            )

        reference_embedding = faces[0].embedding

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Error processing sample image: {str(e)}",
        )

    # --------------------------------
    # Step 2: Build S3 prefix dynamically
    # --------------------------------
    s3_prefix = searcher.build_s3_prefix(
        folder_name=folder_name,
        customer_id=customer_id,
        vendor_id=vendor_id,
    )

    # -----------------------------
    # Step 3: Stream search results
    # -----------------------------
    return StreamingResponse(
        searcher.stream_search_batch(s3_prefix, reference_embedding, subFolderId),
        media_type="text/event-stream",
    )

face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')


def find_vips_using_cooccurrence(person_clusters: list, total_images_count: int) -> list:
    """
    Identifies VIPs based on shared frames with the primary VIP (P1).
    """
    if not person_clusters:
        return []

    sorted_groups = sorted(person_clusters, key=lambda x: x["count"], reverse=True)
    
    min_photos_threshold = max(int(total_images_count * 0.10), 10)
    
    p1_group = sorted_groups[0]
    if p1_group["count"] < min_photos_threshold:
        return [p1_group]

    p1_images = set(p1_group["images"])
    vip_groups = [p1_group]

    candidates = sorted_groups[1:]

    for candidate in candidates:
        if candidate["count"] < min_photos_threshold:
            continue
        
        candidate_images = set(candidate["images"])
        
        shared_photos = p1_images.intersection(candidate_images)
        shared_count = len(shared_photos)
        
        co_occurrence_ratio = shared_count / float(candidate["count"])
        shared_with_p1_ratio = shared_count / float(p1_group["count"])

        print(f"🔍 Co-occurrence Check for {candidate['folderName']}:")
        print(f"   ├─ Total Photos    : {candidate['count']}")
        print(f"   ├─ Shared with P1  : {shared_count}")
        print(f"   └─ Co-occurrence % : {co_occurrence_ratio * 100:.1f}%")

        if co_occurrence_ratio >= 0.25 or shared_with_p1_ratio >= 0.15:
            vip_groups.append(candidate)
            print(f"   ✅ CONFIRMED VIP: {candidate['folderName']}")
            
            if len(vip_groups) >= 3:
                break
        else:
            print(f"   ❌ REJECTED (High solo photos, but not co-occurring with P1)")

    return vip_groups



def get_best_banner_image(image_keys: list, expected_vip_count: int) -> str:
    if not image_keys:
        return None
    if len(image_keys) == 1:
        return image_keys[0]

    best_key = image_keys[0]
    best_score = -1.0

    candidates_to_test = image_keys[:20]
    BATCH_SIZE = 10

    for batch_start in range(0, len(candidates_to_test), BATCH_SIZE):
        batch_candidates = candidates_to_test[batch_start : batch_start + BATCH_SIZE]

        for key in batch_candidates:
            try:
                link_doc = WebLinks.objects(originalKey=key).first() or WebLinks.objects(thumbnailKey=key).first()
                target_s3_key = (link_doc.thumbnailKey if link_doc and link_doc.thumbnailKey else key)

                s3_obj = s3_client.get_object(Bucket=BUCKET_NAME, Key=target_s3_key)
                img_bytes = s3_obj['Body'].read()

                nparr = np.frombuffer(img_bytes, np.uint8)
                img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

                if img is None:
                    continue

                height, width, _ = img.shape
                aspect_ratio = width / float(height)

                if aspect_ratio < 1.1: 
                    del img_bytes, nparr, img
                    continue

                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                sharpness = cv2.Laplacian(gray, cv2.CV_64F).var()
                megapixels = (width * height) / 1000000.0

                faces = face_cascade.detectMultiScale(
                    gray, 
                    scaleFactor=1.1, 
                    minNeighbors=5, 
                    minSize=(20, 20)
                )
                detected_faces_count = len(faces)

                if detected_faces_count == expected_vip_count:
                    face_score_multiplier = 2.0 
                elif detected_faces_count > expected_vip_count:
                    extra_faces = detected_faces_count - expected_vip_count
                    face_score_multiplier = max(0.2, 1.0 - (extra_faces * 0.3))
                else:
                    face_score_multiplier = 0.5 if detected_faces_count > 0 else 0.2

                aspect_ratio_multiplier = min(aspect_ratio, 1.8) 
                score = ((sharpness * 0.6) + (megapixels * 20.0)) * face_score_multiplier * aspect_ratio_multiplier

                if score > best_score:
                    best_score = score
                    best_key = key

                del img_bytes, nparr, img, gray

            except Exception as e:
                print(f"⚠️ Image score error for {key}: {str(e)}")
                continue

        gc.collect()

    return best_key


# =========================================================
# 2. STRICT VIP-ONLY FILTER
# =========================================================
def filter_strict_vip_photos(candidate_keys: list, exact_vip_count: int) -> tuple[list, bool]:
    strict_candidates = []

    for key in candidate_keys:
        link_doc = WebLinks.objects(originalKey=key).first() or WebLinks.objects(thumbnailKey=key).first()
        if link_doc and link_doc.folderIds:
            if len(link_doc.folderIds) == exact_vip_count:
                strict_candidates.append(key)

    if strict_candidates:
        return strict_candidates, True
    
    return candidate_keys, False


# =========================================================
# 3. MAIN BANNER GENERATOR FUNCTION
# =========================================================
def generate_and_save_folder_banner(folderId: str) -> dict:
    try:
        folder_doc = Folder.objects(id=folderId).first()
        if not folder_doc:
            print(f"❌ Banner Error: Folder ID {folderId} not found")
            return {"success": False, "message": "Folder not found"}

        folder_name = getattr(folder_doc, 'folderName', 'N/A')
        order_id = getattr(folder_doc, 'orderId', 'N/A')

        print("\n==================================================")
        print("🚀 AUTOMATIC BANNER GENERATION STARTED")
        print(f"📌 Folder ID   : {folderId}")
        print(f"📂 Folder Name : {folder_name}")
        print(f"📦 Order ID          : {int(order_id) + 10800 if str(order_id).isdigit() else order_id}")
        print("==================================================\n")

        subfolders = folder_doc.subFolders or []
        person_subfolders = [
            sub for sub in subfolders if getattr(sub, 'isPersonFolder', False)
        ]

        if not person_subfolders:
            print("⚠️ No person subfolders found for banner selection.")
            return {"success": False, "message": "No person subfolders found"}

        person_clusters = []
        all_event_images = set()

        for sub in person_subfolders:
            sub_id = str(sub._id)
            links = WebLinks.objects(folderIds=sub_id)
            
            valid_links = []
            for link in links:
                key = link.originalKey or link.thumbnailKey
                url = link.originalUrl or link.thumbnailImageUrl
                if key and url:
                    valid_links.append({"key": key, "url": url})

            if valid_links:
                image_keys = [item["key"] for item in valid_links]
                person_clusters.append({
                    "subFolderId": sub_id,
                    "folderName": sub.folderName,
                    "count": len(valid_links),
                    "images": image_keys,
                    "links": valid_links
                })
                all_event_images.update(image_keys)

        total_images_count = len(all_event_images)

        if not person_clusters:
            print("⚠️ Tagged WebLinks not found for subfolders.")
            return {"success": False, "message": "No tagged WebLinks found"}

        vip_groups = find_vips_using_cooccurrence(person_clusters, total_images_count)
        vip_count = len(vip_groups)

        print(f"\n🌟 TOTAL VIPs IDENTIFIED: {vip_count}")
        for v in vip_groups:
            print(f"   ⭐ VIP Name: {v['folderName']} (Photos: {v['count']})")

        main_persons_image_sets = [set(vip["images"]) for vip in vip_groups]
        selected_banner_key = None

        if vip_count > 1:
            common_all = set.intersection(*main_persons_image_sets)
            if common_all:
                clean_candidates, _ = filter_strict_vip_photos(list(common_all), exact_vip_count=vip_count)
                selected_banner_key = get_best_banner_image(clean_candidates, expected_vip_count=vip_count)

            if not selected_banner_key and vip_count >= 3:
                top_2_common = set.intersection(main_persons_image_sets[0], main_persons_image_sets[1])
                if top_2_common:
                    clean_candidates, _ = filter_strict_vip_photos(list(top_2_common), exact_vip_count=2)
                    selected_banner_key = get_best_banner_image(clean_candidates, expected_vip_count=2)

            if not selected_banner_key:
                union_all = set.union(*main_persons_image_sets)
                clean_candidates, _ = filter_strict_vip_photos(list(union_all), exact_vip_count=vip_count)
                selected_banner_key = get_best_banner_image(clean_candidates, expected_vip_count=vip_count)

        elif vip_count == 1:
            clean_candidates, _ = filter_strict_vip_photos(list(main_persons_image_sets[0]), exact_vip_count=1)
            selected_banner_key = get_best_banner_image(clean_candidates, expected_vip_count=1)
        else:
            sorted_groups = sorted(person_clusters, key=lambda x: x["count"], reverse=True)
            selected_banner_key = get_best_banner_image(sorted_groups[0]["images"], expected_vip_count=1)

        banner_url = None
        if selected_banner_key:
            banner_doc = WebLinks.objects(originalKey=selected_banner_key).first() or \
                         WebLinks.objects(thumbnailKey=selected_banner_key).first()
            if banner_doc:
                banner_url = banner_doc.thumbnailImageUrl or banner_doc.originalUrl

                folder_doc.selectedForBanner = banner_doc.thumbnailImageUrl or banner_doc.originalUrl
                folder_doc.updatedAt = datetime.utcnow()
                folder_doc.save()

        if banner_url:
            return {"success": True, "bannerUrl": banner_url, "selectedKey": selected_banner_key}
        
        return {"success": False, "message": "Banner key found but URL missing"}

    except Exception as e:
        print(f"❌ Error in generate_and_save_folder_banner: {str(e)}")
        return {"success": False, "error": str(e)}

    
EPS = 0.6
MIN_SAMPLES = 2
MERGE_THRESHOLD = 0.50  
STALE_LOCK_MINUTES = 15
BLUR_THRESHOLD = 300.0
 
 # =========================================================
# MOBILENETV3 ONNX MODEL INITIALIZATION
# =========================================================


MODEL_PATH = os.path.join(
    os.path.dirname(__file__),
    "models",
    "mobilenetv3_anime_real.onnx"
)

session_options = ort.SessionOptions()
session_options.intra_op_num_threads = 2
session_options.inter_op_num_threads = 1

session_options.enable_cpu_mem_arena = False
session_options.enable_mem_pattern = False

session_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

mobilenet_session = ort.InferenceSession(
    MODEL_PATH,
    sess_options=session_options,
    providers=["CPUExecutionProvider"]
)

MOBILENET_INPUT = mobilenet_session.get_inputs()[0].name
MOBILENET_OUTPUT = mobilenet_session.get_outputs()[0].name
print_memory("AFTER MOBILENET LOAD")

print("✅ MobileNetV3 loaded successfully")
 

def upload_face_crop(face_crop, folder_id, person_id):
    h, w = face_crop.shape[:2]
    MIN_DP_SIZE = 250
    if max(h, w) < MIN_DP_SIZE:
        scale = MIN_DP_SIZE / max(h, w)
        new_w, new_h = int(w * scale), int(h * scale)
        face_crop = cv2.resize(
            face_crop, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4
        )
        if scale > 1.8:
            face_crop = sharpen_crop(face_crop)

    buffer = io.BytesIO()
    Image.fromarray(face_crop).save(
        buffer,
        format="WEBP",
        quality=95,
        method=6
    )
    buffer.seek(0)

    crop_key = f"face-groups/{folder_id}/{person_id}.webp"
    s3.upload_fileobj(
        buffer,
        S3_BUCKET,
        crop_key,
        ExtraArgs={"ContentType": "image/webp"}
    )

    crop_url = f"https://{S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{crop_key}"
    return crop_key, crop_url

def compute_sharpness(face_crop):
    """
    Sirf sharpness measure karta hai (Laplacian variance), koi side-effect nahi.
    Isse best_crop selection me det_score ke saath-saath sharpness bhi factor banta hai.
    """
    try:
        gray = cv2.cvtColor(face_crop, cv2.COLOR_RGB2GRAY)
        return cv2.Laplacian(gray, cv2.CV_64F).var()
    except Exception:
        return 0.0

def is_side_face(face_obj):
    try:
        # 1️⃣ Agar InsightFace ne direct Pose Angle (Yaw) detect kiya hai
        pose = getattr(face_obj, 'pose', None)
        if pose is not None and len(pose) >= 2:
            yaw = abs(pose[1])  # Yaw = Head rotation angle (Left/Right)
            if yaw > 22.0:      # 22 degree se zyaada tilted face ko side face maano
                return True
 
        # 2️⃣ Keypoint (Landmark) Asymmetry Check
        kps = getattr(face_obj, 'kps', None)
        if kps is None or len(kps) < 5:
            # Agar landmarks clearly detect nahi hue (extreme side profile)
            return True
 
        left_eye, right_eye, nose = kps[0], kps[1], kps[2]
 
        dist_left = np.linalg.norm(nose - left_eye)
        dist_right = np.linalg.norm(nose - right_eye)
 
        max_dist = max(dist_left, dist_right)
        if max_dist == 0:
            return True
 
        asymmetry_ratio = abs(dist_left - dist_right) / max_dist
 
        # 💡 Threshold ko 0.32 se ghata kar 0.20 kar diya hai
        # Cropped Face DPs me 20% asymmetry ka matlab clearly Profile / Side Face hai
        return asymmetry_ratio > 0.20
 
    except Exception as e:
        print(f"⚠️ is_side_face check failed: {e}")
        return False

def is_real_human_face_batch(face_crops, threshold=0.70, batch_size=8):
    results = []

    for start in range(0, len(face_crops), batch_size):
        batch = face_crops[start:start + batch_size]
        processed = []

        for crop in batch:
            try:
                if crop is None or crop.size == 0:
                    processed.append(None)
                    continue

                image = Image.fromarray(crop.astype(np.uint8)).convert("RGB")

                image = image.resize(
                    (384, 384),
                    Image.Resampling.BILINEAR
                )

                data = np.asarray(image, dtype=np.float32) / 255.0

                data = np.transpose(data, (2, 0, 1))
                data = (data - 0.5) / 0.5

                processed.append(data.astype(np.float32))

            except Exception as e:
                print(f"⚠️ MobileNet preprocessing failed: {e}")
                processed.append(None)

        valid_indices = [
            i for i, item in enumerate(processed)
            if item is not None
        ]

        if not valid_indices:
            results.extend([(False, 0.0)] * len(batch))
            continue

        input_batch = np.stack(
            [processed[i] for i in valid_indices]
        ).astype(np.float32)

        try:
            outputs = mobilenet_session.run(
                [MOBILENET_OUTPUT],
                {MOBILENET_INPUT: input_batch}
            )[0]

            for i, output in enumerate(outputs):
                # Model labels: anime = 0, real = 1
                real_score = float(output[1])

                # Output is class scores; convert to probability.
                scores = np.exp(output - np.max(output))
                probabilities = scores / np.sum(scores)

                real_probability = float(probabilities[1])

                processed[valid_indices[i]] = (
                    real_probability >= threshold,
                    real_probability
                )

        except Exception as e:
            print(f"❌ MobileNet inference failed: {e}")

            for idx in valid_indices:
                processed[idx] = (False, 0.0)

        for item in processed:
            if item is None:
                results.append((False, 0.0))
            else:
                results.append(item)

    return results

_SMILE_CASCADE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_smile.xml")


def is_smiling_face(face_crop):
    """
    face_crop: RGB numpy array (tight recognition crop)
    Return: True agar smile detect hua, warna False
    """
    try:
        gray = cv2.cvtColor(face_crop, cv2.COLOR_RGB2GRAY)
        gray = cv2.equalizeHist(gray)  
        smiles = _SMILE_CASCADE.detectMultiScale(
            gray,
            scaleFactor=1.7,
            minNeighbors=22,
            minSize=(int(gray.shape[1] * 0.3), int(gray.shape[0] * 0.15))
        )
        return len(smiles) > 0
    except Exception as e:
        print(f"⚠️ is_smiling_face check failed: {e}")
        return False


def sharpen_crop(img):
    """
    Heavy upscale ke baad blockiness kam dikhane ke liye halka unsharp-mask.
    """
    try:
        blurred = cv2.GaussianBlur(img, (0, 0), sigmaX=2)
        sharpened = cv2.addWeighted(img, 1.4, blurred, -0.4, 0)
        return sharpened
    except Exception:
        return img

    
def is_blurry_face(face_crop, threshold=BLUR_THRESHOLD):
    try:
        gray = cv2.cvtColor(face_crop, cv2.COLOR_RGB2GRAY)
        lap_var = cv2.Laplacian(gray, cv2.CV_64F).var()
        print(f"🔬 BLUR VARIANCE = {lap_var:.2f} (threshold={threshold})")
        return lap_var < threshold
    except Exception as e:
        print(f"⚠️ is_blurry_face check failed: {e}")
        return False


def find_matching_person(new_embedding, existing_people):
    """
    new_embedding: 1D np.array (naye cluster ka representative embedding)
    existing_people: list of {"sub_id": str, "embedding": np.array}
    Returns matched sub_id agar koi existing person match kare, warna None
    """
    if not existing_people or new_embedding is None:
        print("⚠️ find_matching_person: existing_people empty ya new_embedding None hai — skip")
        return None

    best_sub_id = None
    best_dist = float("inf")

    new_vec = new_embedding / (np.linalg.norm(new_embedding) + 1e-8)

    # 🆕 DEBUG: har existing person ke against distance collect karo
    all_distances = []

    for person in existing_people:
        existing_vec = person["embedding"]
        if existing_vec is None or len(existing_vec) == 0:
            continue
        existing_vec = existing_vec / (np.linalg.norm(existing_vec) + 1e-8)
        cosine_dist = 1 - np.dot(new_vec, existing_vec)

        all_distances.append((person["sub_id"], round(float(cosine_dist), 4)))

        if cosine_dist < best_dist:
            best_dist = cosine_dist
            best_sub_id = person["sub_id"]

    # 🆕 DEBUG: saare distances ek line me print karo (readable format)
    if all_distances:
        print(f"📏 DISTANCES this cluster vs existing people -> {all_distances}")

    if best_dist <= MERGE_THRESHOLD:
        print(f"🔗 MATCH FOUND -> sub_id={best_sub_id} (distance={round(best_dist, 4)}, threshold={MERGE_THRESHOLD})")
        return best_sub_id

    # 🆕 DEBUG LOG: match fail hua toh bhi best distance print karo.
    # Isse pata chalega ki threshold kitna badhana chahiye — bina isse
    # hum blindly guess kar rahe the.
    if best_sub_id is not None:
        print(f"❌ NO MATCH (closest was sub_id={best_sub_id}, "
              f"distance={round(best_dist, 4)}, threshold={MERGE_THRESHOLD}) "
              f"-> naya person banega")

    return None


def compute_cluster_embedding(group_data):
    """
    Cluster ka final "representative" embedding nikaalta hai.
    Is run ke cluster ke saare FRONTAL (non-side) face embeddings ka
    MEAN nikaala jaata hai -> zyada stable representative.
    Extra DB storage NAHI lagti, final me ek hi vector (same size) return hota hai.
    """
    frontal_embs = group_data.get("all_frontal_embeddings") or []
    if len(frontal_embs) >= 1:
        stacked = np.array(frontal_embs)
        mean_emb = stacked.mean(axis=0)
        return mean_emb
    return group_data["best_embedding"]


def update_running_centroid(old_embedding, old_count, new_embedding):
    """
    MERGE hone par existing person ka stored embedding "running weighted
    average" (centroid) ki tarah update hota hai. Storage same rehta hai.
    """
    old_embedding = np.array(old_embedding, dtype=np.float64)
    new_embedding = np.array(new_embedding, dtype=np.float64)

    weight_old = max(1, old_count)
    weight_new = 1

    updated = (old_embedding * weight_old + new_embedding * weight_new) / (weight_old + weight_new)
    norm = np.linalg.norm(updated)
    if norm > 0:
        updated = updated / norm
    return updated


# =========================================================
# CONCURRENCY LOCK (folderId-specific, alag orders parallel chalte rahenge)
# =========================================================
def try_acquire_clustering_lock(folderId, isLastBatch=False):
    """
    NO SCHEMA CHANGE (mostly): 'clusteringStatus' aur 'updatedAt' se lock hota hai.
    Naya field 'pendingLastBatch' add kiya hai taaki agar isLastBatch=True wali
    call skip ho jaaye (kyunki dusra run chal raha tha), toh uska flag kho na jaaye.
    """
    now = datetime.utcnow()

    result = Folder.objects(
        id=folderId,
        clusteringStatus__ne="IN_PROGRESS"
    ).update_one(
        set__clusteringStatus="IN_PROGRESS",
        set__updatedAt=now
    )

    if result:
        return True

    # Lock nahi mila -> agar ye call isLastBatch=True thi, toh uska
    # flag folder doc pe save kar do taaki jo run currently chal raha
    # hai, wo baad me isko dekh kar banner generate kar sake.
    if isLastBatch:
        Folder.objects(id=folderId).update_one(
            set__pendingLastBatch=True
        )
        print(f"📌 isLastBatch flag SAVED as pending for folder {folderId} (lock busy tha)")

    folder_doc = Folder.objects(id=folderId).first()
    if not folder_doc:
        return False

    last_updated = folder_doc.updatedAt or now
    is_stale = (now - last_updated) > timedelta(minutes=STALE_LOCK_MINUTES)

    if is_stale:
        print(f"⚠️ STALE LOCK detected for folder {folderId}. Force-acquiring.")
        Folder.objects(id=folderId).update_one(
            set__clusteringStatus="IN_PROGRESS",
            set__updatedAt=now
        )
        return True

    print(f"⏩ SKIPPING trigger for folder {folderId} — clustering already IN_PROGRESS")
    return False
 
 
def cleanup_small_side_face_folders(folderId):
    try:
        folder_doc = Folder.objects(id=folderId).first()
        if not folder_doc:
            print("❌ Folder not found in cleanup")
            return
 
        subfolders_to_keep = []
        removed_count = 0
 
        for subfolder in folder_doc.subFolders:
            if not subfolder.isPersonFolder:
                subfolders_to_keep.append(subfolder)
                continue
 
            sub_id = str(subfolder._id)
 
            # ✅ Live/real-time tagged count — WebLinks DB se (source of truth)
            actual_tagged_count = WebLinks.objects(folderIds=sub_id).count()
 
            is_side = getattr(subfolder, "isSideFace", None)
            should_delete = False
 
            # 🎯 Core Requirement: isSideFace == True AND tagged count <= 2
            if is_side is True and actual_tagged_count <= 2:
                should_delete = True
                print(f"🔍 SIDE-FACE (Count={actual_tagged_count}): Deleting subfolder {sub_id}")
 
            # Final Action Block
            if should_delete:
                removed_count += 1
                s3_key = getattr(subfolder.folderDp, "s3Key", None) if subfolder.folderDp else None
 
                # 1. Delete S3 Crop Image
                if s3_key:
                    try:
                        s3.delete_object(Bucket=S3_BUCKET, Key=s3_key)
                        print(f"🗑️ S3 crop deleted: {s3_key}")
                    except Exception as s3_err:
                        print(f"⚠️ S3 delete failed for {sub_id}: {s3_err}")
 
                # 2. Untag WebLinks DB
                try:
                    WebLinks.objects(folderIds=sub_id).update(pull__folderIds=sub_id)
                except Exception as tag_err:
                    print(f"⚠️ Untagging failed for {sub_id}: {tag_err}")
 
                print(f"🗑️ SUCCESSFULLY REMOVED Side-Face Folder (ID: {sub_id}, Tagged Count: {actual_tagged_count})")
            else:
                # Keep folder and sync real count
                subfolder.personCount = actual_tagged_count
                subfolders_to_keep.append(subfolder)
 
        # Database update if any folder removed
        if removed_count > 0:
            folder_doc.subFolders = subfolders_to_keep
            folder_doc.totalPersonCount = max(0, (folder_doc.totalPersonCount or 0) - removed_count)
            folder_doc.save()
            print(f"✅ Cleanup completed — Total {removed_count} side-face folder(s) removed.")
        else:
            print("✅ Cleanup completed — No side-face folders with <=2 images found.")
 
    except Exception as e:
        print(f"❌ Error in cleanup_small_side_face_folders: {str(e)}")
 
def process_face_clustering_in_background(image_keys, folderId, userId, folder_name, isLastBatch=False):
    if not try_acquire_clustering_lock(folderId, isLastBatch):
        print(f"⏩ Background task SKIPPED for folder {folderId} — already in progress")
        return
    try:
        BATCH_SIZE = 10
        DISPLAY_PAD_RATIO = 0.60
        MIN_GOOD_FACE_SIZE = 90

        print(f"Total Images Found = {len(image_keys)}")

        # =====================================================
        # STAGE 0: LOAD EXISTING PERSONS FIRST
        # =====================================================
        folder_doc = Folder.objects(id=folderId).first()

        if not folder_doc:
            print(f"❌ Folder not found: {folderId}")
            return

        existing_people = []

        for sf in folder_doc.subFolders:
            sf_embedding = getattr(sf, "embedding", None)

            if (
                getattr(sf, "isPersonFolder", False)
                and sf_embedding is not None
                and len(sf_embedding) > 0
            ):
                existing_people.append({
                    "sub_id": str(sf._id),
                    "embedding": np.asarray(sf_embedding, dtype=np.float32),
                    "sf": sf,
                })

        print(
            f"🔎 Existing persons loaded FIRST: "
            f"{len(existing_people)}"
        )

        valid_subfolders = []
        merged_count = 0
        removed_side_count = 0

        # =====================================================
        # HELPER: SELECT BEST FACE FROM A GROUP
        # =====================================================
        def build_group(group_metadata):
            group = {
                "images": [],
                "best_crop_key": None,
                "best_crop_coords": None,
                "best_display_coords": None,
                "max_score": -1,
                "best_sharpness": -1,
                "best_is_side": True,
                "best_is_smile": False,
                "best_face_size": 0,
                "best_embedding": None,
                "all_frontal_embeddings": [],
            }

            for metadata in group_metadata:
                key = metadata["key"]

                if key not in group["images"]:
                    group["images"].append(key)

                if not metadata["is_side"]:
                    group["all_frontal_embeddings"].append(
                        metadata["embedding"]
                    )

                # Best face selection ke liye image load
                image = read_s3_image(key)

                if image is None:
                    continue

                x1 = metadata["crop_x1"]
                y1 = metadata["crop_y1"]
                x2 = metadata["crop_x2"]
                y2 = metadata["crop_y2"]

                crop = image[y1:y2, x1:x2]

                if crop.size == 0:
                    continue

                sharpness = compute_sharpness(crop)
                face_size = min(
                    metadata["face_w"],
                    metadata["face_h"]
                )

                new_priority = (
                    1 if face_size >= MIN_GOOD_FACE_SIZE else 0,
                    0 if metadata["is_side"] else 1,
                    1 if metadata["is_smile"] else 0,
                )

                old_priority = (
                    1 if group["best_face_size"] >= MIN_GOOD_FACE_SIZE else 0,
                    0 if group["best_is_side"] else 1,
                    1 if group["best_is_smile"] else 0,
                )

                should_replace = False

                if group["best_crop_key"] is None:
                    should_replace = True

                elif new_priority > old_priority:
                    should_replace = True

                elif new_priority == old_priority:
                    det_close = (
                        metadata["det_score"]
                        >= group["max_score"] - 0.05
                    )

                    if (
                        sharpness > group["best_sharpness"]
                        and det_close
                    ):
                        should_replace = True

                    elif (
                        metadata["det_score"]
                        > group["max_score"] + 0.05
                    ):
                        should_replace = True

                if should_replace:
                    group["best_crop_key"] = key

                    group["best_crop_coords"] = (
                        x1, y1, x2, y2
                    )

                    group["best_display_coords"] = (
                        metadata["disp_x1"],
                        metadata["disp_y1"],
                        metadata["disp_x2"],
                        metadata["disp_y2"],
                    )

                    group["max_score"] = metadata["det_score"]
                    group["best_sharpness"] = sharpness
                    group["best_is_side"] = metadata["is_side"]
                    group["best_is_smile"] = metadata["is_smile"]
                    group["best_face_size"] = face_size
                    group["best_embedding"] = metadata["embedding"]

            if group["best_crop_key"] is None:
                return None

            return group

        # =====================================================
        # STAGE 1: PROCESS IMAGES 10-10
        # =====================================================
        for batch_number, start in enumerate(
            range(0, len(image_keys), BATCH_SIZE),
            start=1
        ):
            keys = image_keys[start:start + BATCH_SIZE]

            print(
                f"\n📦 Processing Batch {batch_number} "
                f"| Images: {len(keys)}"
            )

            print_memory(f"BATCH {batch_number} - BEFORE IMAGE LOAD")


            imgs = [read_s3_image(k) for k in keys]

            batch_images = {
                key: img
                for key, img in zip(keys, imgs)
                if img is not None
            }

            print_memory(f"BATCH {batch_number} - AFTER IMAGE LOAD")

            batch_candidates = []

            for img, key in zip(imgs, keys):
                if img is None:
                    continue

                faces = searcher.app.get(img)

                for face in faces:
                    det_score = float(
                        getattr(face, "det_score", 1.0)
                    )

                    if det_score < 0.65:
                        continue

                    x1, y1, x2, y2 = map(int, face.bbox)

                    face_w = x2 - x1
                    face_h = y2 - y1

                    if face_w < 35 or face_h < 35:
                        continue

                    h, w = img.shape[:2]

                    pad_x = int(face_w * 0.20)
                    pad_y = int(face_h * 0.20)

                    crop_x1 = max(0, x1 - pad_x)
                    crop_y1 = max(0, y1 - pad_y)
                    crop_x2 = min(w, x2 + pad_x)
                    crop_y2 = min(h, y2 + pad_y)

                    face_crop = img[
                        crop_y1:crop_y2,
                        crop_x1:crop_x2
                    ]

                    if face_crop.size == 0:
                        continue

                    disp_pad_x = int(face_w * DISPLAY_PAD_RATIO)
                    disp_pad_y = int(face_h * DISPLAY_PAD_RATIO)

                    disp_x1 = max(0, x1 - disp_pad_x)
                    disp_y1 = max(0, y1 - disp_pad_y)
                    disp_x2 = min(w, x2 + disp_pad_x)
                    disp_y2 = min(h, y2 + disp_pad_y)

                    if is_blurry_face(face_crop):
                        print(f"🚫 Blurry face rejected: {key}")
                        continue

                    batch_candidates.append({
                        "key": key,
                        "crop_x1": crop_x1,
                        "crop_y1": crop_y1,
                        "crop_x2": crop_x2,
                        "crop_y2": crop_y2,
                        "disp_x1": disp_x1,
                        "disp_y1": disp_y1,
                        "disp_x2": disp_x2,
                        "disp_y2": disp_y2,
                        "det_score": det_score,
                        "embedding": np.asarray(
                            face.embedding,
                            dtype=np.float32
                        ),
                        "is_side": is_side_face(face),
                        "is_smile": is_smiling_face(face_crop),
                        "face_w": face_w,
                        "face_h": face_h,
                    })

            print_memory(f"BATCH {batch_number} - AFTER INSIGHTFACE")


            if not batch_candidates:
                print("⚠️ No face candidates in this batch")

                del imgs
                del batch_images
                del batch_candidates
                continue

            # =================================================
            # STAGE 2: MOBILENET HUMAN FACE FILTER
            # =================================================
            crops = [
                batch_images[c["key"]][
                    c["crop_y1"]:c["crop_y2"],
                    c["crop_x1"]:c["crop_x2"]
                ]
                for c in batch_candidates
            ]

            human_results = is_real_human_face_batch(crops)

            print_memory(f"BATCH {batch_number} - AFTER MOBILENET")

            valid_candidates = []

            for candidate, (is_human, score) in zip(
                batch_candidates,
                human_results
            ):
                if not is_human:
                    print(
                        f"🚫 Non-human rejected "
                        f"(score={score:.2f}): {candidate['key']}"
                    )
                    continue

                valid_candidates.append(candidate)

            del crops
            del human_results
            del batch_candidates
            del imgs
            del batch_images

            if not valid_candidates:
                continue

            # =================================================
            # STAGE 3: EXISTING PERSON MATCHING FIRST
            # =================================================
            matched_groups = {}
            unmatched_candidates = []

            for candidate in valid_candidates:
                matched_sub_id = find_matching_person(
                    candidate["embedding"],
                    existing_people
                )

                if matched_sub_id:
                    matched_groups.setdefault(
                        matched_sub_id, []
                    ).append(candidate)

                    print(
                        f"✅ Existing person matched: "
                        f"{matched_sub_id} | {candidate['key']}"
                    )
                else:
                    unmatched_candidates.append(candidate)

            # =================================================
            # STAGE 4: TAG MATCHED EXISTING PERSONS
            # =================================================
            for sub_id, matched_faces in matched_groups.items():

                group_keys = list({
                    face["key"] for face in matched_faces
                })

                group_filenames = [
                    key.split("/")[-1]
                    for key in group_keys
                    if "/" in key
                ]

                try:
                    folder_filter = Q(mainFolderId=folderId)

                    key_filter = (
                        Q(thumbnailKey__in=group_keys)
                        | Q(originalKey__in=group_keys)
                        | Q(thumbnailKey__in=group_filenames)
                        | Q(originalKey__in=group_filenames)
                    )

                    WebLinks.objects(
                        folder_filter & key_filter
                    ).update(add_to_set__folderIds=sub_id)

                except Exception as e:
                    print(
                        f"❌ Existing person tagging failed "
                        f"{sub_id}: {e}"
                    )

                actual_count = WebLinks.objects(
                    folderIds=sub_id
                ).count()

                person_data = next(
                    (
                        p for p in existing_people
                        if p["sub_id"] == sub_id
                    ),
                    None
                )

                if person_data:
                    sf = person_data["sf"]

                    old_count = sf.personCount or 1

                    frontal_embeddings = [
                        f["embedding"]
                        for f in matched_faces
                        if not f["is_side"]
                    ]

                    # Frontal embeddings ko preference
                    if frontal_embeddings:
                        new_embedding = np.mean(
                            np.asarray(
                                frontal_embeddings,
                                dtype=np.float32
                            ),
                            axis=0
                        )
                    else:
                        new_embedding = matched_faces[0]["embedding"]

                    updated_embedding = update_running_centroid(
                        person_data["embedding"],
                        old_count,
                        new_embedding
                    )

                    person_data["embedding"] = np.asarray(
                        updated_embedding,
                        dtype=np.float32
                    )

                    sf.embedding = list(
                        map(float, updated_embedding)
                    )

                    sf.personCount = actual_count

                merged_count += 1

                print(
                    f"🔁 Existing person updated: {sub_id} "
                    f"| Tagged count: {actual_count}"
                )

            # =================================================
            # STAGE 5: DBSCAN ONLY ON UNMATCHED FACES
            # =================================================
            if unmatched_candidates:

                print(
                    f"🧩 Unmatched faces for DBSCAN: "
                    f"{len(unmatched_candidates)}"
                )

                unmatched_embeddings = np.asarray(
                    [
                        c["embedding"]
                        for c in unmatched_candidates
                    ],
                    dtype=np.float32
                )

                clustering = DBSCAN(
                    eps=EPS,
                    min_samples=MIN_SAMPLES,
                    metric="cosine"
                )

                labels = clustering.fit_predict(
                    unmatched_embeddings
                )

                del clustering
                del unmatched_embeddings

                batch_groups = {}


                for label, candidate in zip(labels, unmatched_candidates):

                    if label == -1:
                       print(
                        f"⏭️ DBSCAN noise face skipped: {candidate['key']}"
                       )
                       continue

                    group_id = int(label)

                    batch_groups.setdefault(
                        group_id, []
                    ).append(candidate)

                print(
                    f"🧩 New batch clusters: "
                    f"{len(batch_groups)}"
                )

                # =============================================
                # STAGE 6: CREATE ONLY UNMATCHED NEW PERSONS
                # =============================================
                for group_id, group_candidates in batch_groups.items():

                    group_data = build_group(group_candidates)

                    if not group_data:
                        continue

                    representative_embedding = (
                        compute_cluster_embedding(group_data)
                    )

                    group_keys = group_data["images"]

                    group_filenames = [
                        key.split("/")[-1]
                        for key in group_keys
                        if "/" in key
                    ]

                    best_image = read_s3_image(
                        group_data["best_crop_key"]
                    )

                    if best_image is None:
                        print(
                            f"⚠️ Best image not found: "
                            f"{group_data['best_crop_key']}"
                        )
                        continue

                    x1, y1, x2, y2 = (
                        group_data["best_crop_coords"]
                    )

                    dx1, dy1, dx2, dy2 = (
                        group_data["best_display_coords"]
                    )

                    face_crop = best_image[y1:y2, x1:x2]
                    display_crop = best_image[dy1:dy2, dx1:dx2]

                    if (
                        face_crop.size == 0
                        or display_crop.size == 0
                    ):
                        print(
                            f"⚠️ Invalid crop for group {group_id}"
                        )
                        continue

                    person_id = str(uuid.uuid4())

                    crop_key, crop_url = upload_face_crop(
                        display_crop,
                        folderId,
                        person_id
                    )

                    del best_image
                    del face_crop
                    del display_crop

                    sub_id = str(bson.ObjectId())

                    try:
                        folder_filter = Q(mainFolderId=folderId)

                        key_filter = (
                            Q(thumbnailKey__in=group_keys)
                            | Q(originalKey__in=group_keys)
                            | Q(thumbnailKey__in=group_filenames)
                            | Q(originalKey__in=group_filenames)
                        )

                        WebLinks.objects(
                            folder_filter & key_filter
                        ).update(add_to_set__folderIds=sub_id)

                    except Exception as e:
                        print(
                            f"❌ New person tagging failed "
                            f"{sub_id}: {e}"
                        )

                    actual_count = WebLinks.objects(
                        folderIds=sub_id
                    ).count()

                    # Existing side-face cleanup behavior
                    if (
                        group_data["best_is_side"] is True
                        and actual_count <= 2
                    ):
                        removed_side_count += 1

                        print(
                            f"🔍 SIDE-FACE FILTER "
                            f"(Count={actual_count}): {sub_id}"
                        )

                        try:
                            s3.delete_object(
                                Bucket=S3_BUCKET,
                                Key=crop_key
                            )
                        except Exception as e:
                            print(
                                f"⚠️ S3 crop delete failed: {e}"
                            )

                        WebLinks.objects(
                            folderIds=sub_id
                        ).update(pull__folderIds=sub_id)

                        continue

                    subfolder = SubFolder(
                        _id=sub_id,
                        folderName="Person",
                        type="others",
                        userId=userId,
                        personCount=actual_count,
                        isPersonFolder=True,
                        isSideFace=group_data["best_is_side"],
                        embedding=list(
                            map(float, representative_embedding)
                        ),
                        folderDp={
                            "fileUrl": crop_url,
                            "thumbnailUrl": crop_url,
                            "s3Key": crop_key,
                            "thumbnailKey": crop_key,
                        }
                    )

                    valid_subfolders.append(subfolder)

                    # IMPORTANT:
                    # Naya person immediately existing_people mein add.
                    # Next batch isi embedding se match kar sakta hai.
                    existing_people.append({
                        "sub_id": sub_id,
                        "embedding": np.asarray(
                            representative_embedding,
                            dtype=np.float32
                        ),
                        "sf": subfolder,
                    })

                    print(
                        f"🆕 New person created: {sub_id} "
                        f"| Batch: {batch_number}"
                    )

            del valid_candidates
            del unmatched_candidates
            del matched_groups

            gc.collect()

            print_memory(f"BATCH {batch_number} - AFTER CLEANUP")

        # =====================================================
        # STAGE 7: FINAL DB SAVE
        # =====================================================
        folder_doc = Folder.objects(id=folderId).first()

        if not folder_doc:
            print("❌ Folder not found during final save")
            return

        current_person_count = folder_doc.totalPersonCount or 0

        folder_doc.subFolders.extend(valid_subfolders)

        folder_doc.totalPersonCount = (
            current_person_count + len(valid_subfolders)
        )

        folder_doc.uniqueFaceCount = (
            (folder_doc.uniqueFaceCount or 0)
            + len(valid_subfolders)
        )

        # Existing matched persons ki updated embeddings/counts save
        for person in existing_people:
            sf = person["sf"]

            for db_sf in folder_doc.subFolders:
                if str(db_sf._id) == person["sub_id"]:
                    db_sf.personCount = sf.personCount
                    db_sf.embedding = sf.embedding
                    break

        folder_doc.save()

        print(
            f"✅ FINAL SAVE COMPLETED "
            f"| New: {len(valid_subfolders)} "
            f"| Merged: {merged_count} "
            f"| Side filtered: {removed_side_count}"
        )

        # =====================================================
        # STAGE 8: LAST BATCH / BANNER HANDLING
        # =====================================================
        folder_doc_check = Folder.objects(id=folderId).first()

        pending_flag = (
            getattr(folder_doc_check, "pendingLastBatch", False)
            if folder_doc_check else False
        )

        if pending_flag:
            print(
                "📌 Pending last batch found — "
                "fresh processing trigger"
            )

            Folder.objects(id=folderId).update_one(
                set__pendingLastBatch=False,
                set__clusteringStatus="DONE"
            )

            fresh_image_keys = list_s3_images(folder_name)

            process_face_clustering_in_background(
                fresh_image_keys,
                folderId,
                userId,
                folder_name,
                isLastBatch=True
            )

            return

        if isLastBatch:
            print("🎨 Last batch received, generating banner...")

            raw_order_id = getattr(
                folder_doc, "orderId", None
            )

            display_order_id = (
                int(raw_order_id) + 10800
                if str(raw_order_id).isdigit()
                else raw_order_id
            )

            Folder.objects(id=folderId).update_one(
                set__clusteringStatus="DONE"
            )

            print(
                f"🎨 Generating banner for Order: "
                f"{display_order_id}"
            )

            banner_result = generate_and_save_folder_banner(
                folderId=folderId
            )

            if banner_result.get("success"):
                banner_url = banner_result.get("bannerUrl")

                print(
                    f"🎉 Banner automatically assigned: "
                    f"{banner_url}"
                )

                NODE_API_URL = (
                    "https://horaservices.com/api/internal/generate-banner"
                )

                try:
                    img_response = requests.get(
                        banner_url,
                        timeout=10
                    )
                    img_response.raise_for_status()

                    image_bytes = io.BytesIO(
                        img_response.content
                    )

                    response = requests.post(
                        NODE_API_URL,
                        data={"folderId": str(folderId)},
                        files={
                            "leftImage": (
                                "left_image.jpg",
                                image_bytes,
                                "image/jpeg"
                            )
                        },
                        timeout=30
                    )

                    res_data = response.json()

                    if (
                        response.status_code == 200
                        and res_data.get("success")
                    ):
                        print(
                            "✅ Node.js Canvas Banner Generated & Saved"
                        )
                    else:
                        print(
                            f"❌ Node.js API Error: "
                            f"{res_data.get('error') or res_data.get('message')}"
                        )

                    image_bytes.close()

                except Exception as req_err:
                    print(
                        f"❌ Node.js Banner API Error: {req_err}"
                    )

            else:
                print("⚠️ Banner generation failed")

        else:
            Folder.objects(id=folderId).update_one(
                set__clusteringStatus="DONE"
            )

            print(
                "⏩ Not last batch, banner generation skipped"
            )

    except Exception as e:
        print(
            f"❌ Error in background face recognition: {str(e)}"
        )

        Folder.objects(id=folderId).update_one(
            set__clusteringStatus="FAILED"
        )


 
@app.post("/count-unique-persons")
async def count_unique_persons(
    background_tasks: BackgroundTasks,
    folder_name: str = Form(...),
    folderId: str = Form(...),
    userId: str = Form(...),
    isLastBatch: bool = Form(False),
):
    try:
        image_keys = list_s3_images(folder_name)
 
        if not image_keys:
            print(f"⚠️ [API] No images found in S3 bucket for folder: {folder_name} (ID: {folderId})")
            return {"success": False, "message": "No images found in S3 bucket"}
 
        # Folder doc se orderId aur folderName fetch kar rahe hain for logging
        folder_doc = Folder.objects(id=folderId).first()
        orderId = getattr(folder_doc, "orderId", "N/A") if folder_doc else "N/A"
        display_name = getattr(folder_doc, "folderName", folder_name) if folder_doc else folder_name
 
        print("\n" + "📥" * 30)
        print(f"📌 [API RECEIVED] Count Unique Persons Triggered")
        print(f"📁 Folder Name : {display_name}")
        print(f"🆔 Folder ID   : {folderId}")
        print(f"📦 Order ID    : {orderId}")  # ✅ Using orderId key
        print(f"👤 Customer ID : {userId}")  # ✅ Using customerId
        print(f"🖼️ S3 Images   : {len(image_keys)} files found")
        print(f"isLast Batch.    : {isLastBatch}")
        print("📥" * 30 + "\n")


        print(f"=======================.   :{isLastBatch} =================")
        background_tasks.add_task(
            process_face_clustering_in_background,
            image_keys,
            folderId,
            userId,  # ✅ Passing customerId as userId parameter
            folder_name,
            isLastBatch,
        )
 
        return {
            "success": True,
            "message": "Face recognition and clustering started in background.",
            "totalPhotosFound": len(image_keys)
        }
 
    except Exception as e:
        print(f"❌ [API ERROR] Failed to initialize face count: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/api/test/generate-banner/{folder_id}")
async def test_generate_banner_endpoint(folder_id: str):
    """Directly triggers banner scoring logic & Node.js API call only if eventId exists in the folder."""
    try:
        print("\n==================================================")
        print(f"🧪 TEST API HIT: Generating Banner for Folder {folder_id}")
        print("==================================================\n")

        folder_doc = Folder.objects(id=folder_id).first()

        if not folder_doc:
            raise HTTPException(
                status_code=404,
                detail=f"Folder not found with id: {folder_id}",
            )

        banner_result = generate_and_save_folder_banner(folderId=folder_id)

        if not banner_result.get("success"):
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "Fail banner selection",
                    "details": banner_result,
                },
            )

        banner_url = banner_result.get("bannerUrl")
        selected_key = banner_result.get("selectedKey")

        event_id = getattr(folder_doc, "eventId", None)
        node_res_data = None
        node_api_called = False

        

        NODE_API_URL = "https://horaservices.com/api/internal/generate-banner"

        img_response = requests.get(banner_url, timeout=10)
        img_response.raise_for_status()

        image_bytes = io.BytesIO(img_response.content)

        payload = {"folderId": str(folder_id), "eventId": str(event_id)}
        files = {"leftImage": ("left_image.jpg", image_bytes, "image/jpeg")}

        node_response = requests.post(
            NODE_API_URL, data=payload, files=files, timeout=30
        )
        node_res_data = node_response.json()

        image_bytes.close()
        node_api_called = True
        

        return {
            "success": True,
            "message": (
                "Banner generated and pushed to Node.js successfully!"
                if node_api_called
                else "Banner selected in Python, but Node.js API skipped because eventId does not exist."
            ),
            "data": {
                "folderId": folder_id,
                "eventId": event_id,
                "selectedKey": selected_key,
                "bannerUrl": banner_url,
                "nodeApiCalled": node_api_called,
                "nodeApiResponse": node_res_data,
            },
        }

    except HTTPException as http_ex:
        raise http_ex
    except Exception as e:
        print(f"❌ Error in test_generate_banner_endpoint: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

    
# Serve index.html at root
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """Serve the main HTML page."""
    index_path = "index.html"
    if not os.path.exists(index_path):
        return HTMLResponse("<h1>index.html not found</h1>", status_code=404)
    
    with open(index_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
    print_memory("SERVER START")
