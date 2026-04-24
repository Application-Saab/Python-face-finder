from mongoengine import Document, EmbeddedDocument, StringField, ListField, EmbeddedDocumentField, DateTimeField
from datetime import datetime
import bson

class FolderDp(EmbeddedDocument):
    fileUrl = StringField()
    thumbnailUrl = StringField()
    s3Key = StringField()
    thumbnailKey = StringField()


class SubFolder(EmbeddedDocument):
    _id = StringField(default=lambda: str(bson.ObjectId()))
    folderName = StringField(required=True)
    type = StringField(required=True, choices=["my_photos", "others"])
    userId = StringField(required=True)
    folderDp = EmbeddedDocumentField(FolderDp)
    createdAt = DateTimeField(default=datetime.utcnow)


class EventInvites(Document):
    meta = {
        "collection": "eventInvites",
        "indexes": ["userId"]
    }

    id = StringField(primary_key=True, default=lambda: str(bson.ObjectId()))

    userId = StringField(required=True)

    subFolders = ListField(EmbeddedDocumentField(SubFolder), default=list)

    createdAt = DateTimeField(default=datetime.utcnow)
    updatedAt = DateTimeField(default=datetime.utcnow)