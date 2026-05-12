from mongoengine import (
    Document,
    StringField,
    ListField,
    DateTimeField,
    IntField
)
from datetime import datetime
import bson


class WebLinks(Document):
    meta = {
        "collection": "weblinks",
        "indexes": [
            "mainFolderId",
            "driveFileId",
            "orderId",
            "orderById",
            "type",
            "folderIds",
            "likedBy",
            {
                "fields": ["driveFileId", "orderId"],
                "unique": True
            }
        ]
    }

    id = StringField(
        primary_key=True,
        default=lambda: str(bson.ObjectId())
    )

    mainFolderId = StringField(
        default=""
    )

    driveFileId = StringField()

    orderId = StringField(
        required=True
    )

    fileId = StringField(
        unique=True,
    )

    status = StringField(
        choices=["uploading", "done", "failed"],
        default="uploading"
    )

    retryCount = IntField(
        default=0
    )

    orderById = StringField()

    orderByName = StringField()

    type = StringField(
        choices=["image", "video"],
        default=""
    )

    originalUrl = StringField()

    # removed unique=True because empty string duplicates cause E11000
    originalKey = StringField(
        default=""
    )

    thumbnailImageUrl = StringField()

    thumbnailKey = StringField()

    videoClipUrl = StringField()

    videoClipKey = StringField()

    folderIds = ListField(
        StringField(),
        default=list
    )

    likedBy = ListField(
        StringField(),
        default=list
    )

    downloadCount = IntField(
        default=0
    )

    shareCount = IntField(
        default=0
    )

    createdAt = DateTimeField(
        default=datetime.utcnow
    )

    updatedAt = DateTimeField(
        default=datetime.utcnow
    )
