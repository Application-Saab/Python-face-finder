from mongoengine import (
    Document,
    EmbeddedDocument,
    StringField,
    ListField,
    EmbeddedDocumentField,
    DateTimeField
)
from datetime import datetime
import bson


# ---------------------------
# Embedded: FolderDp
# ---------------------------
class FolderDp(EmbeddedDocument):
    fileUrl = StringField(required=True)
    thumbnailUrl = StringField(required=True)
    s3Key = StringField()
    thumbnailKey = StringField()


# ---------------------------
# Embedded: SubFolder
# (NODE uses _id → we also use _id)
# ---------------------------
class SubFolder(EmbeddedDocument):
    _id = StringField(default=lambda: str(bson.ObjectId()))
    folderName = StringField(required=True)
    type = StringField(required=True, choices=["my_photos", "others"])
    userId = StringField(required=True)
    folderDp = EmbeddedDocumentField(FolderDp)
    createdAt = DateTimeField(default=datetime.utcnow)


# ---------------------------
# Root: Folder
# ---------------------------
class Folder(Document):
    meta = {
        "collection": "folder",
        "indexes": [
            "customerId",
            "vendorId",
            "eventId",
            "orderId",
            {
                "fields": ["customerId", "eventId"],
                "unique": True,
                "partialFilterExpression": {
                    "eventId": {"$exists": True}
                }
            }
        ]
    }

    # NODE: _id as string ObjectId
    id = StringField(primary_key=True, default=lambda: str(bson.ObjectId()))

    folderName = StringField(required=True)

    customerId = StringField(required=True)
    vendorId = StringField()
    eventId = StringField()
    orderId = StringField()

    subFolders = ListField(EmbeddedDocumentField(SubFolder), default=list)

    createdAt = DateTimeField(default=datetime.utcnow)
    updatedAt = DateTimeField(default=datetime.utcnow)
