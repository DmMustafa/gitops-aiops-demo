import os
import uuid
import chromadb

CHROMA_HOST = os.environ.get("CHROMA_HOST", "chromadb.chromadb.svc.cluster.local")
CHROMA_PORT = int(os.environ.get("CHROMA_PORT", "8000"))
CHROMA_COLLECTION = os.environ.get("CHROMA_COLLECTION", "gitlab-incidents")

_client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
_collection = _client.get_or_create_collection(CHROMA_COLLECTION)


def add_incident(description: str, metadata: dict, doc_id: str | None = None) -> str:
    """Сохраняет инцидент (описание проблемы + примененный фикс) в базу знаний."""
    doc_id = doc_id or str(uuid.uuid4())
    _collection.upsert(
        ids=[doc_id],
        documents=[description],
        metadatas=[metadata],
    )
    return doc_id


def query_similar(description: str, n_results: int = 3) -> list[dict]:
    """Ищет похожие прошлые инциденты по семантическому сходству."""
    res = _collection.query(query_texts=[description], n_results=n_results)
    out = []
    docs = res.get("documents", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    dists = res.get("distances", [[]])[0]
    for doc, meta, dist in zip(docs, metas, dists):
        out.append({"document": doc, "metadata": meta, "distance": dist})
    return out
