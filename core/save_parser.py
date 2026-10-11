"""
Phigros Save Parser & Manager Module
Handles parsing, modifying, and serializing Phigros PlayerPrefs save data.
"""
import os
import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import unquote, quote
from typing import Dict, Any, List, Optional, Tuple

from core.crypto import aes_decrypt, aes_encrypt
from core.song_helper import DIFFICULTIES, calculate_rating, parse_song_id, RATING_PRESETS
from core.apk_parser import load_database


def single_chart_rks(acc: float, constant: float) -> float:
    """
    Official per-chart RKS: ((acc-55)/45)^2 * constant.

    Scores under 70% acc count for nothing, and a chart whose constant is
    unknown (not in the imported catalog) contributes nothing rather than
    silently scoring as zero-difficulty.
    """
    if acc < 70.0 or constant <= 0.0:
        return 0.0
    return (((acc - 55.0) / 45.0) ** 2) * constant


def acc_required_for_rks(target_rks: float, constant: float) -> Optional[float]:
    """
    The accuracy needed to reach `target_rks` on a chart of `constant`.

    Inverts the RKS formula. Returns None when the chart cannot reach the
    target even at 100% -- i.e. its constant is simply too low.
    """
    if constant <= 0.0 or target_rks <= 0.0:
        return None
    ratio = target_rks / constant
    if ratio > 1.0:
        return None
    acc = 55.0 + 45.0 * (ratio ** 0.5)
    return acc if acc <= 100.0 else None


class PhigrosSave:
    def __init__(self):
        self.raw_items: List[Dict[str, Any]] = []  # Preserve ordered raw entries from XML
        self.data_map: Dict[str, Any] = {}         # key -> item
        self.songs: Dict[str, Dict[str, Any]] = {} # song_id -> {diff -> record}
        self.source_path: Optional[str] = None
        self.modified: bool = False
        # Cloud-save context. Set only when the loaded save is a cloud-format
        # .save archive (pulled from the network or opened from disk); it is
        # what gates the gameProgress editor and the raw .save download.
        self._cloud_parsed: Optional[Dict[str, Any]] = None
        self._cloud_package: Optional[Any] = None
        self._cloud_raw_bytes: Optional[bytes] = None

    def clear_cloud_context(self) -> None:
        """Drops any cloud-save context. Called on every non-.save load."""
        self._cloud_parsed = None
        self._cloud_package = None
        self._cloud_raw_bytes = None

    def unpack_any(self, file_path_or_bytes, filename: str = "") -> str:
        """
        Auto-detects format (XML or JSON) and loads it into memory.
        Returns format description string.
        """
        # An XML/JSON load is never a cloud save; drop any stale context.
        self.clear_cloud_context()

        if isinstance(file_path_or_bytes, (str, os.PathLike)) and os.path.exists(file_path_or_bytes):
            self.source_path = str(file_path_or_bytes)
            with open(file_path_or_bytes, "rb") as f:
                content = f.read()
            name = os.path.basename(str(file_path_or_bytes))
        else:
            content = file_path_or_bytes if isinstance(file_path_or_bytes, (bytes, bytearray)) else file_path_or_bytes.encode("utf-8")
            name = filename

        # 1. Check for JSON
        stripped = content.strip()
        if stripped.startswith(b"{") or name.endswith(".json"):
            self.load_json(content)
            return "Decrypted JSON (.json)"

        # 2. Default to XML
        self.load_xml(content)
        return "Encrypted PlayerPrefs XML (.xml)"

    def load_xml(self, file_path_or_bytes) -> None:
        """
        Loads and decrypts a Phigros playerprefs XML file.
        """
        if isinstance(file_path_or_bytes, (str, os.PathLike)) and os.path.exists(file_path_or_bytes):
            self.source_path = str(file_path_or_bytes)
            tree = ET.parse(file_path_or_bytes)
            root = tree.getroot()
        elif isinstance(file_path_or_bytes, (bytes, bytearray)):
            root = ET.fromstring(file_path_or_bytes)
        elif isinstance(file_path_or_bytes, str):
            root = ET.fromstring(file_path_or_bytes.encode("utf-8"))
        self.raw_items = []
        self.data_map = {}
        self.songs = {}

        for child in root:
            tag = child.tag
            if tag == "string":
                raw_name = child.attrib.get("name", "")
                raw_val = child.text or ""
                dec_name, is_enc_name = aes_decrypt(raw_name)
                if is_enc_name:
                    dec_val, is_enc_val = aes_decrypt(raw_val)
                else:
                    dec_val = unquote(raw_val)
                    is_enc_val = False

                item = {
                    "tag": "string",
                    "name": dec_name,
                    "val": dec_val,
                    "is_enc": is_enc_name,
                    "raw_name": raw_name,
                    "raw_val": raw_val
                }
            elif tag == "int":
                raw_name = child.attrib.get("name", "")
                dec_name = unquote(raw_name)
                val = child.attrib.get("value", "")
                item = {
                    "tag": "int",
                    "name": dec_name,
                    "val": val,
                    "is_enc": False,
                    "raw_name": raw_name,
                    "raw_val": val
                }
            else:
                raw_name = child.attrib.get("name", "")
                dec_name = unquote(raw_name)
                val = child.text or child.attrib.get("value", "")
                item = {
                    "tag": tag,
                    "name": dec_name,
                    "val": val,
                    "is_enc": False,
                    "raw_name": raw_name,
                    "raw_val": val
                }

            self.raw_items.append(item)
            self.data_map[item["name"]] = item

        self._index_songs()
        self.modified = False

    def load_json(self, file_path_or_str) -> None:
        """
        Loads a decrypted JSON save file (e.g. decrypted_save.json or FormatSave output).
        """
        if isinstance(file_path_or_str, (str, os.PathLike)) and os.path.exists(file_path_or_str):
            with open(file_path_or_str, "r", encoding="utf-8-sig") as f:
                raw_data = json.load(f)
            self.source_path = str(file_path_or_str)
        elif isinstance(file_path_or_str, str):
            raw_data = json.loads(file_path_or_str)
        elif isinstance(file_path_or_str, (bytes, bytearray)):
            raw_data = json.loads(file_path_or_str.decode("utf-8-sig"))
        else:
            raise ValueError("Unsupported input type for load_json")

        self.raw_items = []
        self.data_map = {}
        self.songs = {}

        # If json is nested from FormatSave (contains 'record', 'info', 'key' etc.)
        if isinstance(raw_data, dict) and any(k in raw_data for k in ["record", "info", "key"]):
            flat_data = {}
            if "int" in raw_data:
                for k, v in raw_data["int"].items():
                    flat_data[k] = {"val": str(v), "tag": "int"}
            if "record" in raw_data:
                for k, v in raw_data["record"].items():
                    flat_data[k] = {"val": v if isinstance(v, str) else json.dumps(v), "tag": "string"}
            if "key" in raw_data:
                heads = {"single": "0key", "collection": "1key", "illustration": "2key", "avatar": "3key"}
                for hk, head in heads.items():
                    for k, v in raw_data["key"].get(hk, {}).items():
                        flat_data[head + k] = {"val": str(v), "tag": "string"}
            if "open" in raw_data:
                for k, v in raw_data["open"].items():
                    flat_data[k + "CollectionTextOpened"] = {"val": str(v), "tag": "string"}
            if "grade" in raw_data:
                for k, v in raw_data["grade"].items():
                    flat_data[k + "Grade"] = {"val": str(v), "tag": "string"}
            for section in ["info", "lock", "other"]:
                for k, v in raw_data.get(section, {}).items():
                    flat_data[k] = {"val": str(v), "tag": "string"}
            
            for k, meta in flat_data.items():
                item = {
                    "tag": meta.get("tag", "string"),
                    "name": k,
                    "val": meta["val"],
                    "is_enc": meta.get("tag", "string") == "string" and k not in ["unity.player_session_count", "unity.player_sessionid"]
                }
                self.raw_items.append(item)
                self.data_map[k] = item
        else:
            # Flat dictionary format like decrypted_save.json
            for k, v in raw_data.items():
                val_str = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                is_int = k in ["__UNITY_PLAYERPREFS_VERSION__", "Screenmanager%20Resolution%20Width", "Screenmanager%20Resolution%20Height", "NeedUpdateSongsData", "setDspBuffer"]
                item = {
                    "tag": "int" if is_int else "string",
                    "name": k,
                    "val": val_str,
                    "is_enc": not is_int and k not in ["unity.player_session_count", "unity.player_sessionid"]
                }
                self.raw_items.append(item)
                self.data_map[k] = item

        self._index_songs()
        self.modified = True

    def _index_songs(self) -> None:
        """
        Parses all .Record. entries and builds self.songs.
        Also finds any 0key songs that might not have records yet.
        """
        self.songs = {}

        # 1. First collect all existing song records
        for name, item in self.data_map.items():
            m = re.match(r"^(.*)\.Record\.(EZ|HD|IN|AT|Legacy)$", name)
            if m:
                song_id, diff = m.groups()
                if song_id not in self.songs:
                    self.songs[song_id] = {}
                try:
                    rec_dict = json.loads(item["val"])
                except Exception:
                    rec_dict = {"s": 0, "a": 0.0, "c": 0}

                s = int(rec_dict.get("s", 0))
                a = float(rec_dict.get("a", 0.0))
                c = int(rec_dict.get("c", 0))
                fc = (c == 1)

                self.songs[song_id][diff] = {
                    "score": s,
                    "acc": round(a, 4),
                    "c": c,
                    "fc": fc,
                    "rating": calculate_rating(s, fc),
                    "has_record": True
                }

        # 2. Add songs found from 0key single unlocks if not already present
        for name in self.data_map.keys():
            if name.startswith("0key"):
                song_title = name[4:].strip()
                # Check if this matches an existing song
                match_found = False
                for sid in self.songs.keys():
                    if sid.startswith(song_title + ".") or sid == song_title:
                        match_found = True
                        break
                if not match_found and song_title:
                    # Register candidate song ID
                    cand_id = f"{song_title}.0"
                    if cand_id not in self.songs:
                        self.songs[cand_id] = {}

    def get_summary(self) -> Dict[str, Any]:
        """
        Returns statistical summary of the loaded save, incorporating catalog statistics.
        """
        catalog = load_database()
        cat_songs = catalog.get("songs", {})
        total_catalog_songs = len(cat_songs)

        total_records = 0
        played_songs_count = 0
        diff_counts = {d: 0 for d in DIFFICULTIES}
        rating_counts = {
            "phi": 0,
            "v_fc": 0,
            "v": 0,
            "s": 0,
            "a": 0,
            "b": 0,
            "c": 0,
            "f": 0
        }

        for sid, diffs in self.songs.items():
            has_any = False
            for d, rec in diffs.items():
                if rec.get("has_record"):
                    has_any = True
                    total_records += 1
                    diff_counts[d] = diff_counts.get(d, 0) + 1
                    rating = rec.get("rating", "False")
                    if rating == "φ":
                        rating_counts["phi"] += 1
                    elif rating == "V (FC)":
                        rating_counts["v_fc"] += 1
                    elif rating.startswith("V"):
                        rating_counts["v"] += 1
                    elif rating.startswith("S"):
                        rating_counts["s"] += 1
                    elif rating.startswith("A"):
                        rating_counts["a"] += 1
                    elif rating.startswith("B"):
                        rating_counts["b"] += 1
                    elif rating.startswith("C"):
                        rating_counts["c"] += 1
                    else:
                        rating_counts["f"] += 1
            if has_any:
                played_songs_count += 1

        money = {}
        for i in range(5):
            k = f"NumOfMoney{i}"
            money[k] = self.data_map.get(k, {}).get("val", "0")

        challenge_rank = self.data_map.get("ChallengeModeRank", {}).get("val", "0")

        unlocks_count = {
            "single": sum(1 for k in self.data_map if k.startswith("0key")),
            "collection": sum(1 for k in self.data_map if k.startswith("1key")),
            "illustration": sum(1 for k in self.data_map if k.startswith("2key")),
            "avatar": sum(1 for k in self.data_map if k.startswith("3key")),
            "grade": sum(1 for k in self.data_map if k.endswith("Grade"))
        }

        return {
            "source_path": self.source_path or "未保存 / 内存中",
            "modified": self.modified,
            "total_items": len(self.data_map),
            "total_songs": (total_catalog_songs if total_catalog_songs > 0 else len(self.songs)) if len(self.data_map) > 0 else 0,
            "total_catalog_songs": total_catalog_songs,
            "played_songs": played_songs_count,
            "total_records": total_records,
            "diff_counts": diff_counts,
            "rating_counts": rating_counts,
            "money": money,
            "challenge_rank": challenge_rank,
            "unlocks_count": unlocks_count,
            "rks": self.calculate_rks(),
            "db_version": catalog.get("version", "未导入 APK"),
            "catalog_stats": {
                "songs": total_catalog_songs,
                "collections": len(catalog.get("collections", [])),
                "single_keys": len(catalog.get("single_keys", [])),
                "illustration_keys": len(catalog.get("illustration_keys", [])),
                "avatars": len(catalog.get("avatars", []))
            }
        }

    def _build_diff_map(self) -> Dict[str, Dict[str, float]]:
        """
        Chart constants keyed by both bare and '.0'-suffixed song ids.

        Every value comes from the user's own APK extraction; the program
        ships no chart data of its own, so an empty map simply means the
        catalog has not been imported yet.
        """
        catalog = load_database()
        diff_map: Dict[str, Dict[str, float]] = {}
        for song_id, info in catalog.get("songs", {}).items():
            bare = song_id[:-2] if song_id.endswith(".0") else song_id
            values: Dict[str, float] = {}
            # NOTE: `difficulties` maps a difficulty name straight to a float
            # (e.g. {"EZ": 1.0, "HD": 6.5}) -- it is NOT a nested dict.
            for diff_name, diff_value in (info.get("difficulties") or {}).items():
                try:
                    values[diff_name] = float(diff_value)
                except (TypeError, ValueError):
                    continue
            diff_map[bare] = values
            diff_map[bare + ".0"] = values
        return diff_map

    def _scored_charts(self) -> List[Dict[str, Any]]:
        """
        Every played chart with its constant and single-chart RKS attached.

        This is the shared basis for the player RKS, the per-chart ranking on
        the main page, and the analysis panel.
        """
        diff_map = self._build_diff_map()
        if not diff_map:
            return []

        charts: List[Dict[str, Any]] = []
        for name, item in self.data_map.items():
            m = re.match(r"^(.*)\.Record\.(EZ|HD|IN|AT|Legacy)$", name)
            if not m:
                continue
            song_id, diff = m.groups()
            clean_sid = song_id[:-2] if song_id.endswith(".0") else song_id
            try:
                rec = json.loads(item["val"])
            except Exception:
                continue

            constants = diff_map.get(clean_sid) or diff_map.get(song_id) or {}
            constant = float(constants.get(diff, 0.0))
            score = int(rec.get("s", 0))
            acc = float(rec.get("a", 0.0))
            charts.append({
                "song_id": song_id,
                "diff": diff,
                "constant": constant,
                "score": score,
                "acc": acc,
                "rks": single_chart_rks(acc, constant),
            })
        return charts

    def calculate_rks(self) -> float:
        """
        Player ranking score: mean of (best 3 phi charts + best 27 others).

        The two pools do not overlap -- a chart picked for p3 is removed from
        the b27 candidates -- and the divisor stays 30 even when the player
        has fewer than 3 AP charts. This matches the game's own behaviour.
        """
        try:
            scored = self._scored_charts()
            if not scored:
                return 0.0

            scored.sort(key=lambda r: r["rks"], reverse=True)
            phi = [r for r in scored if r["score"] == 1000000][:3]
            phi_ids = {id(r) for r in phi}
            best = [r for r in scored if id(r) not in phi_ids][:27]

            return round(sum(r["rks"] for r in phi + best) / 30.0, 4)
        except Exception:
            return 0.0

    def build_analysis(self) -> Dict[str, Any]:
        """
        Full offline RKS analysis of the loaded save.

        Depends only on the save plus the user's own imported catalog, so it
        works with no network and no cloud login. An empty catalog yields
        `catalog_ready: False` rather than a screen full of zeros.
        """
        catalog = load_database()
        catalog_songs = catalog.get("songs", {})
        if not catalog_songs:
            return {
                "catalog_ready": False,
                "message": "尚未导入 APK，无法获取谱面定数，无法计算 RKS。",
            }

        # Titles for display; the save itself only stores song ids.
        titles: Dict[str, str] = {}
        for cat_id, info in catalog_songs.items():
            bare = cat_id[:-2] if cat_id.endswith(".0") else cat_id
            title = info.get("title") or parse_song_id(cat_id)[0]
            titles[bare] = title
            titles[bare + ".0"] = title

        def label(song_id: str) -> str:
            bare = song_id[:-2] if song_id.endswith(".0") else song_id
            return titles.get(bare) or titles.get(song_id) or parse_song_id(song_id)[0]

        scored = self._scored_charts()
        if not scored:
            return {
                "catalog_ready": True,
                "has_records": False,
                "message": "存档中没有任何成绩记录。",
            }

        for c in scored:
            c["title"] = label(c["song_id"])
            c["rks"] = round(c["rks"], 4)

        scored.sort(key=lambda r: r["rks"], reverse=True)

        # p3 / b27, non-overlapping: a chart taken for p3 leaves the b27 pool.
        phi = [r for r in scored if r["score"] == 1000000][:3]
        phi_ids = {id(r) for r in phi}
        best = [r for r in scored if id(r) not in phi_ids][:27]

        rks = round(sum(r["rks"] for r in phi + best) / 30.0, 4)
        # The bar a new chart must clear to enter b27. Until the player has a
        # full 27, anything scoring at all gets in, so the bar is 0.
        threshold = best[-1]["rks"] if len(best) >= 27 else 0.0

        in_b30 = phi_ids | {id(r) for r in best}
        suggestions = []
        for c in scored:
            if c["score"] == 1000000 or c["constant"] <= 0:
                continue

            # The bar this chart must clear to be worth playing again: either
            # it is outside b27 and must beat the weakest entry, or it is
            # already in and must beat its own current score.
            inside = id(c) in in_b30
            bar = c["rks"] if inside else max(threshold, c["rks"])

            # Aim at a realistic next step rather than "just barely enough".
            # A target that only ties the bar yields no gain at all, which is
            # why each candidate acc must clear it outright.
            floor_acc = acc_required_for_rks(bar, c["constant"])
            if floor_acc is None:
                continue  # constant too low to ever matter

            target = None
            for step in (0.25, 0.5, 1.0, 2.0):
                cand = max(floor_acc + 0.01, c["acc"] + step)
                if cand <= 100.0:
                    target = cand
                    break
            if target is None:
                target = 100.0
            if target <= c["acc"]:
                continue

            new_rks = single_chart_rks(target, c["constant"])
            if new_rks <= bar:
                continue

            # Replacing the weakest b27 entry (or improving one's own slot)
            # moves the 30-chart mean by the difference over 30.
            displaced = c["rks"] if inside else threshold
            gain = (new_rks - displaced) / 30.0
            if gain <= 0:
                continue

            suggestions.append({
                "song_id": c["song_id"],
                "title": c["title"],
                "diff": c["diff"],
                "constant": c["constant"],
                "acc": c["acc"],
                "acc_needed": round(target, 3),
                "acc_delta": round(target - c["acc"], 3),
                "rks_gain": round(gain, 4),
                "in_b30": inside,
            })

        # Both orderings are precomputed; the UI just picks one. Charts
        # already in b30 and those outside it are kept apart, because
        # "polish what counts" and "break into the ranking" are different
        # goals and the former would otherwise crowd out the latter.
        inside_list = [s for s in suggestions if s["in_b30"]]
        outside_list = [s for s in suggestions if not s["in_b30"]]

        def top(items, key):
            return sorted(items, key=key)[:30]

        by_effort = top(suggestions, lambda s: s["acc_delta"])
        by_gain = top(suggestions, lambda s: -s["rks_gain"])

        # Constant distribution over played charts.
        distribution: Dict[str, Dict[str, Any]] = {}
        for c in scored:
            if c["constant"] <= 0:
                continue
            bucket = f"{int(c['constant'])}.0-{int(c['constant'])}.9"
            d = distribution.setdefault(bucket, {"count": 0, "acc_sum": 0.0})
            d["count"] += 1
            d["acc_sum"] += c["acc"]
        for bucket, d in distribution.items():
            d["avg_acc"] = round(d["acc_sum"] / d["count"], 2)
            del d["acc_sum"]

        # Coverage per difficulty: played charts vs charts that exist.
        total_by_diff: Dict[str, int] = {}
        for info in catalog_songs.values():
            for diff_name, value in (info.get("difficulties") or {}).items():
                try:
                    if float(value) > 0:
                        total_by_diff[diff_name] = total_by_diff.get(diff_name, 0) + 1
                except (TypeError, ValueError):
                    continue
        played_by_diff: Dict[str, int] = {}
        for c in scored:
            played_by_diff[c["diff"]] = played_by_diff.get(c["diff"], 0) + 1

        coverage = {}
        for diff_name in ("EZ", "HD", "IN", "AT"):
            coverage[diff_name] = {
                "played": played_by_diff.get(diff_name, 0),
                "total": total_by_diff.get(diff_name, 0),
            }

        return {
            "catalog_ready": True,
            "has_records": True,
            "rks": rks,
            "threshold": round(threshold, 4),
            "phi": phi,
            "best": best,
            "suggestions_by_effort": by_effort,
            "suggestions_by_gain": by_gain,
            "suggestions_inside_by_effort": top(inside_list, lambda s: s["acc_delta"]),
            "suggestions_inside_by_gain": top(inside_list, lambda s: -s["rks_gain"]),
            "suggestions_outside_by_effort": top(outside_list, lambda s: s["acc_delta"]),
            "suggestions_outside_by_gain": top(outside_list, lambda s: -s["rks_gain"]),
            "distribution": distribution,
            "coverage": coverage,
            "played_charts": len(scored),
            "total_charts": sum(total_by_diff.values()),
        }

    def get_songs_data(self) -> List[Dict[str, Any]]:
        """
        Returns full structured list of songs and difficulties.
        Merges local game catalog (data/phigros_data.json) so unplayed songs
        are visible, searchable, and editable.
        """
        if len(self.data_map) == 0:
            return []

        catalog = load_database()
        catalog_songs = catalog.get("songs", {})

        result = []
        handled_song_keys = set()

        # Build index mapping base IDs and raw IDs to recorded self.songs keys
        recorded_by_base = {}
        for sid in self.songs.keys():
            base_id = sid[:-2] if sid.endswith(".0") else sid
            recorded_by_base[base_id] = sid
            recorded_by_base[sid] = sid

        # 1. Process all catalog songs
        for cat_id, cat_info in catalog_songs.items():
            base_cat_id = cat_id[:-2] if cat_id.endswith(".0") else cat_id
            title = cat_info.get("title") or parse_song_id(cat_id)[0]
            artist = cat_info.get("artist") or parse_song_id(cat_id)[1]
            raw_id = cat_info.get("songs_id") or (f"{base_cat_id}.0")

            matched_sid = None
            if cat_id in self.songs:
                matched_sid = cat_id
            elif raw_id in self.songs:
                matched_sid = raw_id
            elif base_cat_id in recorded_by_base:
                matched_sid = recorded_by_base[base_cat_id]
            else:
                for sid in self.songs.keys():
                    stitle, _ = parse_song_id(sid)
                    if stitle and stitle == title:
                        matched_sid = sid
                        break

            if matched_sid:
                handled_song_keys.add(matched_sid)
                handled_song_keys.add(matched_sid[:-2] if matched_sid.endswith(".0") else matched_sid)
                actual_id = matched_sid
            else:
                actual_id = raw_id

            avail_diffs = list(cat_info.get("available_diffs", ["EZ", "HD", "IN"]))
            diff_constants = cat_info.get("difficulties", {})
            if "AT" in diff_constants and "AT" not in avail_diffs:
                avail_diffs.append("AT")

            diffs_dict = {}
            has_any_record = False
            for d in avail_diffs:
                if matched_sid and d in self.songs[matched_sid] and self.songs[matched_sid][d].get("has_record"):
                    diffs_dict[d] = self.songs[matched_sid][d]
                    has_any_record = True
                else:
                    diffs_dict[d] = {
                        "score": 0,
                        "acc": 0.0,
                        "c": 0,
                        "fc": False,
                        "rating": "未游玩",
                        "has_record": False
                    }

            if matched_sid:
                for d, rec in self.songs[matched_sid].items():
                    if d not in diffs_dict and rec.get("has_record"):
                        diffs_dict[d] = rec
                        has_any_record = True

            unlocked = False
            for k in [f"0key{title}", f"0key{actual_id}", f"0key{cat_id}"]:
                if k in self.data_map:
                    unlocked = (self.data_map[k].get("val") == "1")
                    break

            result.append({
                "id": actual_id,
                "catalog_id": cat_id,
                "title": title,
                "artist": artist,
                "illustrator": cat_info.get("illustrator", ""),
                "diff_constants": diff_constants,
                "charters": cat_info.get("charters", {}),
                "unlocked": unlocked,
                "has_records": has_any_record,
                "diffs": diffs_dict
            })

        # 2. Add remaining songs from self.songs (custom songs)
        for sid in sorted(self.songs.keys()):
            base_id = sid[:-2] if sid.endswith(".0") else sid
            if sid in handled_song_keys or base_id in handled_song_keys:
                continue
            title, artist = parse_song_id(sid)
            diffs_dict = {}
            has_any_record = False
            for d in DIFFICULTIES:
                if d in self.songs[sid] and self.songs[sid][d].get("has_record"):
                    diffs_dict[d] = self.songs[sid][d]
                    has_any_record = True
                else:
                    diffs_dict[d] = {
                        "score": 0,
                        "acc": 0.0,
                        "c": 0,
                        "fc": False,
                        "rating": "未游玩",
                        "has_record": False
                    }
            unlocked = False
            for k in [f"0key{title}", f"0key{sid}"]:
                if k in self.data_map:
                    unlocked = (self.data_map[k].get("val") == "1")
                    break

            result.append({
                "id": sid,
                "catalog_id": sid,
                "title": title,
                "artist": artist,
                "illustrator": "",
                "diff_constants": {},
                "charters": {},
                "unlocked": unlocked,
                "has_records": has_any_record,
                "diffs": diffs_dict
            })

        # 3. Attach the chart constant and single-chart RKS to every record.
        #    Done once here rather than in each branch above, so catalog songs
        #    and custom songs cannot drift apart.
        for song in result:
            constants = song.get("diff_constants") or {}
            best = 0.0
            for diff_name, rec in song.get("diffs", {}).items():
                try:
                    constant = float(constants.get(diff_name, 0.0) or 0.0)
                except (TypeError, ValueError):
                    constant = 0.0
                rec["constant"] = constant
                if rec.get("has_record"):
                    rec["rks"] = round(
                        single_chart_rks(float(rec.get("acc", 0.0)), constant), 4
                    )
                else:
                    rec["rks"] = 0.0
                if rec["rks"] > best:
                    best = rec["rks"]
            # Highest single-chart RKS on this song, for the card-grid sort.
            song["best_chart_rks"] = round(best, 4)

        return result

    def update_record(self, song_id: str, diff: str, score: int, acc: float, c: int) -> None:
        """
        Updates or creates a song record.
        Resolves proper save key format and automatically unlocks the song and chart grade.
        """
        if diff not in DIFFICULTIES:
            raise ValueError(f"Invalid difficulty: {diff}")

        score = max(0, min(1000000, int(score)))
        acc = max(0.0, min(100.0, float(acc)))
        c = 1 if (c == 1 or score == 1000000) else 0
        fc = (c == 1)

        catalog = load_database()
        cat_songs = catalog.get("songs", {})

        save_song_id = song_id
        title = ""

        if song_id in cat_songs:
            save_song_id = cat_songs[song_id].get("songs_id") or f"{song_id}.0"
            title = cat_songs[song_id].get("title", "")
        else:
            base_id = song_id[:-2] if song_id.endswith(".0") else song_id
            if base_id in cat_songs:
                save_song_id = cat_songs[base_id].get("songs_id") or song_id
                title = cat_songs[base_id].get("title", "")

        if not title:
            title, _ = parse_song_id(save_song_id)

        record_val = json.dumps({"s": score, "a": acc, "c": c}, separators=(",", ":"))
        key_name = f"{save_song_id}.Record.{diff}"

        if key_name in self.data_map:
            self.data_map[key_name]["val"] = record_val
        else:
            item = {
                "tag": "string",
                "name": key_name,
                "val": record_val,
                "is_enc": True
            }
            self.raw_items.append(item)
            self.data_map[key_name] = item

        # Auto-unlock song via 0key
        if title:
            single_key = f"0key{title}"
            if single_key not in self.data_map:
                item = {"tag": "string", "name": single_key, "val": "1", "is_enc": True}
                self.raw_items.append(item)
                self.data_map[single_key] = item
            else:
                self.data_map[single_key]["val"] = "1"

            # Auto-unlock IN/AT grades
            if diff == "IN":
                grade_key = f"{title}INSGrade"
                if grade_key not in self.data_map:
                    item = {"tag": "string", "name": grade_key, "val": "True", "is_enc": True}
                    self.raw_items.append(item)
                    self.data_map[grade_key] = item
                else:
                    self.data_map[grade_key]["val"] = "True"
            elif diff == "AT":
                grade_key = f"{title}ATSGrade"
                if grade_key not in self.data_map:
                    item = {"tag": "string", "name": grade_key, "val": "True", "is_enc": True}
                    self.raw_items.append(item)
                    self.data_map[grade_key] = item
                else:
                    self.data_map[grade_key]["val"] = "True"

        if save_song_id not in self.songs:
            self.songs[save_song_id] = {}

        self.songs[save_song_id][diff] = {
            "score": score,
            "acc": round(acc, 4),
            "c": c,
            "fc": fc,
            "rating": calculate_rating(score, fc),
            "has_record": True
        }
        self.modified = True

    def delete_record(self, song_id: str, diff: str) -> bool:
        """
        Removes a song difficulty record.
        """
        save_song_id = song_id
        if song_id not in self.songs:
            base_id = song_id[:-2] if song_id.endswith(".0") else song_id
            if base_id in self.songs:
                save_song_id = base_id
            elif f"{song_id}.0" in self.songs:
                save_song_id = f"{song_id}.0"

        key_name = f"{save_song_id}.Record.{diff}"
        found = False
        if key_name in self.data_map:
            del self.data_map[key_name]
            self.raw_items = [item for item in self.raw_items if item["name"] != key_name]
            found = True

        if save_song_id in self.songs and diff in self.songs[save_song_id]:
            del self.songs[save_song_id][diff]
            found = True

        if found:
            self.modified = True
            return True
        return False

    def batch_update_records(
        self,
        diff_target: str,  # 'ALL' or 'EZ', 'HD', 'IN', 'AT', 'Legacy'
        preset: str,       # 'phi', 'v_fc', 'v', 's', 'a', 'b', 'c', 'f'
        custom_score: Optional[int] = None,
        custom_acc: Optional[float] = None,
        custom_c: Optional[int] = None,
        song_ids: Optional[List[str]] = None
    ) -> int:
        """
        Batch updates records according to preset or custom values.
        Supports applying to all catalog songs as well.
        """
        preset_info = RATING_PRESETS.get(preset, RATING_PRESETS["phi"])
        score = custom_score if custom_score is not None else preset_info["score"]
        acc = custom_acc if custom_acc is not None else preset_info["acc"]
        c = custom_c if custom_c is not None else preset_info["c"]

        catalog = load_database()
        cat_songs = catalog.get("songs", {})

        if song_ids is not None:
            target_songs = song_ids
        else:
            target_set = set(self.songs.keys())
            for sid, meta in cat_songs.items():
                raw_id = meta.get("songs_id") or (f"{sid}.0" if not sid.endswith(".0") else sid)
                target_set.add(raw_id)
            target_songs = list(target_set)

        updated_count = 0
        for sid in target_songs:
            cat_meta = cat_songs.get(sid) or cat_songs.get(sid[:-2] if sid.endswith(".0") else sid)
            avail = cat_meta.get("available_diffs", ["EZ", "HD", "IN"]) if cat_meta else DIFFICULTIES

            if diff_target in DIFFICULTIES:
                target_diffs = [diff_target] if (diff_target in avail or sid in self.songs) else []
            else:
                target_diffs = [d for d in avail if d in DIFFICULTIES]

            for d in target_diffs:
                self.update_record(sid, d, score, acc, c)
                updated_count += 1

        self.modified = True
        return updated_count

    def batch_unlock(
        self,
        unlock_songs: bool = True,
        unlock_collections: bool = True,
        unlock_illustrations: bool = True,
        unlock_avatars: bool = True,
        unlock_grades: bool = True
    ) -> Dict[str, int]:
        """
        Batch unlocks game items using both existing save keys and the full APK game catalog.
        """
        catalog = load_database()
        counts = {"songs": 0, "collections": 0, "illustrations": 0, "avatars": 0, "grades": 0}

        def _ensure_entry(key_name: str, val: str) -> bool:
            if key_name in self.data_map:
                if self.data_map[key_name].get("val") != val:
                    self.data_map[key_name]["val"] = val
                    return True
                return False
            else:
                item = {"tag": "string", "name": key_name, "val": val, "is_enc": True}
                self.raw_items.append(item)
                self.data_map[key_name] = item
                return True

        # 1. Unlock songs (0key)
        if unlock_songs:
            for k, item in self.data_map.items():
                if k.startswith("0key") and item.get("val") != "1":
                    item["val"] = "1"
                    counts["songs"] += 1

            for k in catalog.get("single_keys", []):
                if _ensure_entry(f"0key{k}", "1"):
                    counts["songs"] += 1

            for song in catalog.get("songs", {}).values():
                title = song.get("title", "")
                if title and _ensure_entry(f"0key{title}", "1"):
                    counts["songs"] += 1

        # 2. Unlock collections (1key & opened)
        if unlock_collections:
            for k, item in self.data_map.items():
                if k.startswith("1key") and item.get("val") != "1":
                    item["val"] = "1"
                    counts["collections"] += 1
                elif k.endswith("CollectionTextOpened") and item.get("val") == "0":
                    item["val"] = "1"
                    counts["collections"] += 1

            for col in catalog.get("collections", []):
                k = col.get("key")
                if k:
                    if _ensure_entry(f"1key{k}", "1"):
                        counts["collections"] += 1
                    if _ensure_entry(f"{k}CollectionTextOpened", "1"):
                        counts["collections"] += 1

        # 3. Unlock illustrations (2key)
        if unlock_illustrations:
            for k, item in self.data_map.items():
                if k.startswith("2key") and item.get("val") != "1":
                    item["val"] = "1"
                    counts["illustrations"] += 1

            for k in catalog.get("illustration_keys", []):
                if _ensure_entry(f"2key{k}", "1"):
                    counts["illustrations"] += 1

        # 4. Unlock avatars (3key)
        if unlock_avatars:
            for k, item in self.data_map.items():
                if k.startswith("3key") and item.get("val") != "1":
                    item["val"] = "1"
                    counts["avatars"] += 1

            for k in catalog.get("avatars", []):
                if _ensure_entry(f"3key{k}", "1"):
                    counts["avatars"] += 1

        # 5. Unlock grades (Grade)
        if unlock_grades:
            for k, item in self.data_map.items():
                if k.endswith("Grade") and item.get("val") != "True":
                    item["val"] = "True"
                    counts["grades"] += 1

            for song in catalog.get("songs", {}).values():
                title = song.get("title", "")
                if title:
                    if _ensure_entry(f"{title}INSGrade", "True"):
                        counts["grades"] += 1
                    diffs = song.get("difficulties", {})
                    avail = song.get("available_diffs", [])
                    if "AT" in diffs or "AT" in avail:
                        if _ensure_entry(f"{title}ATSGrade", "True"):
                            counts["grades"] += 1

        self.modified = True
        return counts

    def update_currencies_and_settings(self, money_dict: Dict[str, str], challenge_rank: Optional[str] = None) -> None:
        """
        Updates Data money (NumOfMoney0..4) and ChallengeModeRank.
        """
        for k, v in money_dict.items():
            if k.startswith("NumOfMoney"):
                if k in self.data_map:
                    self.data_map[k]["val"] = str(v)
                else:
                    item = {"tag": "string", "name": k, "val": str(v), "is_enc": True}
                    self.raw_items.append(item)
                    self.data_map[k] = item

        if challenge_rank is not None:
            cr_key = "ChallengeModeRank"
            if cr_key in self.data_map:
                self.data_map[cr_key]["val"] = str(challenge_rank)
            else:
                item = {"tag": "string", "name": cr_key, "val": str(challenge_rank), "is_enc": True}
                self.raw_items.append(item)
                self.data_map[cr_key] = item

        self.modified = True

    def add_song(self, song_id: str) -> bool:
        """
        Adds a new song ID to the editor.
        """
        song_id = song_id.strip()
        if not song_id:
            return False
        if song_id not in self.songs:
            self.songs[song_id] = {}
            self.modified = True
            return True
        return False

    def export_xml(self) -> bytes:
        """
        Serializes all current data into Phigros PlayerPrefs encrypted XML byte string.
        """
        root = ET.Element("map")
        # To maintain fidelity, we write all items in data_map
        for item in self.data_map.values():
            tag = item.get("tag", "string")
            name = item["name"]
            val = str(item.get("val", ""))
            is_enc = item.get("is_enc", True)

            if tag == "string":
                if is_enc:
                    enc_name = aes_encrypt(name)
                    enc_val = aes_encrypt(val)
                else:
                    enc_name = quote(name, safe="").replace("/", "%2F")
                    enc_val = val
                elem = ET.SubElement(root, "string", {"name": enc_name})
                elem.text = enc_val
            elif tag == "int":
                enc_name = quote(name, safe="").replace("/", "%2F")
                ET.SubElement(root, "int", {"name": enc_name, "value": val})
            else:
                enc_name = quote(name, safe="").replace("/", "%2F")
                elem = ET.SubElement(root, tag, {"name": enc_name})
                elem.text = val

        xml_declaration = "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>\n"
        body = ET.tostring(root, encoding="utf-8").decode("utf-8")
        # Format with clean indentation
        from xml.dom import minidom
        dom = minidom.parseString(body)
        pretty_xml = dom.toprettyxml(indent="    ")
        # Replace minidom header with the exact Unity standalone header
        lines = pretty_xml.splitlines()
        if lines and lines[0].startswith("<?xml"):
            lines[0] = "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>"
        else:
            lines.insert(0, "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>")
        result = "\n".join(lines) + "\n"
        return result.encode("utf-8")

    def export_json(self) -> str:
        """
        Exports the entire decrypted save as a clean formatted JSON string.
        """
        export_dict = {}
        for k, item in self.data_map.items():
            export_dict[k] = item["val"]
        return json.dumps(export_dict, ensure_ascii=False, indent=4)
