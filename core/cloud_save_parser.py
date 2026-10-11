"""
Phigros cloud save (.save ZIP) packer / unpacker.

A cloud save is a ZIP of up to five entries (gameRecord, gameKey,
gameProgress, settings, user). Each entry is a 1-byte format version
followed by an AES-256-CBC ciphertext.

Design note - forward compatibility:
    Every entry is parsed independently. gameProgress tracks the story
    chapters and therefore changes shape whenever Pigeon Games ships new
    content. When it fails to parse, we keep its ORIGINAL ENCRYPTED BYTES
    and write them back untouched on repack, so an unrecognised layout
    degrades to "progress editing unavailable" instead of corrupting the
    player's save. Callers check `progress_supported` to decide whether to
    expose the progress editor.
"""
import io
import copy
import json
import re
import zipfile
from typing import Any, Dict, List, Optional, Tuple

from core.crypto import decrypt_cloud_bytes, encrypt_cloud_bytes
from core import phi_struct as ps
from core.apk_parser import load_database

# Entry order used by the game when it writes the archive.
ENTRY_ORDER = ("gameKey", "gameProgress", "gameRecord", "settings", "user")

DIFFICULTIES = ("EZ", "HD", "IN", "AT", "Legacy")
SUMMARY_DIFFICULTIES = ("EZ", "HD", "IN", "AT")

# gameKey 5-slot mask <-> local PlayerPrefs key naming.
#   slot 0: collection text read -> "<name>CollectionTextOpened"
#   slot 1: single song unlocked -> "0key<name>"
#   slot 2: collection owned     -> "1key<name>"
#   slot 3: illustration owned   -> "2key<name>"
#   slot 4: avatar owned         -> "3key<name>"
SLOT_COLLECTION_READ = 0
SLOT_SINGLE = 1
SLOT_COLLECTION = 2
SLOT_ILLUSTRATION = 3
SLOT_AVATAR = 4

COLLECTION_READ_SUFFIX = "CollectionTextOpened"
SLOT_PREFIXES = {
    SLOT_SINGLE: "0key",
    SLOT_COLLECTION: "1key",
    SLOT_ILLUSTRATION: "2key",
    SLOT_AVATAR: "3key",
}

DEFAULT_SETTINGS = {
    "chordSupport": 1,
    "fcAPIndicator": 1,
    "enableHitSound": 1,
    "lowResolutionMode": 0,
    "deviceName": "Phigros",
    "bright": 1.0,
    "musicVolume": 1.0,
    "effectVolume": 1.0,
    "hitSoundVolume": 1.0,
    "soundOffset": 0.0,
    "noteScale": 1.0,
}

DEFAULT_USER = {
    "showPlayerId": 1,
    "selfIntro": "",
    "avatar": "Introduction",
    "background": "Introduction",
}

DEFAULT_GAME_KEY = {
    "keyList": {},
    "lanotaReadKeys": "[0, 0, 0, 0, 0, 0]",
    "camelliaReadKey": "[0, 0, 0, 0, 0, 0, 0, 0]",
    "sideStory4BeginReadKey": 0,
    "oldScoreClearedV390": 1,
}


# ============================================================
#  Slot helpers
# ============================================================

def slots_from_entry(entry: Dict[str, Any]) -> List[int]:
    """Expands a {type, flag} gameKey entry into a dense 5-slot value list."""
    try:
        mask = ps.coerce_int_list(entry.get("type", "[0, 0, 0, 0, 0]"))
    except Exception:
        mask = [0, 0, 0, 0, 0]
    try:
        flags = ps.coerce_int_list(entry.get("flag", "[]"))
    except Exception:
        flags = []

    slots = [0, 0, 0, 0, 0]
    flag_index = 0
    for slot in range(min(5, len(mask))):
        if mask[slot]:
            slots[slot] = flags[flag_index] if flag_index < len(flags) else 1
            flag_index += 1
    return slots


def entry_from_slots(slots: List[int]) -> Dict[str, str]:
    """Collapses a dense 5-slot value list back into a {type, flag} entry."""
    mask = [1 if slots[i] > 0 else 0 for i in range(5)]
    flags = [slots[i] for i in range(5) if slots[i] > 0]
    return {"type": str(mask), "flag": str(flags)}


def local_key_name(slot: int, name: str) -> str:
    """Builds the local PlayerPrefs key for a gameKey slot."""
    if slot == SLOT_COLLECTION_READ:
        return name + COLLECTION_READ_SUFFIX
    return SLOT_PREFIXES[slot] + name


def parse_local_key(key: str) -> Optional[Tuple[str, int]]:
    """Inverse of local_key_name. Returns (name, slot) or None."""
    for slot, prefix in SLOT_PREFIXES.items():
        if key.startswith(prefix):
            return key[len(prefix):], slot
    if key.endswith(COLLECTION_READ_SUFFIX) and len(key) > len(COLLECTION_READ_SUFFIX):
        return key[:-len(COLLECTION_READ_SUFFIX)], SLOT_COLLECTION_READ
    return None


# ============================================================
#  Package
# ============================================================

class CloudSavePackage:
    """A decrypted, editable view over a Phigros .save archive."""

    def __init__(self, raw_zip_bytes: bytes):
        self.raw_zip_bytes = raw_zip_bytes
        self.raw_entries: Dict[str, bytes] = {}   # name -> original bytes (version + ciphertext)
        self.versions: Dict[str, int] = {}
        self.parsed: Dict[str, Any] = {}
        self.errors: Dict[str, str] = {}

        with zipfile.ZipFile(io.BytesIO(raw_zip_bytes)) as zf:
            for info in zf.filelist:
                self.raw_entries[info.filename] = zf.read(info.filename)

        for name, blob in self.raw_entries.items():
            if not blob:
                self.errors[name] = "entry is empty"
                continue

            version = blob[0]
            self.versions[name] = version
            try:
                payload = decrypt_cloud_bytes(blob[1:])
            except Exception as exc:
                self.errors[name] = "decryption failed: %s" % exc
                continue

            try:
                self.parsed[name] = ps.parse_entry(name, version, payload)
            except Exception as exc:
                self.errors[name] = str(exc)

    # -- capability flags -------------------------------------------------

    @property
    def progress_supported(self) -> bool:
        """False when gameProgress uses a layout this build does not know."""
        return "gameProgress" in self.parsed

    @property
    def progress_error(self) -> Optional[str]:
        return self.errors.get("gameProgress")

    def capability_report(self) -> Dict[str, Any]:
        """Per-entry parse status, for surfacing in the UI."""
        report = {}
        for name in self.raw_entries:
            report[name] = {
                "version": self.versions.get(name),
                "parsed": name in self.parsed,
                "error": self.errors.get(name),
                "max_supported_version": ps.LATEST_VERSIONS.get(name),
            }
        return report

    # -- accessors --------------------------------------------------------

    def game_record(self) -> Dict[str, Any]:
        return self.parsed.setdefault("gameRecord", {})

    def game_key(self) -> Dict[str, Any]:
        gk = self.parsed.get("gameKey")
        if not isinstance(gk, dict):
            gk = copy.deepcopy(DEFAULT_GAME_KEY)
            self.parsed["gameKey"] = gk
        gk.setdefault("keyList", {})
        return gk

    def game_progress(self) -> Optional[Dict[str, Any]]:
        """Returns the parsed progress dict, or None on an unknown layout."""
        return self.parsed.get("gameProgress")

    def progress_field_types(self) -> Dict[str, str]:
        """
        Per-field UI kinds for gameProgress, straight from the structure
        definition. Empty when the layout is unrecognised.
        """
        if not self.progress_supported:
            return {}
        version = self.versions.get("gameProgress", ps.LATEST_VERSIONS["gameProgress"])
        return ps.describe_structure("gameProgress", version)

    def progress_bits_widths(self) -> Dict[str, int]:
        """Width of every Bits field, so edits cannot change a field's size."""
        types = self.progress_field_types()
        if not types:
            return {}
        version = self.versions.get("gameProgress", ps.LATEST_VERSIONS["gameProgress"])
        widths = {}
        for field, kind in types.items():
            if kind == "bits":
                width = ps.bits_width("gameProgress", version, field)
                if width:
                    widths[field] = width
        return widths

    def settings(self) -> Dict[str, Any]:
        s = self.parsed.get("settings")
        if not isinstance(s, dict):
            s = copy.deepcopy(DEFAULT_SETTINGS)
            self.parsed["settings"] = s
        return s

    def user(self) -> Dict[str, Any]:
        u = self.parsed.get("user")
        if not isinstance(u, dict):
            u = copy.deepcopy(DEFAULT_USER)
            self.parsed["user"] = u
        return u

    # -- mutation ---------------------------------------------------------

    def update_settings(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        """Applies a partial settings update, coercing to the on-disk types."""
        settings = self.settings()
        bit_fields = ("chordSupport", "fcAPIndicator", "enableHitSound", "lowResolutionMode")
        float_fields = ("bright", "musicVolume", "effectVolume", "hitSoundVolume", "soundOffset", "noteScale")

        for field, value in (changes or {}).items():
            if field in bit_fields:
                if isinstance(value, str):
                    value = value.strip().lower() not in ("", "0", "false", "no")
                settings[field] = 1 if value else 0
            elif field in float_fields:
                try:
                    settings[field] = float(value)
                except (TypeError, ValueError):
                    continue
            elif field == "deviceName":
                settings[field] = str(value)

        # Volumes and brightness are 0..1 sliders in game.
        for field in ("bright", "musicVolume", "effectVolume", "hitSoundVolume"):
            settings[field] = max(0.0, min(1.0, float(settings.get(field, 1.0))))
        return settings

    def update_user(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        """Applies a partial name-card update (intro / avatar / background)."""
        user = self.user()
        for field in ("selfIntro", "avatar", "background"):
            if field in (changes or {}):
                user[field] = str(changes[field])
        if "showPlayerId" in (changes or {}):
            value = changes["showPlayerId"]
            if isinstance(value, str):
                value = value.strip().lower() not in ("", "0", "false", "no")
            user["showPlayerId"] = 1 if value else 0
        return user

    def update_progress(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        """
        Applies a partial gameProgress update.

        Only fields already present in the parsed save are accepted, so a
        payload aimed at a different game version can never introduce a
        field the current structure cannot serialize.
        """
        progress = self.game_progress()
        if progress is None:
            raise ValueError(
                "gameProgress uses an unrecognised format (%s); progress editing is disabled"
                % (self.progress_error or "unknown layout")
            )

        for field, value in (changes or {}).items():
            if field not in progress:
                continue
            current = progress[field]

            if field == "money":
                money = ps.coerce_int_list(value)
                progress[field] = [max(0, min(0xFFFF, m)) for m in (money + [0] * 5)[:5]]
            elif field == "challengeModeRank":
                progress[field] = max(0, min(0xFFFF, int(value)))
            elif isinstance(current, str) and current.startswith("["):
                # Bit-list field: keep the original width.
                width = len(ps.coerce_int_list(current))
                bits = ps.coerce_int_list(value)
                bits = (bits + [0] * width)[:width]
                progress[field] = str([1 if b else 0 for b in bits])
            elif isinstance(current, str):
                progress[field] = str(value)
            else:
                progress[field] = int(value)

        return progress

    # -- serialization ----------------------------------------------------

    def repack(self) -> bytes:
        """
        Rebuilds the .save archive.

        Entries that parsed are re-serialized from the (possibly edited)
        dicts at their original format version. Entries that did not parse
        are written back byte-for-byte from the original archive.
        """
        output: Dict[str, bytes] = {}

        for name in self.raw_entries:
            if name not in self.parsed:
                output[name] = self.raw_entries[name]
                continue

            version = self.versions.get(name, ps.LATEST_VERSIONS.get(name, 1))
            # A structure gains fields across versions; make sure the version
            # we write can actually hold everything the dict carries.
            if name == "gameProgress":
                version = max(version, ps.detect_progress_version(self.parsed[name]))
            elif name == "gameKey":
                version = max(version, ps.detect_key_version(self.parsed[name]))

            payload = ps.build_entry(name, version, self.parsed[name])
            output[name] = bytes([version]) + encrypt_cloud_bytes(payload)

        ordered = [n for n in ENTRY_ORDER if n in output]
        ordered += [n for n in output if n not in ordered]

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name in ordered:
                zf.writestr(name, output[name])
        return buffer.getvalue()

    # -- reporting --------------------------------------------------------

    def export_decrypted_json(self) -> Dict[str, Any]:
        """Full decrypted dump, including entries that could not be parsed."""
        return {
            "versions": self.versions,
            "entries_present": list(self.raw_entries.keys()),
            "capabilities": self.capability_report(),
            "progress_supported": self.progress_supported,
            "parse_errors": self.errors,
            "data": self.parsed,
        }

    def export_records_json(self) -> Dict[str, Any]:
        """Just gameRecord + gameKey, which is what players usually want."""
        return {
            "gameRecord": self.parsed.get("gameRecord", {}),
            "gameKey": self.parsed.get("gameKey", {}),
            "versions": {
                "gameRecord": self.versions.get("gameRecord"),
                "gameKey": self.versions.get("gameKey"),
            },
        }

    def get_inspect_summary(self) -> Dict[str, Any]:
        records = self.parsed.get("gameRecord", {})
        progress = self.parsed.get("gameProgress") or {}
        key_list = (self.parsed.get("gameKey") or {}).get("keyList", {})

        total_levels = 0
        ap_records = 0
        for diffs in records.values():
            if not isinstance(diffs, dict):
                continue
            for record in diffs.values():
                if not isinstance(record, dict):
                    continue
                total_levels += 1
                if record.get("score") == 1000000:
                    ap_records += 1

        challenge_rank = int(progress.get("challengeModeRank", 0) or 0)
        return {
            "played_songs": len(records),
            "total_level_records": total_levels,
            "ap_records": ap_records,
            "money": progress.get("money", [0, 0, 0, 0, 0]),
            "challenge_mode_rank": challenge_rank,
            "rank_level": challenge_rank // 100,
            "rank_num": challenge_rank % 100,
            "keys_count": len(key_list),
            "files_present": list(self.raw_entries.keys()),
            "progress_supported": self.progress_supported,
            "progress_error": self.progress_error,
            "capabilities": self.capability_report(),
        }


# ============================================================
#  RKS and summary
# ============================================================

def build_difficulty_map() -> Dict[str, Dict[str, float]]:
    """Chart constants keyed by both bare and '.0'-suffixed song ids."""
    catalog = load_database()
    diff_map: Dict[str, Dict[str, float]] = {}
    for song_id, info in catalog.get("songs", {}).items():
        bare = song_id[:-2] if song_id.endswith(".0") else song_id
        values = {}
        for diff_name, diff_value in (info.get("difficulties") or {}).items():
            try:
                values[diff_name] = float(diff_value)
            except (TypeError, ValueError):
                continue
        diff_map[bare] = values
        diff_map[bare + ".0"] = values
    return diff_map


def single_rks(acc: float, constant: float) -> float:
    """Official per-chart RKS: ((acc-55)/45)^2 * constant, zero below 70% acc."""
    if acc < 70.0 or constant <= 0.0:
        return 0.0
    return (((acc - 55.0) / 45.0) ** 2) * constant


def compute_rks(records: Dict[str, Any], diff_map: Optional[Dict[str, Dict[str, float]]] = None) -> float:
    """Player RKS = mean of (best 3 phi charts + best 27 charts overall)."""
    if diff_map is None:
        diff_map = build_difficulty_map()
    if not diff_map:
        return 0.0

    scored = []
    for song_id, diffs in records.items():
        if not isinstance(diffs, dict):
            continue
        constants = diff_map.get(song_id) or diff_map.get(song_id + ".0") or {}
        for diff_name, record in diffs.items():
            if not isinstance(record, dict):
                continue
            constant = constants.get(diff_name, 0.0)
            acc = float(record.get("acc", 0.0))
            scored.append({
                "score": int(record.get("score", 0)),
                "rks": single_rks(acc, constant),
            })

    if not scored:
        return 0.0

    scored.sort(key=lambda r: r["rks"], reverse=True)
    phi = [r for r in scored if r["score"] == 1000000][:3]
    phi_ids = {id(r) for r in phi}
    best = [r for r in scored if id(r) not in phi_ids][:27]

    return round(sum(r["rks"] for r in phi + best) / 30.0, 4)


def difficulty_statistics(records: Dict[str, Any]) -> Dict[str, List[int]]:
    """Per-difficulty [cleared, full combo, phi] counts for the summary blob."""
    stats = {d: [0, 0, 0] for d in SUMMARY_DIFFICULTIES}
    for diffs in records.values():
        if not isinstance(diffs, dict):
            continue
        for diff_name in SUMMARY_DIFFICULTIES:
            record = diffs.get(diff_name)
            if not isinstance(record, dict):
                continue
            score = int(record.get("score", 0))
            if score <= 0:
                continue
            stats[diff_name][0] += 1
            if int(record.get("fc", 0)) or score == 1000000:
                stats[diff_name][1] += 1
            if score == 1000000:
                stats[diff_name][2] += 1
    return stats


def build_summary_for_package(
    pkg: CloudSavePackage,
    base_summary: Optional[Dict[str, Any]] = None,
) -> bytes:
    """Builds the LeanCloud summary blob from the package's real contents."""
    records = pkg.parsed.get("gameRecord", {})
    progress = pkg.parsed.get("gameProgress") or {}
    user = pkg.parsed.get("user") or {}
    stats = difficulty_statistics(records)

    base = base_summary or {}
    challenge = progress.get("challengeModeRank")
    if challenge is None:
        challenge = base.get("challenge_rank", 0)

    summary = {
        "saveVersion": int(base.get("save_version") or 7),
        "challenge": max(0, min(0xFFFF, int(challenge or 0))),
        "rks": compute_rks(records),
        "gameVersion": int(base.get("game_version") or 1),
        "avatar": user.get("avatar") or base.get("avatar") or "Introduction",
        "EZ": stats["EZ"],
        "HD": stats["HD"],
        "IN": stats["IN"],
        "AT": stats["AT"],
    }
    return ps.build_summary(summary)


# ============================================================
#  Cloud  <->  editor bridge
# ============================================================

def cloud_package_to_data_map(pkg: CloudSavePackage) -> Dict[str, Dict[str, Any]]:
    """Flattens gameRecord + gameKey into the editor's PlayerPrefs data_map."""
    data_map: Dict[str, Dict[str, Any]] = {}

    def put(name: str, value: str, tag: str = "string", encrypted: bool = True) -> None:
        data_map[name] = {"tag": tag, "name": name, "val": value, "is_enc": encrypted}

    # gameRecord -> "<songId>.0.Record.<diff>"
    for song_id, diffs in (pkg.parsed.get("gameRecord") or {}).items():
        if not isinstance(diffs, dict):
            continue
        full_id = song_id if song_id.endswith(".0") else song_id + ".0"
        for diff_name, record in diffs.items():
            if not isinstance(record, dict):
                continue
            score = int(record.get("score", 0))
            # Keep full float precision: acc is a float32 widened to float64,
            # so it round-trips exactly. Rounding here would silently rewrite
            # every accuracy value (and shift RKS) on each upload.
            put(
                "%s.Record.%s" % (full_id, diff_name),
                json.dumps({
                    "s": score,
                    "a": float(record.get("acc", 0.0)),
                    "c": 1 if (int(record.get("fc", 0)) or score == 1000000) else 0,
                }),
            )

    # gameKey -> 0key/1key/2key/3key/CollectionTextOpened
    game_key = pkg.parsed.get("gameKey") or {}
    for name, entry in (game_key.get("keyList") or {}).items():
        if not isinstance(entry, dict):
            continue
        for slot, value in enumerate(slots_from_entry(entry)):
            if value:
                put(local_key_name(slot, name), str(value))

    for field, local_name in (
        ("lanotaReadKeys", "LanotaReadKeys"),
        ("camelliaReadKey", "CamelliaReadKey"),
        ("sideStory4BeginReadKey", "SideStory4BeginReadKey"),
        ("oldScoreClearedV390", "OldScoreClearedV390"),
    ):
        if field in game_key:
            put(local_name, str(game_key[field]), encrypted=False)

    # gameProgress money / rank are editable in the existing currency panel.
    progress = pkg.parsed.get("gameProgress")
    if isinstance(progress, dict):
        put("ChallengeModeRank", str(progress.get("challengeModeRank", 0)), encrypted=False)
        money = progress.get("money") or []
        for index in range(5):
            put("NumOfMoney%d" % index, str(money[index] if index < len(money) else 0))

    return data_map


def apply_data_map_to_package(pkg: CloudSavePackage, data_map: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
    """
    Merges the editor's data_map back into a package.

    gameRecord and gameKey are rebuilt from the editor state; every other
    entry (settings, user, and gameProgress beyond money/rank) is left
    exactly as it came off the server.
    """
    stats = {"records": 0, "keys": 0}

    # -- gameRecord ---------------------------------------------------
    records = pkg.game_record()
    record_pattern = re.compile(r"^(?P<song>.+)\.Record\.(?P<diff>EZ|HD|IN|AT|Legacy)$")
    for name, item in data_map.items():
        match = record_pattern.match(name)
        if not match:
            continue
        song_id = match.group("song")
        if song_id.endswith(".0"):
            song_id = song_id[:-2]
        diff_name = match.group("diff")

        try:
            value = json.loads(item.get("val") or "{}")
        except Exception:
            continue

        score = int(value.get("s", 0) or 0)
        acc = float(value.get("a", 0.0) or 0.0)
        combo = int(value.get("c", 0) or 0)
        if score <= 0 and acc <= 0.0 and combo <= 0:
            continue

        records.setdefault(song_id, {})[diff_name] = {
            "score": max(0, min(1000000, score)),
            "acc": max(0.0, min(100.0, acc)),
            "fc": 1 if (combo or score == 1000000) else 0,
        }
        stats["records"] += 1

    # -- gameKey ------------------------------------------------------
    game_key = pkg.game_key()
    key_list = game_key["keyList"]

    incoming: Dict[str, List[int]] = {}
    for name, item in data_map.items():
        parsed_key = parse_local_key(name)
        if not parsed_key:
            continue
        key_name, slot = parsed_key
        raw = str(item.get("val", "0")).strip()
        try:
            value = int(raw)
        except ValueError:
            value = 1 if raw.lower() in ("true", "yes") else 0
        if value <= 0:
            continue
        incoming.setdefault(key_name, [0, 0, 0, 0, 0])[slot] = value

    for key_name, new_slots in incoming.items():
        current = slots_from_entry(key_list[key_name]) if key_name in key_list else [0, 0, 0, 0, 0]
        merged = [max(current[i], new_slots[i]) for i in range(5)]
        if merged != current:
            stats["keys"] += 1
        key_list[key_name] = entry_from_slots(merged)

    for local_name, field in (
        ("LanotaReadKeys", "lanotaReadKeys"),
        ("CamelliaReadKey", "camelliaReadKey"),
    ):
        if local_name in data_map and field in game_key:
            try:
                width = len(ps.coerce_int_list(game_key[field]))
                bits = ps.coerce_int_list(data_map[local_name].get("val"))
                game_key[field] = str([1 if b else 0 for b in (bits + [0] * width)[:width]])
            except Exception:
                pass

    for local_name, field in (
        ("SideStory4BeginReadKey", "sideStory4BeginReadKey"),
        ("OldScoreClearedV390", "oldScoreClearedV390"),
    ):
        if local_name in data_map and field in game_key:
            try:
                game_key[field] = int(str(data_map[local_name].get("val", 0)).strip())
            except ValueError:
                pass

    # -- gameProgress money / challenge rank --------------------------
    progress = pkg.game_progress()
    if isinstance(progress, dict):
        if "ChallengeModeRank" in data_map and "challengeModeRank" in progress:
            try:
                progress["challengeModeRank"] = max(
                    0, min(0xFFFF, int(str(data_map["ChallengeModeRank"].get("val", 0)).strip()))
                )
            except ValueError:
                pass

        if "money" in progress:
            money = list(progress.get("money") or [0, 0, 0, 0, 0])
            money = (money + [0, 0, 0, 0, 0])[:5]
            for index in range(5):
                entry = data_map.get("NumOfMoney%d" % index)
                if not entry:
                    continue
                try:
                    money[index] = max(0, min(0xFFFF, int(str(entry.get("val", 0)).strip())))
                except ValueError:
                    pass
            progress["money"] = money

    return stats


def editor_save_to_cloud_package_bytes(
    save: Any,
    base_pkg: CloudSavePackage,
    base_summary: Optional[Dict[str, Any]] = None,
) -> Tuple[bytes, bytes, Dict[str, int]]:
    """
    Projects the editor's current save onto a freshly pulled remote package.

    `base_pkg` must be the package just downloaded from the server: it
    supplies settings, user and gameProgress, so a local XML/JSON save
    (which has no equivalent of those) can be pushed without losing them.

    Returns (save_zip_bytes, summary_bytes, merge_stats).
    """
    if base_pkg is None:
        raise ValueError("a freshly pulled remote package is required before uploading")

    stats = apply_data_map_to_package(base_pkg, save.data_map)
    save_bytes = base_pkg.repack()
    summary_bytes = build_summary_for_package(base_pkg, base_summary)
    return save_bytes, summary_bytes, stats
