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

from eventFaceFinder import router as event_router




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

def cosine_sim(a, b):
    return float(np.dot(a, b) / (norm(a) * norm(b) + 1e-8))


# -----------------------------
# MAIN PROCESS FUNCTION
# -----------------------------
def process_face_count(folder_name: str):
    print(f"STARTING FACE PROCESSING: {folder_name}")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        image_keys = list_s3_images(folder_name)

        if not image_keys:
            print("No images found")
            return

        print(f"Total Images: {len(image_keys)}")

        persons = []
        total_faces_detected = 0

        BATCH_SIZE = 10
        THRESHOLD = 0.58   # improved stable value

        batches = [
            image_keys[i:i + BATCH_SIZE]
            for i in range(0, len(image_keys), BATCH_SIZE)
        ]

        for batch_no, keys in enumerate(batches, start=1):

            print(f"\nProcessing Batch {batch_no}/{len(batches)}")

            imgs = loop.run_until_complete(
                asyncio.gather(
                    *[read_s3_image_async(k, loop) for k in keys]
                )
            )

            for img in imgs:
                if img is None:
                    continue

                faces = searcher.app.get(img)

                print(f"Detected faces: {len(faces)}")

                for face in faces:

                    # -------------------------
                    # FILTER BAD DETECTIONS
                    # -------------------------
                    if face.embedding is None:
                        continue

                    if getattr(face, "det_score", 1.0) < 0.75:
                        continue

                    bbox = face.bbox
                    w = bbox[2] - bbox[0]
                    h = bbox[3] - bbox[1]

                    if w < 40 or h < 40:
                        continue

                    emb = face.normed_embedding.astype(np.float32)
                    total_faces_detected += 1

                    # -------------------------
                    # FIND BEST MATCH
                    # -------------------------
                    best_sim = -1
                    best_idx = -1

                    for i, p in enumerate(persons):
                        sim = cosine_sim(emb, p["embedding"])

                        if sim > best_sim:
                            best_sim = sim
                            best_idx = i

                    # -------------------------
                    # DECISION
                    # -------------------------
                    if best_idx != -1 and best_sim >= THRESHOLD:

                        p = persons[best_idx]

                        # table centroid update + normalization
                        new_emb = (
                            (p["embedding"] * p["count"]) + emb
                        ) / (p["count"] + 1)

                        p["embedding"] = new_emb / (norm(new_emb) + 1e-8)
                        p["count"] += 1

                    else:
                        persons.append({
                            "embedding": emb.copy(),
                            "count": 1
                        })

        print("\n================")
        print("TOTAL FACES:", total_faces_detected)
        print("UNIQUE PERSONS:", len(persons))
        print("================")

        # -------------------------
        # SAVE TO DB
        # -------------------------
        folder_doc = Folder.objects(folderName=folder_name).first()

        print("DB SAVE START")
        if folder_doc:
            folder_doc.totalPersonCount = len(persons)
            folder_doc.totalFacesDetected = total_faces_detected
            folder_doc.updatedAt = datetime.utcnow()
            folder_doc.save()

        return {
            "success": True,
            "totalFaces": total_faces_detected,
            "uniquePersons": len(persons)
        }

    finally:
        loop.close()


# -----------------------------
# FASTAPI ENDPOINT
# -----------------------------
@app.post("/count-unique-persons")
async def count_unique_persons(
    background_tasks: BackgroundTasks,
    folder_name: str = Form(...)
):
    try:
        background_tasks.add_task(process_face_count, folder_name)

        return {
            "success": True,
            "message": "Face processing started in background"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# DBSCAN Config (Senior ke parameters)
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


@app.post("/count-unique-persons4")
async def count_unique_persons(
    folder_name: str = Form(...),
    folderId: str = Form(...),
    userId: str = Form(...)
):
    try:
        loop = asyncio.get_running_loop()
        image_keys = list_s3_images(folder_name)

        if not image_keys:
            return {"success": False, "message": "No images found"}
        

        print(f"Total Images Found = {len(image_keys)}")

        # Arrays to collect face data for DBSCAN (Senior's Approach)
        all_embeddings = []
        face_metadata = []  # To store references of key, face_crop, and det_score

        BATCH_SIZE = 10
        batches = [
            image_keys[i:i + BATCH_SIZE]
            for i in range(0, len(image_keys), BATCH_SIZE)
        ]

        # =========================================================
        # STAGE 1: EXTRACT EMBEDDINGS FROM ALL IMAGES
        # =========================================================
        for keys in batches:
            imgs = await asyncio.gather(*[
                read_s3_image_async(k, loop)
                for k in keys
            ])

            for img, key in zip(imgs, keys):
                if img is None:
                    continue

                faces = searcher.app.get(img)
                if not faces:
                    continue

                # Senior's Logic: Agar image me multiple faces hain, toh sabse bada face lo
                if len(faces) > 1:
                    face = max(
                        faces,
                        key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1])
                    )
                else:
                    face = faces[0]

                # Low confidence filter strictly kept to avoid pure noise/non-faces
                det_score = getattr(face, 'det_score', 1.0)
                if det_score < 0.70:  
                    continue

                # Crop generation for folder display icon
                x1, y1, x2, y2 = map(int, face.bbox)
                face_w, face_h = x2 - x1, y2 - y1
                pad_x, pad_y = int(face_w * 0.20), int(face_h * 0.20)
                h, w = img.shape[:2]
                
                crop_x1 = max(0, x1 - pad_x)
                crop_y1 = max(0, y1 - pad_y)
                crop_x2 = min(w, x2 + pad_x)
                crop_y2 = min(h, y2 + pad_y)
                face_crop = img[crop_y1:crop_y2, crop_x1:crop_x2]

                # Collect for DBSCAN clustering
                all_embeddings.append(face.embedding)
                face_metadata.append({
                    "key": key,
                    "face_crop": face_crop.copy() if face_crop.size > 0 else img[y1:y2, x1:x2],
                    "det_score": det_score
                })

        if not all_embeddings:
            raise HTTPException(status_code=400, detail="No faces detected in the given images.")

        # =========================================================
        # STAGE 2: DBSCAN CLUSTERING (Senior's Core Logic)
        # =========================================================
        np_embeddings = np.array(all_embeddings)
        clustering = DBSCAN(eps=EPS, min_samples=MIN_SAMPLES, metric="cosine")
        labels = clustering.fit_predict(np_embeddings)

        # Map DBSCAN results back to groups
        # Format: { label_id: { "images": [...], "best_crop": crop, "max_score": float } }
        cluster_groups = {}
        unknown_images = []

        for label, metadata in zip(labels, face_metadata):
            if label == -1:
                # -1 means Outlier/Unknown face in DBSCAN
                unknown_images.append(metadata["key"])
                continue

            if label not in cluster_groups:
                cluster_groups[label] = {
                    "images": [],
                    "best_crop": metadata["face_crop"],
                    "max_score": metadata["det_score"]
                }
            
            cluster_groups[label]["images"].append(metadata["key"])
            
            # Agar kisi face ki quality/score better hai toh usko Profile Pic (DP) banayenge
            if metadata["det_score"] > cluster_groups[label]["max_score"]:
                cluster_groups[label]["best_crop"] = metadata["face_crop"]
                cluster_groups[label]["max_score"] = metadata["det_score"]

        # =========================================================
        # STAGE 3: DB PERSISTENCE & S3 UPLOAD
        # =========================================================
        folder_doc = Folder.objects(id=folderId).first()
        if not folder_doc:
            raise HTTPException(status_code=404, detail="Folder not found")

        created_subfolders = []
        persons_response_data = []

        for index, (label, group_data) in enumerate(cluster_groups.items()):
            person_id = str(uuid.uuid4())
            
            # Uploading the best available face crop as Folder Thumbnail
            crop_key, crop_url = upload_face_crop(group_data["best_crop"], folderId, person_id)

            subfolder = SubFolder(
                folderName=f"Person {index + 1}",
                type="others",
                userId=userId,
                personId=person_id,
                personCount=len(group_data["images"]),
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
                "personId": person_id,
                "images": group_data["images"],  # Kept for the tagging phase below
                "sampleImage": crop_url,
                "sampleImageKey": crop_key
            })

            persons_response_data.append({
                "person_id": person_id,
                "count": len(group_data["images"]),
                "subFolderId": str(subfolder._id),
                "sampleImage": crop_url,
                "sampleImageKey": crop_key
            })

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
                    docs = WebLinks.objects(thumbnailKey__icontains=filename)
                    for doc in docs:
                        WebLinks.objects(id=doc.id).update_one(
                            add_to_set__folderIds=sub_folder_id
                        )
                except Exception as e:
                    print(f"❌ Failed Tagging {image_key}: {str(e)}")

        print("\n✅ IMAGE TAGGING COMPLETED")

        return {
            "success": True,
            "totalPhotos": len(image_keys),
            "totalFaceDetections": len(all_embeddings),
            "uniquePersons": len(cluster_groups),
            "subFoldersCreated": len(created_subfolders),
            "unknownFacesCount": len(unknown_images),
            "groups": persons_response_data
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
