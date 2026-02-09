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
from models.folders import Folder

from database import connect_db
from dotenv import load_dotenv
load_dotenv()



AWS_REGION = os.getenv("AWS_REGION", "eu-north-1")
S3_BUCKET = os.getenv("S3_BUCKET_NAME", "photography-hora")




def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]

s3 = boto3.client("s3", region_name=AWS_REGION)
from concurrent.futures import ThreadPoolExecutor

EXECUTOR = ThreadPoolExecutor(max_workers=4)
BATCH_SIZE = 4



app = FastAPI(title="Face Recognition Server", version="1.0.0")


# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
        

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
    def __init__(self, min_similarity: float = 0.5):
        self.min_similarity = min_similarity

        self.app = FaceAnalysis(
            name="buffalo_l",
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

        

        # Tunables
        BATCH_SIZE = 5
        DOWNLOAD_LIMIT = 4
        SCAN_LIMIT = 4

        download_sem = asyncio.Semaphore(DOWNLOAD_LIMIT)
        scan_sem = asyncio.Semaphore(SCAN_LIMIT)

         
        batches = [
            image_keys[i : i + BATCH_SIZE]
            for i in range(0, len(image_keys), BATCH_SIZE)
        ]


        tasks = []
        match_count = 0

        
        for batch_id, keys in enumerate(batches, start=1):

            async def handle_batch(
                batch_id=batch_id,
                keys=keys,
            ):
                # 🔽 DOWNLOAD STAGE
                async with download_sem:
                    

                    s3_tasks = [
                        read_s3_image_async(key, loop)
                        for key in keys
                    ]
                    imgs = await asyncio.gather(*s3_tasks)

                    

                batch_imgs = list(zip(keys, imgs))

                
                async with scan_sem:
                    

                    start = time.perf_counter()
                    results = await loop.run_in_executor(
                        EXECUTOR,
                        process_image_batch,
                        self.app,
                        batch_imgs,
                        reference_embedding,
                        self.min_similarity,
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
    # 🔥 MongoDB connect
    connect_db()
    print("✅ MongoDB connected")
   
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
            delete_subfolder_by_id(subFolderId)
            raise HTTPException(
                status_code=400,
                detail="No face found in sample image",
            )

        reference_embedding = faces[0].embedding

    except HTTPException:
        raise
    except Exception as e:
        delete_subfolder_by_id(subFolderId)
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
