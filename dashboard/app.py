import json
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "output_youtube"
PROGRESS_FILE = BASE_DIR / "progress.json"
HISTORY_FILE = BASE_DIR / "history.json"
CONFIG_FILE = BASE_DIR / "config.json"
CLIENT_SECRET_FILE = BASE_DIR / "client_secret.json"

run_thread = None

class GenerateRequest(BaseModel):
    topic: Optional[str] = None

class ConfigRequest(BaseModel):
    pexels_key: Optional[str] = None
    client_secret_json: Optional[str] = None
    llm_url: Optional[str] = None
    llm_model: Optional[str] = None
    llm_api_key: Optional[str] = None

def run_agent_task(topic: str = None):
    global run_thread
    log_path = BASE_DIR / "dashboard_agent.log"
    try:
        with open(log_path, "a") as log_file:
            log_file.write(f"\n--- NEW RUN STARTED AT {datetime.now()} ---\n")
            
            speaker_wav = "/app/custom_speakers/2026-03-31_092300902.wav"
            venv_python = str(BASE_DIR / ".venv" / "bin" / "python3")
            
            env = os.environ.copy()
            
            cmd = [venv_python, "youtube_agent.py", "--speaker-wav", speaker_wav]
            if topic:
                cmd.extend(["--topic", topic])
                
            process = subprocess.Popen(
                cmd,
                cwd=str(BASE_DIR),
                stdout=log_file,
                stderr=log_file,
                env=env,
                text=True
            )
            
            process.wait()
            log_file.write(f"--- RUN FINISHED WITH CODE {process.returncode} AT {datetime.now()} ---\n")
    except Exception as e:
        print(f"[Dashboard Error]: {e}")
        with open(PROGRESS_FILE, "w") as f:
            json.dump({"phase": "Error", "message": f"Critical Failure: {str(e)}", "timestamp": time.time()}, f)
    finally:
        run_thread = None

@app.get("/api/videos")
def get_videos():
    if not HISTORY_FILE.exists(): return []
    try:
        with open(HISTORY_FILE, "r") as f: return json.load(f)
    except: return []

@app.get("/api/progress")
def get_progress():
    status = {"phase": "Idle", "message": "Ready to generate.", "is_running": run_thread is not None}
    if not PROGRESS_FILE.exists(): return status
    
    try:
        with open(PROGRESS_FILE, "r") as f:
            data = json.load(f)
            data["is_running"] = run_thread is not None
            if time.time() - data.get("timestamp", 0) > 120 and run_thread is None:
                return status
            return data
    except:
        return {"phase": "Idle", "message": "Starting...", "is_running": run_thread is not None}

@app.get("/api/config")
def get_config():
    c = {"pexels_key": "", "llm_url": "", "llm_model": "", "llm_api_key": ""}
    client_secret = ""
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r") as f:
                saved = json.load(f)
                c.update(saved)
    except: pass
    try:
        if CLIENT_SECRET_FILE.exists():
            with open(CLIENT_SECRET_FILE, "r") as f:
                client_secret = f.read()
    except: pass
    c["client_secret_json"] = client_secret
    return c

@app.post("/api/config")
def save_config(req: ConfigRequest):
    c = {}
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f: c = json.load(f)
        except: pass
        
    if req.pexels_key is not None: c["pexels_key"] = req.pexels_key
    if req.llm_url is not None: c["llm_url"] = req.llm_url
    if req.llm_model is not None: c["llm_model"] = req.llm_model
    if req.llm_api_key is not None: c["llm_api_key"] = req.llm_api_key
    
    with open(CONFIG_FILE, "w") as f:
        json.dump(c, f, indent=2)
            
    if req.client_secret_json is not None:
        try:
            # Parse it just to ensure it's valid JSON before saving
            parsed = json.loads(req.client_secret_json)
            with open(CLIENT_SECRET_FILE, "w") as f:
                json.dump(parsed, f, indent=2)
        except Exception as e:
            raise HTTPException(status_code=400, detail="Invalid Client Secret JSON")
            
    return {"status": "saved"}

@app.post("/api/generate")
def trigger_generation(req: GenerateRequest, background_tasks: BackgroundTasks):
    global run_thread
    if run_thread is not None:
        raise HTTPException(status_code=400, detail="Generation already in progress.")
    
    with open(PROGRESS_FILE, "w") as f:
        json.dump({"phase": "Starting", "message": "Initializing agent...", "timestamp": time.time()}, f)
        
    run_thread = threading.Thread(target=run_agent_task, args=(req.topic,))
    run_thread.start()
    return {"status": "started"}

@app.get("/thumbnails/{video_folder}/{image_name}")
def get_thumbnail(video_folder: str, image_name: str):
    path = OUTPUT_DIR / video_folder / image_name
    if path.exists():
        return FileResponse(path)
    return HTTPException(status_code=404)

@app.get("/", response_class=HTMLResponse)
def serve_index():
    index_path = Path(__file__).parent / "index.html"
    return index_path.read_text()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
