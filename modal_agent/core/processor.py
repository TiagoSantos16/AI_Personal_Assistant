import contextlib
import logging
import os
import re
import subprocess
import time
import uuid

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from pydantic import BaseModel, Field

from core.config import CRITIC_MODELS, VISION_CONTENT_MODELS
from core.llm import (
    ProviderChainError,
    _status_of,
    build_vision_messages,
    get_llm,
    invoke_with_retry,
    is_quota_error,
    quota_reset_seconds,
)
from core.rag import index_note, remove_from_index

NOTES_DIR = "/data/notes"
IMAGES_DIR = "/data/images"
FAILED_DIR = "/data/failed"

SCENE_THRESHOLD = 0.25
FRAME_WIDTH = 512
MAX_FRAMES = 8
REEL_SAMPLE_INTERVAL_SEC = 0.75
REEL_MIN_HAMMING_DIST = 10
REEL_HASH_SIZE = 8
REEL_MAX_FRAMES = 20
REEL_MIN_KEEP_GAP_SEC = 1.5
REEL_ANCHOR_INTERVAL_SEC = 2.0
REEL_MIN_ANCHOR_GAP_SEC = 1.0
REEL_ANCHOR_LOOKBACK_SEC = 0.5
REEL_ANCHOR_LOOKAHEAD_SEC = 2.0
MAX_POST_IMAGES = 10
MAX_POST_MEDIA = 16
MAX_POST_VIDEO_FRAMES = 4
POST_FRAME_INTERVAL_SEC = 1.5
POST_MIN_HAMMING_DIST = 12
POST_HASH_SIZE = 8
POST_PREDESCRIBE_CHUNK_SIZE = 4
MIN_SPEECH_WORDS = 5
MIN_WORDS_PER_SECOND = 0.5

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def media_kind(url: str) -> str:
    """Return 'post' for /p/ URLs, 'reel' otherwise."""
    path = url.split("?")[0]
    return "post" if "/p/" in path else "reel"


class Note(BaseModel):
    title: str = Field(description="Short, punchy title for the note")
    category: str = Field(description="One of: Culinary, Travel, Entertainment, Coding, Finance, Career, General")
    markdown_content: str = Field(description="Full markdown summary from multi-agent workflow")


def download_video(url: str) -> tuple[str, dict]:
    import yt_dlp

    logger.info(f"Downloading video from {url}")
    output = f"/tmp/{uuid.uuid4()}"
    opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": f"{output}.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "merge_output_format": "mp4",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
    logger.info(f"Video downloaded to {path}")
    return path, info


def _post_candidate_frames(video_path: str, tmpdir: str) -> list[str]:
    """Single ffmpeg pass, then all-kept dHash selection, capped."""
    from PIL import Image

    def dhash(image, hash_size=POST_HASH_SIZE):
        resample = getattr(Image, "Resampling", Image).LANCZOS
        small = image.convert("L").resize((hash_size + 1, hash_size), resample)
        pixels = small.tobytes()
        bits = 0
        for row in range(hash_size):
            offset = row * (hash_size + 1)
            for col in range(hash_size):
                bits = (bits << 1) | (pixels[offset + col] > pixels[offset + col + 1])
        return bits

    def hamming(a, b):
        return bin(a ^ b).count("1")

    pattern = os.path.join(tmpdir, "cand_%03d.jpg")
    command = [
        "ffmpeg", "-y", "-i", video_path,
        "-vf", f"fps=1/{POST_FRAME_INTERVAL_SEC:g},scale='min(640,iw)':-2",
        "-q:v", "3", pattern,
    ]
    subprocess.run(command, check=True, capture_output=True)
    candidates = sorted(Path(tmpdir).glob("cand_*.jpg"))
    selected = []
    selected_hashes = []
    for candidate in candidates:
        with Image.open(candidate) as image:
            current = dhash(image)
        distance = min((hamming(current, h) for h in selected_hashes), default=None)
        if distance is None or distance >= POST_MIN_HAMMING_DIST:
            selected.append(candidate)
            selected_hashes.append(current)
            if len(selected) >= MAX_POST_VIDEO_FRAMES:
                break
    return [str(path) for path in selected]


def _post_visual_summary(image_paths: list[str]) -> str:
    prompt = (
        "List every visible title, name, number, and label verbatim from these images. "
        "Do not summarize, paraphrase, or merge items. One item per line. "
        "If an image has no text, list its main visual subject in one short line."
    )
    summaries = []
    for start in range(0, len(image_paths), POST_PREDESCRIBE_CHUNK_SIZE):
        chunk = image_paths[start:start + POST_PREDESCRIBE_CHUNK_SIZE]
        response = invoke_with_retry(
            get_llm(VISION_CONTENT_MODELS),
            build_vision_messages(prompt, chunk),
        )
        summaries.append(f"Media {start + 1}-{start + len(chunk)}:\n{str(response.content or '').strip()}")
    return "\n\n".join(summaries)


def _post_needs_raw_images(description: str, visual_summary: str, note_text: str) -> bool:
    prompt = (
        "You are a completeness critic. Decide whether this note misses visual subjects that are clearly "
        "present in the extracted list or description. Reply exactly REVISE_FRAMES if raw images are needed. "
        "Reply exactly OK otherwise.\n\n"
        f"Description:\n{description}\n\nExtracted visual list:\n{visual_summary}\n\nNote:\n{note_text}"
    )
    response = invoke_with_retry(get_llm(CRITIC_MODELS), prompt)
    return "REVISE_FRAMES" in str(response.content or "").upper()


def _post_item_kind(item) -> str:
    vcodec = str(item.get("vcodec") or "").lower()
    if vcodec and vcodec != "none":
        return "video"
    for fmt in item.get("formats") or []:
        fmt_vcodec = str(fmt.get("vcodec") or "").lower()
        if fmt_vcodec and fmt_vcodec != "none":
            return "video"
    item_url = str(item.get("url") or "")
    if Path(urlparse(item_url).path).suffix.lower() in (".mp4", ".mov", ".webm", ".mkv", ".m4v"):
        return "video"
    return "image"


def _post_download_image(item) -> str | None:
    import shutil
    import urllib.request

    thumbs = sorted(
        [t for t in (item.get("thumbnails") or []) if t.get("url")],
        key=lambda t: (t.get("width") or 0) * (t.get("height") or 0),
        reverse=True,
    )
    urls = [t["url"] for t in thumbs]
    if item.get("thumbnail"):
        urls.append(item["thumbnail"])
    if item.get("url"):
        urls.append(str(item["url"]))
    urls = list(dict.fromkeys(urls))

    for image_url in urls:
        try:
            request = urllib.request.Request(
                image_url,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.instagram.com/"},
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read()
            if not data:
                continue
            raw = os.path.join(IMAGES_DIR, f"{uuid.uuid4().hex}.raw")
            with open(raw, "wb") as f:
                f.write(data)
            final = os.path.join(IMAGES_DIR, f"{uuid.uuid4().hex}.jpg")
            try:
                subprocess.run(["ffmpeg", "-y", "-i", raw, "-q:v", "2", final], check=True, capture_output=True)
                os.remove(raw)
                return final
            except subprocess.CalledProcessError:
                os.remove(raw)
                continue
        except Exception:
            continue
    return None


def _post_download_video(item) -> str | None:
    import yt_dlp

    url = str(item.get("url") or "")
    if not url:
        for fmt in item.get("formats") or []:
            if fmt.get("url"):
                url = str(fmt["url"])
                break
    if not url:
        return None

    output = f"/tmp/{uuid.uuid4()}"
    attempts = [
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "best",
    ]
    for fmt in attempts:
        opts = {
            "format": fmt,
            "outtmpl": f"{output}.%(ext)s",
            "noplaylist": True,
            "quiet": True,
            "merge_output_format": "mp4",
            "ignore_no_formats_error": True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if not info:
                    continue
                path = ydl.prepare_filename(info)
            if os.path.exists(path):
                return path
        except Exception as exc:
            logger.warning(f"Video download attempt failed ({fmt}): {exc}")
    return None


def download_post(url: str) -> tuple[list[str], dict]:
    """Download images (and video frames) from an Instagram post or carousel."""
    import shutil

    import yt_dlp

    logger.info(f"Downloading post from {url}")
    tmpdir = f"/tmp/{uuid.uuid4()}"
    os.makedirs(tmpdir, exist_ok=True)

    opts = {
        "quiet": True,
        "noplaylist": False,
        "ignore_no_formats_error": True,
        "extract_flat": False,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            post_info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise RuntimeError(f"Could not read post metadata: {exc}") from exc

    entries = post_info.get("entries") or [post_info]
    logger.info(f"Found {len(entries)} entries in post")

    all_paths: list[str] = []
    image_count = 0

    for index, item in enumerate(entries, 1):
        if len(all_paths) >= MAX_POST_MEDIA:
            logger.warning(f"Reached MAX_POST_MEDIA limit ({MAX_POST_MEDIA})")
            break

        if _post_item_kind(item) == "video":
            video_path = _post_download_video(item)
            if video_path:
                frame_tmp = os.path.join(tmpdir, f"frames_{uuid.uuid4().hex}")
                os.makedirs(frame_tmp, exist_ok=True)
                try:
                    frames = _post_candidate_frames(video_path, frame_tmp)
                except subprocess.CalledProcessError as exc:
                    logger.warning(f"Video frames failed for item {index}, trying image: {exc}")
                    os.remove(video_path)
                    frames = []
                else:
                    if frames:
                        for frame in frames:
                            if len(all_paths) >= MAX_POST_MEDIA:
                                break
                            dst_path = os.path.join(IMAGES_DIR, f"{uuid.uuid4().hex}.jpg")
                            shutil.move(frame, dst_path)
                            all_paths.append(dst_path)
                        os.remove(video_path)
                        continue
                    os.remove(video_path)
            if image_count < MAX_POST_IMAGES:
                image_path = _post_download_image(item)
                if image_path:
                    all_paths.append(image_path)
                    image_count += 1
        else:
            if image_count >= MAX_POST_IMAGES:
                logger.warning(f"Reached MAX_POST_IMAGES limit ({MAX_POST_IMAGES})")
                continue
            image_path = _post_download_image(item)
            if not image_path:
                continue
            all_paths.append(image_path)
            image_count += 1

    shutil.rmtree(tmpdir, ignore_errors=True)
    logger.info(f"Downloaded {len(all_paths)} media items from post")
    return all_paths, post_info


def _scene_times(video_path: str) -> list[float]:
    filter_graph = f"movie={video_path},select='gt(scene,{SCENE_THRESHOLD})'"
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-f",
        "lavfi",
        "-i",
        filter_graph,
        "-show_frames",
        "-show_entries",
        "frame=pts_time",
        "-of",
        "csv=p=0",
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    times = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            try:
                times.append(float(line))
            except ValueError:
                continue
    return times


def _transcript_reliable(segments: list[dict]) -> bool:
    if not segments:
        return False
    words = sum(len(s.get("text", "").split()) for s in segments)
    duration = segments[-1].get("end", 0) - segments[0].get("start", 0)
    if words < MIN_SPEECH_WORDS:
        return False
    if duration > 0 and words / duration < MIN_WORDS_PER_SECOND:
        return False
    return True


def _even_times(duration: float, count: int) -> list[float]:
    if duration <= 0 or count <= 0:
        return []
    step = duration / (count + 1)
    return [step * i for i in range(1, count + 1)]


def _spread_times(times: list[float], count: int) -> list[float]:
    times = sorted(times)
    if len(times) <= count:
        return times
    step = (len(times) - 1) / (count - 1) if count > 1 else 0
    return sorted({times[round(step * i)] for i in range(count)})


def _quietest_times(cuts: list[float], segments: list[dict], count: int) -> list[float]:
    def distance(t: float) -> float:
        best = float("inf")
        for s in segments:
            start, end = s.get("start", 0), s.get("end", 0)
            if start <= t <= end:
                return 0.0
            best = min(best, abs(t - start), abs(t - end))
        return best

    chosen: list[float] = []
    for t in sorted(cuts, key=distance, reverse=True):
        if chosen and min(abs(t - c) for c in chosen) < 1.5:
            continue
        chosen.append(t)
        if len(chosen) >= count:
            break
    return sorted(chosen)


def pick_frames(scene_times: list[float], segments: list[dict], duration: float, reliable: bool) -> list[float]:
    if reliable:
        if scene_times:
            return _quietest_times(scene_times, segments, 6) or _spread_times(scene_times, 6)
        return _even_times(duration, 4)
    return _even_times(duration, MAX_FRAMES)


def _extract_frames_at(video_path: str, times: list[float], base: str) -> list[str]:
    os.makedirs(IMAGES_DIR, exist_ok=True)
    paths = []
    for i, t in enumerate(times, 1):
        out = os.path.join(IMAGES_DIR, f"{base}_{i:04d}.jpg")
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            str(t),
            "-i",
            video_path,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-vf",
            f"scale={FRAME_WIDTH}:-1",
            out,
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        paths.append(out)
    return paths


def _reel_anchor_times(segments: list[dict], scene_times: list[float]) -> list[float]:
    anchors = []
    for segment in segments:
        start = float(segment.get("start") or 0)
        end = float(segment.get("end") or start)
        anchors.append(start + 0.25)
        anchors.append((start + end) / 2)
        anchors.append(max(start, end - 0.25))
        cursor = start + REEL_ANCHOR_INTERVAL_SEC
        while cursor < end:
            anchors.append(cursor)
            cursor += REEL_ANCHOR_INTERVAL_SEC
    anchors.extend(scene_times)
    ordered = sorted(anchors)
    kept = []
    last = None
    for value in ordered:
        if last is None or value - last >= REEL_MIN_ANCHOR_GAP_SEC:
            kept.append(value)
            last = value
    return kept


def _reel_candidate_frames(video_path: str, tmpdir: str) -> list[tuple[str, float]]:
    pattern = os.path.join(tmpdir, "cand_%03d.jpg")
    command = [
        "ffmpeg", "-y", "-i", video_path,
        "-vf", f"fps=1/{REEL_SAMPLE_INTERVAL_SEC:g},scale='min(640,iw)':-2,showinfo",
        "-q:v", "3", pattern,
    ]
    result = subprocess.run(command, check=True, capture_output=True)
    stderr = result.stderr.decode("utf-8", errors="replace")
    pts = [float(m.group(1)) for m in re.finditer(r"pts_time:([0-9.]+)", stderr)]
    candidates = sorted(Path(tmpdir).glob("cand_*.jpg"))
    if len(pts) == len(candidates):
        return list(zip([str(p) for p in candidates], pts))
    return [(str(p), index * REEL_SAMPLE_INTERVAL_SEC) for index, p in enumerate(candidates)]


def _reel_select_frames(candidates, anchors, base: str) -> list[str]:
    import shutil
    from PIL import Image

    def dhash(image, hash_size=REEL_HASH_SIZE):
        resample = getattr(Image, "Resampling", Image).LANCZOS
        small = image.convert("L").resize((hash_size + 1, hash_size), resample)
        pixels = small.tobytes()
        bits = 0
        for row in range(hash_size):
            offset = row * (hash_size + 1)
            for col in range(hash_size):
                bits = (bits << 1) | (pixels[offset + col] > pixels[offset + col + 1])
        return bits

    def hamming(a, b):
        return bin(a ^ b).count("1")

    os.makedirs(IMAGES_DIR, exist_ok=True)
    selected_paths = []
    selected_hashes = []
    used = set()
    last_kept_time = None

    for anchor in anchors:
        if len(selected_paths) >= REEL_MAX_FRAMES:
            break
        window = [
            (index, time_value)
            for index, (_, time_value) in enumerate(candidates)
            if index not in used
            and anchor - REEL_ANCHOR_LOOKBACK_SEC <= time_value <= anchor + REEL_ANCHOR_LOOKAHEAD_SEC
        ]
        if last_kept_time is not None:
            window = [item for item in window if item[1] - last_kept_time >= REEL_MIN_KEEP_GAP_SEC]
        if not window:
            continue

        scored = []
        for index, time_value in window:
            with Image.open(candidates[index][0]) as image:
                current = dhash(image)
            score = 0 if not selected_hashes else min(hamming(current, h) for h in selected_hashes)
            scored.append((score, index, time_value, current))

        if selected_hashes:
            score, index, time_value, current = max(scored, key=lambda item: item[0])
            if score < REEL_MIN_HAMMING_DIST:
                continue
        else:
            score, index, time_value, current = min(scored, key=lambda item: abs(item[2] - anchor))

        destination = os.path.join(IMAGES_DIR, f"{base}_{len(selected_paths) + 1:04d}.jpg")
        shutil.move(candidates[index][0], destination)
        selected_paths.append(destination)
        selected_hashes.append(current)
        used.add(index)
        last_kept_time = time_value

    return selected_paths


def extract_frames_for_state(video_path: str, result: dict) -> tuple[list[str], bool]:
    import tempfile
    import shutil

    segments = result.get("segments", [])
    duration = result.get("duration") or (segments[-1]["end"] if segments else 0)
    reliable = _transcript_reliable(segments)

    scene_times: list[float] = []
    try:
        scene_times = _scene_times(video_path)
        logger.info(f"Scene detection found {len(scene_times)} cuts")
    except subprocess.CalledProcessError as exc:
        logger.warning(f"Scene detection failed, falling back to time-based frames: {exc}")

    anchors = _reel_anchor_times(segments, scene_times)
    if not anchors:
        anchors = _even_times(duration, MAX_FRAMES)

    base = os.path.splitext(os.path.basename(video_path))[0]
    tmpdir = tempfile.mkdtemp()
    try:
        try:
            candidates = _reel_candidate_frames(video_path, tmpdir)
        except subprocess.CalledProcessError as exc:
            logger.warning(f"Candidate extraction failed, falling back to time-based frames: {exc}")
            return _extract_frames_at(video_path, _even_times(duration, MAX_FRAMES), base), reliable
        if not candidates:
            return _extract_frames_at(video_path, _even_times(duration, MAX_FRAMES), base), reliable
        frames = _reel_select_frames(candidates, anchors, base)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    logger.info(f"Selected {len(frames)} frames (audio reliable={reliable})")
    return frames, reliable


def transcribe_with_timestamps(file_path: str) -> dict:
    import whisper

    logger.info(f"Transcribing {file_path} with whisper turbo")
    model = whisper.load_model("turbo")
    result = model.transcribe(file_path, word_timestamps=False)
    logger.info(f"Transcription complete, {len(result.get('segments', []))} segments")
    return result


def _slugify(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")


def _build_metadata_section(url: str, models_used: list[str], meta: dict | None = None) -> str:
    meta = meta or {}
    lines = ["\n---\n", "**Generation Info:**\n"]
    lines.append(f"- **Source URL:** {url}\n")
    if meta.get("creator"):
        lines.append(f"- **Creator:** {meta['creator']}\n")
    if meta.get("username"):
        lines.append(f"- **Creator URL:** https://www.instagram.com/{meta['username']}\n")
    if meta.get("audio_mode"):
        lines.append(f"- **Audio:** {meta['audio_mode']}\n")
    if meta.get("platform"):
        lines.append(f"- **Platform:** {meta['platform']}\n")

    frame_images = meta.get("frame_images") or []
    lines.append(f"**Frames ({len(frame_images)}):**\n")
    for p in frame_images:
        lines.append(f"- `{os.path.basename(p)}`\n")

    if meta.get("transcript"):
        lines.append(f"**Transcript:**\n\n{meta['transcript']}\n\n")

    writer_history = meta.get("writer_history") or []
    critique_history = meta.get("critique_history") or []
    if writer_history or critique_history:
        lines.append("**Writer & Critic Loop:**\n")
        rounds = max(len(writer_history), len(critique_history))
        for i in range(rounds):
            if i < len(writer_history):
                lines.append(f"**Round {i + 1} - Writer:**\n\n{writer_history[i]}\n\n")
            if i < len(critique_history):
                lines.append(f"**Round {i + 1} - Critic:**\n\n{critique_history[i]}\n\n")

    model_log = meta.get("model_log") or []
    if model_log:
        lines.append("**Models Used:**\n")
        for entry in model_log:
            agent = entry.get("agent", "")
            rnd = entry.get("round", 1)
            if agent in ("culinary", "travel", "entertainment", "coding", "general"):
                label = f"writer ({agent}) {rnd}"
            else:
                label = f"{agent} {rnd}".strip()
            model = entry.get("model", "")
            lines.append(f"- **{label}:** `{model}`\n")
            tried = ", ".join(f"`{c}`" for c in (entry.get("candidates") or [model]))
            lines.append(f"  - tried: {tried}\n")
    elif models_used:
        lines.append("**Models Used:**\n")
        for m in models_used:
            lines.append(f"- `{m}`\n")
    return "".join(lines)


def save_note(
    note: Note, volume, url: str = "", models_used: list[str] | None = None, meta: dict | None = None
) -> str:
    filename = f"{_slugify(note.category)}_{_slugify(note.title)}.md"
    metadata = _build_metadata_section(url, models_used or [], meta)
    content = f"# {note.title}\n\n**Category:** {note.category}\n\n{note.markdown_content}{metadata}\n"
    os.makedirs(NOTES_DIR, exist_ok=True)
    with open(f"{NOTES_DIR}/{filename}", "w", encoding="utf-8") as f:
        f.write(content)
    volume.commit()
    logger.info(f"Note saved: {filename}")
    try:
        index_note(f"{NOTES_DIR}/{filename}", volume)
    except Exception as exc:
        logger.warning(f"Indexing failed for {filename}, note kept as saved: {exc}")
    return filename


def _failed_path(note_name: str) -> str:
    dst = f"{FAILED_DIR}/{note_name}"
    if not os.path.exists(dst):
        return dst
    base, ext = os.path.splitext(note_name)
    return f"{FAILED_DIR}/{base}-{uuid.uuid4().hex[:8]}{ext}"


def move_to_failed(
    note_name: str,
    volume,
    error: str,
    url: str = "",
    models_used: list[str] | None = None,
    meta: dict | None = None,
) -> None:
    os.makedirs(FAILED_DIR, exist_ok=True)
    dst_name = note_name if note_name.endswith(".md") else f"{note_name}.md"
    label = note_name.removesuffix(".md")
    src = f"{NOTES_DIR}/{dst_name}"
    dst = _failed_path(dst_name)
    if os.path.exists(src):
        lines = [f"- **Source URL:** {url}"] if url else []
        with open(src, "r", encoding="utf-8") as f:
            lines.append(f.read().rstrip())
        os.remove(src)
        remove_from_index(dst_name, volume)
        content = "\n\n".join(lines)
    elif meta and any(meta.values()):
        content = _build_metadata_section(url, models_used or [], meta).strip()
    else:
        content = f"- **Source URL:** {url}" if url else ""
    with open(dst, "w", encoding="utf-8") as f:
        f.write(f"# FAILED: {label}\n\n**Error:** {error}\n\n---\n\n{content}\n")
    volume.commit()
    logger.warning(f"Moved failed note to {dst}: {error}")


def read_notes() -> list[str]:
    try:
        return sorted(f for f in os.listdir(NOTES_DIR) if f.endswith(".md"))
    except FileNotFoundError:
        return []


def read_failed() -> list[str]:
    try:
        return sorted(f for f in os.listdir(FAILED_DIR) if f.endswith(".md"))
    except FileNotFoundError:
        return []


def find_notes(query: str) -> list[str]:
    q = query.lower()
    matches = []
    for name in read_notes():
        with open(f"{NOTES_DIR}/{name}", encoding="utf-8") as f:
            if q in f.read().lower():
                matches.append(name)
    return matches


def send_telegram_message(chat_id: int | None, text: str) -> None:
    if not chat_id:
        return
    import requests

    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)


def _trim(text: str, limit: int = 280) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "\u2026"


def _raw_hint(text: str) -> str | None:
    match = re.search(r"(?:'|\")raw(?:'|\"):\s*(?:'|\")([^'\"]*)", text)
    if match:
        return match.group(1)
    match = re.search(r"(?:'|\")message(?:'|\"):\s*(?:'|\")([^'\"]*)", text)
    return match.group(1) if match else None


def _format_wait(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    if seconds < 90:
        return "about a minute"
    if seconds < 3600:
        minutes = max(1, int(seconds // 60))
        return f"about {minutes} minute" if minutes == 1 else f"about {minutes} minutes"
    reset_at = datetime.fromtimestamp(time.time() + seconds, tz=timezone.utc)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    human = f"about {hours} hour" if hours == 1 else f"about {hours} hours"
    if minutes:
        human += f" {minutes} minutes"
    return f"{human} (resets at {reset_at.strftime('%H:%M')} UTC)"


def _quota_scope(exc: Exception) -> str:
    """day | minute | unknown — from OpenRouter's error text."""
    if isinstance(exc, ProviderChainError):
        texts = " ".join(str(err).lower() for _, err in exc.failures)
    else:
        texts = str(exc).lower()
    if "per-day" in texts or "per_day" in texts or "daily" in texts:
        return "day"
    if "per-min" in texts or "per_min" in texts:
        return "minute"
    return "unknown"


def _seconds_until_utc_midnight() -> float:
    now = time.time()
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    next_midnight = midnight.timestamp() + 86400
    return next_midnight - now


def _quota_message(exc: Exception, wait: str | None, scope: str, status: int | None) -> str:
    if status == 402:
        return (
            "The AI account is out of credits, so the note wasn't finished.\n\n"
            "It is saved in the Failed list on the dashboard — once credits are added on "
            "OpenRouter you can retry it right away from there."
        )
    if scope == "minute":
        return (
            "The free AI models hit the per-minute rate limit, so the note wasn't finished.\n\n"
            "It is saved in the Failed list on the dashboard. "
            f"You can send reels again in {wait or 'about a minute'}."
        )
    if scope == "day":
        if wait is None:
            wait = _format_wait(_seconds_until_utc_midnight())
        return (
            "Free AI limit reached for today, so the note wasn't finished.\n\n"
            "It is saved in the Failed list on the dashboard. "
            f"You can send reels again in {wait}."
        )
    return (
        "The free AI models are at capacity right now, so the note wasn't finished.\n\n"
        "It is saved in the Failed list on the dashboard. "
        f"Try again in {wait or 'a few minutes'}."
    )


def _error_context(exc: Exception, media: str = "reel") -> dict:
    status = _status_of(exc)
    text = str(exc)
    lowered = text.lower()
    detail = _trim(_raw_hint(text) or text, 300)

    if isinstance(exc, ProviderChainError):
        lines = [
            f"- `{model}`: {_trim(_raw_hint(str(err)) or str(err), 140)}" for model, err in exc.failures
        ]
        if is_quota_error(exc):
            wait = _format_wait(quota_reset_seconds(exc))
            return {
                "lead": _quota_message(exc, wait, _quota_scope(exc), _status_of(exc.failures[0][1])),
                "action": "",
                "detail": "",
                "calm": True,
            }
        return {
            "lead": f"all {len(exc.failures)} AI models that could handle this step failed",
            "action": f"resend the {media}, or see the detail below and the Failed section of the dashboard",
            "detail": "\n".join(lines),
        }

    if is_quota_error(exc):
        wait = _format_wait(quota_reset_seconds(exc))
        return {
            "lead": _quota_message(exc, wait, _quota_scope(exc), status),
            "action": "",
            "detail": "",
            "calm": True,
        }

    if status in (401, 403):
        return {
            "lead": "the assistant's AI key is no longer accepted by the provider",
            "action": "update the OPENROUTER_API_KEY secret on Modal",
            "detail": f"HTTP {status} \u00b7 {detail}",
        }
    if status == 404:
        return {
            "lead": "one of my AI models was retired by its provider",
            "action": "refresh the model list in core/config.py, then resend the " + media,
            "detail": f"HTTP {status} \u00b7 {detail}",
        }
    if status in (400, 422):
        return {
            "lead": "the AI provider rejected the request (likely a provider quirk)",
            "action": f"resend the {media} \u2014 it usually works on a retry",
            "detail": f"HTTP {status} \u00b7 {detail}",
        }
    if status in (408, 500, 502, 503, 504):
        return {
            "lead": "the AI provider was briefly unavailable (I retried automatically)",
            "action": f"send the {media} again in a minute if it still failed",
            "detail": f"HTTP {status} \u00b7 {detail}",
        }
    if status is None and any(
        marker in lowered
        for marker in (
            "login",
            "private",
            "sign in to confirm",
            "is not available",
            "unavailable",
            "rate-limit",
            "rate limit",
            "region blocked",
            "copyright",
        )
    ):
        return {
            "lead": f"I couldn't download that {media} \u2014 Instagram blocked the fetch",
            "action": f"make sure the {media} is public, otherwise Instagram needs the download session refreshed",
            "detail": detail,
        }
    if status is None and any(
        name in lowered for name in ("ffmpeg", "whisper", "calledprocesserror", "oscarerror", "subprocess")
    ):
        return {
            "lead": f"I couldn't read the {media}'s audio while transcribing",
            "action": f"resend the {media} \u2014 if it keeps failing the video file may be damaged",
            "detail": detail,
        }
    return {
        "lead": f"something went wrong while processing the {media}",
        "action": f"resend the {media}, or find the full error in the Failed section of the dashboard",
        "detail": detail,
    }


def _error_message(exc: Exception, media: str = "reel") -> str:
    ctx = _error_context(exc, media)
    if ctx.get("calm"):
        return ctx["lead"]
    return (
        f"Couldn't process that {media} \u2014 {ctx['lead']}.\n\n"
        f"What you can do: {ctx['action']}.\n\n"
        f"Detail: {ctx['detail']}"
    )


def process_reel(url: str, chat_id: int | None, volume) -> None:
    from core.graph import reel_graph

    logger.info(f"Processing reel: {url}")
    transcript = ""
    segments = []
    keyframes = []
    creator = ""
    username = ""
    reliable = False
    try:
        video_path, info = download_video(url)
        creator = str(info.get("uploader") or "").strip()
        username = str(info.get("channel") or "").strip().lstrip("@")
        description = str(info.get("description") or "").strip()
        try:
            result = transcribe_with_timestamps(video_path)
            transcript = result["text"]
            segments = result.get("segments", [])
            keyframes, reliable = extract_frames_for_state(video_path, result)

            initial_state = {
                "url": url,
                "video_path": video_path,
                "transcript": transcript,
                "segments": segments,
                "description": description,
                "images": keyframes,
                "title": "",
                "category": "",
                "notes": [],
                "writer_history": [],
                "critique": "",
                "critique_history": [],
                "model_log": [],
                "retries": 0,
                "final_note": None,
                "error": None,
                "models_used": [],
            }
            final_state = reel_graph.invoke(initial_state)
            logger.info(f"Graph finished. Models used: {final_state.get('models_used', [])}")

            title = final_state.get("title") or "Reel Note"
            note = Note(
                title=title[:80],
                category=final_state["category"].title(),
                markdown_content=final_state["final_note"] or "No content generated.",
            )
            audio_mode = "none" if not segments else ("ok" if reliable else "weak")
            meta = {
                "creator": creator or "",
                "username": username or "",
                "audio_mode": audio_mode,
                "frame_images": keyframes,
                "transcript": transcript,
                "platform": "Instagram Reel",
                "critique_history": final_state.get("critique_history") or [],
                "writer_history": final_state.get("writer_history") or [],
                "model_log": final_state.get("model_log") or [],
            }
            save_note(note, volume, url, final_state.get("models_used", []), meta)
            send_telegram_message(chat_id, f"Done! Saved note: {note.title}")
            logger.info(f"Successfully processed reel: {note.title}")
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.remove(video_path)
    except Exception as exc:
        logger.error(f"Failed to process reel {url}: {exc}")
        send_telegram_message(chat_id, _error_message(exc))
        partial = getattr(exc, "partial_state", None)
        meta = {}
        if transcript or keyframes or creator or username or partial:
            meta = {
                "creator": creator or "",
                "username": username or "",
                "audio_mode": "none" if not segments else ("ok" if reliable else "weak"),
                "frame_images": keyframes,
                "transcript": transcript,
            }
            if partial:
                meta["critique_history"] = partial.get("critique_history") or []
                meta["writer_history"] = partial.get("writer_history") or []
                meta["model_log"] = partial.get("model_log") or []
        models_used = (partial.get("models_used") or []) if partial else []
        parts = [p for p in url.split("?")[0].rstrip("/").split("/") if p]
        note_name = parts[-1] if parts else "unknown"
        move_to_failed(note_name, volume, str(exc), url=url, models_used=models_used, meta=meta)


def process_post(url: str, chat_id: int | None, volume) -> None:
    from core.graph import reel_graph

    logger.info(f"Processing post: {url}")
    images = []
    transcript = ""
    segments = []
    creator = ""
    username = ""
    reliable = False
    try:
        images, info = download_post(url)
        if not images:
            raise RuntimeError("No media downloaded from post")
        creator = str(info.get("uploader") or "").strip()
        username = str(info.get("channel") or "").strip().lstrip("@")
        description = str(info.get("description") or "").strip()

        try:
            visual_summary = _post_visual_summary(images)
        except Exception as exc:
            logger.warning(f"Visual extraction failed, falling back to raw images: {exc}")
            visual_summary = ""

        def build_state(state_description, state_images):
            return {
                "url": url,
                "video_path": "",
                "transcript": transcript,
                "segments": segments,
                "description": state_description,
                "images": state_images,
                "title": "",
                "category": "",
                "notes": [],
                "writer_history": [],
                "critique": "",
                "critique_history": [],
                "model_log": [],
                "retries": 0,
                "final_note": None,
                "error": None,
                "models_used": [],
            }

        if visual_summary:
            combined_description = (
                f"{description}\n\nExtracted visual list:\n{visual_summary}\n\n"
                "Keep every item from the extracted list above. The note must list all titles and names."
            )
            final_state = reel_graph.invoke(build_state(combined_description, []))
            note_b = Note(
                title=(final_state.get("title") or "Post Note")[:80],
                category=final_state["category"].title(),
                markdown_content=final_state["final_note"] or "No content generated.",
            )
            try:
                needs_raw = _post_needs_raw_images(description, visual_summary, note_b.markdown_content)
            except Exception as exc:
                logger.warning(f"Frame critic failed, keeping Setup B note: {exc}")
                needs_raw = False
            if needs_raw:
                logger.info("Post frame critic requested raw images; falling back to Setup A")
                final_state = reel_graph.invoke(build_state(description, images))
                note = Note(
                    title=(final_state.get("title") or "Post Note")[:80],
                    category=final_state["category"].title(),
                    markdown_content=final_state["final_note"] or "No content generated.",
                )
            else:
                note = note_b
        else:
            final_state = reel_graph.invoke(build_state(description, images))
            note = Note(
                title=(final_state.get("title") or "Post Note")[:80],
                category=final_state["category"].title(),
                markdown_content=final_state["final_note"] or "No content generated.",
            )

        logger.info(f"Graph finished. Models used: {final_state.get('models_used', [])}")
        meta = {
            "creator": creator or "",
            "username": username or "",
            "audio_mode": "none",
            "frame_images": images,
            "transcript": transcript,
            "platform": "Instagram Post",
            "critique_history": final_state.get("critique_history") or [],
            "writer_history": final_state.get("writer_history") or [],
            "model_log": final_state.get("model_log") or [],
        }
        save_note(note, volume, url, final_state.get("models_used", []), meta)
        send_telegram_message(chat_id, f"Done! Saved note: {note.title}")
        logger.info(f"Successfully processed post: {note.title}")
    except Exception as exc:
        logger.error(f"Failed to process post {url}: {exc}")
        send_telegram_message(chat_id, _error_message(exc, "post"))
        partial = getattr(exc, "partial_state", None)
        meta = {"creator": creator or "", "username": username or "", "audio_mode": "none"}
        if images:
            meta["frame_images"] = images
        if partial:
            meta["critique_history"] = partial.get("critique_history") or []
            meta["writer_history"] = partial.get("writer_history") or []
            meta["model_log"] = partial.get("model_log") or []
        models_used = (partial.get("models_used") or []) if partial else []
        parts = [p for p in url.split("?")[0].rstrip("/").split("/") if p]
        note_name = parts[-1] if parts else "unknown"
        move_to_failed(note_name, volume, str(exc), url=url, models_used=models_used, meta=meta)


def process_url(url: str, chat_id: int | None, volume) -> None:
    if media_kind(url) == "post":
        process_post(url, chat_id, volume)
    else:
        process_reel(url, chat_id, volume)



def redo_note(note_name: str, chat_id: int | None, volume) -> None:
    logger.info(f"Redoing note: {note_name}")
    note_path = next(
        (f"{d}/{note_name}" for d in (NOTES_DIR, FAILED_DIR) if os.path.exists(f"{d}/{note_name}")),
        None,
    )
    if note_path is None:
        logger.warning(f"Note not found for redo: {note_name}")
        return

    with open(note_path, "r", encoding="utf-8") as f:
        content = f.read()

    url_match = re.search(r"\*\*(?:Source URL|URL|Source):\*\*\s*(\S+)", content)
    url = url_match.group(1) if url_match else ""

    if not url:
        logger.warning(f"No URL found in note {note_name}, cannot redo")
        return

    if note_path.startswith(NOTES_DIR):
        logger.info(f"Preserving old note before redo: {note_name}")
        os.makedirs(FAILED_DIR, exist_ok=True)
        with open(_failed_path(note_name), "w", encoding="utf-8") as f:
            f.write(f"# FAILED: {note_name}\n\n**Error:** Redo requested - reprocessing\n\n---\n\n{content}")
        os.remove(note_path)
        remove_from_index(note_name, volume)
        volume.commit()
    else:
        logger.info(f"Keeping failed note for reference: {note_name}")

    process_url(url, chat_id, volume)