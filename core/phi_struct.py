"""
Native Phigros binary save structure engine.

Pure in-house implementation of the Phigros cloud-save binary format:
primitive data types, bit-packed readers/writers, and the versioned
structure tables for gameKey / gameProgress / settings / user / summary.

No third-party save libraries are imported. Only PyCryptodome (AES) is
used, and that lives in core.crypto.
"""
from struct import pack, unpack
from ast import literal_eval
from typing import Any, Dict, List, Optional, Tuple


# ============================================================
#  Primitive data types
# ============================================================

class DataTypeBase:
    """Marker base class for composite (self-describing) data types."""
    @staticmethod
    def read(data: bytes, pos: int): ...

    @staticmethod
    def write(data: bytearray, value): ...


class Bit:
    """A single bit inside a bit-packed byte. Handled specially by Reader/Writer."""

    @staticmethod
    def read(data: int, index: int) -> int:
        return (data >> index) & 1

    @staticmethod
    def write(data: int, index: int, value: int) -> int:
        mask = 1 << index
        return (data & ~mask) | ((int(value) & 1) << index)


class Bits(DataTypeBase):
    """A whole byte expanded into a list of 8 bits, serialized as its str() form."""

    @staticmethod
    def read(data: bytes, pos: int) -> Tuple[str, int]:
        return str([Bit.read(data[pos], i) for i in range(8)]), pos + 1

    @staticmethod
    def write(data: bytearray, value) -> bytearray:
        bits = coerce_int_list(value)
        if len(bits) < 8:
            bits.extend([0] * (8 - len(bits)))
        byte = 0
        for i, b in enumerate(bits[:8]):
            byte = Bit.write(byte, i, b)
        data.append(byte)
        return data

    @staticmethod
    def __class_getitem__(length: int):
        return _Bits(length)


class _Bits(DataTypeBase):
    """A byte truncated to the first `length` bits (e.g. Bits[6])."""

    def __init__(self, length: int = 8):
        self._len = length

    def read(self, data: bytes, pos: int) -> Tuple[str, int]:
        return str([Bit.read(data[pos], i) for i in range(self._len)]), pos + 1

    @staticmethod
    def write(data: bytearray, value) -> bytearray:
        return Bits.write(data, value)


class Byte(DataTypeBase):
    """One raw byte (or a raw byte blob when written)."""

    @staticmethod
    def read(data: bytes, pos: int) -> Tuple[int, int]:
        return data[pos], pos + 1

    @staticmethod
    def write(data: bytearray, value) -> bytearray:
        if isinstance(value, int):
            data.append(value & 0xFF)
        else:
            data.extend(value)
        return data


class NibbleLow(DataTypeBase):
    """Low 4 bits of a byte. Must be immediately followed by NibbleHigh."""

    @staticmethod
    def read(data: bytes, pos: int) -> Tuple[int, int]:
        return data[pos] & 0x0F, pos + 1

    @staticmethod
    def write(data: bytearray, value: int) -> bytearray:
        data.append(int(value) & 0x0F)
        return data


class NibbleHigh(DataTypeBase):
    """High 4 bits of the byte written/read by the preceding NibbleLow."""

    @staticmethod
    def read(data: bytes, pos: int) -> Tuple[int, int]:
        return (data[pos - 1] >> 4) & 0x0F, pos

    @staticmethod
    def write(data: bytearray, value: int) -> bytearray:
        data[-1] = (data[-1] & 0x0F) | ((int(value) & 0x0F) << 4)
        return data


class ShortInt(DataTypeBase):
    """Unsigned 16-bit little-endian integer."""

    @staticmethod
    def read(data: bytes, pos: int) -> Tuple[int, int]:
        return unpack("<H", data[pos:pos + 2])[0], pos + 2

    @staticmethod
    def write(data: bytearray, value: int) -> bytearray:
        data.extend(pack("<H", max(0, min(0xFFFF, int(value)))))
        return data


class Int(DataTypeBase):
    """Unsigned 32-bit little-endian integer."""

    @staticmethod
    def read(data: bytes, pos: int) -> Tuple[int, int]:
        return unpack("<I", data[pos:pos + 4])[0], pos + 4

    @staticmethod
    def write(data: bytearray, value: int) -> bytearray:
        data.extend(pack("<I", max(0, min(0xFFFFFFFF, int(value)))))
        return data


class Float(DataTypeBase):
    """32-bit little-endian IEEE-754 float."""

    @staticmethod
    def read(data: bytes, pos: int) -> Tuple[float, int]:
        return unpack("<f", data[pos:pos + 4])[0], pos + 4

    @staticmethod
    def write(data: bytearray, value: float) -> bytearray:
        data.extend(pack("<f", float(value)))
        return data


class VarInt(DataTypeBase):
    """Variable-length integer, 1-2 bytes (7 bits per byte, high bit = continue)."""

    @staticmethod
    def read(data: bytes, pos: int) -> Tuple[int, int]:
        if data[pos] > 127:
            value = (data[pos] & 0b01111111) ^ (data[pos + 1] << 7)
            return value, pos + 2
        return data[pos], pos + 1

    @staticmethod
    def write(data: bytearray, value: int) -> bytearray:
        value = max(0, int(value))
        if value > 127:
            data.append((value & 0b01111111) | 0b10000000)
            data.append((value >> 7) & 0xFF)
        else:
            data.append(value)
        return data


class String(DataTypeBase):
    """VarInt length-prefixed UTF-8 string."""

    @staticmethod
    def read(data: bytes, pos: int) -> Tuple[str, int]:
        length, pos = VarInt.read(data, pos)
        return data[pos:pos + length].decode("utf-8"), pos + length

    @staticmethod
    def write(data: bytearray, value: str) -> bytearray:
        encoded = ("" if value is None else str(value)).encode("utf-8")
        VarInt.write(data, len(encoded))
        data.extend(encoded)
        return data


# ============================================================
#  Composite data types
# ============================================================

def coerce_int_list(value) -> List[int]:
    """Accepts a real list or its str()/JSON form and returns a list of ints."""
    if isinstance(value, (list, tuple)):
        parsed = list(value)
    elif isinstance(value, str):
        try:
            parsed = literal_eval(value)
        except Exception as exc:
            raise ValueError("Cannot parse %r as a list: %s" % (value, exc)) from exc
    else:
        raise TypeError("Expected list or list-literal string, got %s" % type(value).__name__)

    if not isinstance(parsed, (list, tuple)):
        raise TypeError("Expected a list, parsed as %s" % type(parsed).__name__)
    return [int(x) for x in parsed]


class GameKey(DataTypeBase):
    """
    The unlock / collection key table.

    Each entry carries a 5-slot type mask plus a flag list whose length
    equals the number of set bits in that mask:

        slot 0: collection text read  -> local key "<name>CollectionTextOpened"
        slot 1: single song unlocked  -> local key "0key<name>"
        slot 2: collection owned      -> local key "1key<name>"
        slot 3: illustration owned    -> local key "2key<name>"
        slot 4: avatar owned          -> local key "3key<name>"
    """

    @staticmethod
    def read(data: bytes, pos: int) -> Tuple[Dict[str, Dict[str, str]], int]:
        all_keys: Dict[str, Dict[str, str]] = {}
        reader = Reader(data, pos)
        key_count = reader.type_read(VarInt)

        for _ in range(key_count):
            name = reader.type_read(String)
            length = reader.type_read(Byte)
            entry = all_keys[name] = {}
            entry["type"] = reader.type_read(Bits[5])

            flags: List[int] = []
            for _ in range(length - 1):  # the type mask already consumed one byte
                flag_value, reader.pos = Byte.read(data, reader.pos)
                flags.append(flag_value)
            entry["flag"] = str(flags)

        return all_keys, reader.pos

    @staticmethod
    def write(data: bytearray, value: Dict[str, Dict[str, Any]]) -> bytearray:
        writer = Writer(data)
        writer.type_write(VarInt, len(value))

        for name, entry in value.items():
            flags = coerce_int_list(entry.get("flag", "[]"))
            writer.type_write(String, name)
            writer.type_write(Byte, len(flags) + 1)
            writer.type_write(Bits, entry.get("type", "[0, 0, 0, 0, 0]"))
            for flag in flags:
                writer.type_write(Byte, flag)

        return writer.get_data()


class Money(DataTypeBase):
    """The five Data currency slots, each a VarInt."""

    @staticmethod
    def read(data: bytes, pos: int) -> Tuple[List[int], int]:
        money = []
        for _ in range(5):
            value, pos = VarInt.read(data, pos)
            money.append(value)
        return money, pos

    @staticmethod
    def write(data: bytearray, value) -> bytearray:
        money = coerce_int_list(value)
        money = (money + [0, 0, 0, 0, 0])[:5]
        for amount in money:
            VarInt.write(data, amount)
        return data


class GameRecord(DataTypeBase):
    """Per-song, per-difficulty score records."""

    DIFFICULTIES = ("EZ", "HD", "IN", "AT", "Legacy")

    @staticmethod
    def read(data: bytes, pos: int) -> Tuple[Dict[str, Dict[str, Any]], int]:
        all_records: Dict[str, Dict[str, Any]] = {}
        reader = Reader(data, pos)
        song_count = reader.type_read(VarInt)

        for _ in range(song_count):
            song_name = reader.type_read(String)
            # A handful of song ids ship without the ".0" suffix; normalise.
            if song_name.endswith(".0"):
                song_name = song_name[:-2]

            length = reader.type_read(VarInt)
            end_position = reader.pos + length
            unlock = reader.type_read(Byte)
            full_combo = reader.type_read(Byte)
            song = all_records[song_name] = {}

            for level in range(5):
                if Bit.read(unlock, level):
                    score = reader.type_read(Int)
                    acc = reader.type_read(Float)
                    song[GameRecord.DIFFICULTIES[level]] = {
                        "score": score,
                        "acc": acc,
                        "fc": Bit.read(full_combo, level),
                    }

            if reader.pos != end_position:
                raise ValueError(
                    'gameRecord entry "%s" ended at byte %d, expected %d'
                    % (song_name, reader.pos, end_position)
                )

        return all_records, reader.pos

    @staticmethod
    def write(data: bytearray, value: Dict[str, Dict[str, Any]]) -> bytearray:
        index_of = {name: i for i, name in enumerate(GameRecord.DIFFICULTIES)}

        writer = Writer(data)
        writer.type_write(VarInt, len(value))

        for name, song in value.items():
            writer.type_write(String, name if name.endswith(".0") else name + ".0")

            unlock = [0] * 8
            full_combo = [0] * 8
            body = Writer()
            present = 0
            for diff, index in index_of.items():
                record = song.get(diff)
                if not isinstance(record, dict):
                    continue
                present += 1
                unlock[index] = 1
                full_combo[index] = 1 if int(record.get("fc", 0)) else 0
                body.type_write(Int, int(record.get("score", 0)))
                body.type_write(Float, float(record.get("acc", 0.0)))

            # payload = unlock byte + fc byte + 8 bytes per unlocked difficulty
            writer.type_write(VarInt, present * 8 + 2)
            writer.type_write(Bits, str(unlock))
            writer.type_write(Bits, str(full_combo))
            writer.type_write(Byte, bytes(body.get_data()))

        return writer.get_data()


class Triple(DataTypeBase):
    """A (cleared, full combo, phi) triple of ShortInts, used by summary."""

    @staticmethod
    def read(data: bytes, pos: int) -> Tuple[List[int], int]:
        reader = Reader(data, pos)
        return [reader.type_read(ShortInt) for _ in range(3)], reader.pos

    @staticmethod
    def write(data: bytearray, value) -> bytearray:
        triple = coerce_int_list(value)
        triple = (triple + [0, 0, 0])[:3]
        writer = Writer(data)
        for item in triple:
            writer.type_write(ShortInt, item)
        return writer.get_data()


# ============================================================
#  Reader / Writer
# ============================================================

class Reader:
    """Sequential binary reader with transparent bit-packing support."""

    def __init__(self, data, pos: int = 0):
        self.data = data
        self.pos = pos
        self._bit_byte = 0
        self._bit_active = False
        self._bit_index = 0

    def type_read(self, type_class) -> Any:
        if type_class is Bit:
            # Consecutive Bit fields share one byte, LSB first.
            if not self._bit_active or self._bit_index >= 8:
                self._bit_byte, self.pos = Byte.read(self.data, self.pos)
                self._bit_active = True
                self._bit_index = 0
            value = Bit.read(self._bit_byte, self._bit_index)
            self._bit_index += 1
            return value

        # Any non-Bit field closes the current bit-packed byte.
        self._bit_active = False
        self._bit_index = 0
        value, self.pos = type_class.read(self.data, self.pos)
        return value

    def parse_structure(self, structure) -> Dict[str, Any]:
        """
        Deserializes according to a structure class' annotations.
        Raises ValueError if the payload is not fully consumed, which is how
        we detect that the game shipped a newer, unknown layout.
        """
        instance = structure()
        if isinstance(instance, DataTypeBase):
            result = self.type_read(instance)
        else:
            result = {}
            for field, field_type in structure.__annotations__.items():
                result[field] = self.type_read(field_type)

        leftover = self.remaining()
        if leftover != 0:
            raise ValueError(
                'structure "%s" left %d unread byte(s); the save format has probably changed'
                % (structure.__name__, leftover)
            )
        return result

    def remaining(self) -> int:
        return len(self.data) - self.pos


class Writer:
    """Sequential binary writer with transparent bit-packing support."""

    def __init__(self, data=None):
        if data is None:
            self.data = bytearray()
        elif isinstance(data, bytes):
            self.data = bytearray(data)
        elif isinstance(data, bytearray):
            self.data = data
        else:
            raise TypeError("Unsupported buffer type: %s" % type(data).__name__)

        self._bit_byte = 0
        self._bit_active = False
        self._bit_index = 0

    def _flush_bits(self) -> None:
        """Emits the pending bit-packed byte, if any."""
        if self._bit_active:
            self._bit_active = False
            self._bit_index = 0
            Byte.write(self.data, self._bit_byte)
            self._bit_byte = 0

    def type_write(self, type_class, value) -> None:
        if type_class is Bit:
            if not self._bit_active or self._bit_index >= 8:
                self._flush_bits()
                self._bit_byte = 0
                self._bit_active = True
                self._bit_index = 0
            self._bit_byte = Bit.write(self._bit_byte, self._bit_index, value)
            self._bit_index += 1
            return

        self._flush_bits()
        self.data = type_class.write(self.data, value)

    def build_structure(self, structure, data: dict) -> bytearray:
        """Serializes a dict according to a structure class' annotations."""
        instance = structure()
        if isinstance(instance, DataTypeBase):
            self.type_write(instance, data)
        else:
            for field, field_type in structure.__annotations__.items():
                if field not in data:
                    raise KeyError('missing field "%s" for structure "%s"' % (field, structure.__name__))
                self.type_write(field_type, data[field])

        # A structure ending on a Bit field still has a byte pending.
        self._flush_bits()
        return self.data

    def get_data(self) -> bytearray:
        self._flush_bits()
        return self.data


# ============================================================
#  Structure definitions
# ============================================================

class Settings01:
    """Player preferences (file head 0x01)."""
    file_head = b"\x01"

    chordSupport: Bit
    fcAPIndicator: Bit
    enableHitSound: Bit
    lowResolutionMode: Bit
    deviceName: String
    bright: Float
    musicVolume: Float
    effectVolume: Float
    hitSoundVolume: Float
    soundOffset: Float
    noteScale: Float


class User01:
    """Player name card (file head 0x01)."""
    file_head = b"\x01"

    showPlayerId: Byte
    selfIntro: String
    avatar: String
    background: String


class GameRecord01:
    """Score records (file head 0x01)."""
    file_head = b"\x01"

    gameRecord: GameRecord


class GameKey01:
    file_head = b"\x01"

    keyList: GameKey
    lanotaReadKeys: Bits[6]


class GameKey02:
    file_head = b"\x02"

    keyList: GameKey
    lanotaReadKeys: Bits[6]
    camelliaReadKey: Bits


class GameKey03:
    file_head = b"\x03"

    keyList: GameKey
    lanotaReadKeys: Bits[6]
    camelliaReadKey: Bits
    sideStory4BeginReadKey: Byte
    oldScoreClearedV390: Byte


class GameProgress01:
    file_head = b"\x01"

    isFirstRun: Bit
    legacyChapterFinished: Bit
    alreadyShowCollectionTip: Bit
    alreadyShowAutoUnlockINTip: Bit
    completed: String
    songUpdateInfo: VarInt
    challengeModeRank: ShortInt
    money: Money
    unlockFlagOfSpasmodic: Bits[4]
    unlockFlagOfIgallta: Bits[4]
    unlockFlagOfRrharil: Bits[4]
    flagOfSongRecordKey: Bits


class GameProgress02:
    file_head = b"\x02"

    isFirstRun: Bit
    legacyChapterFinished: Bit
    alreadyShowCollectionTip: Bit
    alreadyShowAutoUnlockINTip: Bit
    completed: String
    songUpdateInfo: VarInt
    challengeModeRank: ShortInt
    money: Money
    unlockFlagOfSpasmodic: Bits[4]
    unlockFlagOfIgallta: Bits[4]
    unlockFlagOfRrharil: Bits[4]
    flagOfSongRecordKey: Bits
    randomVersionUnlocked: Bits[6]


class GameProgress03:
    file_head = b"\x03"

    isFirstRun: Bit
    legacyChapterFinished: Bit
    alreadyShowCollectionTip: Bit
    alreadyShowAutoUnlockINTip: Bit
    completed: String
    songUpdateInfo: VarInt
    challengeModeRank: ShortInt
    money: Money
    unlockFlagOfSpasmodic: Bits[4]
    unlockFlagOfIgallta: Bits[4]
    unlockFlagOfRrharil: Bits[4]
    flagOfSongRecordKey: Bits
    randomVersionUnlocked: Bits[6]
    chapter8UnlockBegin: Bit
    chapter8UnlockSecondPhase: Bit
    chapter8Passed: Bit
    chapter8SongUnlocked: Bits[6]


class GameProgress04:
    file_head = b"\x04"

    isFirstRun: Bit
    legacyChapterFinished: Bit
    alreadyShowCollectionTip: Bit
    alreadyShowAutoUnlockINTip: Bit
    completed: String
    songUpdateInfo: VarInt
    challengeModeRank: ShortInt
    money: Money
    unlockFlagOfSpasmodic: Bits[4]
    unlockFlagOfIgallta: Bits[4]
    unlockFlagOfRrharil: Bits[4]
    flagOfSongRecordKey: Bits
    randomVersionUnlocked: Bits[6]
    chapter8UnlockBegin: Bit
    chapter8UnlockSecondPhase: Bit
    chapter8Passed: Bit
    chapter8SongUnlocked: Bits[6]
    flagOfSongRecordKeyTakumi: Bits[3]


class GameProgress05:
    file_head = b"\x05"

    isFirstRun: Bit
    legacyChapterFinished: Bit
    alreadyShowCollectionTip: Bit
    alreadyShowAutoUnlockINTip: Bit
    completed: String
    songUpdateInfo: VarInt
    challengeModeRank: ShortInt
    money: Money
    unlockFlagOfSpasmodic: Bits[4]
    unlockFlagOfIgallta: Bits[4]
    unlockFlagOfRrharil: Bits[4]
    flagOfSongRecordKey: Bits
    randomVersionUnlocked: Bits[6]
    chapter8UnlockBegin: Bit
    chapter8UnlockSecondPhase: Bit
    chapter8Passed: Bit
    chapter8SongUnlocked: Bits[6]
    flagOfSongRecordKeyTakumi: Bits[3]
    chapter9UnlockBegin: Bit
    chapter9SecretChallengePendingLifeUnlock: Bit
    chapter9SongUnlocked: Bits[6]
    chapter9SecretChallengeLifeTier: NibbleLow
    chapter9SecretChallengeSelectedLifeTier: NibbleHigh
    chapter9SecretPassword: String


class GameProgress06:
    file_head = b"\x06"

    isFirstRun: Bit
    legacyChapterFinished: Bit
    alreadyShowCollectionTip: Bit
    alreadyShowAutoUnlockINTip: Bit
    completed: String
    songUpdateInfo: VarInt
    challengeModeRank: ShortInt
    money: Money
    unlockFlagOfSpasmodic: Bits[4]
    unlockFlagOfIgallta: Bits[4]
    unlockFlagOfRrharil: Bits[4]
    flagOfSongRecordKey: Bits
    randomVersionUnlocked: Bits[6]
    chapter8UnlockBegin: Bit
    chapter8UnlockSecondPhase: Bit
    chapter8Passed: Bit
    chapter8SongUnlocked: Bits[6]
    flagOfSongRecordKeyTakumi: Bits[3]
    chapter9UnlockBegin: Bit
    chapter9SecretChallengePendingLifeUnlock: Bit
    chapter9SongUnlocked: Bits[6]
    chapter9SecretChallengeLifeTier: NibbleLow
    chapter9SecretChallengeSelectedLifeTier: NibbleHigh
    chapter9SecretPassword: String
    chapter9Phase2SongUnlocked: Bits[6]
    chapter9Phase2Begin: Bit
    chapter9Phase2Passed: Bit
    c9BaselineChallengeReached: Bit
    chapter9Phase2Step: Byte


class SummaryStruct:
    """LeanCloud name-card summary (not part of the .save ZIP)."""

    saveVersion: Byte
    challenge: ShortInt
    rks: Float
    gameVersion: VarInt
    avatar: String
    EZ: Triple
    HD: Triple
    IN: Triple
    AT: Triple


# ------------------------------------------------------------
#  Version dispatch
# ------------------------------------------------------------

GAME_KEY_VERSIONS = {1: GameKey01, 2: GameKey02, 3: GameKey03}
GAME_PROGRESS_VERSIONS = {
    1: GameProgress01,
    2: GameProgress02,
    3: GameProgress03,
    4: GameProgress04,
    5: GameProgress05,
    6: GameProgress06,
}
GAME_RECORD_VERSIONS = {1: GameRecord01}
SETTINGS_VERSIONS = {1: Settings01}
USER_VERSIONS = {1: User01}

ENTRY_VERSIONS = {
    "gameKey": GAME_KEY_VERSIONS,
    "gameProgress": GAME_PROGRESS_VERSIONS,
    "gameRecord": GAME_RECORD_VERSIONS,
    "settings": SETTINGS_VERSIONS,
    "user": USER_VERSIONS,
}

# Highest layout this build knows how to write for each entry.
LATEST_VERSIONS = {name: max(table) for name, table in ENTRY_VERSIONS.items()}


def get_structure(entry_name: str, version: int):
    """
    Resolves the structure class for an entry at a given file-head version.
    Returns None when the version is newer (or older) than anything we know,
    which callers treat as 'preserve the raw bytes untouched'.
    """
    return ENTRY_VERSIONS.get(entry_name, {}).get(version)


def describe_structure(entry_name: str, version: int) -> Dict[str, str]:
    """
    Maps each field of a structure to a UI-facing kind.

    Kinds:
        "bit"   single on/off flag        -> checkbox
        "bits"  fixed-width list of bits  -> comma-separated list, width locked
        "int"   numeric field             -> number input
        "text"  length-prefixed string    -> text input
        "money" the 5-slot currency array -> dedicated inputs
        "other" anything else             -> read-only passthrough

    Returns {} when the layout is unknown, which is the UI's cue to hide
    the editor entirely.
    """
    structure = get_structure(entry_name, version)
    if structure is None:
        return {}

    kinds: Dict[str, str] = {}
    for field, annotation in getattr(structure, "__annotations__", {}).items():
        if annotation is Bit:
            kinds[field] = "bit"
        elif annotation is Bits or isinstance(annotation, _Bits):
            kinds[field] = "bits"
        elif annotation is Money:
            kinds[field] = "money"
        elif annotation is String:
            kinds[field] = "text"
        elif annotation in (Byte, ShortInt, Int, VarInt, NibbleLow, NibbleHigh):
            kinds[field] = "int"
        elif annotation is Float:
            kinds[field] = "float"
        else:
            kinds[field] = "other"
    return kinds


def bits_width(entry_name: str, version: int, field: str) -> Optional[int]:
    """Width of a Bits field, so the UI can keep the list from growing."""
    structure = get_structure(entry_name, version)
    if structure is None:
        return None
    annotation = getattr(structure, "__annotations__", {}).get(field)
    if annotation is Bits:
        return 8
    if isinstance(annotation, _Bits):
        return annotation._len
    return None


def detect_progress_version(progress: Dict[str, Any]) -> int:
    """Picks the lowest gameProgress layout that can hold every field present."""
    if progress.get("chapter9Phase2Begin") is not None:
        return 6
    if progress.get("chapter9UnlockBegin") is not None:
        return 5
    if progress.get("flagOfSongRecordKeyTakumi") is not None:
        return 4
    if progress.get("chapter8UnlockBegin") is not None:
        return 3
    if progress.get("randomVersionUnlocked") is not None:
        return 2
    return 1


def detect_key_version(game_key: Dict[str, Any]) -> int:
    """Picks the lowest gameKey layout that can hold every field present."""
    if game_key.get("oldScoreClearedV390") is not None:
        return 3
    if game_key.get("camelliaReadKey") is not None:
        return 2
    return 1


def parse_entry(entry_name: str, version: int, payload: bytes) -> Dict[str, Any]:
    """Deserializes one decrypted save entry. Raises on unknown/changed layouts."""
    structure = get_structure(entry_name, version)
    if structure is None:
        raise ValueError(
            'unknown "%s" format version %d (this build supports up to %d)'
            % (entry_name, version, LATEST_VERSIONS.get(entry_name, 0))
        )

    parsed = Reader(payload).parse_structure(structure)
    # gameRecord is a bare composite type; unwrap it to the song dict.
    if entry_name == "gameRecord":
        return parsed.get("gameRecord", parsed) if isinstance(parsed, dict) else parsed
    return parsed


def build_entry(entry_name: str, version: int, value: Dict[str, Any]) -> bytes:
    """Serializes one save entry back to its decrypted binary payload."""
    structure = get_structure(entry_name, version)
    if structure is None:
        raise ValueError('cannot serialize unknown "%s" version %d' % (entry_name, version))

    if entry_name == "gameRecord":
        value = {"gameRecord": value}
    return bytes(Writer().build_structure(structure, value))


def build_summary(summary: Dict[str, Any]) -> bytes:
    """Serializes the LeanCloud summary blob."""
    return bytes(Writer().build_structure(SummaryStruct, summary))


def parse_summary(payload: bytes) -> Dict[str, Any]:
    """Deserializes the LeanCloud summary blob."""
    return Reader(payload).parse_structure(SummaryStruct)
