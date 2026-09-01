import logging
import os

from core.config import RAG_MODELS
from core.llm import get_llm, invoke_with_retry

NOTES_DIR = "/data/notes"
VECTOR_DIR = "/data/vector_store"
TABLE_NAME = "notes"
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384
TOP_K = 6
MAX_SOURCES = 4
RELEVANT_THRESHOLD = 0.6

_RAG_SYSTEM = (
    "You answer questions about a personal collection of saved notes. "
    "Only use the notes given to you between the markers below. If they do "
    "not answer the question, say you could not find it in the notes and do "
    "not name any files. Do not invent facts or add outside knowledge. "
    "Finish by listing the note file names you used."
)

logger = logging.getLogger(__name__)


def _embed_texts(texts: list[str]) -> list[list[float]]:
    from fastembed import TextEmbedding

    model = TextEmbedding(model_name=EMBEDDING_MODEL)
    return [vec.tolist() for vec in model.embed(list(texts))]


def _split(text: str) -> list[str]:
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(chunk_size=900, chunk_overlap=100)
    return splitter.split_text(text)


def _table_names(con) -> list[str]:
    names = con.table_names()
    if hasattr(names, "tables"):
        names = names.tables
    return list(names)


def _table(con):
    import pyarrow as pa

    if TABLE_NAME not in _table_names(con):
        schema = pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("source", pa.string()),
                pa.field("text", pa.string()),
                pa.field("embedding", pa.list_(pa.float32(), EMBEDDING_DIM)),
            ]
        )
        return con.create_table(TABLE_NAME, schema=schema, mode="create")
    return con.open_table(TABLE_NAME)


def index_note(file_path: str, volume) -> None:
    import lancedb

    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()
    source = os.path.basename(file_path)
    chunks = _split(text)
    vectors = _embed_texts(chunks)
    os.makedirs(VECTOR_DIR, exist_ok=True)
    table = _table(lancedb.connect(VECTOR_DIR))
    table.delete(f"source = '{source}'")
    if chunks:
        table.add(
            [
                {"id": f"{source}~{i}", "source": source, "text": chunk, "embedding": vectors[i]}
                for i, chunk in enumerate(chunks)
            ]
        )
    volume.commit()
    logger.info(f"Indexed note: {source} ({len(chunks)} chunks)")


def remove_from_index(source: str, volume) -> None:
    import lancedb

    try:
        if not os.path.isdir(VECTOR_DIR):
            return
        con = lancedb.connect(VECTOR_DIR)
        if TABLE_NAME not in _table_names(con):
            return
        con.open_table(TABLE_NAME).delete(f"source = '{source}'")
        volume.commit()
        logger.info(f"Removed {source} from index")
    except Exception as exc:
        logger.warning(f"Could not remove {source} from index: {exc}")


def reindex_all_notes(volume) -> None:
    import lancedb

    os.makedirs(VECTOR_DIR, exist_ok=True)
    con = lancedb.connect(VECTOR_DIR)
    if TABLE_NAME in _table_names(con):
        con.drop_table(TABLE_NAME)
    table = _table(con)
    rows = []
    for name in sorted(os.listdir(NOTES_DIR)):
        if not name.endswith(".md"):
            continue
        with open(f"{NOTES_DIR}/{name}", "r", encoding="utf-8") as f:
            chunks = _split(f.read())
        vectors = _embed_texts(chunks)
        rows.extend(
            {"id": f"{name}~{i}", "source": name, "text": chunk, "embedding": vectors[i]}
            for i, chunk in enumerate(chunks)
        )
    if rows:
        table.add(rows)
    volume.commit()
    logger.info(f"Reindexed {len(rows)} chunks from {NOTES_DIR}")


def _grounded_answer(query: str, context: str) -> str:
    prompt = f"{_RAG_SYSTEM}\n\nNotes:\n{context}\n\nQuestion: {query}"
    result = invoke_with_retry(get_llm(RAG_MODELS), [{"role": "user", "content": prompt}])
    return str(result.content).strip()


def ask_notes(query: str) -> dict:
    if not os.path.isdir(VECTOR_DIR):
        return {"answer": "No notes indexed yet. Save a few reels first.", "sources": []}

    import lancedb
    import numpy as np

    con = lancedb.connect(VECTOR_DIR)
    if TABLE_NAME not in _table_names(con):
        return {"answer": "No notes indexed yet. Save a few reels first.", "sources": []}

    query_vec = np.asarray(_embed_texts([query])[0], dtype=np.float32)
    hits = con.open_table(TABLE_NAME).search(query_vec).limit(TOP_K).to_list()
    rows = [h for h in hits if h.get("_distance", 2.0) < RELEVANT_THRESHOLD]
    rows.sort(key=lambda h: h["_distance"])

    kept = []
    seen = set()
    for row in rows:
        source = row.get("source", "")
        if source in seen:
            continue
        seen.add(source)
        kept.append(row)
        if len(kept) >= MAX_SOURCES:
            break

    if not kept:
        return {"answer": "I could not find this in your notes.", "sources": []}

    context = "\n\n".join(f"[{r['source']}]\n{r['text']}" for r in kept)
    return {"answer": _grounded_answer(query, context), "sources": [r["source"] for r in kept]}