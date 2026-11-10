"""Local OCR and conservative detection for gambling-promotion images."""
from __future__ import annotations

import re
import threading

_ENGINE = None
_ENGINE_LOCK = threading.Lock()
_INFERENCE_LOCK = threading.Lock()

_GAMBLING_TERMS = re.compile(
    r"\b(?:casino|slots?|sportsbook|betting|wager(?:ing)?|roulette|blackjack|"
    r"jackpot|baccarat|poker|gambling|usdt|tether)\b",
    re.IGNORECASE,
)
_PROMOTION_TERMS = re.compile(
    r"\b(?:bonuses?|promo(?:tion)?|activate|deposit|withdraw|claim|"
    r"free\s+spins?|welcome\s+offer|play\s+now|promo\s+code|"
    r"win\s+(?:real\s+)?money)\b",
    re.IGNORECASE,
)
_GAME_TERMS = re.compile(r"\b(?:games?|gaming|play\s*to\s*earn)\b", re.IGNORECASE)
_BONUS_TERMS = re.compile(r"\bbonuses?\b", re.IGNORECASE)
_ACTIVATION_TERMS = re.compile(
    r"\b(?:activate|activation|promo\s+code|bonus\s+code)\b", re.IGNORECASE
)
_ACTIVATION_CODE_TERMS = re.compile(
    r"\b(?:activate|activation)\s+code\b", re.IGNORECASE
)
_PAYOUT_TERMS = re.compile(
    r"\b(?:deposit|withdraw(?:al)?|usdt|tether|bank\s+card|wallet)\b",
    re.IGNORECASE,
)
_STRONG_PAYOUT_TERMS = re.compile(
    r"\b(?:withdraw(?:al)?|usdt|tether|crypto|wallet)\b", re.IGNORECASE
)


def looks_like_gambling_promotion(text: str) -> bool:
    """Match casino offers or game ads combining bonus activation and payouts."""
    if _GAMBLING_TERMS.search(text) and _PROMOTION_TERMS.search(text):
        return True
    if (
        _GAME_TERMS.search(text)
        and _BONUS_TERMS.search(text)
        and _ACTIVATION_TERMS.search(text)
        and _PAYOUT_TERMS.search(text)
    ):
        return True
    return bool(
        _BONUS_TERMS.search(text)
        and _ACTIVATION_CODE_TERMS.search(text)
        and _STRONG_PAYOUT_TERMS.search(text)
    )


def _get_engine():
    global _ENGINE
    from rapidocr_onnxruntime import RapidOCR

    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = RapidOCR(
                use_cls=False,
                intra_op_num_threads=2,
                inter_op_num_threads=1,
                max_side_len=1600,
            )
        return _ENGINE


def warmup() -> None:
    """Load OCR models before the first image arrives."""
    _get_engine()


def extract_image_text(image_bytes: bytes) -> str:
    """Read text from an image using the local OCR engine."""
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return ""

    with _INFERENCE_LOCK:
        result, _ = _get_engine()(image)
    return "\n".join(str(line[1]) for line in (result or []) if len(line) > 1)
