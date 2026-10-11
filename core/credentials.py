"""
Local credential storage.

Replaces the old "scan the working directory for a .userdata file" behaviour.
The program now owns its credential: a sessionToken obtained by QR login (or
pasted by the user) is saved here and reused on the next start.

The file lives beside the song catalog and is gitignored, because a
sessionToken grants full access to the player's cloud save.
"""
import os
import json
import stat
from typing import Any, Dict, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREDENTIALS_PATH = os.path.join(BASE_DIR, "data", "credentials.json")


def load_credentials() -> Dict[str, Any]:
    """Reads the stored credential, or {} when there is none."""
    if not os.path.exists(CREDENTIALS_PATH):
        return {}
    try:
        with open(CREDENTIALS_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        # A corrupt file should not stop the program from starting.
        return {}


def load_session_token() -> Optional[str]:
    """The saved sessionToken, or None."""
    token = load_credentials().get("sessionToken")
    return token.strip() if isinstance(token, str) and token.strip() else None


def save_session_token(token: str, user: Optional[Dict[str, Any]] = None) -> None:
    """
    Persists a sessionToken plus a little context for the UI.

    Only display fields are kept from `user`; the rest of the LeanCloud
    record is re-fetched on demand rather than cached.
    """
    if not token or not token.strip():
        raise ValueError("sessionToken 为空，无法保存")

    payload: Dict[str, Any] = {"sessionToken": token.strip()}
    if isinstance(user, dict):
        payload["nickname"] = user.get("nickname") or user.get("username") or ""
        payload["objectId"] = user.get("objectId") or ""

    os.makedirs(os.path.dirname(CREDENTIALS_PATH), exist_ok=True)
    with open(CREDENTIALS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # Restrict to the owner where the platform supports it.
    try:
        os.chmod(CREDENTIALS_PATH, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def clear_credentials() -> bool:
    """Deletes the stored credential. Returns True if a file was removed."""
    if not os.path.exists(CREDENTIALS_PATH):
        return False
    try:
        os.remove(CREDENTIALS_PATH)
        return True
    except Exception:
        return False


def has_credentials() -> bool:
    return load_session_token() is not None
