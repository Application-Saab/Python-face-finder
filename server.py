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
import cv2
from sklearn.cluster import DBSCAN
from fastapi import BackgroundTasks

from database import connect_db
from dotenv import load_dotenv
load_dotenv()
from fastapi import BackgroundTasks
from fastapi import Form, HTTPException
from eventFaceFinder import router as event_router
import gc 
from datetime import datetime
import requests
import bson
from mongoengine.queryset.visitor import Q  

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

        if banner_url:
            return {"success": True, "bannerUrl": banner_url, "selectedKey": selected_banner_key}
        
        return {"success": False, "message": "Banner key found but URL missing"}

    except Exception as e:
        print(f"❌ Error in generate_and_save_folder_banner: {str(e)}")
        return {"success": False, "error": str(e)}

EPS = 0.6
MIN_SAMPLES = 2
MERGE_THRESHOLD = 0.42  # cosine distance — isse kam matlab same person (existing subfolder ke saath merge)


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


def is_side_face(face_obj):
    try:
        pose = getattr(face_obj, 'pose', None)
        if pose is not None and len(pose) >= 2:
            yaw = abs(pose[1])
            if yaw > 22.0:
                return True

        kps = getattr(face_obj, 'kps', None)
        if kps is None or len(kps) < 5:
            return True

        left_eye, right_eye, nose = kps[0], kps[1], kps[2]

        dist_left = np.linalg.norm(nose - left_eye)
        dist_right = np.linalg.norm(nose - right_eye)

        max_dist = max(dist_left, dist_right)
        if max_dist == 0:
            return True

        asymmetry_ratio = abs(dist_left - dist_right) / max_dist
        return asymmetry_ratio > 0.20

    except Exception as e:
        print(f"⚠️ is_side_face check failed: {e}")
        return False


def find_matching_person(new_embedding, existing_people):
    """
    new_embedding: 1D np.array (naye cluster ka best embedding)
    existing_people: list of {"sub_id": str, "embedding": np.array}
    Returns matched sub_id agar koi existing person match kare, warna None
    """
    if not existing_people or new_embedding is None:
        return None

    best_sub_id = None
    best_dist = float("inf")

    new_vec = new_embedding / (np.linalg.norm(new_embedding) + 1e-8)

    for person in existing_people:
        existing_vec = person["embedding"]
        if existing_vec is None or len(existing_vec) == 0:
            continue
        existing_vec = existing_vec / (np.linalg.norm(existing_vec) + 1e-8)
        cosine_dist = 1 - np.dot(new_vec, existing_vec)

        if cosine_dist < best_dist:
            best_dist = cosine_dist
            best_sub_id = person["sub_id"]

    if best_dist <= MERGE_THRESHOLD:
        print(f"🔗 Match found -> sub_id={best_sub_id} (distance={round(best_dist, 4)})")
        return best_sub_id

    return None


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
            actual_tagged_count = WebLinks.objects(folderIds=sub_id).count()

            is_side = getattr(subfolder, "isSideFace", None)
            should_delete = False

            if is_side is True and actual_tagged_count <= 2:
                should_delete = True
                print(f"🔍 SIDE-FACE (Count={actual_tagged_count}): Deleting subfolder {sub_id}")

            if should_delete:
                removed_count += 1
                s3_key = getattr(subfolder.folderDp, "s3Key", None) if subfolder.folderDp else None

                if s3_key:
                    try:
                        s3.delete_object(Bucket=S3_BUCKET, Key=s3_key)
                        print(f"🗑️ S3 crop deleted: {s3_key}")
                    except Exception as s3_err:
                        print(f"⚠️ S3 delete failed for {sub_id}: {s3_err}")

                try:
                    WebLinks.objects(folderIds=sub_id).update(pull__folderIds=sub_id)
                except Exception as tag_err:
                    print(f"⚠️ Untagging failed for {sub_id}: {tag_err}")

                print(f"🗑️ SUCCESSFULLY REMOVED Side-Face Folder (ID: {sub_id}, Tagged Count: {actual_tagged_count})")
            else:
                subfolder.personCount = actual_tagged_count
                subfolders_to_keep.append(subfolder)

        if removed_count > 0:
            folder_doc.subFolders = subfolders_to_keep
            folder_doc.totalPersonCount = max(0, (folder_doc.totalPersonCount or 0) - removed_count)
            folder_doc.save()
            print(f"✅ Cleanup completed — Total {removed_count} side-face folder(s) removed.")
        else:
            print("✅ Cleanup completed — No side-face folders with <=2 images found.")

    except Exception as e:
        print(f"❌ Error in cleanup_small_side_face_folders: {str(e)}")


def process_face_clustering_in_background(image_keys, folderId, userId, folder_name):
    try:
        Folder.objects(id=folderId).update_one(
            set__clusteringStatus="IN_PROGRESS"
        )

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

                    side_flag = is_side_face(face)

                    all_embeddings.append(face.embedding)
                    face_metadata.append({
                        "key": key,
                        "face_crop": face_crop.copy() if face_crop.size > 0 else img[y1:y2, x1:x2],
                        "det_score": det_score,
                        "is_side": side_flag,
                        "embedding": face.embedding   # 🆕 stored for matching later
                    })

        if not all_embeddings:
            print("⚠️ No faces detected in the given images.")
            Folder.objects(id=folderId).update_one(
                set__clusteringStatus="DONE",
            )
            return

        # =========================================================
        # STAGE 2: DBSCAN CLUSTERING (within this run's images)
        # =========================================================
        np_embeddings = np.array(all_embeddings)
        clustering = DBSCAN(eps=EPS, min_samples=MIN_SAMPLES, metric="cosine")
        labels = clustering.fit_predict(np_embeddings)

        cluster_groups = {}
        noise_id_counter = -2

        for label, metadata in zip(labels, face_metadata):
            if label == -1:
                current_group_id = noise_id_counter
                noise_id_counter -= 1
            else:
                current_group_id = label

            if current_group_id not in cluster_groups:
                cluster_groups[current_group_id] = {
                    "images": [],
                    "best_crop": metadata["face_crop"],
                    "best_embedding": metadata["embedding"],   # 🆕
                    "max_score": metadata["det_score"],
                    "best_is_side": metadata["is_side"]
                }

            if metadata["key"] not in cluster_groups[current_group_id]["images"]:
                cluster_groups[current_group_id]["images"].append(metadata["key"])

            if metadata["det_score"] > cluster_groups[current_group_id]["max_score"]:
                cluster_groups[current_group_id]["best_crop"] = metadata["face_crop"]
                cluster_groups[current_group_id]["best_embedding"] = metadata["embedding"]   # 🆕
                cluster_groups[current_group_id]["max_score"] = metadata["det_score"]
                cluster_groups[current_group_id]["best_is_side"] = metadata["is_side"]

        # =========================================================
        # STAGE 3: TAGGING & SIDE-FACE CLEANUP (WITH CROSS-RUN MATCHING)
        # =========================================================
        print("\n🚀 STARTING IMAGE TAGGING & CLEANUP PROCESS")
        valid_subfolders = []
        removed_side_count = 0
        merged_count = 0

        # 🆕 Existing persons load karo (pehle se ban chuke subfolders ke embeddings)
        folder_doc_for_match = Folder.objects(id=folderId).first()
        existing_people = []
        if folder_doc_for_match:
            for sf in folder_doc_for_match.subFolders:
                sf_embedding = getattr(sf, "embedding", None)
                if getattr(sf, "isPersonFolder", False) and sf_embedding:
                    existing_people.append({
                        "sub_id": str(sf._id),
                        "embedding": np.array(sf_embedding)
                    })

        print(f"🔎 {len(existing_people)} existing person(s) loaded for matching")

        for group_id, group_data in cluster_groups.items():

            # 🆕 STEP A: Check ki ye person pehle se exist karta hai kya
            matched_sub_id = find_matching_person(group_data["best_embedding"], existing_people)

            group_keys = group_data["images"]
            group_filenames = [k.split("/")[-1] for k in group_keys if "/" in k]

            if matched_sub_id:
                # ===================== MERGE PATH =====================
                sub_id = matched_sub_id
                try:
                    folder_filter = Q(mainFolderId=folderId)
                    key_filter = (
                        Q(thumbnailKey__in=group_keys) |
                        Q(originalKey__in=group_keys) |
                        Q(thumbnailKey__in=group_filenames) |
                        Q(originalKey__in=group_filenames)
                    )
                    WebLinks.objects(folder_filter & key_filter).update(add_to_set__folderIds=sub_id)
                except Exception as e:
                    print(f"❌ Failed Bulk Tagging (merge) for Subfolder {sub_id}: {str(e)}")

                actual_tagged_count = WebLinks.objects(folderIds=sub_id).count()
                merged_count += 1
                print(f"🔁 MERGED into existing person {sub_id} — new tagged count: {actual_tagged_count}")

                # personCount sync karo existing subfolder object me
                if folder_doc_for_match:
                    for sf in folder_doc_for_match.subFolders:
                        if str(sf._id) == sub_id:
                            sf.personCount = actual_tagged_count
                            break

                continue  # ✅ naya subfolder create nahi hoga

            # ===================== CREATE PATH (naya person) =====================
            person_id = str(uuid.uuid4())
            crop_key, crop_url = upload_face_crop(group_data["best_crop"], folderId, person_id)
            sub_id = str(bson.ObjectId())

            try:
                folder_filter = Q(mainFolderId=folderId)
                key_filter = (
                    Q(thumbnailKey__in=group_keys) |
                    Q(originalKey__in=group_keys) |
                    Q(thumbnailKey__in=group_filenames) |
                    Q(originalKey__in=group_filenames)
                )
                WebLinks.objects(folder_filter & key_filter).update(add_to_set__folderIds=sub_id)
            except Exception as e:
                print(f"❌ Failed Bulk Tagging for Subfolder {sub_id}: {str(e)}")

            actual_tagged_count = WebLinks.objects(folderIds=sub_id).count()
            print(f"📊 Subfolder {sub_id} tagged count: {actual_tagged_count}")

            if group_data["best_is_side"] is True and actual_tagged_count <= 2:
                removed_side_count += 1
                print(f"🔍 SIDE-FACE FILTER (Count={actual_tagged_count}): Deleting crop & untagging subfolder {sub_id}")

                try:
                    s3.delete_object(Bucket=S3_BUCKET, Key=crop_key)
                except Exception as s3_err:
                    print(f"⚠️ S3 delete failed for {sub_id}: {s3_err}")

                try:
                    WebLinks.objects(folderIds=sub_id).update(pull__folderIds=sub_id)
                except Exception as tag_err:
                    print(f"⚠️ Untagging failed for {sub_id}: {tag_err}")
            else:
                subfolder = SubFolder(
                    _id=sub_id,
                    folderName="Person",
                    type="others",
                    userId=userId,
                    personCount=actual_tagged_count,
                    isPersonFolder=True,
                    isSideFace=group_data["best_is_side"],
                    embedding=list(map(float, group_data["best_embedding"])),  # 🆕 save embedding
                    folderDp={
                        "fileUrl": crop_url,
                        "thumbnailUrl": crop_url,
                        "s3Key": crop_key,
                        "thumbnailKey": crop_key
                    }
                )
                valid_subfolders.append(subfolder)

                # 🆕 isi run ke andar bhi aage ke clusters isse match kar sakein
                existing_people.append({
                    "sub_id": sub_id,
                    "embedding": group_data["best_embedding"]
                })

        print(f"✅ TAGGING & CLEANUP COMPLETED "
              f"(New: {len(valid_subfolders)}, Merged: {merged_count}, "
              f"Filtered side-face: {removed_side_count})")

        # =========================================================
        # STAGE 4: FINAL DB SAVE
        # =========================================================
        folder_doc = Folder.objects(id=folderId).first()
        if not folder_doc:
            print("❌ Folder not found in background task")
            return

        current_ai_person_count = folder_doc.totalPersonCount or 0

        folder_doc.subFolders.extend(valid_subfolders)   # sirf NAYE persons add honge
        folder_doc.totalPersonCount = current_ai_person_count + len(valid_subfolders)
        folder_doc.uniqueFaceCount = (folder_doc.uniqueFaceCount or 0) + len(valid_subfolders)

        # 🆕 Existing (merged) subfolders ka personCount bhi persist karo
        if folder_doc_for_match:
            updated_counts = {str(sf._id): sf.personCount for sf in folder_doc_for_match.subFolders}
            for sf in folder_doc.subFolders:
                sid = str(sf._id)
                if sid in updated_counts:
                    sf.personCount = updated_counts[sid]

        folder_doc.save()
        print("✅ Folder Database Setup Success - All verified subfolders saved to DB")

        # =========================================================
        # STAGE 5: BANNER GENERATION
        # =========================================================
        raw_order_id = getattr(folder_doc, 'orderId', None) if folder_doc else 'N/A'
        display_order_id = int(raw_order_id) + 10800 if str(raw_order_id).isdigit() else raw_order_id
        event_id = getattr(folder_doc, "eventId", None) if folder_doc else None

        if event_id and str(event_id).strip():
            print(f"🎨 Generating Best Banner Image for Folder (Order ID: {display_order_id})...")
            banner_result = generate_and_save_folder_banner(folderId=folderId)

        Folder.objects(id=folderId).update_one(
            set__clusteringStatus="DONE"
        )

        print(f"🎨 Generating Best Banner Image for Folder (Order ID: {display_order_id})...")

        banner_result = generate_and_save_folder_banner(folderId=folderId)

        if banner_result.get("success"):
            banner_url = banner_result.get("bannerUrl")
            print(f"🎉 Banner automatically assigned: {banner_url}")

            folder_doc = Folder.objects(id=folderId).first()

            NODE_API_URL = "https://horaservices.com/api/internal/generate-banner"

            try:
                img_response = requests.get(banner_url, timeout=10)
                img_response.raise_for_status()

                image_bytes = io.BytesIO(img_response.content)

                payload = {
                    "folderId": str(folderId),
                }

                files = {
                    "leftImage": ("left_image.jpg", image_bytes, "image/jpeg")
                }

                response = requests.post(NODE_API_URL, data=payload, files=files, timeout=30)
                res_data = response.json()

                if response.status_code == 200 and res_data.get("success"):
                    print(f"✅ Node.js Canvas Banner Generated & Saved: {res_data.get('bannerUrl')}")
                else:
                    print(f"❌ Node.js API Error: {res_data.get('error') or res_data.get('message')}")

                image_bytes.close()

            except Exception as req_err:
                print(f"❌ Error while calling Node.js Banner API: {str(req_err)}")

        else:
            print(f"⚠️ eventId is missing or null for Folder ID {folderId}. Skipping banner generation.")

    except Exception as e:
        print(f"❌ Error in background face recognition: {str(e)}")
        Folder.objects(id=folderId).update_one(
            set__clusteringStatus="FAILED"
        )


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
            print(f"⚠️ [API] No images found in S3 bucket for folder: {folder_name} (ID: {folderId})")
            return {"success": False, "message": "No images found in S3 bucket"}

        folder_doc = Folder.objects(id=folderId).first()
        orderId = getattr(folder_doc, "orderId", "N/A") if folder_doc else "N/A"
        display_name = getattr(folder_doc, "folderName", folder_name) if folder_doc else folder_name

        print("\n" + "📥" * 30)
        print(f"📌 [API RECEIVED] Count Unique Persons Triggered")
        print(f"📁 Folder Name : {display_name}")
        print(f"🆔 Folder ID   : {folderId}")
        print(f"📦 Order ID    : {orderId}")
        print(f"👤 Customer ID : {userId}")
        print(f"🖼️ S3 Images   : {len(image_keys)} files found")
        print("📥" * 30 + "\n")

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
