from __future__ import annotations
import os, re, time, uuid
from pathlib import Path
from typing import Any
import boto3
import redis
from botocore.config import Config
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(2 * 1024**3)))
MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", "1800"))
REDIS_URL = os.getenv("REDIS_URL", "")
S3_BUCKET = os.getenv("S3_BUCKET", "")

app = FastAPI(title="HindiDub AI Cloud", version="1.0.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def clients():
    missing = [k for k in ("REDIS_URL", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY") if not os.getenv(k)]
    if missing:
        raise HTTPException(503, "Cloud services are not configured. Missing: " + ", ".join(missing))
    r = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=5)
    s3 = boto3.client(
        "s3", endpoint_url=os.getenv("S3_ENDPOINT_URL") or None,
        region_name=os.getenv("S3_REGION", "auto"),
        aws_access_key_id=os.getenv("S3_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("S3_SECRET_ACCESS_KEY"),
        config=Config(signature_version="s3v4", s3={"addressing_style": os.getenv("S3_ADDRESSING_STYLE", "path")}),
    )
    return r, s3


def status_key(job_id: str) -> str:
    return f"hindidub:job:{job_id}"

class PresignRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=240)
    size: int = Field(gt=0)
    content_type: str = "application/octet-stream"

class CreateJobRequest(BaseModel):
    job_id: str
    filename: str
    object_key: str

@app.get("/", response_class=HTMLResponse)
def home():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "web-api", "max_minutes": MAX_DURATION_SECONDS // 60,
            "max_upload_bytes": MAX_UPLOAD_BYTES, "processing": "external GPU worker"}

@app.post("/api/uploads/presign")
def presign(body: PresignRequest):
    if body.size > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Upload exceeds {MAX_UPLOAD_BYTES // (1024**3)} GB limit.")
    ext = Path(body.filename).suffix.lower()
    if ext not in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}:
        raise HTTPException(400, "Unsupported video type.")
    try:
        r, s3 = clients()
        job_id = uuid.uuid4().hex[:16]
        key = f"uploads/{job_id}/original{ext}"
        ctype = body.content_type if body.content_type.startswith("video/") else "application/octet-stream"
        url = s3.generate_presigned_url("put_object", Params={"Bucket": S3_BUCKET, "Key": key, "ContentType": ctype}, ExpiresIn=3600)
        r.hset(status_key(job_id), mapping={"id":job_id,"filename":Path(body.filename).name,"status":"uploading","progress":"0","message":"Upload directly to object storage.","created_at":str(time.time()),"object_key":key})
        r.expire(status_key(job_id), 7*24*3600)
        return {"job_id":job_id,"object_key":key,"upload_url":url,"upload_headers":{"Content-Type":ctype}}
    except HTTPException: raise
    except Exception as exc: raise HTTPException(503, f"Could not create upload URL: {exc}")

@app.post("/api/jobs")
def create_job(body: CreateJobRequest):
    if not re.fullmatch(r"[a-f0-9]{16}", body.job_id): raise HTTPException(400, "Invalid job ID.")
    try:
        r, s3 = clients()
        status = r.hgetall(status_key(body.job_id))
        if not status: raise HTTPException(404, "Upload session expired; upload again.")
        if status.get("object_key") != body.object_key: raise HTTPException(400, "Object key does not match upload session.")
        if status.get("status") != "uploading": raise HTTPException(409, "Job was already submitted.")
        head = s3.head_object(Bucket=S3_BUCKET, Key=body.object_key)
        if head.get("ContentLength", 0) <= 0 or head.get("ContentLength", 0) > MAX_UPLOAD_BYTES: raise HTTPException(413, "Uploaded file size is invalid.")
        try:
            s3.head_bucket(Bucket=S3_BUCKET)
        except Exception: pass
        now = str(time.time())
        r.hset(status_key(body.job_id), mapping={"filename":Path(body.filename).name,"status":"queued","progress":"0","message":"Queued for GPU processing.","updated_at":now,"size":str(head.get("ContentLength",0))})
        r.lpush("hindidub:queue", body.job_id)
        return {"id":body.job_id,"status":"queued"}
    except HTTPException: raise
    except Exception as exc: raise HTTPException(503, f"Could not queue job: {exc}")

@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    try:
        r, _ = clients(); job = r.hgetall(status_key(job_id))
        if not job: raise HTTPException(404, "Job not found or expired.")
        for k in ("progress", "duration", "created_at", "updated_at", "size"):
            if k in job:
                try: job[k] = float(job[k]) if k in ("duration", "created_at", "updated_at") else int(float(job[k]))
                except ValueError: pass
        return job
    except HTTPException: raise
    except Exception as exc: raise HTTPException(503, f"Status service unavailable: {exc}")

@app.get("/api/jobs/{job_id}/download")
def download_job(job_id: str):
    try:
        r, s3 = clients(); job = r.hgetall(status_key(job_id))
        if not job or job.get("status") != "completed" or not job.get("output_key"): raise HTTPException(404, "Dubbed video is not ready.")
        url = s3.generate_presigned_url("get_object", Params={"Bucket":S3_BUCKET,"Key":job["output_key"],"ResponseContentDisposition":f'attachment; filename="HindiDub_{job_id}.mp4"'}, ExpiresIn=3600)
        return RedirectResponse(url=url, status_code=307)
    except HTTPException: raise
    except Exception as exc: raise HTTPException(503, f"Download unavailable: {exc}")

@app.get("/api/jobs/{job_id}/transcript")
def download_transcript(job_id: str):
    try:
        r, s3 = clients(); job = r.hgetall(status_key(job_id))
        if not job or not job.get("transcript_key"): raise HTTPException(404, "Transcript is not ready.")
        url = s3.generate_presigned_url("get_object", Params={"Bucket":S3_BUCKET,"Key":job["transcript_key"],"ResponseContentDisposition":f'attachment; filename="HindiDub_{job_id}.txt"'}, ExpiresIn=3600)
        return RedirectResponse(url=url, status_code=307)
    except HTTPException: raise
    except Exception as exc: raise HTTPException(503, f"Transcript unavailable: {exc}")
