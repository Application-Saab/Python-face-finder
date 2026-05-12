from mongoengine import (
    Document,
    EmbeddedDocument,
    StringField,
    ListField,
    EmbeddedDocumentField,
    DateTimeField,
    IntField
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
# ---------------------------
class SubFolder(EmbeddedDocument):
    _id = StringField(
        default=lambda: str(bson.ObjectId())
    )

    folderName = StringField(
        required=True
    )

    type = StringField(
        required=True,
        choices=["my_photos", "others"]
    )

    userId = StringField(
        required=True
    )

    folderDp = EmbeddedDocumentField(FolderDp)

    createdAt = DateTimeField(
        default=datetime.utcnow
    )


# ---------------------------
# Root: Folder
# ---------------------------
class Folder(Document):
    meta = {
        "collection": "folder",
        "indexes": [
            "viewedBy",
            "customerId",
            "vendorId",
            "eventId",
            "orderId",
            "subFolders.userId",
            {
                "fields": ["customerId", "eventId"],
                "unique": True,
                "partialFilterExpression": {
                    "eventId": {"$exists": True}
                }
            }
        ]
    }

    # _id as String ObjectId
    id = StringField(
        primary_key=True,
        default=lambda: str(bson.ObjectId())
    )

    folderName = StringField(
        required=True
    )

    viewedBy = ListField(
        StringField(),
        default=list
    )

    clickCount = IntField(
        default=0
    )

    customerId = StringField(
        required=True
    )

    vendorId = StringField()

    eventId = StringField()

    orderId = StringField()

    subFolders = ListField(
        EmbeddedDocumentField(SubFolder),
        default=list
    )

    createdAt = DateTimeField(
        default=datetime.utcnow
    )

    updatedAt = DateTimeField(
        default=datetime.utcnow
    )

