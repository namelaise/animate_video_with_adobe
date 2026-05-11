# caption_utils.py
# Helpers partagés pour gérer les captions TikTok des vidéos pending_posts.
#
# Usage GUI / scheduler :
#   from caption_utils import load_or_generate_caption
#   caption = load_or_generate_caption(pending_dir)

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEFAULT_CAPTION = "Quand Mr Martin appelle, ça part toujours en vrille 😅 #mrmartin #canular"


def _ensure_hashtags(text: str) -> str:
    text = (text or "").strip().strip('"').strip("'")
    if not text:
        return DEFAULT_CAPTION
    low = text.lower()
    if "#mrmartin" not in low:
        text += " #mrmartin"
    if "#canular" not in low:
        text += " #canular"
    return text


def generate_generic_caption() -> str:
    """
    Génère une caption générique sans transcript (pour les vidéos pending_posts
    dont la transcription n'est plus disponible). Fallback statique si pas d'API key.
    """
    if not os.getenv("OPENAI_API_KEY"):
        return DEFAULT_CAPTION

    try:
        from openai import OpenAI
        client = OpenAI()
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Tu es un community manager TikTok specialise dans les videos de canulars "
                        "telephoniques de Mr Martin. Genere une description TikTok COURTE et generique : "
                        "- 1 a 2 phrases accrocheuses sans reference specifique au contenu. "
                        "- Termine TOUJOURS par #mrmartin #canular. "
                        "- Pas de guillemets, pas d'introduction. Reponds uniquement avec la description."
                    ),
                },
                {"role": "user", "content": "Genere une description generique pour un canular telephonique."},
            ],
            max_tokens=120,
            temperature=0.9,
        )
        text = (resp.choices[0].message.content or "").strip()
        return _ensure_hashtags(text)
    except Exception:
        return DEFAULT_CAPTION


def load_or_generate_caption(pending_dir: Path) -> str:
    """
    Lit pending_dir/caption.txt si présent, sinon génère une caption générique
    et la sauvegarde pour les futurs retries.
    """
    pending_dir = Path(pending_dir)
    caption_path = pending_dir / "caption.txt"

    if caption_path.exists():
        try:
            existing = caption_path.read_text(encoding="utf-8").strip()
            if existing:
                return _ensure_hashtags(existing)
        except Exception:
            pass

    caption = generate_generic_caption()
    try:
        pending_dir.mkdir(parents=True, exist_ok=True)
        caption_path.write_text(caption, encoding="utf-8")
    except Exception:
        pass
    return caption
