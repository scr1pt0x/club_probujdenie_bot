def split_message(text: str, limit: int = 4000) -> list[str]:
    """Split Telegram text on line boundaries whenever possible."""
    if limit < 1:
        raise ValueError("limit must be positive")
    if not text:
        return [""]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at < limit // 2:
            split_at = limit
        chunk = remaining[:split_at].rstrip()
        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip("\n")
    if remaining or not chunks:
        chunks.append(remaining)
    return chunks
