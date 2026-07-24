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
    BooleanField,
    DynamicField,
)
from datetime import datetime
import bson


# ---------------------------
# Embedded: FolderDp
# ---------------------------
class FolderDp(EmbeddedDocument):
    meta = {"strict": False}
    fileUrl = StringField()

    thumbnailUrl = StringField()

    s3Key = StringField()

    thumbnailKey = StringField()


# ---------------------------
# Embedded: SubFolder
# ---------------------------
class SubFolder(EmbeddedDocument):

    meta = {"strict": False}
    _id = StringField(
        default=lambda: str(bson.ObjectId())
    )

    folderName = StringField(
        required=True
    )

    personCount = IntField(default=0)

    isPersonFolder = BooleanField(default=False)

    type = StringField(
        required=True,
        choices=["my_photos", "others"]
    )

    userId = StringField(
        required=True
    )


    folderDp = EmbeddedDocumentField(
        FolderDp
    )

    isLocker = BooleanField(
        default=False
    )

    createdAt = DateTimeField(
        default=datetime.utcnow
    )


# ---------------------------
# Embedded: DeviceTracking
# ---------------------------
class DeviceTracking(EmbeddedDocument):
    meta = {"strict": False}
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
        "strict": False,
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

    bannerImageUrl = StringField()

    viewedBy = DynamicField()

    clickCount = IntField(
        default=0
    )

    customerId = StringField(
        required=True
    )

    vendorId = StringField()

    eventId = StringField()

    orderId = StringField()

    subFolders = EmbeddedDocumentListField(
        SubFolder,
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