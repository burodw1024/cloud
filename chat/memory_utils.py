from .models import ChatMessage

def get_short_memory(session, limit=10):
    messages = (
        ChatMessage.objects
        .filter(session=session)
        .order_by("-created_at")[:limit]
    )

    # Reverse to chronological order
    return list(reversed([
        {"role": m.role, "content": m.content}
        for m in messages
    ]))
