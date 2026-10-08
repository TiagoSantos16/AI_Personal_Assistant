"""Media classification shared by reels and carousel preparation."""
from pathlib import Path
from urllib.parse import urlparse


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
