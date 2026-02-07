# database.py
import os
from mongoengine import connect
from dotenv import load_dotenv

load_dotenv()

def connect_db():
    connect(
        host=os.getenv("DATABASE_URL"),
        uuidRepresentation="standard"
    )
