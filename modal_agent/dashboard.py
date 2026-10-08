"""Private notes dashboard; shared data is accessed through storage only."""
import hmac
import hashlib
import os
import re
import time
from datetime import datetime
from urllib.parse import urlsplit, urlencode
import pandas as pd
import streamlit as st
from core.accounting import summary
from core.auth import COOKIE, issue, valid

device_cookie = st.components.v2.component("device_cookie", js="""
export default function({data, setStateValue}) {
  if (data.token === null) return;
  fetch('/api/device', {method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({token: data.token})})
    .then(r => {setStateValue('saved', r.ok ? data.token : false);})
    .catch(() => setStateValue('saved', false));
}
""")


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


def storage(operation, payload=None):
    import modal
    return modal.Function.from_name("personal-assistant", "storage").remote(operation, payload)


def login():
    expected = os.environ.get("DASHBOARD_PASSWORD")
    if not expected:
        st.error("Dashboard access is unavailable. The owner must configure it.")
        return False
    if st.session_state.get("authenticated"):
        return True
    if not st.session_state.get("signed_out") and valid(st.context.cookies.get(COOKIE), expected):
        st.session_state.authenticated = True
        return True
    st.title("Your notes")
    st.caption("Sign in to your private collection.")
    blocked = st.session_state.get("blocked_until", 0) > time.time()
    with st.form("login"):
        password = st.text_input("Password", type="password")
        remember = st.checkbox("Remember this device for 30 days", value=True)
        submitted = st.form_submit_button("Sign in", disabled=blocked)
    if blocked:
        st.warning("Too many attempts. Please wait a minute.")
    if submitted:
        if hmac.compare_digest(password.encode(), expected.encode()):
            st.session_state.authenticated = True
            st.session_state.login_failures = 0
            st.session_state.signed_out = False
            if remember:
                st.session_state.device_token = issue(expected)
            st.rerun()
        else:
            failures = st.session_state.get("login_failures", 0) + 1
            st.session_state.login_failures = failures
            if failures >= 5:
                st.session_state.blocked_until = time.time() + 60
                st.session_state.login_failures = 0
            st.error("Password not recognised.")
    return False


@st.cache_data(ttl=20, show_spinner=False)
def collection(revision):
    return storage("list")


def refresh():
    collection.clear()
    search.clear()


@st.cache_data(ttl=20, max_entries=128, show_spinner=False)
def search(query, revision, entries):
    return storage("search", {"query": query, "records": [{"section": section, "name": name} for section, name in entries]})


def safe_url(value):
    parsed = urlsplit(value or "")
    return value if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username and not parsed.password else None


def note_title_link(name, title):
    # The encoded identifier routes the link; the fragment is display text only.
    title = re.sub(r"[\x00-\x1f\x7f]", " ", str(title))
    return "?" + urlencode({"note": name}) + "#title=" + title


def render_content(content):
    # HTML stays disabled. Spoilers use a native, safely rendered expander.
    body = re.split(r"(?m)^\s*\*\*Generation Info:\*\*", content, maxsplit=1)[0]
    body = re.sub(r"(?m)^# .*\n?|^\*\*Category:\*\*.*\n?", "", body).strip().rstrip("-").strip()
    body = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"Image: \1", body)
    cursor = 0
    for match in re.finditer(r"<details>\s*<summary>.*?</summary>(.*?)</details>", body, re.S | re.I):
        st.markdown(body[cursor:match.start()])
        with st.expander("Spoiler"):
            st.markdown(match[1])
        cursor = match.end()
    st.markdown(body[cursor:])


def cost_details(note):
    with st.expander("Cost details"):
        records = note.get("accounting")
        if records is None:
            st.caption("Cost not recorded. Legacy notes are never regenerated for pricing.")
            return
        st.caption("AI subtotal: " + summary(records))
        if records:
            st.dataframe(pd.DataFrame(records)[["step", "model", "cost", "cost_status", "duration", "success"]],
                hide_index=True, width="stretch", column_config={"cost": st.column_config.NumberColumn("USD", format="$%.8f")})
        st.caption("AI subtotal uses provider-reported usage.cost only. Cached evidence has no new API charge. Timeouts may be billed; unavailable charges are unknown.")
        st.caption(note.get("compute", {}).get("explanation", "Compute cost unavailable; allocation and shared idle overhead are not measured."))
        if note.get("compute", {}).get("details"):
            st.dataframe(note["compute"]["details"], hide_index=True, width="stretch")


def generation_info(note):
    with st.expander("Generation Info"):
        generation = note.get("generation") or {}
        if note.get("legacy"):
            parsed = parse_metadata(note["content"])
            generation = {"transcript": parsed["transcript"], "model_log": parsed["model_log"],
                "writer_history": [i["text"] for i in parsed["iterations"] if i["kind"] == "writer"],
                "critique_history": [i["text"] for i in parsed["iterations"] if i["kind"] == "critic"]}
            st.caption(f"Frames recorded: {parsed['frames'] or 'not recorded'}")
        if generation.get("routing_degraded"):
            st.caption("Category routing degraded to general.")
        if note.get("coverage"):
            st.warning(" ".join(note["coverage"]))
        for key, title in (("description", "Caption"), ("transcript", "Transcript"), ("visual_extraction", "Visual extraction")):
            if generation.get(key):
                st.subheader(title)
                st.text(generation[key])
        for index, draft in enumerate(generation.get("writer_history", [])):
            st.subheader(f"Draft {index + 1}")
            st.markdown(draft)
        for review in generation.get("critique_history", []):
            st.text(review)
        if generation.get("model_log"):
            st.dataframe(generation["model_log"], hide_index=True, width="stretch")
        # Media is served through a validated storage read, never from arbitrary source paths.
        if note.get("media"):
            if st.button("Show selected media", key="media-" + note["name"]):
                for media in storage("media", {"section": note["section"], "name": note["name"]}):
                    st.image(media, width="stretch")


def dismiss_delete():
    st.session_state.pop("pending_delete", None)


@st.dialog("Delete notes?", on_dismiss=dismiss_delete)
def confirm_delete(records):
    count = len(records)
    st.write("Delete this note?" if count == 1 else f"Delete these {count} notes?")
    st.caption("This can't be undone.")
    cancel, confirm = st.columns(2)
    if cancel.button("Cancel", width="stretch"):
        dismiss_delete()
        st.rerun()
    if confirm.button("Delete", type="primary", width="stretch", key="confirm-delete"):
        try:
            storage("delete", {"records": [{"section": r["section"], "name": r["name"]} for r in records]})
            refresh()
            st.query_params.clear()
            st.session_state.pop("selected_notes", None)
            dismiss_delete()
            st.session_state.page = "Notes"
            st.rerun()
        except Exception:
            st.error("Couldn't delete these notes. Refresh and try again.")


def delete_controls(records, key):
    if records and st.button("Delete", key="delete-" + key):
        st.session_state.pending_delete = records
    if st.session_state.get("pending_delete"):
        confirm_delete(st.session_state.pending_delete)


def detail(note):
    if st.button("Back to collection"):
        st.query_params.clear()
        st.session_state.page = "Notes"
        st.rerun()
    st.title(note["title"])
    date = datetime.fromtimestamp(note.get("saved", note.get("created", 0))).strftime("%d %b %Y")
    st.caption(f"{note.get('category', 'General').title()} · {date} · {note.get('review_status', 'Review not recorded')} · {summary(note.get('accounting'), note.get('compute'))}")
    if note["status"] == "superseded":
        st.info("Previous version — replaced by redo")
    elif note["status"] == "failed":
        from core.errors import user_message
        st.warning(user_message(note.get("error_kind")))
    elif note.get("index_pending"):
        st.warning("Saved, but not searchable yet. Reindex in Settings to retry indexing.")
    links = st.columns(2)
    if safe_url(note.get("url")):
        links[0].link_button("Source post", note["url"])
    if safe_url(note.get("creator_url")):
        links[1].link_button("Account", note["creator_url"])
    render_content(note["content"])
    generation_info(note)
    cost_details(note)
    action = "Retry" if note["status"] == "failed" else "Redo"
    if st.button(action, key="redo-" + note["name"]):
        import modal
        try:
            modal.Function.from_name("personal-assistant", "redo_background").spawn(note["name"], note["section"])
            refresh()
            st.success("Processing requested. The current note stays available until replacement succeeds.")
        except Exception:
            st.error("Could not start processing. Please try again later.")
    delete_controls([note], note["name"])
    if note.get("diagnostic") or note.get("index_diagnostic"):
        with st.expander("Owner error details"):
            st.caption("Job reference: " + note.get("job_id", note["version_id"])[:12])
            st.code(note.get("diagnostic") or note.get("index_diagnostic"), language=None)


def main():
    st.set_page_config(page_title="Your notes", layout="wide", initial_sidebar_state="auto")
    pending = st.session_state.get("device_token")
    if pending is not None:
        result = device_cookie(data={"token": pending}, key="remember-device", on_saved_change=lambda: None)
        if result.saved == pending:
            st.session_state.pop("device_token", None)
        elif result.saved is False:
            st.warning("This device could not be remembered. Your current session remains signed in.")
    if not login():
        return
    page = st.session_state.get("page", "Notes")
    try:
        data = collection(storage("revision"))
    except Exception:
        st.error("Your collection is temporarily unavailable. Please refresh shortly.")
        return
    records = data["records"]
    with st.sidebar:
        st.title("Your notes")
        counts = {section: sum(r["section"] == section for r in records) for section in ("notes", "failed")}
        section = st.radio("Collection", ["notes", "failed"], format_func=lambda s:
            f"{'Notes' if s == 'notes' else 'Error/Failed'} ({counts[s]})", key="section")
        category = st.selectbox("Category", ["All"] + sorted({r.get("category", "general") for r in records}), key="category")
        query = st.text_input("Search notes", key="search")
        with st.expander("Settings"):
            if st.button("Reindex notes"):
                try:
                    with st.spinner("Rebuilding the search index..."):
                        storage("reindex")
                    refresh()
                    st.success("Search index rebuilt.")
                except Exception:
                    st.error("Rebuild failed. The previous index is retained.")
            if st.button("Refresh collection"):
                refresh()
                st.rerun()
            if st.button("Sign out"):
                st.session_state.clear()
                st.session_state.signed_out = True
                st.session_state.device_token = ""
                st.rerun()
    if not st.query_params.get("note"):
        _, center, _ = st.columns([1, 2, 1])
        with center:
            page = st.segmented_control("Page", ["Notes", "Chatbot"], default=page, key="page", label_visibility="collapsed") or "Notes"
    if page == "Chatbot" and not st.query_params.get("note"):
        chatbot()
        return
    target = st.query_params.get("note")
    if target:
        found = next((r for r in records if r["name"] == target), None)
        if found:
            try:
                with st.container(width=900):
                    detail(storage("read", {"section": found["section"], "name": found["name"]}))
            except Exception:
                st.error("This record could not be opened. Refresh the collection.")
            return
        st.warning("This note is no longer available.")
    st.title("Notes" if section == "notes" else "Error/Failed")
    filtered = [r for r in records if r["section"] == section and (category == "All" or r.get("category") == category)]
    if query:
        # Full-text search is performed on demand, never on unrelated widget reruns.
        filtered = search(query, data["revision"], tuple((r["section"], r["name"]) for r in filtered))
    if not filtered:
        st.info("No matching notes." if records else "Send an Instagram reel or post to your bot to create your first note.")
        return
    rows = [{"Title": note_title_link(r["name"], r["title"]), "Category": r.get("category", "General").title(),
        "Creator": r.get("creator", ""), "Saved": datetime.fromtimestamp(r.get("saved", r.get("created", 0))).strftime("%d %b %Y"),
        **({"Status": "Previous version — replaced by redo" if r["status"] == "superseded" else r["status"]} if section == "failed" else {})} for r in filtered]
    selected_names = set(st.session_state.get("selected_notes", {}).get(section, []))
    fingerprint = hashlib.sha256("\n".join(r["name"] for r in filtered).encode()).hexdigest()[:16]
    table = st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
        key=f"notes-table-{section}-{data['revision']}-{fingerprint}",
        on_select="rerun", selection_mode="multi-row",
        selection_default={"selection": {"rows": [i for i, r in enumerate(filtered) if r["name"] in selected_names]}},
        column_config={"Title": st.column_config.LinkColumn("Title", display_text=r"#title=(.*)", width="large", alignment="left")})
    selected = [filtered[i]["name"] for i in table.selection.rows if 0 <= i < len(filtered)]
    saved = dict(st.session_state.get("selected_notes", {}))
    saved[section] = selected
    st.session_state.selected_notes = saved
    delete_controls([r for r in filtered if r["name"] in selected], "bulk")


def chatbot():
    st.title("Ask your notes")
    st.caption("Answers use evidence from your saved notes.")
    messages = st.session_state.get("chat_messages", [])
    for message in messages:
        with st.chat_message(message["role"], avatar=":material/person:" if message["role"] == "user" else ":material/assistant:"):
            chat_answer(message)
    question = st.chat_input("Ask a question about your notes", submit_mode="disable")
    if question:
        from core.rag import answer, still_visible, retrieval_question, retrieval_topic
        history = messages
        messages = messages + [{"role": "user", "content": question,
                                "retrieval_topic": retrieval_topic(question, history)}]
        st.session_state.chat_messages = messages
        with st.chat_message("user", avatar=":material/person:"):
            st.markdown(question)
        with st.chat_message("assistant", avatar=":material/assistant:"):
            try:
                with st.spinner("Searching your notes..."):
                    result = answer(question, storage("retrieve", {"question": retrieval_question(question, history)}), history=history)
                    result = still_visible(result, storage("list")["records"])
                reply = {"role": "assistant", "content": result["answer"], "sources": result.get("source_labels", {}),
                         "source_titles": result.get("source_titles", {})}
            except Exception:
                reply = {"role": "assistant", "content": "Search is unavailable. Please try again later."}
            chat_answer(reply)
        st.session_state.chat_messages = messages + [reply]


def chat_answer(message):
    st.markdown(message["content"])
    if message.get("sources"):
        with st.expander("Sources"):
            for name in message["sources"].values():
                title = message.get("source_titles", {}).get(name, name)
                st.link_button(title, "?" + urlencode({"note": name}))


if __name__ == "__main__":
    main()
