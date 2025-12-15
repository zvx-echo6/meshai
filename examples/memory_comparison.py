#!/usr/bin/env python3
"""
Proof-of-concept: Compare full history vs rolling summary memory.

Demonstrates token savings and performance of different approaches.

Usage:
    python examples/memory_comparison.py
"""

import asyncio
import time
from typing import Optional

from openai import AsyncOpenAI


# ============================================================================
# SIMPLE ROLLING SUMMARY IMPLEMENTATION
# ============================================================================


class SimpleRollingSummary:
    """Minimal rolling summary memory manager for testing."""

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        window_size: int = 4,
    ):
        self.client = client
        self.model = model
        self.window_size = window_size
        self._summary_cache = {}

    async def get_context(
        self, user_id: str, messages: list[dict]
    ) -> tuple[Optional[str], list[dict]]:
        """Return (summary, recent_messages) for optimized context."""

        # Short conversation - return all messages
        if len(messages) <= self.window_size * 2:
            return None, messages

        # Split old and recent
        split = -(self.window_size * 2)
        old = messages[:split]
        recent = messages[split:]

        # Get or create summary
        if user_id not in self._summary_cache:
            summary = await self._summarize(old)
            self._summary_cache[user_id] = summary
        else:
            summary = self._summary_cache[user_id]

        return summary, recent

    async def _summarize(self, messages: list[dict]) -> str:
        """Generate summary of messages."""
        conv = "\n".join([f"{m['role'].upper()}: {m['content']}" for m in messages])

        prompt = f"""Summarize this conversation in 2-3 concise sentences:

{conv}

Summary:"""

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0.3,
        )

        return response.choices[0].message.content.strip()


# ============================================================================
# COMPARISON SCENARIOS
# ============================================================================


async def test_full_history(client: AsyncOpenAI, model: str, messages: list[dict]):
    """Baseline: Send full conversation history."""
    print("\n=== FULL HISTORY APPROACH ===")

    system = "You are a helpful assistant on a mesh network."
    full = [{"role": "system", "content": system}] + messages

    start = time.time()

    response = await client.chat.completions.create(
        model=model, messages=full, max_tokens=100, temperature=0.7
    )

    elapsed = time.time() - start

    # Estimate tokens (rough)
    total_chars = sum(len(m["content"]) for m in full)
    est_tokens = total_chars // 4  # Rough estimate: 4 chars = 1 token

    print(f"Messages sent: {len(full)}")
    print(f"Est. input tokens: {est_tokens}")
    print(f"Response: {response.choices[0].message.content[:100]}...")
    print(f"Time: {elapsed:.2f}s")

    return est_tokens, elapsed


async def test_rolling_summary(
    client: AsyncOpenAI, model: str, messages: list[dict], user_id: str
):
    """Optimized: Send summary + recent messages."""
    print("\n=== ROLLING SUMMARY APPROACH ===")

    memory = SimpleRollingSummary(client, model, window_size=4)

    summary, recent = await memory.get_context(user_id, messages)

    system = "You are a helpful assistant on a mesh network."
    if summary:
        system += f"\n\nPrevious conversation summary: {summary}"

    context = [{"role": "system", "content": system}] + recent

    start = time.time()

    response = await client.chat.completions.create(
        model=model, messages=context, max_tokens=100, temperature=0.7
    )

    elapsed = time.time() - start

    # Estimate tokens
    total_chars = sum(len(m["content"]) for m in context)
    est_tokens = total_chars // 4

    print(f"Messages sent: {len(context)} (summary: {summary is not None})")
    if summary:
        print(f"Summary: {summary[:80]}...")
    print(f"Est. input tokens: {est_tokens}")
    print(f"Response: {response.choices[0].message.content[:100]}...")
    print(f"Time: {elapsed:.2f}s")

    return est_tokens, elapsed


async def test_window_only(client: AsyncOpenAI, model: str, messages: list[dict]):
    """Simple window: Just last N messages, no summary."""
    print("\n=== WINDOW-ONLY APPROACH ===")

    window_size = 4
    recent = messages[-(window_size * 2) :]

    system = "You are a helpful assistant on a mesh network."
    context = [{"role": "system", "content": system}] + recent

    start = time.time()

    response = await client.chat.completions.create(
        model=model, messages=context, max_tokens=100, temperature=0.7
    )

    elapsed = time.time() - start

    total_chars = sum(len(m["content"]) for m in context)
    est_tokens = total_chars // 4

    print(f"Messages sent: {len(context)} (last {window_size} exchanges only)")
    print(f"Est. input tokens: {est_tokens}")
    print(f"Response: {response.choices[0].message.content[:100]}...")
    print(f"Time: {elapsed:.2f}s")

    return est_tokens, elapsed


# ============================================================================
# MAIN TEST
# ============================================================================


async def main():
    """Run comparison test."""

    # Configure your LLM endpoint
    # Update these for your setup (LiteLLM, local model, etc.)
    BASE_URL = "http://192.168.1.239:8000/v1"  # LiteLLM endpoint
    API_KEY = "sk-1234"  # Your API key
    MODEL = "gpt-4o-mini"  # Your model

    print("=" * 70)
    print("LLM Memory Approach Comparison")
    print("=" * 70)

    # Create test conversation (simulate 15 exchanges = 30 messages)
    messages = []
    topics = [
        ("What's the weather?", "It's sunny and 72°F."),
        ("Should I bring an umbrella?", "No need, clear skies all day."),
        ("What about tomorrow?", "Tomorrow looks rainy, bring an umbrella."),
        ("Any hiking recommendations?", "Try Mt. Si, great views!"),
        ("How long is the hike?", "About 4 hours round trip."),
        ("Is it beginner friendly?", "Moderate difficulty, doable for most."),
        ("What should I bring?", "Water, snacks, good boots, and layers."),
        ("Are dogs allowed?", "Yes, but must be leashed."),
        ("Where's the trailhead?", "Off I-90 near North Bend."),
        ("Parking fee?", "Yes, $10 or Northwest Forest Pass."),
        ("What time should I start?", "Early morning, around 7-8 AM."),
        ("How crowded does it get?", "Very crowded on weekends, go weekdays."),
        ("Any other trails nearby?", "Rattlesnake Ledge is easier and closer."),
        ("Tell me about Rattlesnake", "2 miles, great lake views, very popular."),
        ("Which would you recommend?", "If fit: Mt Si. If casual: Rattlesnake."),
    ]

    for user_msg, assistant_msg in topics:
        messages.append({"role": "user", "content": user_msg})
        messages.append({"role": "assistant", "content": assistant_msg})

    print(f"\nTest conversation: {len(messages)} messages ({len(messages)//2} exchanges)")
    print(f"Topics: weather → hiking → trails")
    print(f"Message lengths: {min(len(m['content']) for m in messages)}-{max(len(m['content']) for m in messages)} chars")

    # Initialize client
    client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)

    try:
        # Test each approach
        full_tokens, full_time = await test_full_history(client, MODEL, messages)
        summary_tokens, summary_time = await test_rolling_summary(
            client, MODEL, messages, "!test_user"
        )
        window_tokens, window_time = await test_window_only(client, MODEL, messages)

        # Results
        print("\n" + "=" * 70)
        print("COMPARISON RESULTS")
        print("=" * 70)

        print(f"\n{'Approach':<20} {'Tokens':<15} {'Time':<10} {'Savings'}")
        print("-" * 70)
        print(
            f"{'Full History':<20} {full_tokens:<15} {full_time:<10.2f}s {'(baseline)'}"
        )
        print(
            f"{'Rolling Summary':<20} {summary_tokens:<15} {summary_time:<10.2f}s "
            f"{(1 - summary_tokens/full_tokens)*100:.1f}%"
        )
        print(
            f"{'Window Only':<20} {window_tokens:<15} {window_time:<10.2f}s "
            f"{(1 - window_tokens/full_tokens)*100:.1f}%"
        )

        print("\n" + "=" * 70)
        print("RECOMMENDATIONS")
        print("=" * 70)

        print("\nFull History:")
        print("  ✓ Complete context")
        print("  ✗ High token usage")
        print("  ✗ Slower for long conversations")
        print("  Use: Never (inefficient)")

        print("\nWindow Only:")
        print("  ✓ Very low token usage")
        print("  ✓ Fast")
        print("  ✗ Loses older context completely")
        print("  Use: Short-term conversations only")

        print("\nRolling Summary:")
        print("  ✓ Balanced token usage")
        print("  ✓ Preserves long-term context")
        print("  ✓ Fast after initial summary")
        print("  ✗ Slight overhead for summarization")
        print("  Use: RECOMMENDED for MeshAI")

        print("\n" + "=" * 70)

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
