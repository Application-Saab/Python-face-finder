from fastapi import APIRouter
from models.folders import Folder

router = APIRouter()

@router.get("/folders")
def get_folders(customerId: str):
    return Folder.objects(customerId=customerId)
