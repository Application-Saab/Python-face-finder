from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import StreamingResponse
import numpy as np
from PIL import Image
import io
import asyncio
import json
from datetime import datetime
from numpy.linalg import norm

from insightface.app import FaceAnalysis

from models.event_posts import EventPosts
from models.event_invites import EventInvites

from concurrent.futures import ThreadPoolExecutor
import boto3
import os

router = APIRouter()

AWS_REGION = os.getenv("AWS_REGION", "eu-north-1")
S3_BUCKET = os.getenv("S3_BUCKET_NAME", "photography-hora")

s3 = boto3.client("s3", region_name=AWS_REGION)
EXECUTOR = ThreadPoolExecutor(max_workers=3)


# -------------------------
# Helpers
# -------------------------
def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log(msg):
    print(f"[{ts()}] {msg}", flush=True)


def get_event_images(eventId):
    log(f"?? Fetching images from DB for eventId={eventId}")
    posts = EventPosts.objects(eventId=eventId).only("postWebpKey")

    keys = [post.postWebpKey for post in posts if post.postWebpKey]

    log(f"?? Total images from DB: {len(keys)}")

    if len(keys) > 0:
        log(f"?? Sample keys: {keys[:3]}")

    return keys


def read_s3_image(key):
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        img_bytes = obj["Body"].read()
        img = np.array(Image.open(io.BytesIO(img_bytes)).convert("RGB"))
        return img
    except Exception as e:
        log(f"? S3 read failed for {key}: {e}")
        return None


async def read_s3_async(key, loop):
    return await loop.run_in_executor(EXECUTOR, read_s3_image, key)


def process_image(app, fname, img, ref_emb, min_sim):
    if img is None:
        return []

    try:
        faces = app.get(img)
    except Exception as e:
        log(f"? Face detection failed for {fname}: {e}")
        return []

    results = []

    for face in faces:
        try:
            sim = np.dot(ref_emb, face.embedding) / (
                norm(ref_emb) * norm(face.embedding)
            )

            if sim >= min_sim:
                results.append((fname, sim * 100))
        except Exception as e:
            log(f"? Similarity error in {fname}: {e}")

    return results


def delete_subfolder(eventId, subfolder_id):
    log(f"?? Deleting subfolder: {subfolder_id}")
    EventInvites.objects(id=eventId).update_one(
        pull__subFolders___id=subfolder_id
    )


# -------------------------
# Face Search Class
# -------------------------
class EventFaceSearcher:
    def __init__(self):
        log("?? Initializing FaceAnalysis model...")
        self.app = FaceAnalysis(
            name="buffalo_s",
            providers=["CPUExecutionProvider"]
        )
        self.app.prepare(ctx_id=0, det_size=(640, 640))
        self.min_similarity = 0.3
        log("? Model loaded successfully")

    async def stream_search(self, eventId, ref_emb, subFolderId):
        log("?? Starting face search stream...")

        loop = asyncio.get_running_loop()

        image_keys = get_event_images(eventId)

        if not image_keys:
            log("?? No images found in DB")
            yield f"data: {json.dumps({'type': 'complete', 'totalMatches': 0})}\n\n"
            return

        match_count = 0

        tasks = []

        for key in image_keys:
            async def task(k=key):
                log(f"?? Fetching image: {k}")

                img = await read_s3_async(k, loop)

                if img is None:
                    log(f"? Image load failed: {k}")
                    return []

                result = await loop.run_in_executor(
                    EXECUTOR,
                    process_image,
                    self.app,
                    k,
                    img,
                    ref_emb,
                    self.min_similarity
                )

                return result

            tasks.append(asyncio.create_task(task()))

        log(f"?? Total async tasks created: {len(tasks)}")

        for coro in asyncio.as_completed(tasks):
            results = await coro

            for fname, conf in results:
                match_count += 1

                log(f"? Match found: {fname} | {conf:.2f}%")

                try:
                    EventPosts.objects(postWebpKey=fname).update_one(
                        add_to_set__folderIds=subFolderId
                    )
                except Exception as e:
                    log(f"? DB update failed for {fname}: {e}")

                data = {
                    "type": "match",
                    "file": fname,
                    "confidence": round(float(conf), 2),
                    "matchNo": int(match_count)
                }

                yield f"data: {json.dumps(data)}\n\n"

        log(f"?? Search complete. Total matches: {match_count}")

        yield f"data: {json.dumps({'type': 'complete', 'totalMatches': match_count})}\n\n"


searcher = EventFaceSearcher()


# -------------------------
# ROUTE
# -------------------------
@router.post("/event-face-search")
async def event_face_search(
    sample_image: UploadFile = File(...),
    eventId: str = Form(...),
    subFolderId: str = Form(...)
):
    log("?? New request received")

    try:
        content = await sample_image.read()
        img = Image.open(io.BytesIO(content)).convert("RGB")

        log("?? Detecting face in uploaded image...")

        faces = searcher.app.get(np.array(img))

        if not faces:
            log("? No face found in uploaded image")
            delete_subfolder(eventId, subFolderId)
            raise HTTPException(status_code=400, detail="No face found")

        ref_emb = faces[0].embedding
        log("? Reference face embedding created")

    except Exception as e:
        log(f"? Error processing input image: {e}")
        delete_subfolder(eventId, subFolderId)
        raise HTTPException(status_code=400, detail=str(e))

    return StreamingResponse(
        searcher.stream_search(eventId, ref_emb, subFolderId),
        media_type="text/event-stream"
    )