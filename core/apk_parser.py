"""
Phigros APK Asset Parser Module
Extracts song definitions, difficulties, collections, illustrations, and avatars directly from Phigros APK.
"""
import os
import sys
import json
import zipfile
from io import BytesIO
from typing import Dict, Any, List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DATA_FILE = os.path.join(DATA_DIR, "phigros_data.json")
TYPETREE_FILE = os.path.join(DATA_DIR, "typetree.json")


def load_database() -> Dict[str, Any]:
    """
    Loads local cached Phigros game database (data/phigros_data.json).
    Returns empty structure if not found.
    """
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    data = json.loads(content)
                    if isinstance(data, dict) and data.get("total_songs", 0) > 0:
                        return data
        except Exception as e:
            print(f"[APK Parser] Failed to load {DATA_FILE}: {e}")

    return {
        "version": "未导入 APK",
        "total_songs": 0,
        "songs": {},
        "collections": [],
        "single_keys": [],
        "illustration_keys": [],
        "avatars": []
    }


def save_database(data: Dict[str, Any]) -> str:
    """
    Saves parsed database to data/phigros_data.json.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return DATA_FILE


def clear_database() -> bool:
    """
    Clears local cached Phigros game database (data/phigros_data.json).
    Returns True if successfully deleted or already absent.
    """
    if os.path.exists(DATA_FILE):
        try:
            os.remove(DATA_FILE)
            return True
        except Exception as e:
            print(f"[APK Parser] Failed to delete {DATA_FILE}: {e}")
            return False
    return True


def parse_apk(apk_path_or_bytes) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """
    Parses a Phigros APK file and extracts songs, collections, illustrations, and avatars.
    Returns (full_database, stats_dict).
    """
    try:
        from UnityPy import Environment
    except ImportError:
        raise RuntimeError("UnityPy is not installed. Please run 'pip install UnityPy' to parse APK files.")

    # Load typetree
    typetree = {}
    if os.path.exists(TYPETREE_FILE):
        with open(TYPETREE_FILE, "r", encoding="utf-8") as f:
            typetree = json.load(f)

    env = Environment()

    if isinstance(apk_path_or_bytes, (str, os.PathLike)):
        apk_zip = zipfile.ZipFile(apk_path_or_bytes, "r")
    elif isinstance(apk_path_or_bytes, (bytes, bytearray)):
        apk_zip = zipfile.ZipFile(BytesIO(apk_path_or_bytes), "r")
    else:
        apk_zip = zipfile.ZipFile(apk_path_or_bytes, "r")

    loaded_assets = False
    try:
        if "assets/bin/Data/data.unity3d" in apk_zip.namelist():
            with apk_zip.open("assets/bin/Data/data.unity3d") as f:
                env.load_file(BytesIO(f.read()), name="assets/bin/Data/data.unity3d")
                loaded_assets = True
        else:
            # Fallback to loose assets
            for name in ["assets/bin/Data/globalgamemanagers.assets", "assets/bin/Data/level0"]:
                if name in apk_zip.namelist():
                    with apk_zip.open(name) as f:
                        env.load_file(BytesIO(f.read()), name=name)
                        loaded_assets = True

        # Check addressables or other assets if not loaded
        if not loaded_assets:
            for n in apk_zip.namelist():
                if n.startswith("assets/bin/Data/") and (n.endswith(".assets") or n.endswith(".unity3d")):
                    with apk_zip.open(n) as f:
                        env.load_file(BytesIO(f.read()), name=n)
                        loaded_assets = True
    finally:
        apk_zip.close()

    if not loaded_assets:
        raise ValueError("Could not find Unity data assets (assets/bin/Data/data.unity3d or .assets) in the provided APK.")

    GameInformation = None
    Collections = None
    Tips = None

    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue

        script_name = None
        # Method 1: parse_monobehaviour_head (UnityPy 1.25+)
        if hasattr(obj, "parse_monobehaviour_head"):
            try:
                head = obj.parse_monobehaviour_head()
                if head and hasattr(head, "m_Script") and head.m_Script:
                    s = head.m_Script.read()
                    if s:
                        script_name = getattr(s, "m_Name", None) or getattr(s, "m_ClassName", None) or getattr(s, "name", None)
            except Exception:
                pass

        # Method 2: classic obj.read() (older UnityPy)
        if not script_name:
            try:
                data = obj.read()
                script = getattr(data, "m_Script", None)
                if script:
                    if hasattr(script, "get_obj"):
                        s = script.get_obj()
                        if s:
                            script_name = getattr(s.read(), "name", None) or getattr(s.read(), "m_Name", None)
                    elif hasattr(script, "read"):
                        s = script.read()
                        if s:
                            script_name = getattr(s, "m_Name", None) or getattr(s, "m_ClassName", None) or getattr(s, "name", None)
            except Exception:
                pass

        if not script_name:
            continue

        if script_name == "GameInformation":
            if "GameInformation" in typetree:
                GameInformation = obj.read_typetree(typetree["GameInformation"])
            else:
                GameInformation = obj.read_typetree()
        elif script_name == "GetCollectionControl":
            if "GetCollectionControl" in typetree:
                Collections = obj.read_typetree(typetree["GetCollectionControl"])
            else:
                Collections = obj.read_typetree()
        elif script_name == "TipsProvider":
            if "TipsProvider" in typetree:
                Tips = obj.read_typetree(typetree["TipsProvider"])
            else:
                Tips = obj.read_typetree()

    if not GameInformation:
        raise ValueError("Could not extract GameInformation MonoBehaviour from APK assets.")

    # 1. Parse Songs
    extracted_songs = {}
    diff_names = ["EZ", "HD", "IN", "AT"]

    song_groups = GameInformation.get("song", {})
    for group_key, song_list in song_groups.items():
        if group_key == "otherSongs":
            continue
        for s in song_list:
            raw_id = s.get("songsId", "")
            # Phigros internal ID often ends in .0, .1 etc.
            sid = raw_id[:-2] if raw_id.endswith(".0") else raw_id
            name = s.get("songsName", "")
            composer = s.get("composer", "")
            illustrator = s.get("illustrator", "")
            diff_list = s.get("difficulty", [])
            charter_list = s.get("charter", [])

            # Clean diffs
            if len(diff_list) == 5:
                diff_list.pop()
            if diff_list and diff_list[-1] == 0.0:
                diff_list.pop()
                if charter_list:
                    charter_list.pop()

            d_map = {}
            for i in range(min(len(diff_list), len(diff_names))):
                d_map[diff_names[i]] = round(float(diff_list[i]), 1)

            c_map = {}
            for i in range(min(len(charter_list), len(diff_names))):
                c_map[diff_names[i]] = str(charter_list[i])

            avail_diffs = list(d_map.keys()) if d_map else ["EZ", "HD", "IN"]

            extracted_songs[sid] = {
                "id": sid,
                "songs_id": raw_id,
                "title": name,
                "artist": composer,
                "illustrator": illustrator,
                "difficulties": d_map,
                "charters": c_map,
                "available_diffs": avail_diffs
            }

    # 2. Parse Single & Illustration Keys from keyStore
    single_keys = []
    illustration_keys = []
    for key_item in GameInformation.get("keyStore", []):
        kind = key_item.get("kindOfKey", -1)
        kname = key_item.get("keyName", "")
        if not kname:
            continue
        if kind == 0:
            single_keys.append(kname)
        elif kind == 2 and kname != "Introduction" and kname not in single_keys:
            illustration_keys.append(kname)

    # 3. Parse Collections & Avatars
    collections = []
    avatars = []
    if Collections:
        col_items = Collections.get("collectionItems", []) if isinstance(Collections, dict) else getattr(Collections, "collectionItems", [])
        for item in col_items:
            if isinstance(item, dict):
                k = item.get("key")
                sub_idx = item.get("subIndex", 0)
                mlt = item.get("multiLanguageTitle", {})
                title = mlt.get("chinese", "") if isinstance(mlt, dict) else ""
            else:
                k = getattr(item, "key", None)
                sub_idx = getattr(item, "subIndex", 0)
                mlt = getattr(item, "multiLanguageTitle", None)
                title = getattr(mlt, "chinese", "") if mlt else ""
            if k:
                collections.append({
                    "key": k,
                    "title": title or k,
                    "sub_index": int(sub_idx)
                })

        avatar_items = Collections.get("avatars", []) if isinstance(Collections, dict) else getattr(Collections, "avatars", [])
        for av in avatar_items:
            av_name = av.get("name", "") if isinstance(av, dict) else getattr(av, "name", "")
            if av_name:
                avatars.append(av_name)

    # Existing DB merge
    existing_db = load_database()
    merged_songs = existing_db.get("songs", {})
    merged_songs.update(extracted_songs)

    merged_single = list(set(existing_db.get("single_keys", []) + single_keys))
    merged_illust = list(set(existing_db.get("illustration_keys", []) + illustration_keys))
    merged_avatars = list(set(existing_db.get("avatars", []) + avatars))

    # Collections merge by key
    col_dict = {c["key"]: c for c in existing_db.get("collections", [])}
    for c in collections:
        col_dict[c["key"]] = c
    merged_collections = list(col_dict.values())

    full_db = {
        "version": "Phigros APK Extracted",
        "total_songs": len(merged_songs),
        "songs": merged_songs,
        "collections": merged_collections,
        "single_keys": merged_single,
        "illustration_keys": merged_illust,
        "avatars": merged_avatars
    }

    save_database(full_db)

    stats = {
        "songs": len(extracted_songs),
        "total_songs": len(merged_songs),
        "collections": len(merged_collections),
        "single_keys": len(merged_single),
        "illustration_keys": len(merged_illust),
        "avatars": len(merged_avatars)
    }

    return full_db, stats
