docker run -it --rm -p 8002:8000 -v $(pwd)/albums:/app/albums pkyad/face-recognition-server

docker build --no-cache -t pkyad/face-recognition-server .


# venv Creation
python -m venv venv
venv\Scripts\activate (CMD)
venv\Scripts\Activate.ps1 (PWSH)

# Dep Install
pip install -r requirements.txt

# Server Run
uvicorn main:app --reload
