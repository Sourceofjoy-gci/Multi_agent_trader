"""Deterministic magic-number derivation inside a book's signed range.

Determinism is the point: after a crash, an intent's magic can be recomputed
with no persisted mapping to lose. Sequential allocation would require
persisting that mapping, which is one more thing that can go missing at exactly
the wrong moment.

Magic is a book-and-intent LOCATOR, not an identity. A 10,000-wide range
collides by birthday after roughly 120 concurrent intents, so recovery scopes
queries by magic AND time window, with the intent ledger authoritative.
"""

import hashlib

_DIGEST_HEX_CHARS = 8


def derive_magic(intent_id: str, magic_range: tuple[int, int]) -> int:
    """Map an intent id into the book's magic range, always identically."""

    start, end = magic_range
    if start > end:
        raise ValueError("magic_range must be ascending")
    span = end - start + 1
    digest = hashlib.sha256(intent_id.encode("utf-8")).hexdigest()[:_DIGEST_HEX_CHARS]
    return start + (int(digest, 16) % span)
