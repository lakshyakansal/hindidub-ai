# HindiDub AI — Render + External GPU Worker

A split deployment: Render hosts the lightweight web/API; a separate NVIDIA GPU machine runs the open-source AI pipeline. Uploads and outputs are stored in private S3-compatible object storage (Cloudflare R2 or AWS S3); Redis stores job state and queue entries. The API does not store large videos on Render's ephemeral disk.

## Architecture

Browser → Render FastAPI → presigned object-storage upload → Redis queue → external GPU worker → Whisper transcription → NLLB Hindi translation → Chatterbox multilingual voice cloning → pitch-preserving FFmpeg timing adjustment → FFmpeg mux with `-c:v copy` → object storage → presigned download.

## Required services

1. **Render Web Service** — deploy this repository using `render.yaml` or create a Python web service manually.
2. **Redis-compatible service** — Upstash Redis or another hosted Redis that accepts TLS connections. Set `REDIS_URL` for both API and worker.
3. **Private S3-compatible bucket** — Cloudflare R2 or AWS S3. Create access credentials limited to this bucket. Configure bucket CORS to allow your Render website origin for `PUT`, `GET`, and `HEAD` as needed, and allow `Content-Type` header. Example R2 endpoint: `https://<account-id>.r2.cloudflarestorage.com`. Set the same S3 variables for API and worker.
4. **External GPU machine** — an NVIDIA CUDA-capable VM (RunPod, Vast.ai, a GPU cloud VM, or your own NVIDIA Linux machine). The worker must have outbound access to Redis, object storage, and Hugging Face model downloads. Do not run the GPU worker on Render Free.

## Deploy web API to Render

1. Push this folder to a GitHub repository.
2. In Render, choose **New → Blueprint** and select the repository, or create a Python Web Service manually.
3. Build command: `pip install -r requirements-web.txt`
4. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Add `REDIS_URL`, `S3_BUCKET`, `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_REGION=auto`, and `S3_ADDRESSING_STYLE=path` as environment variables. Blueprint variables marked `sync: false` must be filled in the Render dashboard.
6. Health check path: `/api/health`.

The Render service only serves the UI/API and signs storage URLs; it does not load torch or the AI models.

## Configure storage

Create a **private** bucket. Never put bucket credentials in frontend JavaScript or commit them to Git. The presigned PUT URL is valid for one hour. Configure CORS for the exact Render domain, for example:

```json
[
  {"AllowedOrigins":["https://YOUR-SERVICE.onrender.com"],"AllowedMethods":["PUT","GET","HEAD"],"AllowedHeaders":["Content-Type","*"],"ExposeHeaders":["ETag"],"MaxAgeSeconds":3600}
]
```

Provider-specific CORS syntax can differ. Use the provider's dashboard/API. Presigned uploads avoid sending 2 GB video files through the Render web process. Storage and egress may incur charges; keep lifecycle cleanup enabled.

## Build and run the GPU worker

### Docker (recommended)

Install NVIDIA drivers and NVIDIA Container Toolkit on an Ubuntu GPU host, clone this repository, then run:

```bash
docker build -f Dockerfile.worker -t hindidub-worker .
docker run --gpus all --restart unless-stopped \
  -e REDIS_URL='rediss://...' \
  -e S3_BUCKET='your-private-bucket' \
  -e S3_ENDPOINT_URL='https://your-s3-endpoint' \
  -e S3_ACCESS_KEY_ID='...' \
  -e S3_SECRET_ACCESS_KEY='...' \
  -e S3_REGION='auto' \
  -e S3_ADDRESSING_STYLE='path' \
  -e WHISPER_MODEL='small' \
  hindidub-worker
```

Do not add `--rm`; use a secret manager or protected environment file rather than typing secrets into shared shell history. For an env file, use `--env-file /secure/path/hindidub.env` and lock down its permissions.

The image uses a CUDA-enabled PyTorch base image. Verify compatibility with the GPU host's NVIDIA driver. Model downloads require internet on first startup/job. The first job may take longer while weights download.

### Run worker without Docker (Ubuntu)

Install FFmpeg, Python 3.10/3.11, a compatible CUDA-enabled PyTorch/torchaudio build, then `pip install -r requirements-worker.txt`. Export the same Redis/S3 environment variables and run:

```bash
python -m worker.worker
```

Keep the worker running with systemd or a process supervisor. Only run one worker per GPU until memory and concurrency have been tested.

## User workflow

1. Open the Render URL.
2. Select a video (default max 2 GiB, 30 minutes).
3. The browser uploads directly to private object storage.
4. The API queues a job in Redis; the GPU worker updates progress.
5. Download the MP4 or Hindi transcript from the result card. Download URLs expire after one hour.

## Environment variables

- `REDIS_URL`: shared Redis connection string; use `rediss://` where supported.
- `S3_BUCKET`, `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_REGION`, `S3_ADDRESSING_STYLE`: shared object storage credentials/config.
- `MAX_UPLOAD_BYTES`: default 2 GiB.
- `MAX_DURATION_SECONDS`: default 1800 seconds.
- `WHISPER_MODEL`: `small` by default; `base` uses less memory, `medium` may improve recognition at greater cost.
- `TRANSLATION_MODEL`: defaults to `facebook/nllb-200-distilled-600M`.
- `WORK_DIR`: worker local temporary directory, defaults to `/workspace/jobs`.

## Important caveats

- **Not perfectly identical voice:** voice cloning approximates timbre and identity; pitch, accent, emotion and pronunciation can vary. Use only a voice you have permission to clone.
- **Timing is approximate:** segments are fitted to their detected start/end slots. Large duration changes can sound rushed or unnatural; this is not lip-sync editing.
- **Frames are preserved:** FFmpeg copies the original compressed video stream (`-c:v copy`) and replaces the audio stream. Some unusual containers/codecs may require a compatible output container or remux.
- **Background sound is replaced:** the current processor replaces the entire original audio track; background music and sound effects are not retained.
- **Language coverage:** `NLLB_CODES` includes common languages only; unmapped detected languages fail with an error.
- **GPU/RAM:** a 30-minute video can take a long time and needs sufficient GPU VRAM, CPU RAM and temporary disk. Start with a 1–2 minute test. A 12-second voice reference is extracted from the beginning of the audio and may be poor if the opening has no clean speech.
- **License:** NLLB-200 distilled 600M is CC-BY-NC 4.0; the default stack is not automatically cleared for commercial use. Review all model and dependency licenses before commercial deployment.
- **Prototype security:** add authentication, quotas/rate limiting, CSRF/CORS hardening, abuse prevention, deletion/retention policies, object-key isolation, and monitoring before public launch. The sample API has no user accounts and should be treated as a controlled demo, not a production multi-tenant service.
- **Storage cleanup:** configure bucket lifecycle rules for uploads/outputs. Redis status expires after seven days; object lifecycle must be configured separately.
- **Retries:** this basic worker processes a queued job once; production deployments should add retry/dead-letter handling and stale-job recovery.

## API endpoints

- `GET /api/health`
- `POST /api/uploads/presign` JSON: `{ "filename": "clip.mp4", "size": 1234, "content_type": "video/mp4" }`
- Browser performs `PUT` to the returned `upload_url` using returned headers.
- `POST /api/jobs` JSON: `{ "job_id": "...", "filename": "clip.mp4", "object_key": "uploads/..." }`
- `GET /api/jobs/{job_id}`
- `GET /api/jobs/{job_id}/download` returns a short-lived download URL as JSON.
- `GET /api/jobs/{job_id}/transcript` returns a short-lived transcript URL as JSON.

## Troubleshooting

- **CORS/PUT fails:** configure object storage CORS for the exact deployed origin and `Content-Type`.
- **Worker stays idle:** check shared Redis URL, queue connectivity and that the web API and worker use the same Redis database.
- **S3 signature mismatch:** check endpoint, region, addressing style and system clock. For R2, region is usually `auto` and path-style addressing often works.
- **CUDA OOM:** use `WHISPER_MODEL=base`, ensure only one job is running, or use a GPU with more VRAM. NLLB and TTS model memory is significant.
- **Missing FFmpeg:** confirm `ffmpeg -version` and `ffprobe -version` inside the worker container.
- **Model API changed:** check upstream Chatterbox docs and ensure the pinned model package matches the code.
