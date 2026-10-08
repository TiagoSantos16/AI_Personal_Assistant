"""Bounded media preparation, all writes confined to the claimed attempt."""
import json
import os
import re
import subprocess
import time
from pathlib import Path
import requests
from PIL import Image

MAX_DURATION = float(os.environ.get("MAX_MEDIA_DURATION_SECONDS", "300"))
MAX_BYTES = int(os.environ.get("MAX_MEDIA_BYTES", "104857600"))
MAX_TEMP = int(os.environ.get("MAX_TEMP_BYTES", "524288000"))
MAX_ITEMS = int(os.environ.get("MAX_MEDIA_ITEMS", "16"))
FFMPEG_TIMEOUT = float(os.environ.get("FFMPEG_TIMEOUT_SECONDS", "90"))
MAX_DIMENSION = int(os.environ.get("MAX_IMAGE_DIMENSION", "1280"))
Image.MAX_IMAGE_PIXELS = 25_000_000


def run(command):
    return subprocess.run(command, check=True, capture_output=True, timeout=FFMPEG_TIMEOUT)


def probe(path):
    value = json.loads(run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]).stdout)
    duration = float(value.get("format", {}).get("duration") or 0)
    if not 0 < duration <= MAX_DURATION:
        raise ValueError("Missing or excessive media duration")
    return duration, any(stream.get("codec_type") == "audio" for stream in value.get("streams", []))


def numerical(paths):
    return sorted(paths, key=lambda p: int(re.findall(r"\d+", Path(p).stem)[-1]))


def frame_times(duration, count):
    # Reserve full-duration coverage before refining dense segments.
    return [duration * (i + .5) / count for i in range(count)]


def frames(path, directory, duration, count):
    result = []
    for index, timestamp in enumerate(frame_times(duration, count)):
        target = directory / f"frame_{index:04d}.jpg"
        run(["ffmpeg", "-y", "-ss", str(timestamp), "-i", str(path), "-frames:v", "1",
             "-vf", f"scale={MAX_DIMENSION}:{MAX_DIMENSION}:force_original_aspect_ratio=decrease", "-q:v", "2", str(target)])
        result.append(str(target))
    return result


def download_options(directory):
    def bound(progress):
        if progress.get("downloaded_bytes", 0) > MAX_BYTES:
            raise ValueError("Download byte limit")
        if sum(p.stat().st_size for p in directory.rglob("*") if p.is_file()) > MAX_TEMP:
            raise ValueError("Temporary storage limit")
    def duration(info, *, incomplete=False):
        if info.get("duration", 0) and info["duration"] > MAX_DURATION:
            return "Duration limit exceeded"
    return {"outtmpl": str(directory / "media.%(ext)s"), "quiet": True,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4", "noplaylist": True, "max_filesize": MAX_BYTES,
        "socket_timeout": 15, "retries": 1, "fragment_retries": 1,
        "progress_hooks": [bound], "match_filter": duration}


def download_video(url, directory):
    import yt_dlp
    with yt_dlp.YoutubeDL(download_options(directory)) as downloader:
        info = downloader.extract_info(url, download=True)
        if not info:
            raise ValueError("No downloadable media")
        # yt-dlp records the postprocessed merged path in filepath/requested_downloads.
        candidates = [info.get("filepath")] + [r.get("filepath") for r in info.get("requested_downloads", [])]
        candidates += [str(directory / "media.mp4"), downloader.prepare_filename(info)]
    path = next((Path(p) for p in candidates if p and Path(p).is_file()), None)
    if path is None or path.stat().st_size > MAX_BYTES:
        raise ValueError("Missing merged output or byte limit")
    return path, info


def download_image(item, directory):
    thumbs = sorted(item.get("thumbnails", []), key=lambda t: (t.get("width") or 0) * (t.get("height") or 0), reverse=True)
    urls = [t["url"] for t in thumbs if t.get("url")] + [item.get("thumbnail"), item.get("url")]
    last = None
    for url in dict.fromkeys(u for u in urls if u):
        try:
            raw = directory / "image.raw"
            size = 0
            started = time.monotonic()
            with requests.get(url, stream=True, timeout=(10, 20)) as response:
                response.raise_for_status()
                with raw.open("wb") as handle:
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > MAX_BYTES or time.monotonic() - started > FFMPEG_TIMEOUT:
                            raise ValueError("Image byte/time limit")
                        handle.write(chunk)
            final = directory / "image.jpg"
            with Image.open(raw) as image:
                if image.width * image.height > 25_000_000 or max(image.size) > 16384:
                    raise ValueError("Source image dimensions exceed limit")
                image.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
                image.convert("RGB").save(final, quality=92)
            return str(final)
        except Exception as exc:
            last = exc
        finally:
            (directory / "image.raw").unlink(missing_ok=True)
    raise ValueError("Image extraction failed") from last


def _prepare(job, directory, transcribe, volume):
    from core.processor import _post_item_kind
    directory.mkdir(parents=True, exist_ok=True)
    if job["kind"] == "reel":
        path, info = download_video(job["url"], directory)
        entries = [(info, path)]
    else:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": False, "ignore_no_formats_error": True, "socket_timeout": 15, "retries": 1}) as downloader:
            info = downloader.extract_info(job["url"], download=False)
        entries = [(entry, None) for entry in (info.get("entries") or [info])]
    evidence = {"description": info.get("description") or "", "transcript": "", "images": [], "image_labels": [],
                "coverage": [], "creator": info.get("uploader") or "", "username": info.get("uploader_id") or info.get("channel") or ""}
    transcripts = []
    if len(entries) > MAX_ITEMS:
        evidence["coverage"].append(f"Only the first {MAX_ITEMS} of {len(entries)} media items were processed.")
    for index, (item, path) in enumerate(entries[:MAX_ITEMS], 1):
        target = directory / f"item_{index}"
        target.mkdir(exist_ok=True)
        try:
            if path or _post_item_kind(item) == "video":
                if not path:
                    path, _ = download_video(item.get("webpage_url") or item.get("url"), target)
                duration, audio = probe(path)
                if audio:
                    volume.commit()  # all file handles closed before GPU reload
                    try:
                        result = transcribe(str(path))
                        evidence["gpu_seconds"] = evidence.get("gpu_seconds", 0) + result.get("gpu_seconds", 0)
                        transcripts.append(f"Media item {index}:\n" + "\n".join(
                            f"[{s['start']:.1f}-{s['end']:.1f}s] {s['text'].strip()}" for s in result.get("segments", [])))
                    except Exception as exc:
                        evidence["gpu_measurement_incomplete"] = True
                        evidence["coverage"].append(f"Audio for media item {index} is unavailable ({type(exc).__name__}).")
                        from core.errors import redact
                        evidence.setdefault("diagnostics", []).append(redact(exc))
                count = int(os.environ.get("REEL_MAX_FRAMES", "20")) if job["kind"] == "reel" else 4
                count = max(1, min(count, 20))
                extracted = frames(path, target, duration, count)
                evidence["image_labels"].extend({"id": len(evidence["images"]) + i + 1, "media_item": index, "timestamp": timestamp}
                    for i, timestamp in enumerate(frame_times(duration, count)))
                evidence["images"].extend(extracted)
            else:
                evidence["images"].append(download_image(item, target))
                evidence["image_labels"].append({"id": len(evidence["images"]), "media_item": index})
        except Exception as exc:
            from core.errors import redact
            evidence["coverage"].append(f"Media {index} could not be fully extracted: {type(exc).__name__}.")
            evidence.setdefault("diagnostics", []).append(redact(exc))
        finally:
            if path:
                Path(path).unlink(missing_ok=True)
        if sum(p.stat().st_size for p in directory.rglob("*") if p.is_file()) > MAX_TEMP:
            raise ValueError("Attempt storage limit")
    evidence["transcript"] = "\n\n".join(transcripts)
    if not evidence["images"] and not evidence["transcript"] and not evidence["description"]:
        error = ValueError("No usable source evidence: " + "; ".join(evidence.get("diagnostics", [])))
        error.partial_evidence = evidence
        raise error
    volume.commit()
    return evidence


def prepare(job, directory, transcribe, volume):
    directory = Path(directory)
    try:
        return _prepare(job, directory, transcribe, volume)
    finally:
        # Only this attempt's download/merge scratch files; selected JPEG evidence
        # remains referenced by caches/records and must survive retry and redo.
        if directory.is_dir():
            for path in directory.rglob("*"):
                if path.is_file() and path.suffix.lower() != ".jpg":
                    path.unlink(missing_ok=True)
