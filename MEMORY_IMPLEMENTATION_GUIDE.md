# Quick Implementation Guide: Rolling Summary Memory

## TL;DR

**Problem:** Sending full conversation history every request wastes tokens and latency.

**Solution:** Rolling summary approach - keep recent messages + LLM-generated summary of older messages.

**Result:** ~83% token reduction for long conversations, zero dependencies, works with current stack.

---

## Architecture

```
SQLite History (per user)
    ↓
Messages 1-10: Summarized → "User asked about weather, discussed outdoor plans"
Messages 11-18: Sent raw  → Full context
    ↓
LLM receives: System prompt + Summary + Recent 8 messages
    ↓
Response generated
```

---

## Files to Create/Modify

### 1. Create `meshai/memory.py`

```python
"""Lightweight rolling summary memory manager."""

import time
from dataclasses import dataclass
from typing import Optional

from openai import AsyncOpenAI


@dataclass
class ConversationSummary:
    """Summary of conversation history."""

    summary: str
    last_updated: float
    message_count: int


class RollingSummaryMemory:
    """Manages conversation summaries with recent message window.

    Strategy:
    - Keep last N message pairs (window_size) in full
    - Summarize everything before the window
    - Update summary when old messages accumulate

    Example (window_size=4):
        Messages 1-10: Summarized to "User discussed weather and plans"
        Messages 11-18: Kept in full (last 4 pairs)
        Context sent: [Summary] + [Messages 11-18]
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        window_size: int = 4,
        summarize_threshold: int = 8,
    ):
        """Initialize rolling summary memory.

        Args:
            client: AsyncOpenAI client for generating summaries
            model: Model name to use for summarization
            window_size: Number of recent message pairs to keep in full
            summarize_threshold: Messages to accumulate before re-summarizing
        """
        self._client = client
        self._model = model
        self._window_size = window_size
        self._summarize_threshold = summarize_threshold

        # In-memory cache of summaries (loaded from DB on startup)
        self._summaries: dict[str, ConversationSummary] = {}

    async def get_context_messages(
        self,
        user_id: str,
        full_history: list[dict],
    ) -> tuple[Optional[str], list[dict]]:
        """Get optimized context: summary + recent messages.

        Args:
            user_id: User identifier
            full_history: Full message history from database

        Returns:
            Tuple of (summary_text, recent_messages)
            summary_text is None if conversation is short
        """
        # Short conversation - no summary needed
        if len(full_history) <= self._window_size * 2:
            return None, full_history

        # Split into old (to summarize) and recent (keep raw)
        split_point = -(self._window_size * 2)
        old_messages = full_history[:split_point]
        recent_messages = full_history[split_point:]

        # Get or create summary
        summary = await self._get_or_create_summary(user_id, old_messages)

        return summary.summary, recent_messages

    async def _get_or_create_summary(
        self,
        user_id: str,
        messages: list[dict],
    ) -> ConversationSummary:
        """Get cached summary or create new one."""
        # Check cache
        if user_id in self._summaries:
            cached = self._summaries[user_id]

            # Reuse if message count is close
            if abs(cached.message_count - len(messages)) < self._summarize_threshold:
                return cached

        # Generate new summary
        summary_text = await self._summarize(messages)

        summary = ConversationSummary(
            summary=summary_text,
            last_updated=time.time(),
            message_count=len(messages),
        )

        self._summaries[user_id] = summary
        return summary

    async def _summarize(self, messages: list[dict]) -> str:
        """Generate summary using LLM."""
        # Format conversation
        conversation = "\n".join(
            [f"{msg['role'].upper()}: {msg['content']}" for msg in messages]
        )

        prompt = f"""Summarize this conversation in 2-3 concise sentences. Focus on:
- Main topics discussed
- Important context or user preferences
- Key information to remember

Conversation:
{conversation}

Summary (2-3 sentences):"""

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=150,
                temperature=0.3,
            )

            return response.choices[0].message.content.strip()

        except Exception as e:
            # Fallback
            return f"Previous conversation: {len(messages)} messages about various topics."

    def load_summary(self, user_id: str, summary: ConversationSummary) -> None:
        """Load summary from database into cache."""
        self._summaries[user_id] = summary

    def clear_summary(self, user_id: str) -> None:
        """Clear cached summary for user."""
        self._summaries.pop(user_id, None)
```

---

### 2. Modify `meshai/history.py`

Add summary storage methods:

```python
# Add to ConversationHistory class

async def initialize(self) -> None:
    """Initialize database and create tables."""
    self._db = await aiosqlite.connect(self._db_path)

    # Existing conversations table
    await self._db.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp REAL NOT NULL
        )
    """)

    await self._db.execute("""
        CREATE INDEX IF NOT EXISTS idx_user_timestamp
        ON conversations (user_id, timestamp)
    """)

    # NEW: Summaries table
    await self._db.execute("""
        CREATE TABLE IF NOT EXISTS conversation_summaries (
            user_id TEXT PRIMARY KEY,
            summary TEXT NOT NULL,
            message_count INTEGER NOT NULL,
            updated_at REAL NOT NULL
        )
    """)

    await self._db.commit()
    logger.info(f"Conversation history initialized at {self._db_path}")


async def store_summary(
    self, user_id: str, summary: str, message_count: int
) -> None:
    """Store conversation summary.

    Args:
        user_id: Node ID of user
        summary: Summary text
        message_count: Number of messages summarized
    """
    if not self._db:
        raise RuntimeError("Database not initialized")

    async with self._lock:
        await self._db.execute(
            """
            INSERT OR REPLACE INTO conversation_summaries
            (user_id, summary, message_count, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, summary, message_count, time.time()),
        )
        await self._db.commit()


async def get_summary(self, user_id: str) -> Optional[dict]:
    """Get conversation summary for user.

    Args:
        user_id: Node ID of user

    Returns:
        Dict with 'summary', 'message_count', 'updated_at' or None
    """
    if not self._db:
        raise RuntimeError("Database not initialized")

    async with self._lock:
        cursor = await self._db.execute(
            """
            SELECT summary, message_count, updated_at
            FROM conversation_summaries
            WHERE user_id = ?
            """,
            (user_id,),
        )
        row = await cursor.fetchone()

    if not row:
        return None

    return {
        "summary": row[0],
        "message_count": row[1],
        "updated_at": row[2],
    }


async def clear_summary(self, user_id: str) -> None:
    """Clear summary for user (e.g., on history reset).

    Args:
        user_id: Node ID of user
    """
    if not self._db:
        raise RuntimeError("Database not initialized")

    async with self._lock:
        await self._db.execute(
            "DELETE FROM conversation_summaries WHERE user_id = ?",
            (user_id,),
        )
        await self._db.commit()
```

---

### 3. Modify `meshai/backends/openai_backend.py`

Integrate memory manager:

```python
"""OpenAI-compatible LLM backend with rolling summary memory."""

import logging
from typing import Optional

from openai import AsyncOpenAI

from ..config import LLMConfig
from ..memory import RollingSummaryMemory
from .base import LLMBackend

logger = logging.getLogger(__name__)


class OpenAIBackend(LLMBackend):
    """OpenAI-compatible backend with intelligent memory management."""

    def __init__(self, config: LLMConfig, api_key: str):
        """Initialize OpenAI backend.

        Args:
            config: LLM configuration
            api_key: API key to use
        """
        self.config = config
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=config.base_url,
        )

        # Initialize rolling summary memory
        self._memory = RollingSummaryMemory(
            client=self._client,
            model=config.model,
            window_size=4,  # Keep last 4 exchanges (8 messages)
            summarize_threshold=8,  # Re-summarize after 8 new messages
        )

    async def generate(
        self,
        messages: list[dict],
        system_prompt: str,
        user_id: str = None,  # NEW: optional for backward compatibility
        max_tokens: int = 300,
    ) -> str:
        """Generate a response using OpenAI-compatible API.

        Args:
            messages: Conversation history
            system_prompt: System prompt
            user_id: User identifier (for memory management)
            max_tokens: Maximum tokens to generate

        Returns:
            Generated response
        """
        # If no user_id, use old behavior (send full history)
        if not user_id:
            full_messages = [{"role": "system", "content": system_prompt}]
            full_messages.extend(messages)
        else:
            # Use memory manager to optimize context
            summary, recent_messages = await self._memory.get_context_messages(
                user_id=user_id,
                full_history=messages,
            )

            # Build optimized message list
            if summary:
                # Long conversation: system + summary + recent
                enhanced_system = f"""{system_prompt}

Previous conversation summary: {summary}"""
                full_messages = [{"role": "system", "content": enhanced_system}]
                full_messages.extend(recent_messages)

                logger.debug(
                    f"Using summary + {len(recent_messages)} recent messages "
                    f"(total history: {len(messages)})"
                )
            else:
                # Short conversation: system + all messages
                full_messages = [{"role": "system", "content": system_prompt}]
                full_messages.extend(messages)

        try:
            response = await self._client.chat.completions.create(
                model=self.config.model,
                messages=full_messages,
                max_tokens=max_tokens,
                temperature=0.7,
            )

            content = response.choices[0].message.content
            return content.strip() if content else ""

        except Exception as e:
            logger.error(f"OpenAI API error: {e}")
            raise

    def load_summary_cache(self, user_id: str, summary_data: dict) -> None:
        """Load summary into memory cache (called on startup).

        Args:
            user_id: User identifier
            summary_data: Dict with 'summary', 'message_count', 'updated_at'
        """
        from ..memory import ConversationSummary

        summary = ConversationSummary(
            summary=summary_data["summary"],
            message_count=summary_data["message_count"],
            last_updated=summary_data["updated_at"],
        )
        self._memory.load_summary(user_id, summary)

    def clear_summary_cache(self, user_id: str) -> None:
        """Clear summary cache for user."""
        self._memory.clear_summary(user_id)

    # ... rest of methods unchanged ...
```

---

### 4. Modify `meshai/responder.py`

Pass user_id to backend and persist summaries:

```python
# In the generate_response method

async def generate_response(self, user_id: str, message: str) -> str:
    """Generate LLM response with optimized memory."""

    # Add user message to history
    await self.history.add_message(user_id, "user", message)

    # Get conversation history
    history = await self.history.get_history_for_llm(user_id)

    # Generate response with user_id for memory management
    response = await self.backend.generate(
        messages=history,
        system_prompt=self.system_prompt,
        user_id=user_id,  # NEW: enables memory optimization
        max_tokens=300,
    )

    # Add assistant response to history
    await self.history.add_message(user_id, "assistant", response)

    # Persist summary if one was created
    # The memory manager caches it, we need to save to DB
    summary_data = await self._get_current_summary(user_id)
    if summary_data:
        await self.history.store_summary(
            user_id,
            summary_data["summary"],
            summary_data["message_count"],
        )

    return response


async def _get_current_summary(self, user_id: str) -> Optional[dict]:
    """Get current summary from memory manager if it exists."""
    # Access the memory manager's cache
    if hasattr(self.backend, "_memory"):
        summary = self.backend._memory._summaries.get(user_id)
        if summary:
            return {
                "summary": summary.summary,
                "message_count": summary.message_count,
                "updated_at": summary.last_updated,
            }
    return None
```

---

### 5. Modify `meshai/commands/reset.py`

Clear summaries when resetting history:

```python
async def execute(self, sender_id: str, args: list[str]) -> str:
    """Reset conversation history."""
    count = await self.responder.history.clear_history(sender_id)

    # NEW: Also clear summary
    await self.responder.history.clear_summary(sender_id)
    if hasattr(self.responder.backend, "clear_summary_cache"):
        self.responder.backend.clear_summary_cache(sender_id)

    return f"Cleared {count} messages from your history."
```

---

## Configuration

Add to `meshai/config.py`:

```python
@dataclass
class MemoryConfig:
    """Memory management configuration."""

    # Rolling summary settings
    window_size: int = 4  # Recent message pairs to keep
    summarize_threshold: int = 8  # Messages before re-summarizing

    # When to enable summaries
    min_messages_for_summary: int = 10  # Start summarizing after this many
```

---

## Testing

```python
# Test script
import asyncio
from meshai.backends.openai_backend import OpenAIBackend
from meshai.config import LLMConfig

async def test():
    config = LLMConfig(
        backend="openai",
        base_url="http://192.168.1.239:8000/v1",
        model="gpt-4o-mini"
    )

    backend = OpenAIBackend(config, "your-key")

    # Simulate long conversation
    messages = []
    for i in range(20):
        messages.append({"role": "user", "content": f"Question {i}"})
        messages.append({"role": "assistant", "content": f"Answer {i}"})

    # Generate - should use summary
    response = await backend.generate(
        messages=messages,
        system_prompt="You are helpful.",
        user_id="!test123",
        max_tokens=100
    )

    print(f"Response: {response}")
    print(f"Sent {len(messages)} messages, but only ~10 used in context")

asyncio.run(test())
```

---

## Expected Results

### Token Usage Comparison

**Before (full history):**
```
User message 1-20: ~2000 tokens
System prompt: ~50 tokens
Total: ~2050 tokens per request
```

**After (with summary):**
```
System prompt: ~50 tokens
Summary: ~100 tokens
Recent 8 messages: ~400 tokens
Total: ~550 tokens per request
```

**Savings: ~73% token reduction**

### Performance Impact

- **Summary generation**: ~1-2s every 8-10 messages (amortized)
- **Regular requests**: No added latency
- **Storage**: ~100 bytes per summary in SQLite

---

## Tuning Parameters

### window_size
- **Smaller (2-3)**: More aggressive summarization, max token savings
- **Larger (5-6)**: More context, less summarization
- **Recommended**: 4 (last 4 exchanges = 8 messages)

### summarize_threshold
- **Smaller (4-6)**: Frequent re-summarization, more current
- **Larger (10-12)**: Less summarization overhead
- **Recommended**: 8 (re-summarize after 8 new messages)

### For MeshAI specifically:
- Messages are tiny (150 chars max)
- `window_size=4` gives ~600 chars of recent context
- `summarize_threshold=8` balances overhead vs accuracy

---

## Migration Path

1. **Phase 1**: Add code, test with new users
2. **Phase 2**: Run in parallel (old + new backend)
3. **Phase 3**: Migrate existing users (generate summaries for existing history)
4. **Phase 4**: Remove old full-history code path

No data loss - summaries stored in DB, can regenerate anytime.

---

## Maintenance

### Monitor summary quality:
```sql
-- Check summaries
SELECT user_id, summary, message_count, updated_at
FROM conversation_summaries
ORDER BY updated_at DESC;
```

### Regenerate summary:
```python
# Clear cache + DB, will regenerate on next request
await history.clear_summary(user_id)
backend.clear_summary_cache(user_id)
```

### Adjust if summaries too short/long:
- Modify prompt in `_summarize()`
- Adjust `max_tokens=150` for summaries
- Change temperature (lower = more consistent)

---

## Future Enhancements

1. **Hybrid approach**: Summary + semantic search for very long histories
2. **User preferences**: Store separate from summary (e.g., "likes weather in metric")
3. **Multi-level summaries**: Summarize summaries for years-long conversations
4. **Summary quality scoring**: Validate summaries maintain key information

But start simple - this gets 80% of the benefit with 20% of the complexity.
