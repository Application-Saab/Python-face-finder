from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import StreamingResponse
import numpy as np
from PIL import Image
import io
import asyncio
import json
import time
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
EXECUTOR = ThreadPoolExecutor(max_workers=1)


# -------------------------
# Helpers
# -------------------------
def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def list_s3_images(prefix: str):
    keys = []
    paginator = s3.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.lower().endswith(".webp") and "/thumb_" in key.lower():
                keys.append(key)

    return keys


def read_s3_image(key):
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        img_bytes = obj["Body"].read()
        return np.array(Image.open(io.BytesIO(img_bytes)).convert("RGB"))
    except:
        return None


async def read_s3_async(key, loop):
    return await loop.run_in_executor(EXECUTOR, read_s3_image, key)


def process_batch(app, batch, ref_emb, min_sim):
    results = []
    for fname, img in batch:
        if img is None:
            continue

        faces = app.get(img)
        for face in faces:
            sim = np.dot(ref_emb, face.embedding) / (
                norm(ref_emb) * norm(face.embedding)
            )
            if sim >= min_sim:
                results.append((fname, sim * 100))

    return results


def delete_subfolder(eventId, subfolder_id):
    EventInvites.objects(id=eventId).update_one(
        pull__subFolders___id=subfolder_id
    )


# -------------------------
# Face Search Class
# -------------------------
class EventFaceSearcher:
    def __init__(self):
        self.app = FaceAnalysis(name="buffalo_s", providers=["CPUExecutionProvider"])
        self.app.prepare(ctx_id=0, det_size=(640, 640))
        self.min_similarity = 0.3

    def build_prefix(self, eventId):
        return f"events/{eventId}/"

    async def stream_search(self, prefix, ref_emb, subFolderId):
        loop = asyncio.get_running_loop()

        image_keys = list_s3_images(prefix)

        tasks = []
        match_count = 0

        for key in image_keys:
            async def task(k=key):
                img = await read_s3_async(k, loop)
                result = await loop.run_in_executor(
                    EXECUTOR,
                    process_batch,
                    self.app,
                    [(k, img)],
                    ref_emb,
                    self.min_similarity
                )
                return result

            tasks.append(asyncio.create_task(task()))

        for coro in asyncio.as_completed(tasks):
            results = await coro

            for fname, conf in results:
                match_count += 1

                # ✅ UPDATE eventPosts
                EventPosts.objects(postWebpKey=fname).update_one(
                    add_to_set__folderIds=subFolderId
                )

                yield f"data: {json.dumps({
                    'type': 'match',
                    'file': fname,
                    'confidence': round(conf, 2),
                    'matchNo': match_count
                })}\n\n"

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
    try:
        content = await sample_image.read()
        img = Image.open(io.BytesIO(content)).convert("RGB")

        faces = searcher.app.get(np.array(img))
        if not faces:
            delete_subfolder(eventId, subFolderId)
            raise HTTPException(400, "No face found")

        ref_emb = faces[0].embedding

    except Exception as e:
        delete_subfolder(eventId, subFolderId)
        raise HTTPException(400, str(e))

    prefix = searcher.build_prefix(eventId)

    return StreamingResponse(
        searcher.stream_search(prefix, ref_emb, subFolderId),
        media_type="text/event-stream"
    )