"""Semantic filtering for tools and skills via embedding cosine similarity.

Ranks tools/skills by how well their descriptions match the user query,
keeping only the top-k to reduce context clutter and improve LLM accuracy.
"""

from __future__ import annotations

import math


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors of equal dimension."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def rank_by_similarity(
    query: str,
    items: list[tuple[str, str]],
    *,
    top_k: int | None = None,
    min_score: float = 0.0,
) -> list[tuple[str, str, float]]:
    """Rank *items* by semantic similarity to *query*.

    Args:
        query: User query text.
        items: List of ``(name, description)`` tuples.
        top_k: Max items to return (``None`` = all).
        min_score: Minimum similarity score (0.0–1.0) to include.

    Returns:
        ``[(name, description, similarity_score), ...]`` sorted desc.
        Falls back to returning all items with score 1.0 when embedding
        model is unavailable or *query* is empty.
    """
    if not query or not items:
        return [(name, desc, 1.0) for name, desc in items]

    from utils.embedding import get_embedding_model

    model = get_embedding_model()
    if not model.available:
        return [(name, desc, 1.0) for name, desc in items]

    names = [name for name, _ in items]
    descriptions = [desc for _, desc in items]

    all_embeddings = model.encode([query] + descriptions)
    if all_embeddings is None:
        return [(name, desc, 1.0) for name, desc in items]

    query_emb = all_embeddings[0]
    item_embs = all_embeddings[1:]

    scored: list[tuple[str, str, float]] = []
    for i, (name, desc) in enumerate(items):
        score = cosine_similarity(query_emb, item_embs[i])
        if score >= min_score:
            scored.append((name, desc, score))

    scored.sort(key=lambda x: x[2], reverse=True)

    if top_k is not None and top_k > 0:
        scored = scored[:top_k]

    return scored
