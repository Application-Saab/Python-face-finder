#!/usr/bin/env python3
"""
FastAPI Face Recognition Server with Streaming Results, Image Upload, and Auto-Resizing
Features: Image upload with auto-resizing, listing, and streaming face recognition.
"""

from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from insightface.app import FaceAnalysis
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
from PIL import Image
import io
import cv2
from sklearn.cluster import DBSCAN
from fastapi import BackgroundTasks

from database import connect_db
from dotenv import load_dotenv
load_dotenv()
from fastapi import BackgroundTasks

from eventFaceFinder import router as event_router
import warnings
import cv2
import numpy as np
from PIL import Image
from fastapi import FastAPI, BackgroundTasks, Form, HTTPException
import mediapipe as mp




# ---------------------------------------------------------
# 0. HIDE UNWANTED LIBRARY WARNINGS (Numpy/InsightFace Noise)
# ---------------------------------------------------------
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)




AWS_REGION = os.getenv("AWS_REGION", "eu-north-1")
S3_BUCKET = os.getenv("S3_BUCKET_NAME", "photography-hora")




def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]

s3 = boto3.client("s3", region_name=AWS_REGION)
from concurrent.futures import ThreadPoolExecutor

EXECUTOR = ThreadPoolExecutor(max_workers=3)



app = FastAPI(title="Face Recognition Server", version="1.0.0")


# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(event_router)
        

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

        self.app = FaceAnalysis(
            name="buffalo_s",
            providers=["CPUExecutionProvider"],
        )
        self.app.prepare(ctx_id=0, det_size=(640, 640))

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


EPS = 0.6
MIN_SAMPLES = 2

def upload_face_crop(face_crop, folder_id, person_id):
    buffer = io.BytesIO()
    Image.fromarray(face_crop).save(
        buffer,
        format="WEBP",
        quality=90
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


def process_face_clustering_in_background(image_keys, folderId, userId, folder_name):
    try:
        print(f"Total Images Found = {len(image_keys)}")
        all_embeddings = []
        face_metadata = []

        BATCH_SIZE = 10
        batches = [
            image_keys[i:i + BATCH_SIZE]
            for i in range(0, len(image_keys), BATCH_SIZE)
        ]

        # =========================================================
        # STAGE 1: EXTRACT EMBEDDINGS FROM ALL IMAGES
        # =========================================================
        for keys in batches:
            imgs = [read_s3_image(k) for k in keys]

            for img, key in zip(imgs, keys):
                if img is None:
                    continue

                faces = searcher.app.get(img)
                if not faces:
                    continue


                for face in faces:
                    det_score = getattr(face, 'det_score', 1.0)
                    if det_score < 0.65: 
                        continue

                    x1, y1, x2, y2 = map(int, face.bbox)
                    face_w, face_h = x2 - x1, y2 - y1
                    
                    if face_w < 35 or face_h < 35:
                        continue

                    pad_x, pad_y = int(face_w * 0.20), int(face_h * 0.20)
                    h, w = img.shape[:2]
                    
                    crop_x1 = max(0, x1 - pad_x)
                    crop_y1 = max(0, y1 - pad_y)
                    crop_x2 = min(w, x2 + pad_x)
                    crop_y2 = min(h, y2 + pad_y)
                    face_crop = img[crop_y1:crop_y2, crop_x1:crop_x2]

                    all_embeddings.append(face.embedding)
                    face_metadata.append({
                        "key": key,
                        "face_crop": face_crop.copy() if face_crop.size > 0 else img[y1:y2, x1:x2],
                        "det_score": det_score
                    })

        if not all_embeddings:
            print("⚠️ No faces detected in the given images.")
            return

        # =========================================================
        # STAGE 2: DBSCAN CLUSTERING
        # =========================================================
        np_embeddings = np.array(all_embeddings)
        clustering = DBSCAN(eps=EPS, min_samples=MIN_SAMPLES, metric="cosine")
        labels = clustering.fit_predict(np_embeddings)

        cluster_groups = {}
        unknown_faces_count = 0
        noise_id_counter = -2 

        for label, metadata in zip(labels, face_metadata):
            if label == -1:
                current_group_id = noise_id_counter
                noise_id_counter -= 1
                unknown_faces_count += 1
            else:
                current_group_id = label

            if current_group_id not in cluster_groups:
                cluster_groups[current_group_id] = {
                    "images": [],
                    "best_crop": metadata["face_crop"],
                    "max_score": metadata["det_score"]
                }
            
            if metadata["key"] not in cluster_groups[current_group_id]["images"]:
                cluster_groups[current_group_id]["images"].append(metadata["key"])
                
            if metadata["det_score"] > cluster_groups[current_group_id]["max_score"]:
                cluster_groups[current_group_id]["best_crop"] = metadata["face_crop"]
                cluster_groups[current_group_id]["max_score"] = metadata["det_score"]

        # =========================================================
        # STAGE 3: DB PERSISTENCE & S3 UPLOAD
        # =========================================================
        folder_doc = Folder.objects(id=folderId).first()
        if not folder_doc:
            print("❌ Folder not found in background task")
            return
        
        current_ai_person_count = folder_doc.totalPersonCount or 0
        new_ai_persons_detected = len(cluster_groups)

        created_subfolders = []
        for group_id, group_data in cluster_groups.items():
            person_id = str(uuid.uuid4())
            crop_key, crop_url = upload_face_crop(group_data["best_crop"], folderId, person_id)

            display_name = f"Person"

            subfolder = SubFolder(
                folderName=display_name,
                type="others",
                userId=userId,
                personCount=len(group_data["images"]),
                isPersonFolder=True,
                folderDp={
                    "fileUrl": crop_url,
                    "thumbnailUrl": crop_url,
                    "s3Key": crop_key,
                    "thumbnailKey": crop_key
                }
            )
            folder_doc.subFolders.append(subfolder)

            created_subfolders.append({
                "subFolderId": str(subfolder._id),
                "images": group_data["images"]
            })

        folder_doc.totalPersonCount = current_ai_person_count + new_ai_persons_detected
        folder_doc.uniqueFaceCount = len(cluster_groups)
        folder_doc.save()
        print("✅ Folder Database Setup Success")

        # =====================================================
        # STAGE 4: IMAGE TAGGING PROCESS
        # =====================================================
        print("\n🚀 STARTING IMAGE TAGGING PROCESS")
        for sub_info in created_subfolders:
            sub_folder_id = sub_info["subFolderId"]
            for image_key in sub_info["images"]:
                try:
                    filename = image_key.split("/")[-1]
                    WebLinks.objects(thumbnailKey__endswith=filename).update(
                        add_to_set__folderIds=sub_folder_id
                    )
                except Exception as e:
                    print(f"❌ Failed Tagging {image_key}: {str(e)}")
        print("\n✅ IMAGE TAGGING COMPLETED")

    except Exception as e:
        print(f"❌ Error in background face recognition: {str(e)}")

@app.post("/count-unique-persons")
async def count_unique_persons(
    background_tasks: BackgroundTasks,
    folder_name: str = Form(...),
    folderId: str = Form(...),
    userId: str = Form(...)
):
    try:
        image_keys = list_s3_images(folder_name)

        if not image_keys:
            return {"success": False, "message": "No images found in S3 bucket"}
        
        background_tasks.add_task(
            process_face_clustering_in_background, 
            image_keys, 
            folderId, 
            userId, 
            folder_name
        )

        return {
            "success": True,
            "message": "Face recognition and clustering started in background.",
            "totalPhotosFound": len(image_keys)
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))








# # =========================================================
# # 0. MEDIAPIPE FACE MESH SETUP
# # =========================================================
# mp_face_mesh = mp.solutions.face_mesh
# _face_mesh_detector = mp_face_mesh.FaceMesh(
#     static_image_mode=True,
#     max_num_faces=1,
#     refine_landmarks=True,
#     min_detection_confidence=0.5,
# )

# # ---- Eye landmark indices (EAR) ----
# LEFT_EYE_EAR_IDX = [33, 160, 158, 133, 153, 144]
# RIGHT_EYE_EAR_IDX = [362, 385, 387, 263, 373, 380]
# EAR_CLOSED_THRESHOLD = 0.21

# # ---- Mouth landmark indices (MAR + smile curve) ----
# MOUTH_LEFT_CORNER = 61
# MOUTH_RIGHT_CORNER = 291
# MOUTH_UPPER_CENTER = 13
# MOUTH_LOWER_CENTER = 14

# # Mouth-open ratio (talking/yawning vs closed-mouth smile)
# MAR_OPEN_THRESHOLD = 0.40

# # Lip-corner-curve thresholds (corners raised relative to mouth midline,
# # normalized by mouth width). Positive = corners curving upward = smile.
# SMILE_CURVE_STRONG = 0.06
# SMILE_CURVE_MILD = 0.02


# def _euclidean(p1, p2):
#     return np.linalg.norm(np.array(p1) - np.array(p2))


# # =========================================================
# # 1. SINGLE FACE-MESH PASS (shared by eye + smile checks)
# # =========================================================

# def _get_face_mesh_landmarks(img, face):
#     """
#     Crops the face region once, runs MediaPipe Face Mesh once, and returns
#     (landmarks, crop_w, crop_h) so both eye and mouth checks can reuse the
#     same detection instead of running the mesh model twice per face.
#     Returns None if landmarks couldn't be found.
#     """
#     try:
#         h_img, w_img = img.shape[:2]
#         x1, y1, x2, y2 = map(int, face.bbox)
#         x1, y1 = max(0, x1), max(0, y1)
#         x2, y2 = min(w_img, x2), min(h_img, y2)

#         pad_x = int((x2 - x1) * 0.25)
#         pad_y = int((y2 - y1) * 0.35)
#         cx1, cy1 = max(0, x1 - pad_x), max(0, y1 - pad_y)
#         cx2, cy2 = min(w_img, x2 + pad_x), min(h_img, y2 + pad_y)

#         face_crop = img[cy1:cy2, cx1:cx2]
#         if face_crop.size == 0 or face_crop.shape[0] < 20 or face_crop.shape[1] < 20:
#             return None

#         rgb_crop = cv2.cvtColor(face_crop, cv2.COLOR_BGR2RGB)
#         result = _face_mesh_detector.process(rgb_crop)

#         if not result.multi_face_landmarks:
#             return None

#         crop_h, crop_w = face_crop.shape[:2]
#         return result.multi_face_landmarks[0].landmark, crop_w, crop_h

#     except Exception as e:
#         print(f"[LOG] Face Mesh Extraction Exception: {e}")
#         return None


# # =========================================================
# # 2. ACCURATE EYE BLINK & OPEN CHECK (EAR-based)
# # =========================================================

# def _compute_ear(landmarks, idxs, w, h):
#     pts = [(landmarks[i].x * w, landmarks[i].y * h) for i in idxs]
#     p1, p2, p3, p4, p5, p6 = pts
#     vertical_1 = _euclidean(p2, p6)
#     vertical_2 = _euclidean(p3, p5)
#     horizontal = _euclidean(p1, p4)
#     if horizontal == 0:
#         return None
#     return (vertical_1 + vertical_2) / (2.0 * horizontal)


# def check_eye_open(mesh_data):
#     """
#     Eyes Open/Closed via Eye Aspect Ratio (EAR).
#     mesh_data = (landmarks, crop_w, crop_h) from _get_face_mesh_landmarks, or None.
#     Returns True (Open) as a safe fallback when landmarks are unavailable.
#     """
#     if mesh_data is None:
#         return True

#     landmarks, crop_w, crop_h = mesh_data
#     try:
#         left_ear = _compute_ear(landmarks, LEFT_EYE_EAR_IDX, crop_w, crop_h)
#         right_ear = _compute_ear(landmarks, RIGHT_EYE_EAR_IDX, crop_w, crop_h)
#         valid_ears = [e for e in (left_ear, right_ear) if e is not None]
#         if not valid_ears:
#             return True
#         avg_ear = sum(valid_ears) / len(valid_ears)
#         return avg_ear >= EAR_CLOSED_THRESHOLD
#     except Exception as e:
#         print(f"[LOG] EAR Computation Exception: {e}")
#         return True


# # =========================================================
# # 3. ANGLE-ROBUST SMILE CHECK (MAR + lip-corner curve)
# # =========================================================

# def check_smile(mesh_data):
#     """
#     Detects smile using two signals instead of raw mouth width (which
#     shrinks under face rotation and gives false negatives on turned faces):

#     1. MAR (Mouth Aspect Ratio) = mouth_height / mouth_width
#        -> tells us if the mouth is open (talking/laughing/yawning)
#     2. Lip-corner curve = how much the mouth corners lift above the
#        upper/lower-lip midline, normalized by mouth width
#        -> tells us if the mouth shape is curving into a smile,
#           independent of head yaw (both corners and midline shift
#           together under rotation, so the *relative* curve is stable)

#     Returns (score, label).
#     """
#     if mesh_data is None:
#         return 10, "Neutral Expression (+10) [mesh unavailable]"

#     landmarks, crop_w, crop_h = mesh_data
#     try:
#         left_corner = (landmarks[MOUTH_LEFT_CORNER].x * crop_w, landmarks[MOUTH_LEFT_CORNER].y * crop_h)
#         right_corner = (landmarks[MOUTH_RIGHT_CORNER].x * crop_w, landmarks[MOUTH_RIGHT_CORNER].y * crop_h)
#         upper_center = (landmarks[MOUTH_UPPER_CENTER].x * crop_w, landmarks[MOUTH_UPPER_CENTER].y * crop_h)
#         lower_center = (landmarks[MOUTH_LOWER_CENTER].x * crop_w, landmarks[MOUTH_LOWER_CENTER].y * crop_h)

#         mouth_width = _euclidean(left_corner, right_corner)
#         if mouth_width == 0:
#             return 10, "Neutral Expression (+10)"

#         mouth_height = _euclidean(upper_center, lower_center)
#         mar = mouth_height / mouth_width

#         mid_y = (upper_center[1] + lower_center[1]) / 2.0
#         corner_avg_y = (left_corner[1] + right_corner[1]) / 2.0
#         # Image y-axis grows downward, so corners above the midline
#         # (smaller y) => positive curve => smile
#         curve = (mid_y - corner_avg_y) / mouth_width

#         if curve >= SMILE_CURVE_STRONG:
#             return 30, "Smile (+30)"
#         elif curve >= SMILE_CURVE_MILD:
#             return 20, "Slight Smile (+20)"
#         elif mar >= MAR_OPEN_THRESHOLD:
#             # Mouth wide open but corners not curved up -> likely talking/laughing-open, not a posed smile
#             return 15, "Mouth Open / Talking (+15)"
#         else:
#             return 10, "Neutral Expression (+10)"

#     except Exception as e:
#         print(f"[LOG] Smile Computation Exception: {e}")
#         return 10, "Neutral Expression (+10)"


# # =========================================================
# # 4. PURE FACE-LEVEL SCORING HELPER
# # =========================================================

# def evaluate_single_face_quality(img, face):
#     """
#     Evaluates metrics strictly at the FACE-LEVEL:
#     - Face-Centric Portrait/Bokeh (Face vs Outer Rim Blur)
#     - Eye Open / Blink Status (EAR-based)
#     - Smile / Expression (MAR + lip-curve, angle-robust)
#     - Pose / Yaw Angle Check (graded, candid-friendly)
#     """
#     f_score = 0
#     person_logs = []
#     h_img, w_img = img.shape[:2]

#     x1, y1, x2, y2 = map(int, face.bbox)
#     x1, y1 = max(0, x1), max(0, y1)
#     x2, y2 = min(w_img, x2), min(h_img, y2)
#     face_w = max(1, x2 - x1)

#     # -------------------------------------------------------------
#     # 1. FACE-LEVEL PORTRAIT / BOKEH CHECK
#     # -------------------------------------------------------------
#     try:
#         gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
#         face_crop = gray[y1:y2, x1:x2]

#         pad = int(face_w * 0.4)
#         rx1, ry1 = max(0, x1 - pad), max(0, y1 - pad)
#         rx2, ry2 = min(w_img, x2 + pad), min(h_img, y2 + pad)
#         rim_crop = gray[ry1:ry2, rx1:rx2]

#         face_var = cv2.Laplacian(face_crop, cv2.CV_64F).var()
#         rim_var = cv2.Laplacian(rim_crop, cv2.CV_64F).var()

#         if face_var > 60 and rim_var < (face_var * 0.65):
#             f_score += 20
#             person_logs.append("Portrait Blur Effect (+20)")
#     except Exception:
#         pass

#     # -------------------------------------------------------------
#     # Run Face Mesh ONCE, reuse for both eye + smile checks
#     # -------------------------------------------------------------
#     mesh_data = _get_face_mesh_landmarks(img, face)

#     # -------------------------------------------------------------
#     # 2. EYE OPEN / BLINK CHECK
#     # -------------------------------------------------------------
#     is_eyes_open = check_eye_open(mesh_data)
#     if not is_eyes_open:
#         f_score -= 30
#         person_logs.append("Eyes Closed/Blink (-30)")
#     else:
#         f_score += 30
#         person_logs.append("Eyes Open (+30)")

#     # -------------------------------------------------------------
#     # 3. SMILE / EXPRESSION CHECK
#     # -------------------------------------------------------------
#     smile_score, smile_label = check_smile(mesh_data)
#     f_score += smile_score
#     person_logs.append(smile_label)

#     # -------------------------------------------------------------
#     # 4. POSE / YAW ANGLE CHECK (graded, candid-friendly)
#     # -------------------------------------------------------------
#     # Rationale: a turned head is often a genuine candid interaction
#     # (looking at another subject, a child, a cake, etc.), not a flaw.
#     # We only penalize once the angle is severe enough that the face
#     # is barely usable/recognizable, and we reward front-facing more
#     # than we used to reward a "side" pose.
#     pose = getattr(face, 'pose', [0, 0, 0])
#     yaw = abs(pose[1]) if len(pose) > 1 else 0

#     if yaw <= 20:
#         f_score += 20
#         person_logs.append("Front-Facing / Engaged (+20)")
#     elif yaw <= 45:
#         f_score += 15
#         person_logs.append("Natural Turn / Candid Interaction (+15)")
#     elif yaw <= 60:
#         f_score += 5
#         person_logs.append("Significant Turn, Face Still Visible (+5)")
#     else:
#         f_score -= 10
#         person_logs.append("Extreme Turn / Face Mostly Hidden (-10)")

#     return f_score, person_logs


# def calculate_face_level_score(img, faces):
#     """
#     Computes overall score purely driven by Face-level analysis across all detected faces.
#     """
#     if not faces:
#         print("[LOG] Face Evaluation: No faces detected in image.")
#         return 0, ["⚠️ No faces detected (Score: 0)"]

#     total_face_score = 0
#     face_reasons = []

#     print(f"\n[LOG] --- Starting Pure Face-Level Evaluation ({len(faces)} face(s)) ---")

#     for idx, face in enumerate(faces):
#         f_score, person_logs = evaluate_single_face_quality(img, face)
#         total_face_score += f_score

#         log_summary = f"Person #{idx+1}: Score = {f_score} -> [{', '.join(person_logs)}]"
#         face_reasons.append(log_summary)

#     avg_face_score = total_face_score / len(faces)
#     final_score = max(0, min(100, int(avg_face_score)))

#     return final_score, face_reasons


# # =========================================================
# # 5. BACKGROUND TASK PIPELINE
# # =========================================================

# def process_masterpiece_scoring_in_background(image_keys, folderId):
#     """
#     Processes images strictly using Pure Face-Level Scoring.
#     """
#     try:
#         print(f"\n🚀 STARTING PURE FACE-LEVEL MASTERPIECE SCORING FOR {len(image_keys)} IMAGES")

#         for key in image_keys:
#             try:
#                 filename = key.split("/")[-1]
#                 print("\n" + "-" * 60)
#                 print(f"🖼️ ANALYZING IMAGE: {filename}")

#                 img = read_s3_image(key)
#                 if img is None:
#                     print(f"❌ Failed to read image from S3: {key}")
#                     continue

#                 if not isinstance(img, np.ndarray):
#                     img = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

#                 faces = searcher.app.get(img)
#                 valid_faces = [f for f in faces if getattr(f, 'det_score', 1.0) >= 0.65]

#                 final_score, face_logs = calculate_face_level_score(img, valid_faces)

#                 print(f"  📌 Face Level Checks ({len(valid_faces)} Valid Face(s) Found):")
#                 for log in face_logs:
#                     print(f"     • {log}")

#                 WebLinks.objects(thumbnailKey__endswith=filename).update_one(
#                     set__masterpieceScore=final_score,
#                     set__isProcessedForStory=True
#                 )

#                 print(f"🎯 FINAL FACE-LEVEL SCORE SAVED TO DB: {final_score}/100")

#             except Exception as img_err:
#                 print(f"⚠️ Error scoring image {key}: {str(img_err)}")

#         print("\n" + "=" * 60)
#         print("✅ ALL FACE-LEVEL MASTERPIECE SCORING COMPLETED SUCCESSFULLY")

#     except Exception as e:
#         print(f"❌ Error in background task execution: {str(e)}")


# # =========================================================
# # 6. FASTAPI ROUTE
# # =========================================================

# @app.post("/generate-masterpiece-scores")
# async def generate_masterpiece_scores(
#     background_tasks: BackgroundTasks,
#     folder_name: str = Form(...),
#     folderId: str = Form(...)
# ):
#     try:
#         image_keys = list_s3_images(folder_name)

#         if not image_keys:
#             return {"success": False, "message": "No images found in S3 path"}

#         background_tasks.add_task(
#             process_masterpiece_scoring_in_background,
#             image_keys,
#             folderId
#         )

#         return {
#             "success": True,
#             "message": "Face-level masterpiece photo scoring pipeline started successfully.",
#             "totalImages": len(image_keys)
#         }

#     except Exception as e:
#         raise HTTPException(status_code=500, detail=str(e))








import cv2
import numpy as np
import mediapipe as mp
from fastapi import BackgroundTasks, Form, HTTPException

# =========================================================
# 0. MEDIAPIPE FACE MESH SETUP
# =========================================================
mp_face_mesh = mp.solutions.face_mesh
_face_mesh_detector = mp_face_mesh.FaceMesh(
    static_image_mode=True,
    max_num_faces=1,
    refine_landmarks=True,
    min_detection_confidence=0.5,
)

# ---- Eye landmark indices (EAR) ----
LEFT_EYE_EAR_IDX = [33, 160, 158, 133, 153, 144]
RIGHT_EYE_EAR_IDX = [362, 385, 387, 263, 373, 380]
EAR_CLOSED_THRESHOLD = 0.21

# ---- Mouth landmark indices (MAR + smile curve) ----
MOUTH_LEFT_CORNER = 61
MOUTH_RIGHT_CORNER = 291
MOUTH_UPPER_CENTER = 13
MOUTH_LOWER_CENTER = 14

# Mouth-open ratio (talking/yawning vs closed-mouth smile)
MAR_OPEN_THRESHOLD = 0.40

# Lip-corner-curve thresholds (corners raised relative to mouth midline,
# normalized by mouth width). Positive = corners curving upward = smile.
SMILE_CURVE_STRONG = 0.06
SMILE_CURVE_MILD = 0.02

# ---- Scoring weights (tuned so a genuinely good photo - eyes open +
# any expression + a normal pose - naturally clears the 70 threshold) ----
WEIGHT_PORTRAIT_BOKEH = 10

WEIGHT_EYES_OPEN = 35
WEIGHT_EYES_CLOSED = -35

WEIGHT_SMILE_STRONG = 35
WEIGHT_SMILE_MILD = 25
WEIGHT_MOUTH_OPEN = 20
WEIGHT_EXPRESSION_NEUTRAL = 15

WEIGHT_POSE_FRONT = 20
WEIGHT_POSE_NATURAL_TURN = 18
WEIGHT_POSE_SIGNIFICANT_TURN = 8
WEIGHT_POSE_EXTREME_TURN = -10


def _euclidean(p1, p2):
    return np.linalg.norm(np.array(p1) - np.array(p2))


# =========================================================
# 1. SINGLE FACE-MESH PASS (shared by eye + smile checks)
# =========================================================

def _get_face_mesh_landmarks(img, face):
    """
    Crops the face region once, runs MediaPipe Face Mesh once, and returns
    (landmarks, crop_w, crop_h) so both eye and mouth checks can reuse the
    same detection instead of running the mesh model twice per face.
    Returns None if landmarks couldn't be found.
    """
    try:
        h_img, w_img = img.shape[:2]
        x1, y1, x2, y2 = map(int, face.bbox)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w_img, x2), min(h_img, y2)

        pad_x = int((x2 - x1) * 0.25)
        pad_y = int((y2 - y1) * 0.35)
        cx1, cy1 = max(0, x1 - pad_x), max(0, y1 - pad_y)
        cx2, cy2 = min(w_img, x2 + pad_x), min(h_img, y2 + pad_y)

        face_crop = img[cy1:cy2, cx1:cx2]
        if face_crop.size == 0 or face_crop.shape[0] < 20 or face_crop.shape[1] < 20:
            return None

        # Pad to a square canvas. refine_landmarks=True internally expects a
        # square ROI; a non-square crop triggers MediaPipe's
        # "NORM_RECT without IMAGE_DIMENSIONS" warning and can skew landmark
        # projection slightly. We pad with edge-replicated pixels rather than
        # resizing, so we don't distort the face geometry.
        ch, cw = face_crop.shape[:2]
        side = max(ch, cw)
        pad_top = (side - ch) // 2
        pad_bottom = side - ch - pad_top
        pad_left = (side - cw) // 2
        pad_right = side - cw - pad_left
        face_crop = cv2.copyMakeBorder(
            face_crop, pad_top, pad_bottom, pad_left, pad_right,
            borderType=cv2.BORDER_REPLICATE
        )

        rgb_crop = cv2.cvtColor(face_crop, cv2.COLOR_BGR2RGB)
        result = _face_mesh_detector.process(rgb_crop)

        if not result.multi_face_landmarks:
            return None

        crop_h, crop_w = face_crop.shape[:2]
        return result.multi_face_landmarks[0].landmark, crop_w, crop_h

    except Exception as e:
        print(f"[LOG] Face Mesh Extraction Exception: {e}")
        return None


# =========================================================
# 2. ACCURATE EYE BLINK & OPEN CHECK (EAR-based)
# =========================================================

def _compute_ear(landmarks, idxs, w, h):
    pts = [(landmarks[i].x * w, landmarks[i].y * h) for i in idxs]
    p1, p2, p3, p4, p5, p6 = pts
    vertical_1 = _euclidean(p2, p6)
    vertical_2 = _euclidean(p3, p5)
    horizontal = _euclidean(p1, p4)
    if horizontal == 0:
        return None
    return (vertical_1 + vertical_2) / (2.0 * horizontal)


def check_eye_open(mesh_data):
    """
    Eyes Open/Closed via Eye Aspect Ratio (EAR).
    mesh_data = (landmarks, crop_w, crop_h) from _get_face_mesh_landmarks, or None.
    Returns True (Open) as a safe fallback when landmarks are unavailable.
    """
    if mesh_data is None:
        return True

    landmarks, crop_w, crop_h = mesh_data
    try:
        left_ear = _compute_ear(landmarks, LEFT_EYE_EAR_IDX, crop_w, crop_h)
        right_ear = _compute_ear(landmarks, RIGHT_EYE_EAR_IDX, crop_w, crop_h)
        valid_ears = [e for e in (left_ear, right_ear) if e is not None]
        if not valid_ears:
            return True
        avg_ear = sum(valid_ears) / len(valid_ears)
        return avg_ear >= EAR_CLOSED_THRESHOLD
    except Exception as e:
        print(f"[LOG] EAR Computation Exception: {e}")
        return True


# =========================================================
# 3. ANGLE-ROBUST SMILE CHECK (MAR + lip-corner curve)
# =========================================================

def check_smile(mesh_data):
    """
    Detects smile using two signals instead of raw mouth width (which
    shrinks under face rotation and gives false negatives on turned faces):

    1. MAR (Mouth Aspect Ratio) = mouth_height / mouth_width
       -> tells us if the mouth is open (talking/laughing/yawning)
    2. Lip-corner curve = how much the mouth corners lift above the
       upper/lower-lip midline, normalized by mouth width
       -> tells us if the mouth shape is curving into a smile,
          independent of head yaw (both corners and midline shift
          together under rotation, so the *relative* curve is stable)

    Returns (score, label).
    """
    if mesh_data is None:
        return WEIGHT_EXPRESSION_NEUTRAL, f"Neutral Expression (+{WEIGHT_EXPRESSION_NEUTRAL}) [mesh unavailable]"

    landmarks, crop_w, crop_h = mesh_data
    try:
        left_corner = (landmarks[MOUTH_LEFT_CORNER].x * crop_w, landmarks[MOUTH_LEFT_CORNER].y * crop_h)
        right_corner = (landmarks[MOUTH_RIGHT_CORNER].x * crop_w, landmarks[MOUTH_RIGHT_CORNER].y * crop_h)
        upper_center = (landmarks[MOUTH_UPPER_CENTER].x * crop_w, landmarks[MOUTH_UPPER_CENTER].y * crop_h)
        lower_center = (landmarks[MOUTH_LOWER_CENTER].x * crop_w, landmarks[MOUTH_LOWER_CENTER].y * crop_h)

        mouth_width = _euclidean(left_corner, right_corner)
        if mouth_width == 0:
            return WEIGHT_EXPRESSION_NEUTRAL, f"Neutral Expression (+{WEIGHT_EXPRESSION_NEUTRAL})"

        mouth_height = _euclidean(upper_center, lower_center)
        mar = mouth_height / mouth_width

        mid_y = (upper_center[1] + lower_center[1]) / 2.0
        corner_avg_y = (left_corner[1] + right_corner[1]) / 2.0
        # Image y-axis grows downward, so corners above the midline
        # (smaller y) => positive curve => smile
        curve = (mid_y - corner_avg_y) / mouth_width

        if curve >= SMILE_CURVE_STRONG:
            return WEIGHT_SMILE_STRONG, f"Smile (+{WEIGHT_SMILE_STRONG})"
        elif curve >= SMILE_CURVE_MILD:
            return WEIGHT_SMILE_MILD, f"Slight Smile (+{WEIGHT_SMILE_MILD})"
        elif mar >= MAR_OPEN_THRESHOLD:
            # Mouth wide open but corners not curved up -> likely talking/laughing-open, not a posed smile
            return WEIGHT_MOUTH_OPEN, f"Mouth Open / Talking (+{WEIGHT_MOUTH_OPEN})"
        else:
            return WEIGHT_EXPRESSION_NEUTRAL, f"Neutral Expression (+{WEIGHT_EXPRESSION_NEUTRAL})"

    except Exception as e:
        print(f"[LOG] Smile Computation Exception: {e}")
        return WEIGHT_EXPRESSION_NEUTRAL, f"Neutral Expression (+{WEIGHT_EXPRESSION_NEUTRAL})"


# =========================================================
# 4. PURE FACE-LEVEL SCORING HELPER
# =========================================================

def evaluate_single_face_quality(img, face):
    """
    Evaluates metrics strictly at the FACE-LEVEL:
    - Face-Centric Portrait/Bokeh (Face vs Outer Rim Blur)
    - Eye Open / Blink Status (EAR-based)
    - Smile / Expression (MAR + lip-curve, angle-robust)
    - Pose / Yaw Angle Check (graded, candid-friendly)
    """
    f_score = 0
    person_logs = []
    h_img, w_img = img.shape[:2]

    x1, y1, x2, y2 = map(int, face.bbox)
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w_img, x2), min(h_img, y2)
    face_w = max(1, x2 - x1)

    # -------------------------------------------------------------
    # 1. FACE-LEVEL PORTRAIT / BOKEH CHECK
    # -------------------------------------------------------------
    try:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        face_crop = gray[y1:y2, x1:x2]

        pad = int(face_w * 0.4)
        rx1, ry1 = max(0, x1 - pad), max(0, y1 - pad)
        rx2, ry2 = min(w_img, x2 + pad), min(h_img, y2 + pad)
        rim_crop = gray[ry1:ry2, rx1:rx2]

        face_var = cv2.Laplacian(face_crop, cv2.CV_64F).var()
        rim_var = cv2.Laplacian(rim_crop, cv2.CV_64F).var()

        if face_var > 60 and rim_var < (face_var * 0.65):
            f_score += WEIGHT_PORTRAIT_BOKEH
            person_logs.append(f"Portrait Blur Effect (+{WEIGHT_PORTRAIT_BOKEH})")
    except Exception:
        pass

    # -------------------------------------------------------------
    # Run Face Mesh ONCE, reuse for both eye + smile checks
    # -------------------------------------------------------------
    mesh_data = _get_face_mesh_landmarks(img, face)

    # -------------------------------------------------------------
    # 2. EYE OPEN / BLINK CHECK
    # -------------------------------------------------------------
    is_eyes_open = check_eye_open(mesh_data)
    if not is_eyes_open:
        f_score += WEIGHT_EYES_CLOSED
        person_logs.append(f"Eyes Closed/Blink ({WEIGHT_EYES_CLOSED})")
    else:
        f_score += WEIGHT_EYES_OPEN
        person_logs.append(f"Eyes Open (+{WEIGHT_EYES_OPEN})")

    # -------------------------------------------------------------
    # 3. SMILE / EXPRESSION CHECK
    # -------------------------------------------------------------
    smile_score, smile_label = check_smile(mesh_data)
    f_score += smile_score
    person_logs.append(smile_label)

    # -------------------------------------------------------------
    # 4. POSE / YAW ANGLE CHECK (graded, candid-friendly)
    # -------------------------------------------------------------
    pose = getattr(face, 'pose', [0, 0, 0])
    yaw = abs(pose[1]) if len(pose) > 1 else 0

    if yaw <= 20:
        f_score += WEIGHT_POSE_FRONT
        person_logs.append(f"Front-Facing / Engaged (+{WEIGHT_POSE_FRONT})")
    elif yaw <= 45:
        f_score += WEIGHT_POSE_NATURAL_TURN
        person_logs.append(f"Natural Turn / Candid Interaction (+{WEIGHT_POSE_NATURAL_TURN})")
    elif yaw <= 60:
        f_score += WEIGHT_POSE_SIGNIFICANT_TURN
        person_logs.append(f"Significant Turn, Face Still Visible (+{WEIGHT_POSE_SIGNIFICANT_TURN})")
    else:
        f_score += WEIGHT_POSE_EXTREME_TURN
        person_logs.append(f"Extreme Turn / Face Mostly Hidden ({WEIGHT_POSE_EXTREME_TURN})")

    return f_score, person_logs


def calculate_face_level_score(img, faces):
    """
    Computes overall score purely driven by Face-level analysis across all detected faces.
    """
    if not faces:
        print("[LOG] Face Evaluation: No faces detected in image.")
        return 0, ["⚠️ No faces detected (Score: 0)"]

    total_face_score = 0
    face_reasons = []

    print(f"\n[LOG] --- Starting Pure Face-Level Evaluation ({len(faces)} face(s)) ---")

    for idx, face in enumerate(faces):
        f_score, person_logs = evaluate_single_face_quality(img, face)
        total_face_score += f_score

        log_summary = f"Person #{idx+1}: Score = {f_score} -> [{', '.join(person_logs)}]"
        face_reasons.append(log_summary)

    avg_face_score = total_face_score / len(faces)
    final_score = max(0, min(100, round(avg_face_score)))

    return final_score, face_reasons


# =========================================================
# 5. BACKGROUND TASK PIPELINE
# =========================================================

def process_masterpiece_scoring_in_background(image_keys, folderId):
    """
    Processes images strictly using Pure Face-Level Scoring.
    """
    try:
        print(f"\n🚀 STARTING PURE FACE-LEVEL MASTERPIECE SCORING FOR {len(image_keys)} IMAGES")

        for key in image_keys:
            try:
                filename = key.split("/")[-1]
                print("\n" + "-" * 60)
                print(f"🖼️ ANALYZING IMAGE: {filename}")

                img = read_s3_image(key)
                if img is None:
                    print(f"❌ Failed to read image from S3: {key}")
                    continue

                if not isinstance(img, np.ndarray):
                    img = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

                faces = searcher.app.get(img)
                valid_faces = [f for f in faces if getattr(f, 'det_score', 1.0) >= 0.65]

                final_score, face_logs = calculate_face_level_score(img, valid_faces)

                print(f"  📌 Face Level Checks ({len(valid_faces)} Valid Face(s) Found):")
                for log in face_logs:
                    print(f"     • {log}")

                WebLinks.objects(thumbnailKey__endswith=filename).update_one(
                    set__masterpieceScore=final_score,
                    set__isProcessedForStory=True
                )

                print(f"🎯 FINAL FACE-LEVEL SCORE SAVED TO DB: {final_score}/100")

            except Exception as img_err:
                print(f"⚠️ Error scoring image {key}: {str(img_err)}")

        print("\n" + "=" * 60)
        print("✅ ALL FACE-LEVEL MASTERPIECE SCORING COMPLETED SUCCESSFULLY")

    except Exception as e:
        print(f"❌ Error in background task execution: {str(e)}")


# =========================================================
# 6. FASTAPI ROUTE
# =========================================================

@app.post("/generate-masterpiece-scores")
async def generate_masterpiece_scores(
    background_tasks: BackgroundTasks,
    folder_name: str = Form(...),
    folderId: str = Form(...)
):
    try:
        image_keys = list_s3_images(folder_name)

        if not image_keys:
            return {"success": False, "message": "No images found in S3 path"}

        background_tasks.add_task(
            process_masterpiece_scoring_in_background,
            image_keys,
            folderId
        )

        return {
            "success": True,
            "message": "Face-level masterpiece photo scoring pipeline started successfully.",
            "totalImages": len(image_keys)
        }

    except Exception as e:
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
