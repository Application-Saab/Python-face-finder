from mongoengine import Document, StringField, ListField, DateTimeField
from datetime import datetime
import bson

class WebLinks(Document):
    meta = {
        "collection": "weblinks",
        "indexes": [
            "mainFolderId",
            "orderId",
            "orderById",
            "type",
            "folderIds"
        ]
    }

    id = StringField(primary_key=True, default=lambda: str(bson.ObjectId()))

    mainFolderId = StringField(null=True)

    orderId = StringField(required=True)
    orderById = StringField(required=True)
    orderByName = StringField()

    type = StringField(required=True, choices=["image", "video"])

    originalUrl = StringField(required=True)
    originalKey = StringField(required=True, unique=True)

    thumbnailImageUrl = StringField()
    thumbnailKey = StringField()

    videoClipUrl = StringField()
    videoClipKey = StringField()

    folderIds = ListField(StringField(), default=list)

    createdAt = DateTimeField(default=datetime.utcnow)
    updatedAt = DateTimeField(default=datetime.utcnow)
