from mongoengine import (
    Document,
    EmbeddedDocument,
    StringField,
    ListField,
    EmbeddedDocumentField,
    EmbeddedDocumentListField,
    DateTimeField,
    IntField,
    FloatField,
    ObjectIdField,
    BooleanField
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

    folderName = StringField(required=True)

    type = StringField(
        required=True,
        choices=["my_photos", "others"]
    )

    userId = StringField(required=True)

    personId = StringField()   # <-- ADD THIS

    isLocker = BooleanField(default=False)

    personCount = IntField(default=0)

    folderDp = EmbeddedDocumentField(FolderDp)

    createdAt = DateTimeField(
        default=datetime.utcnow
    )

# ---------------------------
# Embedded: DeviceTracking
# ---------------------------
class DeviceTracking(EmbeddedDocument):
    _id = ObjectIdField()

    userId = StringField()

    deviceType = StringField(
        choices=["ios", "android"]
    )

    trackedAt = DateTimeField(
        default=datetime.utcnow
    )


# ---------------------------
# Root: Folder
# ---------------------------
class Folder(Document):
    meta = {
        "collection": "folders",
        "indexes": [
            "viewedBy",
            "customerId",
            "vendorId",
            "eventId",
            "orderId",
            "subFolders.userId",
            "deviceTracking.userId",
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

    shareCapsuleCount = IntField(
    default=0
    )

    vendorId = StringField()

    eventId = StringField()

    orderId = StringField()

    subFolders = ListField(
        EmbeddedDocumentField(SubFolder),
        default=list
    )

    deviceTracking = EmbeddedDocumentListField(
        DeviceTracking,
        default=list
    )

    version = IntField(
        db_field="__v",
        default=0
    )

    totalPersonCount = IntField(default=0)


    createdAt = DateTimeField(
        default=datetime.utcnow
    )

    updatedAt = DateTimeField(
        default=datetime.utcnow
    )