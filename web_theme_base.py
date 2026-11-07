"""web_theme_base.py - tiny helpers shared by every web_* module (kept separate to avoid import loops)."""
import html


def q(text) -> str:
    """Escape text for HTML text AND attribute values."""
    return html.escape(str(text if text is not None else ""), quote=True)
