from __future__ import annotations

import os
from typing import Any, Dict, List

import chromadb
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

_CHROMA_PATH = os.path.join(os.path.dirname(__file__), "data", "chroma")
_ef = DefaultEmbeddingFunction()


def _col():
    client = chromadb.PersistentClient(path=_CHROMA_PATH)
    return client.get_or_create_collection(
        name="troubleshooting_sessions",
        embedding_function=_ef,
    )


def store_session(
    session_id: str,
    problem: str,
    summary: str,
    commands: List[Dict[str, Any]],
    notes: str,
) -> None:
    cmd_text = " | ".join(
        c.get("text", "") for c in commands if isinstance(c, dict)
    )
    try:
        _col().upsert(
            documents=[problem],
            metadatas=[{"summary": summary, "commands": cmd_text, "notes": notes}],
            ids=[session_id],
        )
    except Exception as exc:
        print(f"[rag] store_session failed: {exc}")


def get_all_sessions() -> List[Dict[str, str]]:
    try:
        col = _col()
        if col.count() == 0:
            return []
        result = col.get(include=["documents", "metadatas"])
        out = []
        for sid, doc, meta in zip(result["ids"], result["documents"], result["metadatas"]):
            out.append({
                "id": sid,
                "problem": doc,
                "summary": meta.get("summary", ""),
                "commands": meta.get("commands", ""),
                "notes": meta.get("notes", ""),
            })
        return out
    except Exception as exc:
        print(f"[rag] get_all_sessions failed: {exc}")
        return []


def delete_session(session_id: str) -> bool:
    try:
        _col().delete(ids=[session_id])
        return True
    except Exception as exc:
        print(f"[rag] delete_session failed: {exc}")
        return False


def retrieve_similar(
    problem: str, n_results: int = 3, max_distance: float = 0.8
) -> List[Dict[str, str]]:
    """
    ChromaDB default metric is squared-L2. For all-MiniLM-L6-v2 (normalized):
      ~0.3 = very similar, ~0.8 = loosely related, ~1.5+ = unrelated.
    max_distance filters out results that aren't genuinely relevant.
    """
    try:
        col = _col()
        count = col.count()
        if count == 0:
            return []
        results = col.query(
            query_texts=[problem],
            n_results=min(n_results, count),
            include=["documents", "metadatas", "distances"],
        )
        out = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            if dist > max_distance:
                print(f"[rag] skipping session (distance {dist:.3f} > {max_distance}): {doc[:60]}")
                continue
            out.append({
                "problem": doc,
                "summary": meta.get("summary", ""),
                "commands": meta.get("commands", ""),
                "notes": meta.get("notes", ""),
            })
        return out
    except Exception as exc:
        print(f"[rag] retrieve_similar failed: {exc}")
        return []
