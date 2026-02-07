#!/usr/bin/env python3
"""
FastAPI Face Recognition Server with Streaming Results, Image Upload, and Auto-Resizing
Features: Image upload with auto-resizing, listing, and streaming face recognition.
"""

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse, FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from insightface.app import FaceAnalysis
from numpy.linalg import norm
import numpy as np
import cv2
import os
import asyncio
import shutil
from PIL import Image, ImageOps
import io
from typing import List
from fastapi import UploadFile, File, Form, HTTPException
import boto3
import os
from datetime import datetime
import time
import uuid
import json
from typing import List, Tuple
from models.weblink import WebLinks
from models.folders import Folder

from database import connect_db
from routes.folder import router as folder_router
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
app.include_router(folder_router)


# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
        
async def process_batch_parallel(
    batch_id,
    batch,
    app,
    reference_embedding,
    min_similarity,
    semaphore,
    loop
):
    async with semaphore:
        start = time.perf_counter()
        print(f"[{ts()}] 🚀 Batch-{batch_id} START | Images: {len(batch)}")

        results = await loop.run_in_executor(
            EXECUTOR,
            process_image_batch,
            app,
            batch,
            reference_embedding,
            min_similarity
        )

        end = time.perf_counter()
        print(
            f"[{ts()}] ✅ Batch-{batch_id} END | "
            f"Time: {(end - start):.2f}s | "
            f"Matches: {len(results)}"
        )

        return batch_id, results


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

        print(
            f"[{ts()}] 📂 S3 LIST DONE | "
            f"Images: {len(image_keys)} | "
            f"{time.perf_counter() - t0:.2f}s"
        )

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
                        f"Matches: {len(results)}"
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

def ensure_folder_exists(folder_path: str):
    """Create folder if it doesn't exist."""
    os.makedirs(folder_path, exist_ok=True)


# @app.on_event("startup")
# async def startup_event():
#     """Create necessary folders on startup."""
#     ensure_folder_exists("albums")
#     ensure_folder_exists("static")
#     print("✅ Server started - Albums and static folders ready")

@app.on_event("startup")
async def startup_event():
    # 🔥 MongoDB connect
    connect_db()
    print("✅ MongoDB connected")

    # existing logic
    ensure_folder_exists("albums")
    ensure_folder_exists("static")
    print("✅ Server started - Albums and static folders ready")
   


@app.post("/upload")
async def upload_images(
    folder_name: str = Form(...),
    images: List[UploadFile] = File(...)
):
    """
    Upload multiple images to a specific album folder with automatic resizing
    """

    # Validate folder name
    if ".." in folder_name or folder_name.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid folder name")

    folder_path = os.path.join("albums", folder_name)
    ensure_folder_exists(folder_path)

    results = []

    for image in images:
        # Validate filename
        if not image.filename:
            results.append({
                "status": "failed",
                "error": "No filename provided"
            })
            continue

        if not image.filename.lower().endswith(('.jpg', '.jpeg', '.png')):
            results.append({
                "filename": image.filename,
                "status": "failed",
                "error": "Only JPG, JPEG, and PNG files are allowed"
            })
            continue

        try:
            # Read image content
            content = await image.read()

            # Resize image
            resize_result = resizer.resize_image_bytes(content, image.filename)

            if not resize_result['success']:
                results.append({
                    "filename": image.filename,
                    "status": "failed",
                    "error": resize_result['error']
                })
                continue

            # Save resized image as JPG
            output_filename = os.path.splitext(image.filename)[0] + ".jpg"
            file_path = os.path.join(folder_path, output_filename)

            with open(file_path, "wb") as f:
                f.write(resize_result['image_bytes'])

            results.append({
                "status": "success",
                "filename": output_filename,
                "path": file_path,
                "resizing": {
                    "original_size": resizer.format_size(resize_result['original_size']),
                    "final_size": resizer.format_size(resize_result['final_size']),
                    "compression": f"{resize_result['compression_ratio']:.1f}%",
                    "original_dimensions": f"{resize_result['original_dimensions'][0]}×{resize_result['original_dimensions'][1]}",
                    "final_dimensions": f"{resize_result['final_dimensions'][0]}×{resize_result['final_dimensions'][1]}",
                    "quality": resize_result['final_quality']
                }
            })

        except Exception as e:
            results.append({
                "filename": image.filename,
                "status": "failed",
                "error": str(e)
            })

    return {
        "status": "completed",
        "folder": folder_name,
        "total_files": len(images),
        "results": results
    }


@app.get("/list")
async def list_all_albums():
    """
    List all albums with their images.
    
    Returns:
        Dictionary with album names and image lists
    """
    albums_folder = "albums"
    
    if not os.path.exists(albums_folder):
        return {"albums": {}}
    
    albums = {}
    
    try:
        for album_name in os.listdir(albums_folder):
            album_path = os.path.join(albums_folder, album_name)
            
            if not os.path.isdir(album_path):
                continue
            
            images = []
            for filename in os.listdir(album_path):
                if filename.lower().endswith(('.jpg', '.jpeg', '.png')):
                    images.append({
                        "filename": filename,
                        "url": f"/images/albums/{album_name}/{filename}"
                    })
            
            if images:
                albums[album_name] = {
                    "count": len(images),
                    "images": images
                }
        
        return {"albums": albums}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error listing albums: {str(e)}")


@app.get("/list/{album_name}")
async def list_album_images(album_name: str):
    """
    List all images in a specific album.
    
    Args:
        album_name: Name of the album folder
    
    Returns:
        List of images with metadata
    """
    # Validate album name
    if ".." in album_name or album_name.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid album name")
    
    album_path = os.path.join("albums", album_name)
    
    if not os.path.exists(album_path):
        raise HTTPException(status_code=404, detail=f"Album not found: {album_name}")
    
    images = []
    
    try:
        for filename in os.listdir(album_path):
            if filename.lower().endswith(('.jpg', '.jpeg', '.png')):
                file_path = os.path.join(album_path, filename)
                file_size = os.path.getsize(file_path)
                images.append({
                    "filename": filename,
                    "size": file_size,
                    "size_formatted": resizer.format_size(file_size),
                    "url": f"/images/albums/{album_name}/{filename}"
                })
        
        return {
            "album": album_name,
            "count": len(images),
            "images": images
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error listing images: {str(e)}")

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

@app.get("/images/albums/{album_name}/{image_name}")
async def get_image(album_name: str, image_name: str):
    """
    Serve static images from album folders.
    
    Args:
        album_name: Album folder name
        image_name: Image filename
    
    Returns:
        Image file
    """
    # Prevent directory traversal attacks
    if ".." in album_name or ".." in image_name:
        raise HTTPException(status_code=400, detail="Invalid path")
    
    image_path = os.path.join("albums", album_name, image_name)
    
    # Verify the file exists
    if not os.path.exists(image_path):
        raise HTTPException(status_code=404, detail=f"Image not found: {image_path}")
    
    # Verify it's in the correct directory
    real_path = os.path.realpath(image_path)
    real_album = os.path.realpath(os.path.join("albums", album_name))
    
    if not real_path.startswith(real_album):
        raise HTTPException(status_code=400, detail="Invalid path")
    
    # Verify it's an image file
    if not image_name.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.bmp')):
        raise HTTPException(status_code=400, detail="Invalid file type")
    
    return FileResponse(image_path)


@app.delete("/albums/{album_name}")
async def delete_album(album_name: str):
    """
    Delete an entire album folder.
    
    Args:
        album_name: Album folder to delete
    
    Returns:
        Success message
    """
    if ".." in album_name or album_name.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid album name")
    
    album_path = os.path.join("albums", album_name)
    
    if not os.path.exists(album_path):
        raise HTTPException(status_code=404, detail=f"Album not found: {album_name}")
    
    try:
        shutil.rmtree(album_path)
        return {"status": "success", "message": f"Album '{album_name}' deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deleting album: {str(e)}")


@app.delete("/images/albums/{album_name}/{image_name}")
async def delete_image(album_name: str, image_name: str):
    """
    Delete a specific image from an album.
    
    Args:
        album_name: Album folder name
        image_name: Image filename to delete
    
    Returns:
        Success message
    """
    if ".." in album_name or ".." in image_name:
        raise HTTPException(status_code=400, detail="Invalid path")
    
    image_path = os.path.join("albums", album_name, image_name)
    
    if not os.path.exists(image_path):
        raise HTTPException(status_code=404, detail=f"Image not found")
    
    try:
        os.remove(image_path)
        return {"status": "success", "message": f"Image '{image_name}' deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deleting image: {str(e)}")


# Mount static files before catch-all route
app.mount("/static", StaticFiles(directory="static"), name="static")


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
