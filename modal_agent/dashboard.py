import base64
import os
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

NOTES_DIR = Path("/data/notes")
IMAGES_DIR = Path("/data/images")
FAILED_DIR = Path("/data/failed")


def prettify(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip().title()


def _clean(line: str) -> str:
    return line.strip().lstrip("- ").strip()


def parse_metadata(content: str) -> dict:
    meta = {
        "url": "",
        "uploader": "",
        "creator_url": "",
        "frames": "",
        "audio_mode": "",
        "platform": "Instagram Reel",
        "models_used": [],
        "model_log": [],
        "criticism": [],
        "transcript": "",
        "frame_images": [],
        "iterations": [],
    }

    for pat, key in (
        (r"-?\s*\*\*(?:Source URL|Source|URL):\*\*\s*(\S+)", "url"),
        (r"-?\s*\*\*(?:Creator|Uploader|User):\*\*\s*(.+)", "uploader"),
        (r"-?\s*\*\*Creator URL:\*\*\s*(\S+)", "creator_url"),
        (r"-?\s*\*\*Audio:\*\*\s*(\S+)", "audio_mode"),
        (r"-?\s*\*\*Platform:\*\*\s*(\S+)", "platform"),
    ):
        match = re.search(pat, content)
        if match:
            meta[key] = match.group(1).strip().removeprefix("**").strip()

    new_count = re.search(r"\*\*Frames \((\d+)\):\*\*", content)
    old_count = re.search(r"\*\*Frames:\*\*\s*(\d+)", content)
    if new_count:
        meta["frames"] = new_count.group(1)
        frames_section = re.search(r"\*\*Frames \(\d+\):\*\*\n(.*?)(?=\n\*\*|\Z)", content, re.S)
        if frames_section:
            meta["frame_images"] = re.findall(r"`([^`]+\.jpg)`", frames_section.group(1))
    elif old_count:
        meta["frames"] = old_count.group(1)

    transcript = re.search(r"\*\*Transcript:\*\*\n(.*?)(?=\n\*\*|\Z)", content, re.S)
    if transcript:
        meta["transcript"] = transcript.group(1).strip()

    for rnd, kind, text in re.findall(
        r"\*\*Round (\d+) - (Writer|Critic):\*\*\n(.*?)(?=\n\*\*Round \d+ - (?:Writer|Critic):\*\*|\n\*\*Models Used:\*\*|\Z)",
        content,
        re.S,
    ):
        meta["iterations"].append({"round": int(rnd), "kind": kind.lower(), "text": text.strip()})

    models_section = re.search(r"-?\s*\*\*Models Used:\*\*\n(.*?)(?=\n\*\*|\Z)", content, re.S)
    if models_section:
        section = models_section.group(1)
        log: list[dict] = []
        current: dict | None = None
        for raw in section.splitlines():
            entry = re.match(r"\s*-\s*\*\*([^*]+):\*\*\s*`([^`]+)`", raw)
            tried = re.match(r"\s*-\s*tried:\s*(.*)", raw)
            if entry:
                current = {"label": entry.group(1).strip(), "model": entry.group(2).strip(), "tried": []}
                log.append(current)
            elif current and tried:
                current["tried"] = re.findall(r"`([^`]+)`", tried.group(1))
        if log:
            meta["model_log"] = log
        else:
            meta["models_used"] = re.findall(r"`([^`]+)`", section)

    critic_section = re.search(r"-?\s*\*\*Critic Feedback:\*\*\n(.*?)(?=\n\*\*|\Z)", content, re.S)
    if critic_section:
        for line in critic_section.group(1).splitlines():
            item = line.strip()
            if item.startswith(("- ", "* ")):
                item = item[2:]
            item = item.strip()
            if item:
                meta["criticism"].append(item)

    return meta


def _load_dir(directory: Path, failed: bool = False) -> list[dict]:
    if not directory.is_dir():
        return []
    notes = []
    for entry in sorted(directory.iterdir(), key=os.path.getmtime, reverse=True):
        if entry.suffix != ".md":
            continue
        content = entry.read_text(encoding="utf-8")
        lines = content.splitlines()
        first = next((ln for ln in lines if ln.strip()), "")
        title = first.lstrip("# ").strip() or prettify(entry.stem)
        if failed:
            title = title.replace("FAILED: ", "")
            error_match = re.search(r"\*\*Error:\*\*\s*(.+)", content)
            notes.append(
                {
                    "name": entry.name,
                    "title": title,
                    "saved": datetime.fromtimestamp(entry.stat().st_mtime).strftime("%b %d, %Y"),
                    "content": content,
                    "error": error_match.group(1).strip() if error_match else "",
                }
            )
            continue
        category = prettify(entry.stem.split("_", 1)[0])
        meta = parse_metadata(content)
        notes.append(
            {
                "name": entry.name,
                "title": title,
                "category": category,
                "platform": meta["platform"],
                "url": meta["url"],
                "uploader": meta["uploader"],
                "creator_url": meta["creator_url"],
                "frames": meta["frames"],
                "audio_mode": meta["audio_mode"],
                "criticism": meta["criticism"],
                "transcript": meta["transcript"],
                "frame_images": meta["frame_images"],
                "iterations": meta["iterations"],
                "saved": datetime.fromtimestamp(entry.stat().st_mtime).strftime("%b %d, %Y"),
                "content": content,
                "models_used": meta["models_used"],
                "model_log": meta["model_log"],
            }
        )
    return notes


def _creator_username(note: dict) -> str:
    creator_url = note.get("creator_url", "")
    if creator_url:
        return creator_url.rstrip("/").split("/")[-1].lstrip("@")
    return ""


def note_body(content: str) -> str:
    skip_prefixes = (
        "**Category:**",
        "**URL:**",
        "**Source:**",
        "**Source URL:**",
        "**Creator:**",
        "**Uploader:**",
        "**User:**",
        "**Platform:**",
        "**Error:**",
    )
    lines = []
    for ln in content.splitlines():
        line = _clean(ln)
        if line == "**Generation Info:**":
            break
        if line.startswith("# ") or any(line.startswith(p) for p in skip_prefixes):
            continue
        lines.append(ln)
    while lines and _clean(lines[-1]) in ("", "---"):
        lines.pop()
    return "\n".join(lines).strip()


def _as_match(url: str, original: str):
    class _Match:
        def group(self, n=None):
            return original if n in (None, 0) else url

    return _Match()


def _inline_image(path: Path) -> str | None:
    if not path.exists():
        return None
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    ext = path.suffix.lower().lstrip(".")
    mime = f"image/{'jpeg' if ext in ('jpg', 'jpeg') else ext}"
    return f'<img src="data:{mime};base64,{b64}" style="max-width:100%;height:auto;">'


def resolve_image_paths(html_content: str) -> str:
    def replace_img(match):
        src = match.group(1)
        if src.startswith(("/data/images/", "/images/")):
            html = _inline_image(IMAGES_DIR / Path(src).name)
            if html:
                return html
        return match.group(0)

    html_content = re.sub(r'<img\s+src=["\']([^"\']+)["\']', replace_img, html_content)
    html_content = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', lambda m: replace_img(_as_match(m.group(2), m.group(0))), html_content)
    return html_content


def _clear_note_param() -> None:
    if "note" in st.query_params:
        del st.query_params["note"]


def _truncate_one_line(text: str, limit: int = 90) -> str:
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return flat if len(flat) <= limit else flat[: limit - 1] + "\u2026"


def _render_generation_info(note: dict) -> None:
    frames = note.get("frame_images") or []
    transcript = note.get("transcript") or ""
    iterations = note.get("iterations") or []
    models_log = note.get("model_log") or []
    models = note.get("models_used") or []
    if not (frames or transcript or iterations or models_log or models):
        return

    if frames:
        with st.expander(f"Frames ({len(frames)})", expanded=False):
            cols = st.columns(min(4, len(frames)))
            for i, name in enumerate(frames):
                html = _inline_image(IMAGES_DIR / name)
                with cols[i % len(cols)]:
                    if html:
                        st.markdown(html, unsafe_allow_html=True)
                        st.caption(name)
                    else:
                        st.caption(f"{name} (not found on volume)")
    if transcript:
        with st.expander("Transcript", expanded=False):
            st.text(transcript)
    for it in iterations:
        label = f"Round {it['round']} - {'Writer' if it['kind'] == 'writer' else 'Critic'}"
        with st.expander(f"{label}\u2003{_truncate_one_line(it['text'])}", expanded=False):
            st.text(it["text"])
    if models_log:
        with st.expander("Models used", expanded=False):
            for e in models_log:
                st.markdown(f"**{e['label']}:** `{e['model']}`")
                tried = e.get("tried") or []
                if tried:
                    st.caption("tried:\u2002" + ", ".join(f"`{t}`" for t in tried))
    elif models:
        with st.expander(f"Models used ({len(models)})", expanded=False):
            for m in models:
                st.code(m, language=None)


@st.dialog("Generation Info")
def show_generation_info(note: dict) -> None:
    _render_generation_info(note)


st.set_page_config(page_title="Reel Notes", layout="wide")

notes = _load_dir(NOTES_DIR)
failed_notes = _load_dir(FAILED_DIR, failed=True)

view = st.session_state.get("view", "notes")

with st.sidebar:
    st.title("Reel Notes")
    if view == "notes":
        categories = sorted({n["category"] for n in notes})
        selected_cat = st.selectbox("Category", ["All"] + categories)
    else:
        selected_cat = "All"
    query = st.text_input("Search", placeholder="Search by keyword").strip().lower()
    st.divider()
    st.subheader("Ask your notes")
    question = st.text_input(
        "Ask a question about your saved notes:", key="ask_input"
    ).strip()
    ask_result = None
    ask_error = None
    if st.button("Ask", key="ask_btn") and question:
        with st.spinner("Searching your notes..."):
            from core.processor import _error_message
            from core.rag import ask_notes

            try:
                ask_result = ask_notes(question)
            except Exception as exc:
                ask_error = _error_message(exc, "question")
    if st.button("Reindex notes"):
        import modal
        from core.rag import reindex_all_notes

        with st.spinner("Reindexing notes..."):
            reindex_all_notes(modal.Volume.from_name("personal-assistant-data-v2"))
        st.success("Notes reindexed.")
    st.divider()
    if view == "notes":
        if st.button(f"Failed Summaries ({len(failed_notes)})", use_container_width=True):
            st.session_state["view"] = "failed"
            st.rerun()
    else:
        if st.button("← Back to Notes", use_container_width=True):
            st.session_state["view"] = "notes"
            st.rerun()

active_cat = None if selected_cat == "All" else selected_cat
current_notes = failed_notes if view == "failed" else notes

visible = [
    n
    for n in current_notes
    if (active_cat is None or n.get("category") == active_cat)
    and (not query or query in n["title"].lower() or query in n["content"].lower())
]

if ask_error:
    st.info(ask_error)
    st.divider()

if ask_result:
    st.subheader("Answer")
    st.markdown(ask_result["answer"])
    if ask_result["sources"]:
        with st.expander("Sources"):
            for name in ask_result["sources"]:
                st.markdown(f"[{prettify(name.removesuffix('.md'))}](?note={name})")
    st.divider()

selected = st.query_params.get("note")

if selected is None:
    st.subheader("Failed Summaries" if view == "failed" else "Notes")
    if not current_notes:
        st.info(
            "No failed summaries yet."
            if view == "failed"
            else "No notes yet. Send an Instagram Reel URL to the Telegram bot."
        )
        st.stop()
    if not visible:
        st.info("No notes match the current filters.")
        st.stop()

    if view == "failed":
        df = pd.DataFrame(
            {
                "Open": ["Open"] * len(visible),
                "Title": [n["title"] for n in visible],
                "Saved": [n["saved"] for n in visible],
            }
        )
    else:
        df = pd.DataFrame(
            {
                "Open": ["Open"] * len(visible),
                "Title": [n["title"] for n in visible],
                "Category": [n["category"] for n in visible],
                "Platform": [n["platform"] for n in visible],
                "Creator": [f"@{_creator_username(n)}" if n.get("creator_url") else (n["uploader"] or "—") for n in visible],
                "Saved": [n["saved"] for n in visible],
            }
        )

    event = st.dataframe(
        df,
        column_config={"Open": st.column_config.ButtonColumn("Open", type="tertiary", width="small", key="open_click")},
        hide_index=True,
        width="stretch",
        height=560,
        selection_mode="multi-row",
        on_select="rerun",
    )

    open_click = st.session_state.get("open_click")
    if open_click and open_click.get("row") is not None:
        st.query_params["note"] = visible[open_click["row"]]["name"]
        st.rerun()

    selected_rows = event.selection.rows if event and event.selection else []
    if selected_rows:
        col_del, _ = st.columns([1.5, 7])
        if col_del.button(f"🗑️ Delete ({len(selected_rows)})", type="primary"):
            import modal
            from core.rag import remove_from_index

            vol = modal.Volume.from_name("personal-assistant-data-v2")
            target_dir = FAILED_DIR if view == "failed" else NOTES_DIR
            for idx in selected_rows:
                name = visible[idx]["name"]
                target = target_dir / name
                if target.exists():
                    target.unlink()
                remove_from_index(name, vol)
            vol.commit()
            st.rerun()
    else:
        st.caption("Click Open to read a note. Select rows to delete.")

else:
    note = next((n for n in notes if n["name"] == selected), None)
    is_failed = note is None
    if is_failed:
        note = next((n for n in failed_notes if n["name"] == selected), None)
    if note is None:
        _clear_note_param()
        st.rerun()

    if st.button("← Back to all notes"):
        _clear_note_param()
        st.rerun()

    col_title, col_info, col_redo = st.columns([4, 1.5, 1.5])
    with col_title:
        st.header(note["title"])
    with col_info:
        if st.button("⚙️ Generation Info", use_container_width=True):
            show_generation_info(note)
    with col_redo:
        if st.button("🔄 Retry" if is_failed else "🔄 Redo", use_container_width=True):
            import modal

            try:
                url_path = (note.get("url") or "").split("?")[0]
                worker = "redo_post_background" if "/p/" in url_path else "redo_background"
                modal.Function.from_name("personal-assistant", worker).spawn(note["name"])
                st.success("Reprocessing started. Refresh in a minute.")
            except Exception as exc:
                st.error(f"Could not start reprocessing: {exc}")

    if is_failed:
        st.caption(f"Saved {note['saved']}")
    else:
        st.caption(f"{note['platform']} · {note['category']} · Saved {note['saved']}")

    col_reel, col_creator, _ = st.columns([1.5, 3, 4])
    if note.get("url"):
        col_reel.link_button("🎬 View post", note["url"])
    if note.get("creator_url"):
        col_creator.markdown(f"[@{_creator_username(note)}]({note['creator_url']})")
    elif note.get("uploader"):
        col_creator.markdown(note["uploader"])

    st.divider()
    body = note_body(note["content"])
    html_body = resolve_image_paths(body)
    st.markdown(html_body, unsafe_allow_html=True)

    if note.get("error"):
        st.divider()
        with st.expander("Error", expanded=True):
            st.error(note["error"])