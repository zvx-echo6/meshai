"""Hybrid FTS5 + vector knowledge search for MeshAI."""

import logging
import re
import sqlite3
from typing import Optional

import numpy as np
import sqlite_vec
from fastembed import TextEmbedding

logger = logging.getLogger(__name__)

STOPWORDS = {
    'what', 'is', 'the', 'a', 'an', 'and', 'or', 'for', 'on', 'in',
    'to', 'of', 'how', 'do', 'does', 'can', 'will', 'would', 'could',
    'should', 'are', 'was', 'were', 'be', 'been', 'being', 'have',
    'has', 'had', 'not', 'but', 'if', 'then', 'than', 'that', 'this',
    'it', 'its', 'my', 'me', 'i', 'you', 'your', 'we', 'they', 'them',
    'about', 'with', 'from', 'at', 'by', 'up', 'out', 'so', 'no',
    'yes', 'just', 'get', 'got', 'tell', 'know', 'like',
}


class KnowledgeSearch:
    """Hybrid FTS5 + vector knowledge search."""

    def __init__(self, db_path: str, top_k: int = 5):
        self.top_k = top_k
        self.available = False
        self._model = None
        self._conn: Optional[sqlite3.Connection] = None
        self._has_vec = False

        try:
            self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)

            # Check if vec table exists
            tables = [r[0] for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
            self._has_vec = "chunks_vec" in tables

            if self._has_vec:
                logger.info("Loading embedding model for hybrid search...")
                self._model = TextEmbedding("BAAI/bge-small-en-v1.5")
                logger.info("Knowledge base loaded with hybrid search (FTS5 + vector)")
            else:
                logger.info("Knowledge base loaded with FTS5 only (no vector table)")

            count = self._conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
            logger.info(f"Knowledge base: {count} chunks from {db_path}")
            self.available = True

        except Exception as e:
            logger.warning(f"Failed to load knowledge base: {e}")

    def search(self, query: str) -> list[dict]:
        """Search knowledge base using hybrid FTS5 + vector with RRF."""
        if not self.available or not self._conn:
            return []

        try:
            fts_results = self._fts_search(query)

            if self._has_vec and self._model:
                vec_results = self._vec_search(query)
                merged = self._rrf_merge(fts_results, vec_results)
            else:
                merged = [(r[0], r[1]) for r in fts_results]

            # Fetch full data for top results
            top_ids = [r[0] for r in merged[:self.top_k]]
            if not top_ids:
                return []

            results = []
            for chunk_id in top_ids:
                row = self._conn.execute(
                    "SELECT title, content, source, book_title FROM chunks WHERE rowid = ?",
                    [chunk_id]
                ).fetchone()
                if row:
                    # Truncate content to ~500 chars for prompt injection
                    content = row[1][:1000] if row[1] else ""
                    results.append({
                        "title": row[0] or "",
                        "excerpt": content,
                        "source": row[2] or "",
                        "book_title": row[3] or "",
                    })

            logger.debug(f"Knowledge search: query='{query[:50]}' -> {len(results)} results")
            return results

        except Exception as e:
            logger.warning(f"Knowledge search error: {e}")
            return []

    def _fts_search(self, query: str, limit: int = 50) -> list[tuple]:
        """FTS5 keyword search. Returns [(rowid, rank), ...]"""
        # Domain terms - only use these for FTS, ignore likely typos
        DOMAIN_TERMS = {
            'short', 'fast', 'slow', 'long', 'mid', 'medium',
            'meshtastic', 'lora', 'mesh', 'radio', 'preset', 'modem',
            'sf', 'cr', 'bw', 'spreading', 'coding', 'bandwidth',
            'factor', 'rate', 'channel', 'frequency', 'node',
        }

        cleaned = re.sub(r'[^a-zA-Z0-9\s]', '', query.lower())
        words = cleaned.split()

        # Extract only domain terms (ignores typos like "waht", "teh")
        domain_words = [w for w in words if w in DOMAIN_TERMS]

        # Handle compound words: "shortfast" -> ["short", "fast"]
        expanded = []
        for w in domain_words:
            if w == 'shortfast':
                expanded.extend(['short', 'fast'])
            elif w == 'longfast':
                expanded.extend(['long', 'fast'])
            elif w == 'medslow' or w == 'midslow':
                expanded.extend(['mid', 'slow'])
            else:
                expanded.append(w)

        # Also check for these patterns in non-domain words
        for w in words:
            if w not in DOMAIN_TERMS:
                if 'shortfast' in w:
                    expanded.extend(['short', 'fast'])
                elif 'short' in w and 'fast' in w:
                    expanded.extend(['short', 'fast'])
                elif 'longfast' in w:
                    expanded.extend(['long', 'fast'])

        # Dedupe while preserving order
        seen = set()
        unique = []
        for w in expanded:
            if w not in seen:
                seen.add(w)
                unique.append(w)

        if not unique:
            return []

        # Use AND for domain terms - they should all match
        fts_query = " AND ".join(unique[:5])

        try:
            rows = self._conn.execute("""
                SELECT rowid, rank
                FROM chunks_fts
                WHERE chunks_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, [fts_query, limit]).fetchall()
            return rows
        except Exception as e:
            logger.warning(f"FTS search error: {e}")
            return []


    def _vec_search(self, query: str, limit: int = 50) -> list[tuple]:
        """Vector similarity search. Returns [(chunk_rowid, distance), ...]"""
        try:
            query_vec = list(self._model.embed([f"query: {query}"]))[0]
            rows = self._conn.execute("""
                SELECT chunk_rowid, distance
                FROM chunks_vec
                WHERE embedding MATCH ?
                AND k = ?
            """, [query_vec.astype(np.float32).tobytes(), limit]).fetchall()
            return rows
        except Exception as e:
            logger.warning(f"Vector search error: {e}")
            return []

    def _rrf_merge(self, fts_results: list, vec_results: list, k: int = 60) -> list:
        """Reciprocal Rank Fusion merge of FTS5 and vector results."""
        scores = {}

        # FTS weight 0.5
        for rank, (rowid, _) in enumerate(fts_results):
            scores[rowid] = scores.get(rowid, 0) + 0.5 / (k + rank + 1)

        # Vector weight 0.5
        for rank, (chunk_rowid, _) in enumerate(vec_results):
            scores[chunk_rowid] = scores.get(chunk_rowid, 0) + 0.5 / (k + rank + 1)

        return sorted(scores.items(), key=lambda x: x[1], reverse=True)

    def close(self):
        """Close the database connection."""
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            self.available = False
