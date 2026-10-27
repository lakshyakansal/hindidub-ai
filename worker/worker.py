from __future__ import annotations
import json, os, shutil, signal, time, traceback
from pathlib import Path
import boto3, redis
from botocore.config import Config
from app import processor

REDIS_URL = os.environ["REDIS_URL"]
S3_BUCKET = os.environ["S3_BUCKET"]
WORK_DIR = Path(os.getenv("WORK_DIR", "/workspace/jobs")); WORK_DIR.mkdir(parents=True, exist_ok=True)
r = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=10, socket_keepalive=True)
s3 = boto3.client("s3", endpoint_url=os.getenv("S3_ENDPOINT_URL") or None,
    region_name=os.getenv("S3_REGION", "auto"), aws_access_key_id=os.getenv("S3_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("S3_SECRET_ACCESS_KEY"),
    config=Config(signature_version="s3v4", s3={"addressing_style": os.getenv("S3_ADDRESSING_STYLE", "path")}))
processor.JOBS_DIR = WORK_DIR


def persist(job_id: str, changes: dict):
    safe = {k: str(v) for k, v in changes.items() if isinstance(v, (str, int, float, bool))}
    safe["updated_at"] = str(time.time())
    if safe: r.hset(f"hindidub:job:{job_id}", mapping=safe)


def process(job_id: str):
    key = f"hindidub:job:{job_id}"
    meta = r.hgetall(key)
    if not meta: return
    work = WORK_DIR / job_id
    shutil.rmtree(work, ignore_errors=True); work.mkdir(parents=True)
    source_key = meta.get("object_key")
    ext = Path(meta.get("filename", "video.mp4")).suffix.lower() or ".mp4"
    source = work / f"original{ext}"
    try:
        persist(job_id, {"status":"processing","progress":1,"message":"GPU worker downloading source video…"})
        s3.download_file(S3_BUCKET, source_key, str(source))
        processor.jobs[job_id] = {"id":job_id,"filename":meta.get("filename","video.mp4"),"status":"processing","progress":2,"message":"Starting AI pipeline"}
        processor.PROGRESS_HOOK = persist
        processor.process_job(job_id, source, meta.get("filename", "video.mp4"))
        result = dict(processor.jobs.get(job_id, {}))
        if result.get("status") != "completed":
            persist(job_id, {"status":"failed","message":result.get("message","Processing failed"),"error":result.get("error",result.get("message","Processing failed"))})
            return
        output = work / "hindi_dubbed.mp4"
        transcript = work / "hindi_transcript.txt"
        output_key = f"outputs/{job_id}/hindi_dubbed.mp4"
        transcript_key = f"outputs/{job_id}/hindi_transcript.txt"
        persist(job_id, {"progress":96,"message":"Uploading completed files to object storage…"})
        s3.upload_file(str(output), S3_BUCKET, output_key, ExtraArgs={"ContentType":"video/mp4"})
        if transcript.exists(): s3.upload_file(str(transcript), S3_BUCKET, transcript_key, ExtraArgs={"ContentType":"text/plain; charset=utf-8"})
        persist(job_id, {"status":"completed","progress":100,"message":"Hindi dubbing is ready.","output_key":output_key,"transcript_key":transcript_key,"duration":result.get("duration",0),"source_language":result.get("source_language","unknown")})
    except Exception as exc:
        traceback.print_exc()
        persist(job_id, {"status":"failed","message":str(exc)[:1200],"error":str(exc)[:3000]})
    finally:
        processor.PROGRESS_HOOK = None
        processor.jobs.pop(job_id, None)
        shutil.rmtree(work, ignore_errors=True)


def main():
    print("HindiDub GPU worker online; waiting for jobs", flush=True)
    while True:
        try:
            item = r.brpop("hindidub:queue", timeout=5)
            if item:
                _, job_id = item
                print(f"Processing job {job_id}", flush=True)
                process(job_id)
        except KeyboardInterrupt:
            break
        except Exception:
            traceback.print_exc(); time.sleep(5)

if __name__ == "__main__": main()
