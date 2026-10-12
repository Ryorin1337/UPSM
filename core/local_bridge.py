"""
gameProgress / settings / user  <->  local PlayerPrefs bridge.

A local Phigros save (Unity PlayerPrefs XML) and a cloud save store the
same player state in two very different shapes:

    cloud   gameProgress.chapter8SongUnlocked = "[1, 1, 1, 1, 1, 1]"
    local   C8CraveWaveUnlocked = "True", C8DESTRUCTION321Unlocked = "True", ...

Historically `cloud_package_to_data_map()` only projected gameRecord and
gameKey, so a save converted cloud -> local lost roughly a hundred
entries: every chapter unlock, every S-grade AT unlock, and the whole of
settings and user. The game then rebuilt them at their defaults, which
looked like progress had been destroyed.

This module is the missing translation layer, and it runs both ways so a
local save can also be pushed back up without flattening the fields it
has no column for.

Bit ordering
    The song lists below come from the gameProgress STRUCTURE definition
    (field order and bit width), not from game assets, so nothing here is
    extracted game content. Each list is positional: index i of the list
    is bit i of that field.

    `flagOfSongRecordKey` holds 8 bits and `flagOfSongRecordKeyTakumi`
    holds 3 more, together covering the "IN cleared at S -> AT unlocked"
    songs. They are two separate cloud fields but one single family of
    local `<title>INSGrade` keys.
"""
import struct
from typing import Any, Dict, List, Optional, Tuple

from core import phi_struct as ps


# -- positional song lists -------------------------------------------------
# Index = bit index within the field. Names are the local PlayerPrefs
# spelling (title with separators removed), i.e. "<name>INSGrade".

FLAG_OF_SONG_RECORD_KEY = [
    "YouaretheMiserable",                               # 倒霉蛋
    "atruthseekerCommunicationwithUtopiawillbelost",    # 船
    "Shadow",
    "心之所向",
    "inferior",
    "DESTRUCTION321",
    "DistortedFate",
    "Cuvism",
]

# Bits[3]. Only the two names confirmed against a real save are mapped;
# bit 2 is deliberately left unnamed rather than guessed, since writing a
# wrong local key would fabricate an unlock. An unnamed bit is still
# preserved on the cloud side, because `local_to_progress()` keeps the
# existing bit value for any slot with no local key.
FLAG_OF_SONG_RECORD_KEY_TAKUMI = [
    "iLArtifact",
    "Stasis",
    None,
]

CHAPTER8_SONGS = [
    "CraveWave",
    "DESTRUCTION321",
    "DistortedFate",
    "Luminescence",
    "Retribution",
    "TheChariotREVIIVAL",
]

CHAPTER9_SONGS = [
    "Implexrough",
    "AboutTheUniverse",
    "Evanescent",
    "ExoplanetaryMirage",
    "EntranceToTheChaos",
    "S6",                                                # True Home, True World
]

# Spasmodic / Igallta / Rrharil each get Bits[4], one per difficulty.
UNLOCK_FLAG_SONGS = ("Spasmodic", "Igallta", "Rrharil")
UNLOCK_FLAG_DIFFS = ("EZ", "HD", "IN", "AT")


# -- scalar field maps -----------------------------------------------------

PROGRESS_BOOLS: List[Tuple[str, str]] = [
    ("legacyChapterFinished", "finishLegacyChapter"),
    ("alreadyShowCollectionTip", "AlreadyShowCollectionTip"),
    ("chapter8UnlockBegin", "chapter8UnlockBegin"),
    ("chapter8UnlockSecondPhase", "chapter8UnlockSecondPhase"),
    ("chapter8Passed", "chapter8Passed"),
    ("chapter9UnlockBegin", "Chapter9UnlockBegin"),
]

PROGRESS_SCALARS: List[Tuple[str, str]] = [
    ("completed", "GameCompleted"),
    ("songUpdateInfo", "NeedUpdateSongsData"),
    ("challengeModeRank", "ChallengeModeRank"),
    ("chapter9SecretPassword", "Chapter9SecretPassword"),
    ("chapter9SecretChallengeLifeTier", "C9SecretChallengeLifeTier"),
    ("chapter9SecretChallengeSelectedLifeTier", "C9SecretChallengeSelectedLifeTier"),
]

# settings/user use different spellings locally than in the cloud blob.
SETTINGS_BOOLS: List[Tuple[str, str]] = [
    ("chordSupport", "chordSupport"),
    ("fcAPIndicator", "aPfCisOn"),
    ("enableHitSound", "hitFxIsOn"),
    ("lowResolutionMode", "IsLowRes"),
]

SETTINGS_FLOATS: List[Tuple[str, str]] = [
    ("bright", "bright"),
    ("musicVolume", "musicVolume"),
    ("effectVolume", "SEVolume"),
    ("hitSoundVolume", "HitFXVolume"),
    ("soundOffset", "offset"),
    ("noteScale", "noteScale"),
]

USER_FIELDS: List[Tuple[str, str]] = [
    ("selfIntro", "selfIntro"),
    ("avatar", "UserIconKeyName"),
    ("background", "UserIllustrationKeyName"),
]


# -- value helpers ---------------------------------------------------------

def _to_local_bool(value: Any) -> str:
    """Local saves spell booleans 'True'/'False', cloud uses 1/0."""
    if isinstance(value, str):
        return "True" if value.strip().lower() not in ("", "0", "false", "no") else "False"
    return "True" if value else "False"


def _from_local_bool(value: Any) -> int:
    if isinstance(value, str):
        return 1 if value.strip().lower() not in ("", "0", "false", "no") else 0
    return 1 if value else 0


def _fmt_number(value: Any) -> str:
    """
    Formats a float the way Unity's PlayerPrefs string form does.

    A whole number is written without a decimal point ('1', not '1.0'),
    which is what the real save contains for musicVolume / noteScale.

    Fractional values are rounded to float32 precision first. The cloud
    blob stores these as float32; widening to float64 and calling repr()
    turns the save's own '0.4191871' into '0.41918709874153137', which
    would rewrite the value on every conversion.
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if num == int(num):
        return str(int(num))
    # Shortest decimal that still round-trips through float32 -- the same
    # string the game itself wrote ('0.4191871', not '0.41918709874153137').
    packed = struct.unpack("<f", struct.pack("<f", num))[0]
    for digits in range(1, 10):
        text = "%.*g" % (digits, packed)
        if struct.unpack("<f", struct.pack("<f", float(text)))[0] == packed:
            return text
    return repr(packed)


def _bits(value: Any, width: int) -> List[int]:
    """Normalizes a cloud bit-list field to exactly `width` ints."""
    bits = ps.coerce_int_list(value)
    return [1 if b else 0 for b in (bits + [0] * width)[:width]]


# -- cloud -> local --------------------------------------------------------

def progress_to_local(progress: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """
    Expands gameProgress into the local PlayerPrefs keys it stands for.

    Returns {local_key: local_value}. Absent cloud fields are skipped
    rather than defaulted, so an older gameProgress layout simply yields
    fewer keys instead of inventing progress the player never had.
    """
    out: Dict[str, str] = {}
    if not isinstance(progress, dict):
        return out

    for cloud_field, local_key in PROGRESS_BOOLS:
        if cloud_field in progress:
            out[local_key] = _to_local_bool(progress[cloud_field])

    for cloud_field, local_key in PROGRESS_SCALARS:
        if cloud_field in progress:
            out[local_key] = str(progress[cloud_field])

    # IN-cleared-at-S unlock flags: two cloud fields, one local family.
    for field, names in (
        ("flagOfSongRecordKey", FLAG_OF_SONG_RECORD_KEY),
        ("flagOfSongRecordKeyTakumi", FLAG_OF_SONG_RECORD_KEY_TAKUMI),
    ):
        if field not in progress:
            continue
        for index, bit in enumerate(_bits(progress[field], len(names))):
            if names[index] is None:
                continue
            out["%sINSGrade" % names[index]] = _to_local_bool(bit)

    # Per-difficulty unlock flags for the three side-story songs.
    for song in UNLOCK_FLAG_SONGS:
        field = "unlockFlagOf%s" % song
        if field not in progress:
            continue
        for index, bit in enumerate(_bits(progress[field], len(UNLOCK_FLAG_DIFFS))):
            out["unlockFlagOf%s%s" % (song, UNLOCK_FLAG_DIFFS[index])] = _to_local_bool(bit)

    for field, names, prefix in (
        ("chapter8SongUnlocked", CHAPTER8_SONGS, "C8"),
        ("chapter9SongUnlocked", CHAPTER9_SONGS, "C9"),
    ):
        if field not in progress:
            continue
        for index, bit in enumerate(_bits(progress[field], len(names))):
            out["%s%sUnlocked" % (prefix, names[index])] = _to_local_bool(bit)

    if "randomVersionUnlocked" in progress:
        for index, bit in enumerate(_bits(progress["randomVersionUnlocked"], 6)):
            out["randomVersionUnlocked%d" % (index + 1)] = _to_local_bool(bit)

    if "isFirstRun" in progress:
        out["IsFirstRun"] = "1" if _from_local_bool(progress["isFirstRun"]) else "0"

    return out


def settings_to_local(settings: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Expands the cloud settings blob into local PlayerPrefs keys."""
    out: Dict[str, str] = {}
    if not isinstance(settings, dict):
        return out
    for cloud_field, local_key in SETTINGS_BOOLS:
        if cloud_field in settings:
            out[local_key] = _to_local_bool(settings[cloud_field])
    for cloud_field, local_key in SETTINGS_FLOATS:
        if cloud_field in settings:
            out[local_key] = _fmt_number(settings[cloud_field])
    return out


def user_to_local(user: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Expands the cloud user blob (name card) into local PlayerPrefs keys."""
    out: Dict[str, str] = {}
    if not isinstance(user, dict):
        return out
    for cloud_field, local_key in USER_FIELDS:
        if cloud_field in user:
            out[local_key] = str(user[cloud_field])
    if "showPlayerId" in user:
        out["PlayerIdAppear"] = _to_local_bool(user["showPlayerId"])
    return out


# -- local -> cloud --------------------------------------------------------

def local_to_progress(data_map: Dict[str, Dict[str, Any]],
                      progress: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Collapses local PlayerPrefs keys back into gameProgress changes.

    Only fields the current `progress` actually has are produced, and a
    bit-list is only rebuilt when at least one of its local keys exists
    -- a local save missing the whole family leaves the cloud value
    untouched instead of zeroing a chapter the player had unlocked.
    """
    changes: Dict[str, Any] = {}
    if not isinstance(progress, dict):
        return changes

    def local(key: str) -> Optional[str]:
        item = data_map.get(key)
        return None if item is None else str(item.get("val", ""))

    for cloud_field, local_key in PROGRESS_BOOLS:
        if cloud_field in progress:
            value = local(local_key)
            if value is not None:
                changes[cloud_field] = _from_local_bool(value)

    for cloud_field, local_key in PROGRESS_SCALARS:
        if cloud_field in progress:
            value = local(local_key)
            if value is not None:
                changes[cloud_field] = value

    def collect(field: str, keys: List[Optional[str]]) -> None:
        if field not in progress:
            return
        values = [None if k is None else local(k) for k in keys]
        if all(v is None for v in values):
            return
        current = _bits(progress.get(field), len(keys))
        changes[field] = str([
            current[i] if values[i] is None else _from_local_bool(values[i])
            for i in range(len(keys))
        ])

    collect("flagOfSongRecordKey",
            ["%sINSGrade" % n for n in FLAG_OF_SONG_RECORD_KEY])
    collect("flagOfSongRecordKeyTakumi",
            [None if n is None else "%sINSGrade" % n
             for n in FLAG_OF_SONG_RECORD_KEY_TAKUMI])
    for song in UNLOCK_FLAG_SONGS:
        collect("unlockFlagOf%s" % song,
                ["unlockFlagOf%s%s" % (song, d) for d in UNLOCK_FLAG_DIFFS])
    collect("chapter8SongUnlocked", ["C8%sUnlocked" % n for n in CHAPTER8_SONGS])
    collect("chapter9SongUnlocked", ["C9%sUnlocked" % n for n in CHAPTER9_SONGS])
    collect("randomVersionUnlocked",
            ["randomVersionUnlocked%d" % (i + 1) for i in range(6)])

    if "isFirstRun" in progress:
        value = local("IsFirstRun")
        if value is not None:
            changes["isFirstRun"] = _from_local_bool(value)

    return changes


def local_to_settings(data_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Collapses local PlayerPrefs keys back into settings changes."""
    changes: Dict[str, Any] = {}
    for cloud_field, local_key in SETTINGS_BOOLS:
        item = data_map.get(local_key)
        if item is not None:
            changes[cloud_field] = _from_local_bool(item.get("val"))
    for cloud_field, local_key in SETTINGS_FLOATS:
        item = data_map.get(local_key)
        if item is not None:
            try:
                changes[cloud_field] = float(item.get("val"))
            except (TypeError, ValueError):
                continue
    return changes


def local_to_user(data_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Collapses local PlayerPrefs keys back into user (name card) changes."""
    changes: Dict[str, Any] = {}
    for cloud_field, local_key in USER_FIELDS:
        item = data_map.get(local_key)
        if item is not None:
            changes[cloud_field] = str(item.get("val", ""))
    item = data_map.get("PlayerIdAppear")
    if item is not None:
        changes["showPlayerId"] = _from_local_bool(item.get("val"))
    return changes
