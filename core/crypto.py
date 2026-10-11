"""
Phigros Save Crypto Module
Handles AES-256-CBC encryption and decryption compatible with Phigros and Phi-LocalAction-python.
"""
from base64 import b64decode, b64encode
from urllib.parse import unquote, quote
from Crypto.Cipher import AES
from Crypto.Util import Padding
from typing import Tuple

# AES-256 CBC key and IV used by Phigros Unity PlayerPrefs (save.xml)
LOCAL_AES_KEY = bytes.fromhex("627ff1942185e011c815e81e639b9a00001c766b826c29bd96578589f19a6fd6")
LOCAL_AES_IV = bytes.fromhex("be56167f83da3befeff81861a5c5f3cd")
AES_KEY = LOCAL_AES_KEY
AES_IV = LOCAL_AES_IV

# AES-256 CBC key and IV used by Phigros Cloud Saves (.save ZIP entries)
# Verified from wms26/Phi-CloudAction-python and community reverse engineering
CLOUD_AES_KEY = b64decode("6Jaa0qVAJZuXkZCLiOa/Ax5tIZVu+taKUN1V1nqwkks=")
CLOUD_AES_IV = b64decode("Kk/wisgNYwcAV8WVGMgyUw==")


def decrypt_bytes(raw_bytes: bytes) -> bytes:
    """
    Decrypts raw AES-256-CBC ciphertext bytes using Phigros local PlayerPrefs key and IV.
    """
    if not raw_bytes:
        return b""
    cipher = AES.new(AES_KEY, AES.MODE_CBC, AES_IV)
    return Padding.unpad(cipher.decrypt(raw_bytes), AES.block_size)


def encrypt_bytes(cleartext: bytes) -> bytes:
    """
    Encrypts cleartext bytes into AES-256-CBC ciphertext bytes using Phigros local PlayerPrefs key and IV.
    """
    if not cleartext:
        return b""
    pad_bytes = Padding.pad(cleartext, AES.block_size)
    cipher = AES.new(AES_KEY, AES.MODE_CBC, AES_IV)
    return cipher.encrypt(pad_bytes)


def decrypt_cloud_bytes(raw_bytes: bytes) -> bytes:
    """
    Decrypts raw AES-256-CBC ciphertext bytes from Phigros Cloud Save using cloud key and IV.
    """
    if not raw_bytes:
        return b""
    cipher = AES.new(CLOUD_AES_KEY, AES.MODE_CBC, CLOUD_AES_IV)
    return Padding.unpad(cipher.decrypt(raw_bytes), AES.block_size)


def encrypt_cloud_bytes(cleartext: bytes) -> bytes:
    """
    Encrypts cleartext bytes into AES-256-CBC ciphertext bytes for Phigros Cloud Save using cloud key and IV.
    """
    if not cleartext:
        return b""
    pad_bytes = Padding.pad(cleartext, AES.block_size)
    cipher = AES.new(CLOUD_AES_KEY, AES.MODE_CBC, CLOUD_AES_IV)
    return cipher.encrypt(pad_bytes)


def aes_decrypt(data_str: str) -> Tuple[str, bool]:
    """
    Decrypts an AES-CBC encrypted string.
    Returns (decrypted_string, is_encrypted_flag).
    If decryption fails, returns unquoted string and False.
    """
    if not data_str:
        return "", False
    decoded_string = unquote(data_str)
    try:
        raw_bytes = b64decode(decoded_string)
        decrypted_bytes = decrypt_bytes(raw_bytes)
        return decrypted_bytes.decode("utf-8"), True
    except Exception:
        return decoded_string, False


def aes_encrypt(data_str: str) -> str:
    """
    Encrypts a plain string into Phigros PlayerPrefs compatible URL-encoded Base64 string.
    """
    if not data_str:
        return ""
    encrypted_bytes = encrypt_bytes(data_str.encode("utf-8"))
    b64_str = b64encode(encrypted_bytes).decode("ascii")
    # Phigros URL-encodes the base64 string, including converting '/' to '%2F'
    return quote(b64_str, safe="").replace("/", "%2F")
