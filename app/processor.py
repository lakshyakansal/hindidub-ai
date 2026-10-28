from __future__ import annotations
import os, shutil, subprocess, threading, time
from pathlib import Path
from typing import Any
ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = Path(os.getenv("WORK_DIR", "/tmp/hindidub-jobs"))
JOBS_DIR.mkdir(parents=True, exist_ok=True)
MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", str(30 * 60)))
def run_cmd(args: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess:
    """Run a command safely without a shell; raise useful errors."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required executable not found: {args[0]}. Install FFmpeg and add it to PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Command timed out: {args[0]}") from exc
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "No details returned")[-2500:]
        raise RuntimeError(f"Command failed ({args[0]}): {details}")
    return result


def probe_duration(path: Path) -> float:
    result = run_cmd([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ])
    try:
        return float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("Could not determine video duration. Check that the file is a valid video.") from exc


# Worker sets this callback to persist progress to Redis.
PROGRESS_HOOK = None
jobs: dict[str, dict[str, Any]] = {}
state_lock = threading.Lock()

def update_job(job_id: str, **changes: Any) -> None:
    with state_lock:
        if job_id in jobs:
            jobs[job_id].update(changes)
            jobs[job_id]["updated_at"] = time.time()
    if PROGRESS_HOOK:
        try:
            PROGRESS_HOOK(job_id, changes)
        except Exception:
            pass


def translate_to_hindi(text: str, translator: Any, tokenizer: Any, source_code: str) -> str:
    import torch
    if not text.strip():
        return ""
    tokenizer.src_lang = source_code
    encoded = tokenizer(text, return_tensors="pt", truncation=True, max_length=512)
    device = next(translator.parameters()).device
    encoded = {k: v.to(device) for k, v in encoded.items()}
    target_id = tokenizer.convert_tokens_to_ids("hin_Deva")
    with torch.no_grad():
        generated = translator.generate(**encoded, forced_bos_token_id=target_id, max_new_tokens=256)
    return tokenizer.batch_decode(generated, skip_special_tokens=True)[0].strip()


# Whisper language codes do not map 1:1 to NLLB language tags. Expand this map as needed.
NLLB_CODES = {
    "en": "eng_Latn", "hi": "hin_Deva", "es": "spa_Latn", "fr": "fra_Latn",
    "de": "deu_Latn", "it": "ita_Latn", "pt": "por_Latn", "ru": "rus_Cyrl",
    "zh": "zho_Hans", "ja": "jpn_Jpan", "ko": "kor_Hang", "ar": "arb_Arab",
    "tr": "tur_Latn", "nl": "nld_Latn", "pl": "pol_Latn", "uk": "ukr_Cyrl",
    "vi": "vie_Latn", "th": "tha_Thai", "id": "ind_Latn", "ms": "zsm_Latn",
    "bn": "ben_Beng", "ta": "tam_Taml", "te": "tel_Telu", "mr": "mar_Deva",
    "ur": "urd_Arab", "fa": "pes_Arab", "he": "heb_Hebr", "el": "ell_Grek",
    "sv": "swe_Latn", "da": "dan_Latn", "fi": "fin_Latn", "no": "nob_Latn",
    "cs": "ces_Latn", "ro": "ron_Latn", "hu": "hun_Latn", "sw": "swh_Latn",
}


def make_tts_model():
    """Load Chatterbox Multilingual TTS. First launch downloads its model weights."""
    import torch
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = ChatterboxMultilingualTTS.from_pretrained(device=device, t3_model="v3")
    return model, device


def fit_audio_to_duration(source_wav: Path, target_wav: Path, target_seconds: float, workdir: Path) -> None:
    """Time-stretch a speech segment to its slot, keeping pitch as much as FFmpeg allows."""
    if target_seconds <= 0.08:
        run_cmd(["ffmpeg", "-y", "-v", "error", "-i", str(source_wav), "-t", "0.08", str(target_wav)])
        return
    actual = probe_duration(source_wav)
    if actual <= 0:
        raise RuntimeError("TTS generated an empty audio segment.")
    # atempo is playback speed: >1 shortens; <1 lengthens. FFmpeg accepts 0.5..2 per filter.
    speed = actual / target_seconds
    filters: list[float] = []
    while speed > 2.0:
        filters.append(2.0)
        speed /= 2.0
    while speed < 0.5:
        filters.append(0.5)
        speed /= 0.5
    filters.append(speed)
    af = ",".join(f"atempo={v:.6f}" for v in filters)
    run_cmd(["ffmpeg", "-y", "-v", "error", "-i", str(source_wav), "-af", af,
             "-t", f"{target_seconds:.3f}", "-ar", "24000", "-ac", "1", str(target_wav)])


def process_job(job_id: str, source: Path, original_name: str) -> None:
    workdir = JOBS_DIR / job_id
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        update_job(job_id, status="processing", progress=3, message="Checking video and extracting audio…")
        duration = probe_duration(source)
        if duration > MAX_DURATION_SECONDS:
            raise RuntimeError(f"Video is {duration/60:.1f} minutes. This build supports up to {MAX_DURATION_SECONDS//60} minutes.")
        audio_path = workdir / "source.wav"
        run_cmd(["ffmpeg", "-y", "-v", "error", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", str(audio_path)])

        update_job(job_id, progress=8, message="Loading speech recognition model…")
        from faster_whisper import WhisperModel
        whisper_device = "cuda" if __import__("torch").cuda.is_available() else "cpu"
        compute_type = "float16" if whisper_device == "cuda" else "int8"
        whisper = WhisperModel(os.getenv("WHISPER_MODEL", "small"), device=whisper_device, compute_type=compute_type)
        segments_iter, info = whisper.transcribe(str(audio_path), vad_filter=True, beam_size=5)
        segments = list(segments_iter)
        if not segments:
            raise RuntimeError("No speech was detected in this video.")
        source_lang = info.language or "en"
        if source_lang not in NLLB_CODES:
            raise RuntimeError(f"Detected language '{source_lang}' is not mapped in this build. Add its NLLB language code to NLLB_CODES in app/main.py.")
        source_code = NLLB_CODES[source_lang]
        del whisper
        if whisper_device == "cuda":
            import torch
            torch.cuda.empty_cache()

        update_job(job_id, progress=15, message=f"Detected language: {source_lang}. Loading translation model…")
        import torch
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        translation_name = os.getenv("TRANSLATION_MODEL", "facebook/nllb-200-distilled-600M")
        tok = AutoTokenizer.from_pretrained(translation_name)
        trans_device = "cuda" if torch.cuda.is_available() else "cpu"
        translator = AutoModelForSeq2SeqLM.from_pretrained(translation_name).to(trans_device)
        translator.eval()

        translated_segments: list[tuple[Any, str]] = []
        total = len(segments)
        for idx, seg in enumerate(segments):
            original_text = (seg.text or "").strip()
            if not original_text:
                continue
            hindi_text = translate_to_hindi(original_text, translator, tok, source_code)
            if not hindi_text:
                hindi_text = original_text
            translated_segments.append((seg, hindi_text))
            update_job(job_id, progress=20 + int(15 * (idx + 1) / max(1, total)),
                       message=f"Translating segment {idx+1} of {total}…")
        del translator, tok
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        update_job(job_id, progress=37, message="Preparing voice reference and loading voice-cloning model…")
        ref_path = workdir / "voice_reference.wav"
        # Use the first clear few seconds of original audio as the voice prompt.
        run_cmd(["ffmpeg", "-y", "-v", "error", "-i", str(audio_path), "-ss", "0", "-t", "12",
                 "-ar", "24000", "-ac", "1", str(ref_path)])
        tts, tts_device = make_tts_model()
        from pydub import AudioSegment
        final_audio = AudioSegment.silent(duration=int(duration * 1000), frame_rate=24000).set_channels(1)
        translated_lines: list[str] = []
        segment_dir = workdir / "segments"
        segment_dir.mkdir(exist_ok=True)

        for idx, (seg, hindi_text) in enumerate(translated_segments):
            translated_lines.append(f"[{seg.start:07.2f} - {seg.end:07.2f}] {hindi_text}")
            # Chatterbox Multilingual uses language_id='hi' for Hindi in supported builds.
            raw_path = segment_dir / f"{idx:05d}_raw.wav"
            fitted_path = segment_dir / f"{idx:05d}_fit.wav"
            wav = tts.generate(hindi_text, language_id="hi", audio_prompt_path=str(ref_path))
            import torchaudio
            torchaudio.save(str(raw_path), wav.detach().cpu(), tts.sr)
            slot = max(0.25, float(seg.end - seg.start))
            fit_audio_to_duration(raw_path, fitted_path, slot, workdir)
            clip = AudioSegment.from_file(fitted_path).set_frame_rate(24000).set_channels(1)
            position_ms = max(0, int(float(seg.start) * 1000))
            # Overlay at original speech start; original video stream is never changed.
            final_audio = final_audio.overlay(clip, position=position_ms)
            progress = 37 + int(52 * (idx + 1) / max(1, len(translated_segments)))
            update_job(job_id, progress=progress, message=f"Generating Hindi voice for segment {idx+1} of {len(translated_segments)}…")

        hindi_wav = workdir / "hindi_dub.wav"
        final_audio.export(hindi_wav, format="wav")
        (workdir / "hindi_transcript.txt").write_text("\n".join(translated_lines), encoding="utf-8")
        output = workdir / "hindi_dubbed.mp4"
        update_job(job_id, progress=94, message="Muxing Hindi audio with original video frames…")
        # -c:v copy copies the compressed video stream. Audio is encoded to AAC for broad MP4 compatibility.
        run_cmd(["ffmpeg", "-y", "-v", "error", "-i", str(source), "-i", str(hindi_wav),
                 "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                 "-shortest", "-movflags", "+faststart", str(output)])
        update_job(job_id, status="completed", progress=100, message="Hindi dubbing is ready.",
                   output=f"/api/jobs/{job_id}/download", transcript=f"/api/jobs/{job_id}/transcript",
                   source_language=source_lang, duration=duration)
    except Exception as exc:
        update_job(job_id, status="failed", message=str(exc), error=str(exc))



