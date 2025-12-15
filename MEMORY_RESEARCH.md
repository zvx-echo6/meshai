# LLM Conversation Memory Research for MeshAI

## Current Implementation Analysis

**Current approach:** MeshAI stuffs full conversation history into every LLM API call
- Storage: SQLite via aiosqlite
- Retrieval: `get_history_for_llm()` returns all messages (up to `max_messages_per_user * 2`)
- Backend: OpenAI-compatible API (works with LiteLLM, local models)
- Context: 150 char max per message, per-user conversations

**Problem:** Inefficient - sends entire history even when unnecessary, wastes tokens and latency.

---

## 1. LangChain Memory Modules

### Installation
```bash
pip install langchain langchain-community langchain-openai
```

### A. ConversationBufferMemory (Simplest)

**What it does:** Stores raw messages in memory, returns all messages.

```python
from langchain.memory import ConversationBufferMemory
from langchain_openai import ChatOpenAI
from langchain.chains import ConversationChain

# Initialize
llm = ChatOpenAI(
    base_url="http://192.168.1.239:8000/v1",  # LiteLLM
    api_key="your-key",
    model="gpt-4o-mini"
)

memory = ConversationBufferMemory()

chain = ConversationChain(
    llm=llm,
    memory=memory,
    verbose=False
)

# Use it
response = chain.predict(input="What's the weather?")
print(response)

# Access history
print(memory.load_memory_variables({}))
# {'history': 'Human: What's the weather?\nAI: ...'}
```

**Integration with MeshAI:**
```python
# In meshai/backends/openai_backend.py
from langchain.memory import ConversationBufferMemory
from langchain_openai import ChatOpenAI
from langchain.chains import ConversationChain

class OpenAIBackendWithMemory(LLMBackend):
    def __init__(self, config: LLMConfig, api_key: str):
        self.config = config
        self._llm = ChatOpenAI(
            base_url=config.base_url,
            api_key=api_key,
            model=config.model,
            temperature=0.7,
            max_tokens=300
        )
        # Per-user memory storage
        self._user_memories: dict[str, ConversationBufferMemory] = {}

    def _get_memory(self, user_id: str) -> ConversationBufferMemory:
        if user_id not in self._user_memories:
            self._user_memories[user_id] = ConversationBufferMemory()
        return self._user_memories[user_id]

    async def generate(
        self,
        messages: list[dict],
        system_prompt: str,
        user_id: str,  # NEW: need user_id for memory
        max_tokens: int = 300,
    ) -> str:
        memory = self._get_memory(user_id)

        # Create chain with memory
        chain = ConversationChain(
            llm=self._llm,
            memory=memory,
            verbose=False
        )

        # Extract last user message
        last_msg = messages[-1]["content"]

        # Generate with memory
        response = await chain.apredict(input=last_msg)
        return response.strip()
```

**Pros:**
- Dead simple, drop-in replacement
- Works with any OpenAI-compatible API
- No external dependencies
- LangChain handles message formatting

**Cons:**
- Still sends full history (no real efficiency gain)
- Stores everything in RAM (lost on restart)
- Need to manage per-user memory dicts
- Adds LangChain dependency (~50MB)

**Verdict:** Not worth it - adds complexity without solving core problem.

---

### B. ConversationBufferWindowMemory (Better)

**What it does:** Only keeps last N messages in context.

```python
from langchain.memory import ConversationBufferWindowMemory

# Keep only last 5 interactions (10 messages = 5 pairs)
memory = ConversationBufferWindowMemory(k=5)

chain = ConversationChain(
    llm=llm,
    memory=memory
)

# Only last 5 exchanges sent to LLM
response = chain.predict(input="Hello")
```

**Integration:**
```python
class OpenAIBackendWithWindow(LLMBackend):
    def __init__(self, config: LLMConfig, api_key: str):
        self.config = config
        self._llm = ChatOpenAI(
            base_url=config.base_url,
            api_key=api_key,
            model=config.model
        )
        # Per-user windowed memory
        self._user_memories: dict[str, ConversationBufferWindowMemory] = {}
        self._window_size = 5  # Last 5 exchanges

    def _get_memory(self, user_id: str) -> ConversationBufferWindowMemory:
        if user_id not in self._user_memories:
            self._user_memories[user_id] = ConversationBufferWindowMemory(
                k=self._window_size
            )
        return self._user_memories[user_id]
```

**Pros:**
- Simple sliding window approach
- Reduces token usage automatically
- Works with any OpenAI-compatible API
- Configurable window size

**Cons:**
- Still in-memory only (lost on restart)
- Forgets old context completely
- Need to integrate with existing SQLite storage
- Adds LangChain dependency

**Verdict:** Better than full buffer, but loses long-term context.

---

### C. ConversationSummaryMemory (Most Interesting)

**What it does:** Uses LLM to summarize conversation, keeps summary + recent messages.

```python
from langchain.memory import ConversationSummaryMemory

memory = ConversationSummaryMemory(llm=llm)

chain = ConversationChain(
    llm=llm,
    memory=memory
)

# After multiple messages, memory contains:
# - Summary of old conversation
# - Recent raw messages
response = chain.predict(input="What did we talk about?")
# AI can reference both summary and recent context
```

**Integration with SQLite persistence:**
```python
from langchain.memory import ConversationSummaryMemory
from langchain_openai import ChatOpenAI

class OpenAIBackendWithSummary(LLMBackend):
    def __init__(self, config: LLMConfig, api_key: str, history: ConversationHistory):
        self.config = config
        self.history = history  # Existing SQLite history

        self._llm = ChatOpenAI(
            base_url=config.base_url,
            api_key=api_key,
            model=config.model
        )

        # Per-user summaries (load from DB)
        self._user_summaries: dict[str, str] = {}
        self._window_size = 4  # Keep last 4 messages raw

    async def generate(
        self,
        messages: list[dict],
        system_prompt: str,
        user_id: str,
        max_tokens: int = 300,
    ) -> str:
        # Get full history from SQLite
        full_history = await self.history.get_history(user_id)

        if len(full_history) <= self._window_size * 2:
            # Small conversation, just use raw messages
            context_messages = messages
        else:
            # Large conversation: summarize old + keep recent
            old_messages = full_history[:-self._window_size * 2]
            recent_messages = full_history[-self._window_size * 2:]

            # Get or create summary
            summary = await self._get_summary(user_id, old_messages)

            # Build context: system + summary + recent messages
            context_messages = [
                {"role": "system", "content": f"{system_prompt}\n\nConversation summary: {summary}"}
            ]
            context_messages.extend([
                {"role": msg.role, "content": msg.content}
                for msg in recent_messages
            ])

        # Generate response
        response = await self._client.chat.completions.create(
            model=self.config.model,
            messages=context_messages,
            max_tokens=max_tokens,
            temperature=0.7,
        )

        return response.choices[0].message.content.strip()

    async def _get_summary(self, user_id: str, messages: list) -> str:
        """Summarize old messages using LLM."""
        if user_id in self._user_summaries:
            return self._user_summaries[user_id]

        # Create summary prompt
        conversation_text = "\n".join([
            f"{msg.role}: {msg.content}" for msg in messages
        ])

        summary_prompt = f"""Summarize this conversation in 2-3 sentences, focusing on key topics and user preferences:

{conversation_text}

Summary:"""

        response = await self._client.chat.completions.create(
            model=self.config.model,
            messages=[{"role": "user", "content": summary_prompt}],
            max_tokens=150,
            temperature=0.3,
        )

        summary = response.choices[0].message.content.strip()

        # Store in SQLite
        await self._store_summary(user_id, summary)
        self._user_summaries[user_id] = summary

        return summary

    async def _store_summary(self, user_id: str, summary: str):
        """Store summary in SQLite for persistence."""
        # Add new table for summaries
        await self.history._db.execute("""
            CREATE TABLE IF NOT EXISTS conversation_summaries (
                user_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)

        await self.history._db.execute("""
            INSERT OR REPLACE INTO conversation_summaries (user_id, summary, updated_at)
            VALUES (?, ?, ?)
        """, (user_id, summary, time.time()))

        await self.history._db.commit()
```

**Pros:**
- Best balance: compact summary + recent context
- Significantly reduces token usage for long conversations
- Works with existing OpenAI-compatible APIs
- Preserves long-term context
- Can persist summaries in SQLite

**Cons:**
- Costs extra tokens to generate summaries
- Adds latency when summarizing
- Need to decide when to re-summarize
- Still requires LangChain

**Verdict:** BEST LANGCHAIN OPTION for MeshAI - balances efficiency and context retention.

---

## 2. LlamaIndex

### Installation
```bash
pip install llama-index llama-index-llms-openai
```

### Chat Memory

```python
from llama_index.core.memory import ChatMemoryBuffer
from llama_index.llms.openai import OpenAI
from llama_index.core.llms import ChatMessage

# Initialize
llm = OpenAI(
    api_base="http://192.168.1.239:8000/v1",
    api_key="your-key",
    model="gpt-4o-mini"
)

# Create memory buffer
memory = ChatMemoryBuffer.from_defaults(token_limit=1500)

# Add messages
memory.put(ChatMessage(role="user", content="Hello"))
memory.put(ChatMessage(role="assistant", content="Hi there!"))

# Get messages for LLM
messages = memory.get()

# Generate with context
response = llm.chat(messages)
```

**Integration:**
```python
from llama_index.core.memory import ChatMemoryBuffer
from llama_index.llms.openai import OpenAI
from llama_index.core.llms import ChatMessage

class LlamaIndexBackend(LLMBackend):
    def __init__(self, config: LLMConfig, api_key: str):
        self.config = config
        self._llm = OpenAI(
            api_base=config.base_url,
            api_key=api_key,
            model=config.model
        )

        # Per-user memory buffers
        self._user_memories: dict[str, ChatMemoryBuffer] = {}
        self._token_limit = 1500

    def _get_memory(self, user_id: str) -> ChatMemoryBuffer:
        if user_id not in self._user_memories:
            self._user_memories[user_id] = ChatMemoryBuffer.from_defaults(
                token_limit=self._token_limit
            )
        return self._user_memories[user_id]

    async def generate(
        self,
        messages: list[dict],
        system_prompt: str,
        user_id: str,
        max_tokens: int = 300,
    ) -> str:
        memory = self._get_memory(user_id)

        # Add new message to memory
        user_msg = messages[-1]["content"]
        memory.put(ChatMessage(role="user", content=user_msg))

        # Get messages within token limit
        context_messages = memory.get()

        # Add system prompt
        full_messages = [ChatMessage(role="system", content=system_prompt)]
        full_messages.extend(context_messages)

        # Generate
        response = self._llm.chat(full_messages)

        # Store assistant response
        memory.put(ChatMessage(role="assistant", content=response.message.content))

        return response.message.content
```

**Pros:**
- Token-aware buffering (auto-prunes to stay under limit)
- Simple API
- Works with OpenAI-compatible backends
- Better than manual message counting

**Cons:**
- In-memory only (need custom persistence)
- Heavy dependency (~100MB)
- Overkill for simple chat
- Less mature than LangChain

**Verdict:** Token limiting is nice, but not worth the dependency weight.

---

## 3. MemGPT / Letta (Self-Editing Memory)

### Installation
```bash
pip install letta
```

### Usage

**What it does:** Agent manages its own memory, decides what to keep/forget/summarize.

```python
from letta import create_client

client = create_client()

# Create agent with memory management
agent = client.create_agent(
    name="meshai_agent",
    llm_config={
        "model": "gpt-4o-mini",
        "model_endpoint": "http://192.168.1.239:8000/v1"
    },
    embedding_config={
        "embedding_endpoint_type": "openai",
        "embedding_model": "text-embedding-ada-002"
    }
)

# Agent manages memory automatically
response = client.send_message(
    agent_id=agent.id,
    message="What's the weather?",
    role="user"
)

print(response.messages[-1].text)
```

**Architecture:**
- Core memory: Persistent facts the agent always sees
- Recall memory: Searchable vector store of past conversations
- Archival memory: Long-term storage

**Pros:**
- Most sophisticated memory system
- Agent decides what's important
- Built-in vector search
- Handles very long conversations

**Cons:**
- HEAVY (~200MB+ with dependencies)
- Requires vector embeddings (extra API calls/costs)
- Complex setup and learning curve
- Overkill for 150-char mesh messages
- Opinionated architecture (hard to integrate)

**Verdict:** Way too heavy for MeshAI. Only worth it for complex, long-form agents.

---

## 4. Vector Stores (Semantic Memory)

### ChromaDB (Simplest)

```bash
pip install chromadb
```

```python
import chromadb
from chromadb.config import Settings

# Initialize
client = chromadb.Client(Settings(
    persist_directory="/path/to/meshai/memory",
    anonymized_telemetry=False
))

# Create collection per user
collection = client.get_or_create_collection(
    name=f"user_{user_id}",
    metadata={"user_id": user_id}
)

# Add messages
collection.add(
    documents=["What's the weather in Seattle?"],
    metadatas=[{"role": "user", "timestamp": time.time()}],
    ids=["msg_1"]
)

# Semantic search for relevant past messages
results = collection.query(
    query_texts=["weather"],
    n_results=3
)

# Use retrieved messages as context
relevant_context = results['documents'][0]
```

**Integration:**
```python
import chromadb
from chromadb.config import Settings

class VectorMemoryBackend(LLMBackend):
    def __init__(self, config: LLMConfig, api_key: str, db_path: str):
        self.config = config
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=config.base_url,
        )

        # ChromaDB for semantic memory
        self._chroma = chromadb.Client(Settings(
            persist_directory=db_path,
            anonymized_telemetry=False
        ))

        self._window_size = 4  # Keep last 4 messages raw

    def _get_collection(self, user_id: str):
        return self._chroma.get_or_create_collection(
            name=f"user_{user_id.replace('!', '_')}"  # Sanitize ID
        )

    async def generate(
        self,
        messages: list[dict],
        system_prompt: str,
        user_id: str,
        max_tokens: int = 300,
    ) -> str:
        collection = self._get_collection(user_id)

        # Get current query
        current_query = messages[-1]["content"]

        # Search for semantically similar past messages
        try:
            results = collection.query(
                query_texts=[current_query],
                n_results=3,
                where={"role": "assistant"}  # Get past responses
            )
            relevant_history = results['documents'][0] if results['documents'] else []
        except:
            relevant_history = []

        # Build context: system + relevant history + recent messages
        context = system_prompt
        if relevant_history:
            context += "\n\nRelevant past exchanges:\n"
            context += "\n".join(relevant_history[:2])  # Top 2 relevant

        context_messages = [{"role": "system", "content": context}]
        context_messages.extend(messages[-self._window_size*2:])  # Recent messages

        # Generate
        response = await self._client.chat.completions.create(
            model=self.config.model,
            messages=context_messages,
            max_tokens=max_tokens,
            temperature=0.7,
        )

        reply = response.choices[0].message.content.strip()

        # Store in vector DB
        msg_id = f"{user_id}_{int(time.time()*1000)}"
        collection.add(
            documents=[f"User: {current_query}\nAssistant: {reply}"],
            metadatas=[{"role": "assistant", "timestamp": time.time()}],
            ids=[msg_id]
        )

        return reply
```

**Pros:**
- Semantic search - finds relevant past context
- Works great for sparse conversations
- Persistent storage
- Lightweight (~20MB)
- No extra API calls (uses local embeddings)

**Cons:**
- Adds dependency
- Embedding computation overhead
- May surface irrelevant "similar" messages
- Overkill for very short conversations

**Verdict:** Interesting for long-term memory, but maybe overkill for 150-char messages.

---

### Qdrant (Production Alternative)

```bash
pip install qdrant-client
```

```python
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

# Can run in-memory or with server
client = QdrantClient(path="/path/to/meshai/qdrant")

# Create collection
client.create_collection(
    collection_name="meshai_memory",
    vectors_config=VectorParams(size=1536, distance=Distance.COSINE),
)

# Store with embedding (from OpenAI or local model)
client.upsert(
    collection_name="meshai_memory",
    points=[
        PointStruct(
            id=msg_id,
            vector=embedding,  # 1536-dim from text-embedding-ada-002
            payload={"user_id": user_id, "content": content, "role": role}
        )
    ]
)

# Search
results = client.search(
    collection_name="meshai_memory",
    query_vector=query_embedding,
    query_filter={"user_id": user_id},
    limit=3
)
```

**Pros:**
- Production-ready, fast
- Better than ChromaDB for scale
- Rich filtering options
- Can run in-memory or server mode

**Cons:**
- More complex than ChromaDB
- Still requires embeddings
- Heavier dependency

**Verdict:** Better than ChromaDB for production, but still overkill for MeshAI's use case.

---

## 5. Simple Rolling Summary (RECOMMENDED)

**The lightest, most practical approach for MeshAI.**

### Implementation

```python
import asyncio
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

class SimpleRollingSummary:
    """Lightweight rolling summary memory manager."""

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        window_size: int = 4,  # Recent messages to keep raw
        summarize_threshold: int = 10,  # Messages before summarizing
    ):
        self._client = client
        self._model = model
        self._window_size = window_size
        self._summarize_threshold = summarize_threshold

        # Per-user summaries (would be in SQLite in production)
        self._summaries: dict[str, ConversationSummary] = {}

    async def get_context_messages(
        self,
        user_id: str,
        full_history: list[dict],  # From SQLite
    ) -> list[dict]:
        """Get optimized context messages (summary + recent)."""

        # If conversation is short, just return it
        if len(full_history) <= self._window_size * 2:
            return full_history

        # Split into old and recent
        old_messages = full_history[:-self._window_size * 2]
        recent_messages = full_history[-self._window_size * 2:]

        # Get or create summary of old messages
        summary = await self._get_or_create_summary(user_id, old_messages)

        # Return summary as system message + recent raw messages
        context = [
            {"role": "system", "content": f"Previous conversation summary: {summary.summary}"}
        ]
        context.extend(recent_messages)

        return context

    async def _get_or_create_summary(
        self,
        user_id: str,
        messages: list[dict],
    ) -> ConversationSummary:
        """Get existing summary or create new one."""

        # Check if we have a recent summary
        if user_id in self._summaries:
            existing = self._summaries[user_id]

            # If summary covers roughly the same messages, reuse it
            if abs(existing.message_count - len(messages)) < self._summarize_threshold:
                return existing

        # Create new summary
        summary_text = await self._summarize(messages)

        summary = ConversationSummary(
            summary=summary_text,
            last_updated=time.time(),
            message_count=len(messages)
        )

        self._summaries[user_id] = summary
        return summary

    async def _summarize(self, messages: list[dict]) -> str:
        """Summarize a list of messages using the LLM."""

        # Format conversation
        conversation = "\n".join([
            f"{msg['role'].upper()}: {msg['content']}"
            for msg in messages
        ])

        prompt = f"""Summarize this conversation in 2-3 concise sentences. Focus on:
- Main topics discussed
- Any important user preferences or context
- Key information that should be remembered

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
            # Fallback: simple truncation if summarization fails
            return f"Previous conversation covered {len(messages)} messages."
```

### Integration with MeshAI

```python
# In meshai/backends/openai_backend.py

class OpenAIBackend(LLMBackend):
    """OpenAI-compatible backend with rolling summary memory."""

    def __init__(self, config: LLMConfig, api_key: str):
        self.config = config
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=config.base_url,
        )

        # Add rolling summary manager
        self._memory = SimpleRollingSummary(
            client=self._client,
            model=config.model,
            window_size=4,  # Keep last 4 exchanges (8 messages)
            summarize_threshold=10,  # Summarize after 10 messages
        )

    async def generate(
        self,
        messages: list[dict],
        system_prompt: str,
        user_id: str,  # NEW: need user_id
        max_tokens: int = 300,
    ) -> str:
        """Generate with optimized context."""

        # Get optimized context (summary + recent)
        context_messages = await self._memory.get_context_messages(
            user_id=user_id,
            full_history=messages,
        )

        # Add system prompt
        full_messages = [{"role": "system", "content": system_prompt}]
        full_messages.extend(context_messages)

        # Generate
        response = await self._client.chat.completions.create(
            model=self.config.model,
            messages=full_messages,
            max_tokens=max_tokens,
            temperature=0.7,
        )

        return response.choices[0].message.content.strip()
```

### Persist Summaries in SQLite

```python
# Add to meshai/history.py

async def store_summary(self, user_id: str, summary: str, message_count: int) -> None:
    """Store conversation summary."""
    if not self._db:
        raise RuntimeError("Database not initialized")

    async with self._lock:
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS conversation_summaries (
                user_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                message_count INTEGER NOT NULL,
                updated_at REAL NOT NULL
            )
        """)

        await self._db.execute("""
            INSERT OR REPLACE INTO conversation_summaries
            (user_id, summary, message_count, updated_at)
            VALUES (?, ?, ?, ?)
        """, (user_id, summary, message_count, time.time()))

        await self._db.commit()

async def get_summary(self, user_id: str) -> Optional[ConversationSummary]:
    """Retrieve conversation summary."""
    if not self._db:
        raise RuntimeError("Database not initialized")

    async with self._lock:
        cursor = await self._db.execute("""
            SELECT summary, message_count, updated_at
            FROM conversation_summaries
            WHERE user_id = ?
        """, (user_id,))

        row = await cursor.fetchone()

    if not row:
        return None

    return ConversationSummary(
        summary=row[0],
        message_count=row[1],
        last_updated=row[2]
    )
```

**Pros:**
- NO external dependencies
- Works with existing SQLite storage
- Significantly reduces token usage
- Simple to understand and maintain
- Preserves recent context + summarized history
- Configurable window and threshold

**Cons:**
- Costs tokens to generate summaries
- Slight latency when summarizing
- Need to tune window/threshold params

**Verdict:** BEST OPTION for MeshAI - simple, effective, no dependencies.

---

## Comparison Matrix

| Approach | Dependencies | Complexity | Token Savings | Persistence | OpenAI-Compatible |
|----------|-------------|------------|---------------|-------------|-------------------|
| **LangChain BufferMemory** | langchain (~50MB) | Low | None | No | Yes |
| **LangChain WindowMemory** | langchain (~50MB) | Low | Medium | No | Yes |
| **LangChain SummaryMemory** | langchain (~50MB) | Medium | High | No (DIY) | Yes |
| **LlamaIndex** | llama-index (~100MB) | Medium | Medium | No (DIY) | Yes |
| **MemGPT/Letta** | letta (~200MB) | Very High | Very High | Yes | Yes (complex) |
| **ChromaDB** | chromadb (~20MB) | Medium | Medium | Yes | Yes |
| **Qdrant** | qdrant (~30MB) | High | Medium | Yes | Yes |
| **Rolling Summary (DIY)** | None | Low | High | Yes (SQLite) | Yes |

---

## RECOMMENDATION

**Use Simple Rolling Summary (Option 5)** for MeshAI because:

1. **Zero dependencies** - No LangChain, LlamaIndex, or vector stores
2. **Works with current stack** - Uses existing AsyncOpenAI client and SQLite
3. **Significant efficiency gains** - Keeps last 4-6 exchanges + summary of older messages
4. **Persistent** - Summaries stored in SQLite, survive restarts
5. **Simple to tune** - Two params: `window_size` and `summarize_threshold`
6. **OpenAI-compatible** - Works with LiteLLM, local models, anything
7. **Lightweight** - ~100 lines of code

### Implementation Steps

1. Add `SimpleRollingSummary` class (shown above)
2. Add summary table to SQLite schema
3. Modify `OpenAIBackend.generate()` to use `_memory.get_context_messages()`
4. Add summary storage methods to `ConversationHistory`
5. Configure: `window_size=4` (8 messages), `summarize_threshold=10`

### Expected Performance

**Before (full history):**
- 20 message pairs = ~3000 tokens sent every request
- Latency: higher, costs more

**After (rolling summary):**
- Summary (~100 tokens) + 4 recent pairs (~400 tokens) = ~500 tokens
- **83% token reduction** for long conversations
- Faster responses, lower costs

### When to Consider Alternatives

- **Vector stores (ChromaDB)**: If you need semantic search across users or topics
- **LangChain SummaryMemory**: If you want a batteries-included solution (accept dependency)
- **MemGPT**: If conversations become complex multi-day dialogues (they won't on mesh)

---

## Example Usage

```python
# Initialize
backend = OpenAIBackend(config, api_key)

# First few messages - full history sent
await backend.generate(
    messages=[
        {"role": "user", "content": "What's the weather?"},
        {"role": "assistant", "content": "It's sunny!"},
        {"role": "user", "content": "Should I bring an umbrella?"},
        {"role": "assistant", "content": "No need, it's clear!"},
        # ... 6 more exchanges ...
    ],
    system_prompt="You are a helpful assistant.",
    user_id="!abc123",
)

# After 10+ messages - summary + recent sent
# Context sent to LLM:
# [
#   {"role": "system", "content": "Previous conversation summary: User asked about weather and outdoor activities. Confirmed sunny weather, no rain expected."},
#   {"role": "user", "content": "Should I bring an umbrella?"},
#   {"role": "assistant", "content": "No need, it's clear!"},
#   ... (last 4 exchanges)
# ]
```

---

## Code Files to Modify

1. **`meshai/memory.py`** (NEW) - Add `SimpleRollingSummary` class
2. **`meshai/history.py`** - Add summary storage methods + table schema
3. **`meshai/backends/openai_backend.py`** - Integrate memory manager
4. **`meshai/responder.py`** - Pass `user_id` to backend.generate()
5. **`meshai/config.py`** - Add config for window_size, summarize_threshold

Let me know if you want me to implement this!
