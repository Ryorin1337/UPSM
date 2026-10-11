"""
Phigros Song Helper Module
Handles rating calculations, song ID parsing, difficulty definitions, and presets.
"""
from typing import Tuple, Dict, Any, Optional

DIFFICULTIES = ["EZ", "HD", "IN", "AT", "Legacy"]

RATING_PRESETS = {
    "phi": {
        "label": "φ (AP)",
        "score": 1000000,
        "acc": 100.0,
        "fc": True,
        "c": 1,
        "color": "#ffd700",
        "badge": "phi"
    },
    "v_fc": {
        "label": "V (FC)",
        "score": 985000,
        "acc": 98.5,
        "fc": True,
        "c": 1,
        "color": "#00e5ff",
        "badge": "v_fc"
    },
    "v": {
        "label": "V",
        "score": 965000,
        "acc": 96.5,
        "fc": False,
        "c": 0,
        "color": "#2979ff",
        "badge": "v"
    },
    "s": {
        "label": "S",
        "score": 940000,
        "acc": 94.0,
        "fc": False,
        "c": 0,
        "color": "#00e676",
        "badge": "s"
    },
    "a": {
        "label": "A",
        "score": 900000,
        "acc": 90.0,
        "fc": False,
        "c": 0,
        "color": "#7c4dff",
        "badge": "a"
    },
    "b": {
        "label": "B",
        "score": 850000,
        "acc": 85.0,
        "fc": False,
        "c": 0,
        "color": "#ff9100",
        "badge": "b"
    },
    "c": {
        "label": "C",
        "score": 750000,
        "acc": 75.0,
        "fc": False,
        "c": 0,
        "color": "#ff5252",
        "badge": "c"
    },
    "f": {
        "label": "False",
        "score": 500000,
        "acc": 50.0,
        "fc": False,
        "c": 0,
        "color": "#78909c",
        "badge": "f"
    }
}


def calculate_rating(score: int, fc: bool) -> str:
    """
    Computes standard Phigros rating given score and full combo status.
    """
    if score >= 1000000:
        return "φ"
    elif score >= 960000:
        return "V (FC)" if fc else "V"
    elif score >= 920000:
        return "S (FC)" if fc else "S"
    elif score >= 880000:
        return "A (FC)" if fc else "A"
    elif score >= 820000:
        return "B (FC)" if fc else "B"
    elif score >= 700000:
        return "C (FC)" if fc else "C"
    else:
        return "False"


def get_rating_style(rating_str: str) -> Dict[str, str]:
    """
    Returns visual badge attributes for rating display.
    """
    base = rating_str.split()[0]
    is_fc = "(FC)" in rating_str
    
    if base == "φ":
        return {"color": "#ffd700", "bg": "rgba(255, 215, 0, 0.15)", "border": "#ffd700", "glow": "0 0 10px rgba(255, 215, 0, 0.6)"}
    elif base == "V":
        if is_fc:
            return {"color": "#00e5ff", "bg": "rgba(0, 229, 255, 0.15)", "border": "#00e5ff", "glow": "0 0 8px rgba(0, 229, 255, 0.5)"}
        return {"color": "#2979ff", "bg": "rgba(41, 121, 255, 0.15)", "border": "#2979ff", "glow": "none"}
    elif base == "S":
        return {"color": "#00e676", "bg": "rgba(0, 230, 118, 0.15)", "border": "#00e676", "glow": "none"}
    elif base == "A":
        return {"color": "#b388ff", "bg": "rgba(179, 136, 255, 0.15)", "border": "#b388ff", "glow": "none"}
    elif base == "B":
        return {"color": "#ffab40", "bg": "rgba(255, 171, 64, 0.15)", "border": "#ffab40", "glow": "none"}
    elif base == "C":
        return {"color": "#ff5252", "bg": "rgba(255, 82, 82, 0.15)", "border": "#ff5252", "glow": "none"}
    elif base == "False":
        return {"color": "#90a4ae", "bg": "rgba(144, 164, 174, 0.15)", "border": "#90a4ae", "glow": "none"}
    else:
        return {"color": "#607d8b", "bg": "rgba(96, 125, 139, 0.1)", "border": "#455a64", "glow": "none"}


def parse_song_id(song_id: str) -> Tuple[str, str]:
    """
    Parses song_id into (readable_title, artist).
    E.g.: 'BetterGraphicAnimation.ルゼ.0' -> ('BetterGraphicAnimation', 'ルゼ')
          '狂喜蘭舞.LeaF.0' -> ('狂喜蘭舞', 'LeaF')
          'Stasis.Maozon.0' -> ('Stasis', 'Maozon')
    """
    parts = song_id.split(".")
    if len(parts) >= 3:
        title = parts[0]
        artist = parts[1]
    elif len(parts) == 2:
        title = parts[0]
        artist = parts[1] if parts[1] != "0" else ""
    else:
        title = song_id
        artist = ""
    return title, artist
