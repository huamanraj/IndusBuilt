"""
Local context management for IndusBuilt.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


IGNORE_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules"}
DEFAULT_SEARCH_GLOBS = ("*.py", "*.md", "*.toml", "*.json", "*.yaml", "*.yml", "*.txt")


def _slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"[^a-z0-9_-]", "", value)
    value = re.sub(r"-{2,}", "-", value)
    return value.strip("-_") or "item"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trim_text(value: str, max_chars: int) -> str:
    text = value.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


@dataclass
class MemorySearchHit:
    title: str
    topic: str
    memory_type: str
    summary: str
    path: str
    score: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "topic": self.topic,
            "type": self.memory_type,
            "summary": self.summary,
            "path": self.path,
            "score": self.score,
        }


class ContextManager:
    """Builds compact runtime context from local memory and session state."""

    max_active_context_chars = 1200
    max_summary_chars = 1800
    max_retrieval_chars = 1800
    max_recent_messages = 20
    summarize_message_threshold = 36
    large_output_threshold = 4000

    def __init__(self, sandbox_root: Path):
        self.sandbox_root = sandbox_root.resolve()
        self.memory_root = self.sandbox_root / ".indusbuilt" / "memory"
        self.summaries_dir = self.memory_root / "summaries"
        self.knowledge_dir = self.memory_root / "knowledge"
        self.offloaded_dir = self.memory_root / "offloaded"
        self.sqlite_path = self.memory_root / "memory.sqlite3"
        self.summary_file = self.summaries_dir / "current_session.md"
        self.active_context: Dict[str, str] = {
            "goal": "",
            "current_file": "",
            "active_bug": "",
            "next_step": "",
        }
        self._fts_available = False
        self._ensure_storage()
        self._ensure_database()

    def _ensure_storage(self) -> None:
        self.summaries_dir.mkdir(parents=True, exist_ok=True)
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        self.offloaded_dir.mkdir(parents=True, exist_ok=True)
        if not self.summary_file.exists():
            self.summary_file.write_text(
                "# Session Summary\n\nNo session summary yet.\n", encoding="utf-8"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.sqlite_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_database(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_type TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    body TEXT NOT NULL,
                    path TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_topic ON memory_entries(topic)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_type ON memory_entries(memory_type)")
            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS memory_entries_fts
                    USING fts5(title, summary, body, topic, content='memory_entries', content_rowid='id')
                    """
                )
                self._fts_available = True
            except sqlite3.OperationalError:
                self._fts_available = False
            conn.commit()

    def _upsert_entry(
        self,
        memory_type: str,
        topic: str,
        title: str,
        summary: str,
        body: str,
        path: str,
    ) -> None:
        checksum = hashlib.sha256((title + summary + body).encode("utf-8")).hexdigest()
        now = _utc_now_iso()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM memory_entries WHERE checksum = ? AND path = ?",
                (checksum, path),
            ).fetchone()
            if existing is not None:
                entry_id = int(existing["id"])
                conn.execute(
                    """
                    UPDATE memory_entries
                    SET memory_type = ?, topic = ?, title = ?, summary = ?, body = ?, created_at = ?
                    WHERE id = ?
                    """,
                    (memory_type, topic, title, summary, body, now, entry_id),
                )
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO memory_entries(memory_type, topic, title, summary, body, path, checksum, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (memory_type, topic, title, summary, body, path, checksum, now),
                )
                entry_id = int(cursor.lastrowid)

            if self._fts_available:
                conn.execute("DELETE FROM memory_entries_fts WHERE rowid = ?", (entry_id,))
                conn.execute(
                    """
                    INSERT INTO memory_entries_fts(rowid, title, summary, body, topic)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (entry_id, title, summary, body, topic),
                )
            conn.commit()

    def register_user_turn(self, user_input: str) -> None:
        goal = _trim_text(user_input, 220)
        self.active_context["goal"] = goal
        self.active_context["next_step"] = "Draft response using retrieved context."
        guessed_file = self._extract_probable_file(user_input)
        if guessed_file:
            self.active_context["current_file"] = guessed_file

    def register_tool_result(self, tool_name: str, result: Dict[str, Any]) -> None:
        if "error" in result:
            self.active_context["active_bug"] = _trim_text(str(result["error"]), 180)
            self.active_context["next_step"] = f"Fix tool error for `{tool_name}`."
            return

        action = result.get("action") or result.get("message") or "tool call completed"
        self.active_context["next_step"] = _trim_text(f"{tool_name}: {action}", 180)
        path = result.get("path") or result.get("file_path")
        if isinstance(path, str) and path:
            self.active_context["current_file"] = _trim_text(path, 220)

    def _extract_probable_file(self, text: str) -> str:
        candidates = re.findall(r"[\w./-]+\.(?:py|ts|tsx|js|jsx|md|toml|json|yaml|yml|txt)", text)
        return candidates[0] if candidates else ""

    def save_memory(
        self,
        memory_type: str,
        topic: str,
        summary: str,
        content: Optional[str] = None,
    ) -> Dict[str, Any]:
        clean_type = _slugify(memory_type or "note")
        clean_topic = _slugify(topic or "general")
        summary_text = _trim_text(summary or "", 800)
        if not summary_text:
            return {"error": "summary is required"}

        body = (content or "").strip()
        title = f"{clean_topic}-{clean_type}"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        directory = self.knowledge_dir / clean_type
        directory.mkdir(parents=True, exist_ok=True)
        filename = f"{clean_topic}-{timestamp}.md"
        full_path = directory / filename
        markdown = (
            f"# {clean_topic.replace('-', ' ').title()}\n\n"
            f"- Type: {clean_type}\n"
            f"- Topic: {clean_topic}\n"
            f"- Saved At: {_utc_now_iso()}\n\n"
            f"## Summary\n{summary_text}\n\n"
            f"## Details\n{body or summary_text}\n"
        )
        full_path.write_text(markdown, encoding="utf-8")
        self._upsert_entry(
            memory_type=clean_type,
            topic=clean_topic,
            title=title,
            summary=summary_text,
            body=body or summary_text,
            path=str(full_path),
        )
        return {
            "saved": True,
            "type": clean_type,
            "topic": clean_topic,
            "path": str(full_path),
        }

    def search_memory(self, query: str, limit: int = 5) -> Dict[str, Any]:
        q = query.strip()
        if not q:
            return {"error": "query is required"}
        safe_limit = max(1, min(limit, 20))
        hits = self._search_entries(q, safe_limit)
        return {"query": q, "results": [hit.to_dict() for hit in hits]}

    def _search_entries(self, query: str, limit: int) -> List[MemorySearchHit]:
        with self._connect() as conn:
            rows: Iterable[sqlite3.Row]
            if self._fts_available:
                try:
                    rows = conn.execute(
                        """
                        SELECT e.title, e.topic, e.memory_type, e.summary, e.path,
                               bm25(memory_entries_fts) AS score
                        FROM memory_entries_fts
                        JOIN memory_entries e ON e.id = memory_entries_fts.rowid
                        WHERE memory_entries_fts MATCH ?
                        ORDER BY score
                        LIMIT ?
                        """,
                        (query, limit),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = self._fallback_like_query(conn, query, limit)
            else:
                rows = self._fallback_like_query(conn, query, limit)
        return [
            MemorySearchHit(
                title=str(row["title"]),
                topic=str(row["topic"]),
                memory_type=str(row["memory_type"]),
                summary=_trim_text(str(row["summary"]), 300),
                path=str(row["path"]),
                score=float(row["score"]) if row["score"] is not None else 0.0,
            )
            for row in rows
        ]

    def _fallback_like_query(
        self, conn: sqlite3.Connection, query: str, limit: int
    ) -> List[sqlite3.Row]:
        like = f"%{query}%"
        return conn.execute(
            """
            SELECT title, topic, memory_type, summary, path, 1.0 AS score
            FROM memory_entries
            WHERE title LIKE ? OR summary LIKE ? OR body LIKE ? OR topic LIKE ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (like, like, like, like, limit),
        ).fetchall()

    def summarize_session(
        self,
        conversation: List[Dict[str, Any]],
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        user_msgs = [
            _trim_text(str(m.get("content", "")), 220)
            for m in conversation
            if m.get("role") == "user" and m.get("content")
        ]
        assistant_msgs = [
            _trim_text(str(m.get("content", "")), 220)
            for m in conversation
            if m.get("role") == "assistant" and m.get("content")
        ]
        recent_users = user_msgs[-6:]
        recent_assistant = assistant_msgs[-6:]
        current_goal = self.active_context.get("goal") or (recent_users[-1] if recent_users else "")
        lines = [
            "# Session Summary",
            "",
            f"- Updated: {_utc_now_iso()}",
            f"- Reason: {reason or 'manual'}",
            "",
            "## Current",
            f"- Goal: {current_goal or 'n/a'}",
            f"- Current File: {self.active_context.get('current_file') or 'n/a'}",
            f"- Active Bug: {self.active_context.get('active_bug') or 'n/a'}",
            f"- Next Step: {self.active_context.get('next_step') or 'n/a'}",
            "",
            "## Recent User Requests",
        ]
        if recent_users:
            lines.extend(f"- {msg}" for msg in recent_users)
        else:
            lines.append("- n/a")
        lines.extend(["", "## Recent Assistant Outputs"])
        if recent_assistant:
            lines.extend(f"- {msg}" for msg in recent_assistant)
        else:
            lines.append("- n/a")

        summary = "\n".join(lines).strip() + "\n"
        self.summary_file.write_text(summary, encoding="utf-8")
        return {
            "summarized": True,
            "path": str(self.summary_file),
            "reason": reason or "manual",
            "chars": len(summary),
        }

    def read_session_summary(self) -> str:
        try:
            return self.summary_file.read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    def build_messages(
        self,
        system_prompt: str,
        conversation: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        latest_user = ""
        for message in reversed(conversation):
            if message.get("role") == "user":
                latest_user = str(message.get("content") or "").strip()
                break

        retrieved_lines: List[str] = []
        if latest_user:
            retrieval = self.search_memory(latest_user, limit=4)
            for item in retrieval.get("results", []):
                retrieved_lines.append(
                    f"- [{item['type']}/{item['topic']}] {item['summary']} ({item['path']})"
                )
        retrieved_text = "\n".join(retrieved_lines) if retrieved_lines else "- No relevant saved memory."

        active_context_text = json.dumps(self.active_context, ensure_ascii=False, indent=2)
        summary_text = _trim_text(self.read_session_summary(), self.max_summary_chars)
        memory_block = (
            "Context Manager State:\n"
            f"Active Context:\n{_trim_text(active_context_text, self.max_active_context_chars)}\n\n"
            f"Session Summary:\n{summary_text or 'No session summary yet.'}\n\n"
            f"Retrieved Memory:\n{_trim_text(retrieved_text, self.max_retrieval_chars)}"
        )

        recent_conversation = conversation[-self.max_recent_messages :]
        return [
            {"role": "system", "content": system_prompt},
            {"role": "system", "content": memory_block},
            *recent_conversation,
        ]

    def maybe_auto_summarize(self, conversation: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if len(conversation) < self.summarize_message_threshold:
            return None
        return self.summarize_session(conversation, reason="threshold")

    def offload_large_output(self, name: str, content: str) -> Dict[str, Any]:
        label = _slugify(name or "tool-output")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        target = self.offloaded_dir / f"{label}-{timestamp}.txt"
        target.write_text(content, encoding="utf-8")
        preview = _trim_text(content, 600)
        return {
            "offloaded": True,
            "path": str(target),
            "preview": preview,
            "bytes": len(content.encode("utf-8")),
        }

    def maybe_offload_tool_result(self, tool_name: str, result: Dict[str, Any]) -> Dict[str, Any]:
        payload = json.dumps(result, ensure_ascii=False)
        if len(payload) < self.large_output_threshold:
            return result
        offloaded = self.offload_large_output(tool_name, payload)
        return {
            "action": "offloaded_large_output",
            "tool_name": tool_name,
            "offloaded_path": offloaded["path"],
            "preview": offloaded["preview"],
            "bytes": offloaded["bytes"],
        }

    def retrieve_code(self, query: str, limit: int = 5) -> Dict[str, Any]:
        q = query.strip().lower()
        if not q:
            return {"error": "query is required"}
        safe_limit = max(1, min(limit, 20))
        tokens = [token for token in re.split(r"\W+", q) if token]
        results: List[Dict[str, Any]] = []
        for file_path in self._iter_code_files():
            try:
                text = file_path.read_text(encoding="utf-8")
            except Exception:
                continue
            lower = text.lower()
            score = sum(lower.count(token) for token in tokens)
            if score <= 0 and q not in lower and q not in file_path.as_posix().lower():
                continue
            snippet = self._extract_snippet(text, tokens[0] if tokens else q)
            results.append(
                {
                    "path": str(file_path),
                    "score": score if score > 0 else 1,
                    "snippet": snippet,
                }
            )
        results.sort(key=lambda item: item["score"], reverse=True)
        return {"query": query, "results": results[:safe_limit]}

    def _iter_code_files(self) -> Iterable[Path]:
        seen: set[Path] = set()
        for pattern in DEFAULT_SEARCH_GLOBS:
            for file_path in self.sandbox_root.rglob(pattern):
                if not file_path.is_file():
                    continue
                resolved = file_path.resolve()
                if resolved in seen:
                    continue
                try:
                    relative_parts = resolved.relative_to(self.sandbox_root).parts
                except ValueError:
                    continue
                if any(part in IGNORE_DIRS for part in relative_parts):
                    continue
                if ".indusbuilt" in relative_parts and "memory" in relative_parts:
                    continue
                seen.add(resolved)
                yield resolved

    def _extract_snippet(self, text: str, needle: str) -> str:
        if not text:
            return ""
        haystack = text.lower()
        needle_lower = needle.lower()
        position = haystack.find(needle_lower) if needle_lower else -1
        if position < 0:
            return _trim_text(text, 280)
        start = max(0, position - 120)
        end = min(len(text), position + 180)
        snippet = text[start:end].strip()
        return _trim_text(snippet, 280)

    def rebuild_index(self) -> Dict[str, Any]:
        with self._connect() as conn:
            conn.execute("DELETE FROM memory_entries")
            if self._fts_available:
                conn.execute("DELETE FROM memory_entries_fts")
            conn.commit()

        indexed = 0
        for file_path in sorted(self.knowledge_dir.rglob("*.md")):
            try:
                content = file_path.read_text(encoding="utf-8")
            except Exception:
                continue
            rel = file_path.relative_to(self.knowledge_dir).as_posix()
            memory_type = rel.split("/", 1)[0] if "/" in rel else "knowledge"
            topic = file_path.stem.split("-", 1)[0]
            summary = _trim_text(content, 600)
            self._upsert_entry(
                memory_type=memory_type,
                topic=topic,
                title=file_path.stem,
                summary=summary,
                body=content,
                path=str(file_path),
            )
            indexed += 1
        return {"reindexed": True, "count": indexed, "db_path": str(self.sqlite_path)}

    def status(self) -> Dict[str, Any]:
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM memory_entries").fetchone()
            entry_count = int(count["c"]) if count else 0
        files = list(self.knowledge_dir.rglob("*.md"))
        summary = self.read_session_summary()
        return {
            "memory_root": str(self.memory_root),
            "database": str(self.sqlite_path),
            "knowledge_files": len(files),
            "indexed_entries": entry_count,
            "summary_path": str(self.summary_file),
            "summary_chars": len(summary),
            "active_context": self.active_context,
        }
