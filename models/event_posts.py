from mongoengine import Document, StringField, DateTimeField, ListField
from datetime import datetime
import bson

class EventPosts(Document):
    meta = {
        "collection": "event-posts",
        "indexes": [
            "eventId",
            "postById",
            "postType",
            "folderIds"
        ]
    }

    id = StringField(primary_key=True, default=lambda: str(bson.ObjectId()))

    eventId = StringField(required=True)
    fileId = StringField(unique=True, sparse=True)

    status = StringField(
        choices=["uploading", "done", "failed"],
        default="uploading"
    )

    postById = StringField(required=True)
    postByName = StringField(required=True)

    postUrl = StringField(required=True)
    postKey = StringField(required=True)

    postWebpUrl = StringField(required=True)
    postWebpKey = StringField(required=True)

    postType = StringField(
        choices=["selfUploaded", "thankYouNote", "postBadge", "luckyDraw"],
        required=True
    )

    folderIds = ListField(StringField(), default=list)

    createdAt = DateTimeField(default=datetime.utcnow)
    updatedAt = DateTimeField(default=datetime.utcnow)