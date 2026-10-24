"""Explicit personal memory; deterministic persistence and bounded local retrieval."""
import re
import time
import uuid
from datetime import datetime, timezone

MAX_TEXT = 1000
MAX_ENTRIES = 500
SCOPES = {"global", "notes", "movies", "books", "travel", "coding", "career", "finance", "culinary"}


def words(text):
    tokens = set(re.findall(r"[\w]+", text.casefold()))
    for domain, aliases in {
        "movies": {"movie", "movies", "film", "films", "filme", "filmes", "cinema", "watched", "vi"},
        "books": {"book", "books", "livro", "livros", "reading"},
        "coding": {"code", "coding", "python", "programming", "código"},
        "culinary": {"recipe", "recipes", "receita", "receitas", "cooking"},
    }.items():
        if tokens & aliases:
            tokens.add(domain)
    return tokens - {"i", "a", "the", "my", "me", "to", "in", "and", "of", "is", "it", "eu", "de", "o", "e",
        "you", "your", "what", "which", "have", "has", "not", "can", "do", "does", "want",
        "recommend", "please", "prefer", "que", "um", "uma", "os", "as", "por", "para", "não"}


def select(entries, query, purpose="chat", budget=4000):
    query_words = words(query)
    ranked = []
    for entry in entries:
        score = len(query_words & words(entry["text"] + " " + entry["key"] + " " + entry["scope"]))
        applicable = entry["kind"] == "preference" and (entry["scope"] == "global" or
            entry["scope"] == "notes" and purpose == "notes")
        if score or applicable:
            ranked.append((score + int(applicable), entry))
    result, used = [], 0
    for _, entry in sorted(ranked, key=lambda pair: (-pair[0], pair[1]["id"])):
        cost = len(entry["text"]) + 200
        if used + cost <= budget:
            result.append(entry)
            used += cost
        if len(result) == 8:
            break
    return result


def mutate(entries, payload):
    """No inferred deletion, arbitrary fields, or unbounded transcript retention."""
    action = payload["action"]
    if action == "forget":
        item = entries.pop(payload["id"], None)
        return item
    text = str(payload["text"]).strip()
    if not text or len(text) > MAX_TEXT:
        raise ValueError("Memory must contain 1–1,000 characters")
    if action == "correct":
        item = entries[payload["id"]]
    elif action == "remember":
        key = str(payload.get("key") or text).strip().casefold()
        scope, kind = payload.get("scope", "global"), payload.get("kind", "fact")
        if not key or len(key) > 160 or scope not in SCOPES or kind not in {"fact", "preference"}:
            raise ValueError("Invalid memory key, scope or kind")
        matches = [e for e in entries.values() if e["key"] == key and e["scope"] == scope]
        if matches:
            item = matches[0]
            if item["kind"] != kind:
                raise ValueError("Use /correct to change an existing memory")
            if item["text"] == text:
                return item
        else:
            if len(entries) >= MAX_ENTRIES:
                raise ValueError("Memory is full; forget an entry first")
            item = {"id": uuid.uuid4().hex[:12], "key": key, "scope": scope, "kind": kind,
                    "created": datetime.now(timezone.utc).isoformat()}
    else:
        raise ValueError("Unknown memory action")
    item.update(text=text, source={"chat_id": payload.get("chat_id"), "message_id": payload.get("message_id")},
                updated=datetime.now(timezone.utc).isoformat(), revision=time.time_ns())
    entries[item["id"]] = item
    return item


def rows(entries):
    return [{"id": "M" + str(i), "source": f"memory:{e['id']}:{e['revision']}",
             "title": "Personal memory: " + e["key"], "text": e["text"], "memory": True}
            for i, e in enumerate(entries, 1)]
