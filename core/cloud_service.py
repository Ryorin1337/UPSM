"""
Phigros Cloud Service Module
Communicates with LeanCloud and Qiniu Cloud storage for Phigros cloud saves.
Zero external dependencies (uses standard Python urllib.request, json, hashlib, base64, struct).
"""
import os
import json
import base64
import struct
import hashlib
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
import urllib.request
import urllib.error

# LeanCloud & Phigros Cloud endpoints
URL_PHIGROS_SERVICE = "https://rak3ffdi.cloud.tds1.tapapis.cn/1.1"
URL_UPLOAD = "https://upload.qiniup.com/buckets/rAK3Ffdi/objects"
LC_ID = "rAK3FfdieFob2Nn8Am"
LC_KEY = "Qr9AEqtuoSVS3zeD6iVbM4ZC0AtkJcQ89tywVyi0"
USER_AGENT = "LeanCloud-CSharp-SDK/1.0.3"


def extract_session_token(raw_input: Any) -> Optional[str]:
    """
    Intelligently extracts sessionToken whether the user passes:
    1. A raw token string (e.g. 25-50 alphanumeric characters)
    2. A full .userdata JSON string or dictionary
    3. A file path pointing to .userdata
    4. A .env line (PHIGROS_TOKEN=...)
    """
    if not raw_input:
        return None

    if isinstance(raw_input, dict):
        tok = raw_input.get("sessionToken") or raw_input.get("token")
        if tok and isinstance(tok, str):
            return tok.strip()
        return None

    if not isinstance(raw_input, str):
        try:
            raw_input = str(raw_input)
        except Exception:
            return None

    text = raw_input.lstrip("\ufeff").strip()

    # Case 0: File path check
    if os.path.exists(text) and os.path.isfile(text):
        try:
            with open(text, "r", encoding="utf-8-sig") as f:
                text = f.read().strip()
        except Exception:
            pass

    # Case 1: JSON format (e.g. pasted .userdata content)
    if "sessionToken" in text or (text.startswith("{") and text.endswith("}")):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                token = data.get("sessionToken") or data.get("token")
                if token and isinstance(token, str):
                    return token.strip()
        except Exception:
            pass

    # Case 2: Regex extraction anywhere in text
    import re
    m = re.search(r'["\']?sessionToken["\']?\s*[:=]\s*["\']?([a-zA-Z0-9_\-]+)["\']?', text)
    if m:
        candidate = m.group(1).strip()
        if 16 <= len(candidate) <= 64:
            return candidate

    # Case 3: .env format (PHIGROS_TOKEN=...)
    for line in text.splitlines():
        line = line.strip()
        if "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            if k in ("PHIGROS_TOKEN", "sessionToken", "TOKEN"):
                if len(v) >= 16:
                    return v

    # Case 4: Raw token string
    cleaned = text.strip("'\" \t\r\n")
    if 16 <= len(cleaned) <= 64 and re.match(r'^[a-zA-Z0-9_\-]+$', cleaned):
        return cleaned

    return None


def decode_varshort(data: bytes, offset: int) -> Tuple[int, int]:
    """Reads varshort from data at offset. Returns (value, new_offset)."""
    if offset >= len(data):
        return 0, offset
    b0 = data[offset]
    offset += 1
    if b0 & 0x80:
        if offset >= len(data):
            return b0 & 0x7F, offset
        b1 = data[offset]
        offset += 1
        val = (b0 & 0x7F) + (b1 << 7)
        return val, offset
    return b0, offset


def encode_varshort(val: int) -> bytes:
    """Encodes int into varshort bytes."""
    val = max(0, int(val))
    if val < 128:
        return bytes([val])
    else:
        return bytes([(val & 0x7F) | 0x80, (val >> 7) & 0xFF])


def decode_phi_string(data: bytes, offset: int) -> Tuple[str, int]:
    """Reads varshort length-prefixed UTF-8 string."""
    length, offset = decode_varshort(data, offset)
    if length <= 0 or offset >= len(data):
        return "", offset
    end = min(len(data), offset + length)
    s = data[offset:end].decode("utf-8", errors="replace")
    return s, end


def encode_phi_string(s: str) -> bytes:
    """Encodes string to varshort length-prefixed UTF-8."""
    encoded = s.encode("utf-8")
    return encode_varshort(len(encoded)) + encoded


def parse_summary_bytes(buff: bytes) -> Dict[str, Any]:
    """
    Parses unencrypted binary summary buffer from Cloud Save.
    """
    if not buff or len(buff) < 8:
        return {
            "save_version": 0,
            "challenge_rank": 0,
            "rank_level": 0,
            "rank_num": 0,
            "ranking_score": 0.0,
            "game_version": 0,
            "avatar": "",
            "level_summary": []
        }

    offset = 0
    save_version = buff[offset]
    offset += 1

    challenge_rank = struct.unpack_from("<h", buff, offset)[0]
    offset += 2

    ranking_score = struct.unpack_from("<f", buff, offset)[0]
    offset += 4

    game_version = buff[offset]
    offset += 1

    unknown_v7 = 0
    if save_version >= 7 and offset < len(buff):
        unknown_v7 = buff[offset]
        offset += 1

    avatar, offset = decode_phi_string(buff, offset)

    # 4 difficulty levels: EZ, HD, IN, AT
    levels = []
    diff_names = ["EZ", "HD", "IN", "AT"]
    for i in range(4):
        if offset + 6 <= len(buff):
            cleared, fc, phi = struct.unpack_from("<hhh", buff, offset)
            offset += 6
            levels.append({
                "difficulty": diff_names[i],
                "cleared": cleared,
                "fc": fc,
                "phi": phi
            })

    rank_level = challenge_rank // 100
    rank_num = challenge_rank % 100

    rank_colors = {
        5: "彩色 (Rainbow)",
        4: "金色 (Gold)",
        3: "橙色 (Orange)",
        2: "蓝色 (Blue)",
        1: "绿色 (Green)"
    }

    return {
        "save_version": save_version,
        "challenge_rank": challenge_rank,
        "rank_level": rank_level,
        "rank_num": rank_num,
        "rank_color_text": rank_colors.get(rank_level, f"等级 {rank_level}"),
        "ranking_score": round(ranking_score, 4),
        "game_version": game_version,
        "unknown_v7": unknown_v7,
        "avatar": avatar,
        "level_summary": levels
    }


def build_summary_bytes(summary_dict: Dict[str, Any]) -> bytes:
    """
    Encodes summary dictionary into unencrypted binary buffer.
    """
    save_version = summary_dict.get("save_version", 7)
    challenge_rank = summary_dict.get("challenge_rank", 0)
    ranking_score = float(summary_dict.get("ranking_score", 0.0))
    game_version = summary_dict.get("game_version", 1)
    unknown_v7 = summary_dict.get("unknown_v7", 0)
    avatar = summary_dict.get("avatar", "Introduction")

    out = bytearray()
    out.append(save_version)
    out.extend(struct.pack("<h", challenge_rank))
    out.extend(struct.pack("<f", ranking_score))
    out.append(game_version)
    if save_version >= 7:
        out.append(unknown_v7)

    out.extend(encode_phi_string(avatar))

    levels = summary_dict.get("level_summary", [])
    for i in range(4):
        if i < len(levels):
            c = levels[i].get("cleared", 0)
            fc = levels[i].get("fc", 0)
            phi = levels[i].get("phi", 0)
        else:
            c, fc, phi = 0, 0, 0
        out.extend(struct.pack("<hhh", c, fc, phi))

    return bytes(out)


class PhigrosCloudClient:
    """
    LeanCloud API Client for Phigros Cloud Saves.
    """
    def __init__(self, session_token: str):
        self.session_token = session_token.strip()
        self.headers = {
            "X-LC-Id": LC_ID,
            "X-LC-Key": LC_KEY,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "X-LC-Session": self.session_token
        }

    def _request(self, method: str, url: str, data: Optional[bytes] = None, headers: Optional[dict] = None) -> bytes:
        req_headers = dict(self.headers)
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LeanCloud API Error [{e.code}]: {err_body}")
        except Exception as e:
            raise RuntimeError(f"Network request failed: {e}")

    def get_current_user(self) -> Dict[str, Any]:
        """Fetches /users/me to verify token and obtain user profile."""
        url = f"{URL_PHIGROS_SERVICE}/users/me"
        body = self._request("GET", url)
        return json.loads(body.decode("utf-8"))

    def read_remote_saves(self) -> List[Dict[str, Any]]:
        """
        Fetches /classes/_GameSave and filters profiles for the current user,
        sorted by updatedAt descending.
        """
        me = self.get_current_user()
        my_id = me.get("objectId")

        url = f"{URL_PHIGROS_SERVICE}/classes/_GameSave"
        body = self._request("GET", url)
        deser = json.loads(body.decode("utf-8"))
        results = deser.get("results", [])

        # Filter by current user
        my_saves = []
        for p in results:
            u = p.get("user", {})
            uid = u.get("objectId") if isinstance(u, dict) else u
            if not my_id or uid == my_id or not uid:
                # Parse summary
                summary_b64 = p.get("summary", "")
                parsed_summary = {}
                if summary_b64:
                    try:
                        raw_summary = base64.b64decode(summary_b64)
                        parsed_summary = parse_summary_bytes(raw_summary)
                    except Exception:
                        pass
                p["parsed_summary"] = parsed_summary
                my_saves.append(p)

        if not my_saves and results:
            for p in results:
                summary_b64 = p.get("summary", "")
                parsed_summary = {}
                if summary_b64:
                    try:
                        raw_summary = base64.b64decode(summary_b64)
                        parsed_summary = parse_summary_bytes(raw_summary)
                    except Exception:
                        pass
                p["parsed_summary"] = parsed_summary
                my_saves.append(p)

        my_saves.sort(key=lambda s: s.get("updatedAt", ""), reverse=True)
        return my_saves

    def download_save_bytes(self, file_url: str) -> bytes:
        """Downloads the raw .save ZIP bytes from CDN."""
        req = urllib.request.Request(file_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()

    def upload_save(self, save_bytes: bytes, summary_bytes: bytes, target_profile: Dict[str, Any]) -> Dict[str, Any]:
        """
        Full 5-step upload protocol to LeanCloud & Qiniu:
        1. POST /fileTokens
        2. POST /uploads & PUT /uploads/{id}/1 to Qiniu
        3. POST /fileCallback
        4. PUT /classes/_GameSave/{id}
        5. DELETE /files/{old_file_id}
        """
        user_obj_id = target_profile.get("user", {}).get("objectId")
        save_obj_id = target_profile.get("objectId")
        old_file_id = target_profile.get("gameFile", {}).get("objectId")

        if not user_obj_id or not save_obj_id:
            raise ValueError("Target profile missing objectId or user.objectId")

        # Step 1: Create File Token
        md5_hash = hashlib.md5(save_bytes).hexdigest()
        file_token_payload = {
            "name": ".save",
            "__type": "File",
            "ACL": {user_obj_id: {"read": True, "write": True}},
            "prefix": "gamesaves",
            "metaData": {
                "size": len(save_bytes),
                "_checksum": md5_hash,
                "prefix": "gamesaves"
            }
        }
        res_step1 = self._request(
            "POST",
            f"{URL_PHIGROS_SERVICE}/fileTokens",
            data=json.dumps(file_token_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        token_info = json.loads(res_step1.decode("utf-8"))
        token_key = token_info["key"]
        up_token = token_info["token"]
        new_file_obj_id = token_info["objectId"]

        key_b64 = base64.b64encode(token_key.encode("utf-8")).decode("utf-8")

        # Step 2a: Create Qiniu Upload
        res_step2a = self._request(
            "POST",
            f"{URL_UPLOAD}/{key_b64}/uploads",
            headers={"Authorization": f"UpToken {up_token}"}
        )
        upload_info = json.loads(res_step2a.decode("utf-8"))
        upload_id = upload_info["uploadId"]

        # Step 2b: Upload Part 1
        res_step2b = self._request(
            "PUT",
            f"{URL_UPLOAD}/{key_b64}/uploads/{upload_id}/1",
            data=save_bytes,
            headers={
                "Authorization": f"UpToken {up_token}",
                "Content-Type": "application/octet-stream"
            }
        )
        part_info = json.loads(res_step2b.decode("utf-8"))

        # Step 2c: Complete Qiniu Upload
        complete_payload = {"parts": [{"partNumber": 1, "etag": part_info["etag"]}]}
        self._request(
            "POST",
            f"{URL_UPLOAD}/{key_b64}/uploads/{upload_id}",
            data=json.dumps(complete_payload).encode("utf-8"),
            headers={
                "Authorization": f"UpToken {up_token}",
                "Content-Type": "application/json"
            }
        )

        # Step 3: Callback to LeanCloud
        callback_payload = {"result": True, "token": key_b64}
        self._request(
            "POST",
            f"{URL_PHIGROS_SERVICE}/fileCallback",
            data=json.dumps(callback_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )

        # Step 4: Update GameSave Record in LeanCloud
        summary_b64 = base64.b64encode(summary_bytes).decode("utf-8")
        now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
        update_payload = {
            "summary": summary_b64,
            "modifiedAt": {"__type": "Date", "iso": now_iso},
            "gameFile": {"__type": "Pointer", "className": "_File", "objectId": new_file_obj_id},
            "ACL": {user_obj_id: {"read": True, "write": True}},
            "user": {"__type": "Pointer", "className": "_User", "objectId": user_obj_id}
        }
        self._request(
            "PUT",
            f"{URL_PHIGROS_SERVICE}/classes/_GameSave/{save_obj_id}",
            data=json.dumps(update_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )

        # Step 5: Delete Old File (Best Effort)
        if old_file_id:
            try:
                self._request("DELETE", f"{URL_PHIGROS_SERVICE}/files/{old_file_id}")
            except Exception:
                pass

        return {
            "status": "ok",
            "message": "云存档上传成功！请进入游戏点击【云存档 -> 从云端同步】下载生效。",
            "file_object_id": new_file_obj_id,
            "updated_at": now_iso
        }
