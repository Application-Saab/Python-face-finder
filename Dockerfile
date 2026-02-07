# =========================================
# Stage 1 — Build base image with dependencies
# =========================================
FROM python:3.11-slim

# Avoid interactive prompts during build
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies required for dlib, face_recognition, and Pillow
RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    libopenblas-dev \
    liblapack-dev \
    libx11-dev \
    libgtk-3-dev \
    libboost-all-dev \
    python3-dev \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# =========================================
# Stage 2 — Python dependencies
# =========================================
WORKDIR /app

# Create a requirements.txt inline for reproducibility
# (you can also keep it as a separate file)
RUN echo "\
fastapi==0.115.0\n\
uvicorn[standard]==0.30.6\n\
face_recognition==1.3.0\n\
pillow==10.3.0\n\
python-multipart\
" > requirements.txt

RUN pip install --no-cache-dir -r requirements.txt

# =========================================
# Stage 3 — Copy application
# =========================================
COPY . /app

# Ensure albums directory exists at runtime
RUN mkdir -p /app/albums

# Expose FastAPI port
EXPOSE 8000

# Default command — run FastAPI app
CMD ["python", "server.py"]
