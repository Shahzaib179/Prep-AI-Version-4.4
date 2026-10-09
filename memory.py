from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

import numpy as np

from config import MEMORY_MAX_ITEMS, MEMORY_WARN_RATIO
from db import connect, delete_agent_sessions
from db_core import cached_read, db_identity
from rag import embed_texts


class VectorIndex:
    """Exact inner-product search (identical results to ``faiss.IndexFlatIP``).

    A student holds at most MEMORY_MAX_ITEMS (5000) vectors of 384 floats, so one matrix product
    answers a query in well under a millisecond. Keeping it in numpy means the vectors can live in the
    database (permanent) and the index is simply rebuilt from them when a student logs in.
    """

    def __init__(self, vectors: np.ndarray):
        self.v = np.ascontiguousarray(vectors, dtype="float32")

    @property
    def ntotal(self) -> int:
        return int(self.v.shape[0])

    def add(self, vector: np.ndarray) -> None:
        self.v = np.vstack([self.v, np.asarray(vector, dtype="float32")])

    def search(self, query: np.ndarray, k: int):
        sims = np.asarray(query, dtype="float32") @ self.v.T
        idx = np.argsort(-sims, axis=1)[:, :k]
        return np.take_along_axis(sims, idx, 1), idx


# Per-process cache so a Streamlit rerun does not reload thousands of vectors from the database each time.
_CACHE: dict[str, tuple[tuple, VectorIndex | None, list[dict[str, Any]]]] = {}
_CACHE_LOCK = threading.Lock()


@cached_read(30)
def _signature(student_id: str) -> tuple:
    with connect() as con:
        row = con.execute("SELECT COUNT(*), COALESCE(MAX(id),0), COALESCE(SUM(id),0) FROM memories WHERE student_id=? AND is_active=1", (student_id,)).fetchone()
    return (int(row[0]), int(row[1]), int(row[2]))


@cached_read(30)
def _usage_raw(student_id: str) -> tuple[int, int, int, dict[str, int]]:
    with connect() as con:
        used = int(con.execute("SELECT COUNT(*) FROM memories WHERE student_id=? AND is_active=1", (student_id,)).fetchone()[0])
        text_bytes = sum(len((r[0] or "").encode("utf-8")) for r in con.execute("SELECT content FROM memories WHERE student_id=? AND is_active=1", (student_id,)).fetchall())
        vector_bytes = int(con.execute("SELECT COALESCE(SUM(dim),0) FROM memory_vectors WHERE student_id=?", (student_id,)).fetchone()[0]) * 4
        by_type = {r[0] or "other": int(r[1]) for r in con.execute("SELECT memory_type, COUNT(*) FROM memories WHERE student_id=? AND is_active=1 GROUP BY memory_type ORDER BY 2 DESC", (student_id,)).fetchall()}
    return used, text_bytes, vector_bytes, by_type


@cached_read(30)
def _recent_rows(student_id: str, limit: int) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT * FROM memories WHERE student_id=? AND is_active=1 ORDER BY id DESC LIMIT ?", (student_id, limit)).fetchall()
    return [dict(r) for r in rows]


def _to_blob(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype="float32").reshape(-1).tobytes()


def _from_blob(blob: Any) -> np.ndarray:
    return np.frombuffer(bytes(blob), dtype="float32")


class LongTermMemory:
    """Structured memory rows + semantic vectors per student, both stored in the database."""

    def __init__(self, student_id: str):
        self.student_id = student_id
        self._key = f"{db_identity()}|{student_id}"
        self._load()

    # ------------------------------------------------------------------ load / cache
    def _load(self) -> None:
        sig = _signature(self.student_id)
        with _CACHE_LOCK:
            hit = _CACHE.get(self._key)
        if hit and hit[0] == sig:
            self.index, self.metadata = hit[1], [dict(m) for m in hit[2]]
            return
        self._rebuild()

    def _rebuild(self) -> None:
        """Build the in-memory index from the database. Rows with no stored vector are embedded once and saved."""
        with connect() as con:
            rows = [dict(r) for r in con.execute(
                "SELECT m.id, m.memory_type, m.content, m.subject, m.topic, m.importance, m.confidence, m.created_at, v.embedding "
                "FROM memories m LEFT JOIN memory_vectors v ON v.memory_id=m.id "
                "WHERE m.student_id=? AND m.is_active=1 ORDER BY m.id ASC", (self.student_id,)).fetchall()]
        missing = [r for r in rows if r["embedding"] is None]
        if missing:  # e.g. data migrated from v4.3, where vectors only lived in files that were lost
            fresh = embed_texts([r["content"] for r in missing])
            with connect() as con:
                for r, vec in zip(missing, fresh):
                    con.execute("INSERT INTO memory_vectors(memory_id,student_id,dim,embedding) VALUES(?,?,?,?) ON CONFLICT(memory_id) DO NOTHING",
                                (r["id"], self.student_id, int(len(vec)), _to_blob(vec)))
                    r["embedding"] = _to_blob(vec)
        self.metadata = [{"memory_id": int(r["id"]), "content": r["content"], "memory_type": r["memory_type"], "subject": r["subject"], "topic": r["topic"],
                          "importance": r["importance"], "confidence": r["confidence"], "created_at": r["created_at"]} for r in rows]
        self.index = VectorIndex(np.vstack([_from_blob(r["embedding"]) for r in rows])) if rows else None
        self._remember()

    def _remember(self) -> None:
        sig = _signature(self.student_id)
        with _CACHE_LOCK:
            _CACHE[self._key] = (sig, self.index, [dict(m) for m in self.metadata])

    # ------------------------------------------------------------------ write
    def add(self, content: str, memory_type: str = "learning", subject: str = "", topic: str = "", importance: float = 0.7, confidence: float = 0.8) -> int | None:
        content = content.strip()
        if len(content) < 12:
            return None
        # Avoid exact duplicates.
        if any(m.get("content", "").strip().lower() == content.lower() for m in self.metadata):
            return None
        # Keep the store inside its limit: make room by dropping the least valuable memories.
        self._make_room(1)
        now = datetime.utcnow().isoformat()
        vector = embed_texts([content])                      # embed first: if it fails, nothing half-saved is left behind
        with connect() as con:                               # row + vector are saved in ONE transaction
            memory_id = con.insert("INSERT INTO memories(student_id,memory_type,content,subject,topic,importance,confidence,created_at,updated_at,last_used_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                   (self.student_id, memory_type, content, subject, topic, importance, confidence, now, now, now))
            con.execute("INSERT INTO memory_vectors(memory_id,student_id,dim,embedding) VALUES(?,?,?,?)",
                        (memory_id, self.student_id, int(vector.shape[1]), _to_blob(vector[0])))
        if self.index is None:
            self.index = VectorIndex(vector)
        else:
            self.index.add(vector)
        self.metadata.append({"memory_id": memory_id, "content": content, "memory_type": memory_type, "subject": subject, "topic": topic, "importance": importance, "confidence": confidence, "created_at": now})
        self._remember()
        return memory_id

    # ------------------------------------------------------------------ usage / limits
    def count(self) -> int:
        with connect() as con:
            return int(con.execute("SELECT COUNT(*) FROM memories WHERE student_id=? AND is_active=1", (self.student_id,)).fetchone()[0])

    def usage(self) -> dict[str, Any]:
        """How much of this student's memory allowance is in use."""
        used, text_bytes, vector_bytes, by_type = _usage_raw(self.student_id)
        ratio = min(1.0, used / MEMORY_MAX_ITEMS) if MEMORY_MAX_ITEMS else 0.0
        return {
            "used": used, "limit": MEMORY_MAX_ITEMS, "free": max(0, MEMORY_MAX_ITEMS - used),
            "percent": round(ratio * 100, 1), "text_bytes": text_bytes, "vector_bytes": vector_bytes,
            "by_type": by_type, "near_limit": ratio >= MEMORY_WARN_RATIO, "full": used >= MEMORY_MAX_ITEMS,
        }

    def _make_room(self, needed: int = 1) -> int:
        """Evict the lowest-importance (then oldest) memories until `needed` slots are free."""
        over = self.count() + needed - MEMORY_MAX_ITEMS
        if over <= 0:
            return 0
        with connect() as con:
            victims = [int(r[0]) for r in con.execute(
                "SELECT id FROM memories WHERE student_id=? AND is_active=1 ORDER BY importance ASC, created_at ASC, id ASC LIMIT ?",
                (self.student_id, over)).fetchall()]
            if victims:
                marks = ",".join("?" * len(victims))
                con.execute(f"DELETE FROM memory_vectors WHERE student_id=? AND memory_id IN ({marks})", (self.student_id, *victims))
                con.execute(f"DELETE FROM memories WHERE student_id=? AND id IN ({marks})", (self.student_id, *victims))
        self._drop_vectors(set(victims))
        return len(victims)

    def _drop_vectors(self, memory_ids: set[int]) -> None:
        """Remove the in-memory vectors belonging to `memory_ids` (the database rows are already gone)."""
        if not memory_ids or self.index is None or not self.metadata:
            return
        keep = [i for i, m in enumerate(self.metadata) if int(m.get("memory_id", -1)) not in memory_ids]
        if len(keep) == len(self.metadata):
            return
        self.index = VectorIndex(self.index.v[keep]) if keep else None
        self.metadata = [self.metadata[i] for i in keep]
        self._remember()

    # ------------------------------------------------------------------ read
    def retrieve(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        if self.index is None or self.index.ntotal == 0:
            return []
        q = embed_texts([query])
        scores, ids = self.index.search(q, min(top_k, self.index.ntotal))
        result = []
        for score, idx in zip(scores[0], ids[0]):
            if idx < 0 or idx >= len(self.metadata):
                continue
            item = dict(self.metadata[int(idx)])
            item["score"] = float(score)
            result.append(item)
        return result

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        return _recent_rows(self.student_id, limit)

    def clear(self) -> None:
        with connect() as con:
            con.execute("DELETE FROM memory_vectors WHERE student_id=?", (self.student_id,))
            con.execute("DELETE FROM memories WHERE student_id=?", (self.student_id,))
        self.index = None
        self.metadata = []
        self._remember()


def memory_prompt(memories: list[dict[str, Any]]) -> str:
    if not memories:
        return "No long-term student memories were found."
    lines = ["Relevant long-term student memories:"]
    for m in memories:
        lines.append(f"- [{m.get('memory_type','learning')}] {m.get('content','')}")
    return "\n".join(lines)


def reset_memory(student_id: str, include_conversations: bool = False) -> dict[str, int]:
    """User-initiated reset.

    Always clears the semantic long-term memory (database rows + vector index).
    With ``include_conversations`` it also deletes the saved Tutor / Research chat history.
    Quiz history, mastery and revision data are never touched here.
    """
    memory = LongTermMemory(student_id)
    removed = memory.count()
    memory.clear()
    chats = delete_agent_sessions(student_id) if include_conversations else 0
    return {"memories_removed": removed, "conversations_removed": chats}
