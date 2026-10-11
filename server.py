"""
The Universal Phigros Save Manager (UPSM) Server
Starlette-based backend providing REST API and serving WebUI frontend.
"""
import os
import sys
import json
import copy
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import HTMLResponse, JSONResponse, Response

# Add current directory to sys.path so core can be imported cleanly
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.save_parser import PhigrosSave
from core.song_helper import DIFFICULTIES, RATING_PRESETS
from core.apk_parser import parse_apk, load_database, clear_database
from core.cloud_service import extract_session_token, PhigrosCloudClient
from core import taptap_login
from core.credentials import (
    load_session_token,
    save_session_token,
    clear_credentials,
    has_credentials,
)
from core.cloud_save_parser import (
    CloudSavePackage,
    cloud_package_to_data_map,
    apply_data_map_to_package,
    editor_save_to_cloud_package_bytes,
    build_summary_for_package,
    compute_rks,
)

# Global PhigrosSave instance
current_save = PhigrosSave()

# Global Cloud Save state
cloud_state = {
    "client": None,
    "user": None,
    "saves": [],
    "selected_profile": None,
    "raw_save_bytes": None,
    "parsed_package": None
}

# Optional initial save path via environment variable (e.g. PHIGROS_SAVE_PATH)
initial_save_path = os.environ.get("PHIGROS_SAVE_PATH", None)
if initial_save_path and os.path.exists(initial_save_path):
    try:
        current_save.unpack_any(initial_save_path)
        print(f"[Server] Loaded initial save from environment: {initial_save_path}")
    except Exception as e:
        print(f"[Server] Failed to load initial save: {e}")


# Pending QR login sessions, keyed by the device code we hand the browser.
qr_login_state: Dict[str, Dict[str, Any]] = {}


def connect_with_token(token: str, persist: bool = True) -> Dict[str, Any]:
    """
    Authenticates with a sessionToken and installs it into cloud_state.

    Saving the token is what lets the program reconnect on the next start,
    replacing the old habit of scanning the working directory for a
    .userdata file dropped there by the player.
    """
    client = PhigrosCloudClient(token)
    user = client.get_current_user()
    saves = client.read_remote_saves()

    cloud_state["client"] = client
    cloud_state["user"] = user
    cloud_state["saves"] = saves
    cloud_state["selected_profile"] = saves[0] if saves else None
    cloud_state["raw_save_bytes"] = None
    cloud_state["parsed_package"] = None

    if persist:
        try:
            save_session_token(token, user)
        except Exception as e:
            print(f"[Server] Could not save sessionToken: {e}")

    return user


# Reconnect on startup using the token we saved last time.
_saved_token = load_session_token()
if _saved_token:
    try:
        _user_info = connect_with_token(_saved_token, persist=False)
        _disp = _user_info.get("nickname") or _user_info.get("username")
        print(f"[Server] Reconnected to TapTap Cloud for user: {_disp}")
    except Exception as e:
        print(f"[Server] Saved sessionToken is no longer valid: {e}")


# ============================================================
#  Cloud helpers
# ============================================================

def require_catalog():
    """
    Cloud features need the APK-extracted song catalog (for RKS, chart
    constants and unlock names). Returns a JSONResponse carrying
    need_apk=True when it is missing, so the UI can raise the APK
    import dialog; returns None when the catalog is present.
    """
    db = load_database()
    if db.get("total_songs", 0) > 0:
        return None
    return JSONResponse({
        "status": "error",
        "need_apk": True,
        "message": "尚未导入 APK 曲库数据。云端功能需要曲库来计算 RKS 与识别曲目，请先上传 Phigros APK 提取曲库。",
    }, status_code=428)


def write_backup(save_bytes, prefix: str):
    """Writes a timestamped backup under backups/ and returns its filename."""
    if not save_bytes:
        return None
    backup_dir = os.path.join(BASE_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    username = (cloud_state.get("user") or {}).get("username", "player")
    filename = f"{prefix}_{username}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.save"
    with open(os.path.join(backup_dir, filename), "wb") as f:
        f.write(save_bytes)
    return filename


def refresh_profiles(client):
    """Re-reads the remote profile list and reselects the newest save."""
    saves = client.read_remote_saves()
    cloud_state["saves"] = saves
    if saves:
        cloud_state["selected_profile"] = saves[0]
    return cloud_state.get("selected_profile")


def pull_remote_package():
    """
    Downloads the current remote .save and parses it, refreshing
    cloud_state. This is the authoritative starting point for every
    upload, so local edits are always merged onto live remote data.
    """
    client = cloud_state.get("client")
    if not client:
        raise RuntimeError("尚未连接云端账号")

    profile = cloud_state.get("selected_profile") or refresh_profiles(client)
    if not profile:
        raise RuntimeError("未在云端找到有效的游戏档案")

    file_url = (profile.get("gameFile") or {}).get("url")
    if not file_url:
        raise RuntimeError("云端档案缺少 gameFile 下载链接")

    save_bytes = client.download_save_bytes(file_url)
    package = CloudSavePackage(save_bytes)
    cloud_state["raw_save_bytes"] = save_bytes
    cloud_state["parsed_package"] = package
    return package


def load_package_into_editor(pkg, raw_bytes, source_label):
    """
    Loads a cloud-format package (.save) into the editor.

    The package is kept on `current_save` so the editor knows it is holding
    a cloud-shaped save: that is what unlocks the gameProgress editor and
    the raw .save download, regardless of whether the file came from the
    network or from the user's disk.
    """
    data_map = cloud_package_to_data_map(pkg)
    current_save.data_map = data_map
    current_save.raw_items = list(data_map.values())
    current_save._cloud_package = pkg
    current_save._cloud_raw_bytes = raw_bytes
    current_save._cloud_parsed = copy.deepcopy(pkg.parsed)
    current_save.source_path = source_label
    current_save._index_songs()
    current_save.modified = False
    return data_map


def editor_package():
    """The cloud package currently loaded in the editor, or None."""
    return getattr(current_save, "_cloud_package", None)


def active_package(require_remote=False):
    """
    Resolves which package an operation should act on.

    `require_remote=True` always re-downloads, which is what every upload
    path needs: edits must land on top of live remote data, never on a
    stale copy.
    """
    if require_remote:
        return pull_remote_package()
    return editor_package() or cloud_state.get("parsed_package")


def editor_save_bytes():
    """
    Serialises the editor's current state as a cloud .save archive.

    Edits made in the table are merged into the package before packing, so
    the downloaded file matches what the user sees on screen.
    """
    pkg = editor_package()
    if pkg is None:
        return None
    working = copy.deepcopy(pkg)
    try:
        apply_data_map_to_package(working, current_save.data_map)
        return working.repack()
    except Exception:
        # Fall back to the bytes we loaded, rather than emit a broken archive.
        return getattr(current_save, "_cloud_raw_bytes", None)


def looks_like_cloud_save(content: bytes, filename: str) -> bool:
    """A cloud save is a ZIP archive; "PK" is its magic number."""
    return content[:2] == b"PK" or filename.lower().endswith(".save")


def load_save_content(content: bytes, filename: str):
    """
    Loads any supported save format into the editor.

    Accepts all three interchangeably — encrypted PlayerPrefs XML, decrypted
    JSON, and the cloud .save archive — so a save can enter the editor from
    any source and leave in any format.

    Returns (payload_dict, error_response). Exactly one is non-None.
    """
    if looks_like_cloud_save(content, filename):
        # .save carries RKS-bearing records, so the catalog must be present.
        guard = require_catalog()
        if guard:
            return None, guard
        pkg = CloudSavePackage(content)
        load_package_into_editor(pkg, content, "云格式存档 (%s)" % filename)
        message = (
            "成功解包解密！格式：Phigros 云存档 (.save)，已导入 %d 首曲目。"
            % len(current_save.songs)
        )
        if not pkg.progress_supported:
            message += "（gameProgress 为未知新版本结构，剧情进度编辑已隐藏，上传时将原样保留。）"
        return {
            "status": "ok",
            "message": message,
            "summary": current_save.get_summary(),
            "is_cloud_save": True,
            "progress_supported": pkg.progress_supported,
            "progress_error": pkg.progress_error,
        }, None

    # unpack_any() clears the cloud context for us.
    fmt = current_save.unpack_any(content, filename=filename)
    return {
        "status": "ok",
        "message": "成功解包解密！格式：%s" % fmt,
        "summary": current_save.get_summary(),
        "is_cloud_save": False,
        "progress_supported": False,
        "progress_error": None,
    }, None


async def index(request):
    html_path = os.path.join(BASE_DIR, "web", "index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            content = f.read()
        return HTMLResponse(content)
    return HTMLResponse("<h1>Phigros Save Editor</h1><p>web/index.html not found</p>")


async def api_status(request):
    summary = current_save.get_summary()
    has_save = len(current_save.data_map) > 0
    db = load_database()
    # The editor only exposes the gameProgress button when it is holding a
    # cloud-shaped save AND we recognise that save's progress layout.
    ed_pkg = editor_package()
    return JSONResponse({
        "status": "ok",
        "has_save": has_save,
        "summary": summary,
        "is_cloud_save": ed_pkg is not None,
        "progress_supported": bool(ed_pkg is not None and ed_pkg.progress_supported),
        "progress_error": (ed_pkg.progress_error if ed_pkg is not None else None),
        "presets": RATING_PRESETS,
        "difficulties": DIFFICULTIES,
        "apk_status": {
            "has_database": db.get("total_songs", 0) > 0,
            "version": db.get("version", "未导入 APK"),
            "total_songs": db.get("total_songs", 0),
            "collections": len(db.get("collections", [])),
            "avatars": len(db.get("avatars", [])),
            "single_keys": len(db.get("single_keys", [])),
            "illustration_keys": len(db.get("illustration_keys", []))
        },
        "cloud_status": {
            "has_saved_token": has_credentials(),
            "is_connected": bool(cloud_state.get("client")),
            "user": cloud_state.get("user"),
            "selected_profile": cloud_state.get("selected_profile")
        }
    })


async def api_apk_status(request):
    db = load_database()
    return JSONResponse({
        "status": "ok",
        "has_database": db.get("total_songs", 0) > 0,
        "version": db.get("version", "未导入 APK"),
        "total_songs": db.get("total_songs", 0),
        "collections": len(db.get("collections", [])),
        "avatars": len(db.get("avatars", [])),
        "single_keys": len(db.get("single_keys", [])),
        "illustration_keys": len(db.get("illustration_keys", []))
    })


async def api_songs(request):
    if len(current_save.data_map) == 0:
        return JSONResponse({
            "status": "ok",
            "total": 0,
            "songs": []
        })
    songs_data = current_save.get_songs_data()
    return JSONResponse({
        "status": "ok",
        "total": len(songs_data),
        "songs": songs_data
    })


async def api_analysis(request):
    """
    Offline RKS analysis of whatever save the editor holds.

    Deliberately independent of the cloud: it needs no login and no network,
    because analysis is a property of the save, not of the account. Chart
    constants come from the user's own APK import.
    """
    if len(current_save.data_map) == 0:
        return JSONResponse({
            "status": "error",
            "message": "尚未载入存档，无法进行数据分析。"
        }, status_code=400)
    try:
        analysis = current_save.build_analysis()
    except Exception as e:
        return JSONResponse({"status": "error", "message": f"分析失败：{e}"}, status_code=500)

    if not analysis.get("catalog_ready"):
        # Same 428 + need_apk shape the other catalog-dependent features use,
        # so the frontend's handleNeedApk() raises the APK import dialog
        # instead of showing a screen full of zeros.
        return JSONResponse({
            "status": "error",
            "need_apk": True,
            "message": analysis.get("message", "尚未导入 APK 曲库数据，无法计算 RKS。"),
        }, status_code=428)

    return JSONResponse({"status": "ok", **analysis})


async def api_load_path(request):
    try:
        data = await request.json()
        path = data.get("path", "").strip()
        if not path or not os.path.exists(path):
            return JSONResponse({"status": "error", "message": f"文件不存在：{path}"}, status_code=404)
        with open(path, "rb") as f:
            content = f.read()
        payload, error = load_save_content(content, os.path.basename(path))
        if error:
            return error
        if not payload.get("is_cloud_save"):
            current_save.source_path = path
        return JSONResponse(payload)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


async def api_upload(request):
    try:
        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" in content_type:
            form = await request.form()
            upload_file = form.get("file")
            if not upload_file:
                return JSONResponse({"status": "error", "message": "未找到上传的文件"}, status_code=400)
            content = await upload_file.read()
            filename = upload_file.filename or "uploaded_save.xml"
        else:
            content = await request.body()
            filename = request.headers.get("x-filename", "uploaded_save.xml")

        # 1. A pasted/uploaded credential file still works as a manual
        #    fallback for QR login; the token it carries is saved like any
        #    other, rather than being re-read from disk on every start.
        text_preview = content.decode("utf-8", errors="ignore")
        tok = extract_session_token(text_preview)
        if tok and ("sessionToken" in text_preview or "authData" in text_preview or "_User" in text_preview or "userdata" in filename.lower()):
            user = connect_with_token(tok)
            client = cloud_state["client"]

            # Download save bytes automatically for immediate inspection
            if cloud_state["selected_profile"]:
                game_file_url = cloud_state["selected_profile"].get("gameFile", {}).get("url")
                if game_file_url:
                    try:
                        save_bytes = client.download_save_bytes(game_file_url)
                        cloud_state["raw_save_bytes"] = save_bytes
                        cloud_state["parsed_package"] = CloudSavePackage(save_bytes)
                    except Exception:
                        pass

            display_name = user.get("nickname") or user.get("username", "已登录玩家")
            return JSONResponse({
                "status": "ok",
                "is_cloud": True,
                "message": f"成功识别登录凭证！已连接玩家：{display_name}（凭证已保存，下次启动自动登录）",
                "token": tok,
                "user": user,
                "saves": cloud_state["saves"],
                "selected_profile": cloud_state["selected_profile"]
            })

        # 2. Any save format: XML, JSON or cloud .save.
        payload, error = load_save_content(content, filename)
        if error:
            return error
        return JSONResponse(payload)
    except Exception as e:
        return JSONResponse({"status": "error", "message": f"上传处理失败：{str(e)}"}, status_code=500)


async def api_update_record(request):
    if len(current_save.data_map) == 0:
        return JSONResponse({"status": "error", "message": "请先上传或载入存档文件"}, status_code=400)
    try:
        data = await request.json()
        song_id = data.get("song_id")
        diff = data.get("diff")
        score = int(data.get("score", 0))
        acc = float(data.get("acc", 0.0))
        c = int(data.get("c", 0))

        if not song_id or not diff:
            return JSONResponse({"status": "error", "message": "参数缺失：song_id 或 diff"}, status_code=400)

        current_save.update_record(song_id, diff, score, acc, c)
        updated_rec = current_save.songs[song_id][diff]
        return JSONResponse({
            "status": "ok",
            "message": f"已更新 {song_id} [{diff}] 记录",
            "record": updated_rec
        })
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


async def api_delete_record(request):
    if len(current_save.data_map) == 0:
        return JSONResponse({"status": "error", "message": "请先上传或载入存档文件"}, status_code=400)
    try:
        data = await request.json()
        song_id = data.get("song_id")
        diff = data.get("diff")
        if not song_id or not diff:
            return JSONResponse({"status": "error", "message": "参数缺失：song_id 或 diff"}, status_code=400)

        ok = current_save.delete_record(song_id, diff)
        return JSONResponse({
            "status": "ok",
            "message": f"已清除 {song_id} [{diff}] 记录" if ok else "未找到该难度记录"
        })
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


async def api_batch_records(request):
    if len(current_save.data_map) == 0:
        return JSONResponse({"status": "error", "message": "请先上传或载入存档文件"}, status_code=400)
    try:
        data = await request.json()
        diff_target = data.get("diff_target", "ALL")
        preset = data.get("preset", "phi")
        custom_score = data.get("custom_score")
        custom_acc = data.get("custom_acc")
        custom_c = data.get("custom_c")
        song_ids = data.get("song_ids")

        count = current_save.batch_update_records(
            diff_target=diff_target,
            preset=preset,
            custom_score=int(custom_score) if custom_score is not None else None,
            custom_acc=float(custom_acc) if custom_acc is not None else None,
            custom_c=int(custom_c) if custom_c is not None else None,
            song_ids=song_ids
        )
        return JSONResponse({
            "status": "ok",
            "message": f"批量修改成功！共更新了 {count} 条难度记录",
            "count": count
        })
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


async def api_batch_unlock(request):
    if len(current_save.data_map) == 0:
        return JSONResponse({"status": "error", "message": "请先上传或载入存档文件"}, status_code=400)
    try:
        data = await request.json()
        counts = current_save.batch_unlock(
            unlock_songs=data.get("unlock_songs", True),
            unlock_collections=data.get("unlock_collections", True),
            unlock_illustrations=data.get("unlock_illustrations", True),
            unlock_avatars=data.get("unlock_avatars", True),
            unlock_grades=data.get("unlock_grades", True)
        )
        msg = f"全解锁成功！单曲+{counts['songs']}, 收藏品+{counts['collections']}, 曲绘+{counts['illustrations']}, 头像+{counts['avatars']}, Grade+{counts['grades']}"
        return JSONResponse({
            "status": "ok",
            "message": msg,
            "counts": counts
        })
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


async def api_currencies(request):
    if len(current_save.data_map) == 0:
        return JSONResponse({"status": "error", "message": "请先上传或载入存档文件"}, status_code=400)
    try:
        data = await request.json()
        money_dict = data.get("money", {})
        challenge_rank = data.get("challenge_rank")
        current_save.update_currencies_and_settings(money_dict, challenge_rank)
        return JSONResponse({
            "status": "ok",
            "message": "已成功更新货币与课题分！"
        })
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


async def api_add_song(request):
    if len(current_save.data_map) == 0:
        return JSONResponse({"status": "error", "message": "请先上传或载入存档文件"}, status_code=400)
    try:
        data = await request.json()
        song_id = data.get("song_id", "").strip()
        if not song_id:
            return JSONResponse({"status": "error", "message": "曲目ID不能为空"}, status_code=400)
        added = current_save.add_song(song_id)
        if added:
            return JSONResponse({"status": "ok", "message": f"已添加曲目：{song_id}"})
        else:
            return JSONResponse({"status": "error", "message": f"曲目已存在：{song_id}"}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


async def api_export_xml(request):
    if len(current_save.data_map) == 0:
        return JSONResponse({"status": "error", "message": "请先上传或载入存档文件"}, status_code=400)
    try:
        xml_bytes = current_save.export_xml()
        return Response(
            xml_bytes,
            media_type="application/xml",
            headers={"Content-Disposition": 'attachment; filename="com.PigeonGames.Phigros.v2.playerprefs.xml"'}
        )
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


async def api_export_json(request):
    if len(current_save.data_map) == 0:
        return JSONResponse({"status": "error", "message": "请先上传或载入存档文件"}, status_code=400)
    try:
        json_str = current_save.export_json()
        return Response(
            json_str.encode("utf-8"),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="phigros_decrypted_save.json"'}
        )
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


async def api_upload_apk(request):
    try:
        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" in content_type:
            form = await request.form()
            upload_file = form.get("file")
            if not upload_file:
                return JSONResponse({"status": "error", "message": "未找到上传的 APK 文件"}, status_code=400)
            content = await upload_file.read()
        else:
            content = await request.body()
            if not content:
                return JSONResponse({"status": "error", "message": "上传内容为空"}, status_code=400)

        # Parse APK
        db, stats = parse_apk(content)
        return JSONResponse({
            "status": "ok",
            "message": f"APK 解析提取成功！全曲库已更新：共 {stats['total_songs']} 首曲目、{stats['collections']} 项收藏品、{stats['avatars']} 个头像、{stats['illustration_keys']} 张曲绘",
            "stats": stats
        })
    except Exception as e:
        return JSONResponse({"status": "error", "message": f"APK 解析失败：{str(e)}"}, status_code=500)


async def api_load_apk_path(request):
    try:
        data = await request.json()
        path = data.get("path", "").strip()
        if not path or not os.path.exists(path):
            return JSONResponse({"status": "error", "message": f"文件不存在：{path}"}, status_code=404)
        db, stats = parse_apk(path)
        return JSONResponse({
            "status": "ok",
            "message": f"APK 解析提取成功！全曲库已更新：共 {stats['total_songs']} 首曲目、{stats['collections']} 项收藏品、{stats['avatars']} 个头像、{stats['illustration_keys']} 张曲绘",
            "stats": stats
        })
    except Exception as e:
        return JSONResponse({"status": "error", "message": f"APK 解析失败：{str(e)}"}, status_code=500)


async def api_clear_database(request):
    try:
        ok = clear_database()
        if ok:
            return JSONResponse({
                "status": "ok",
                "message": "本地曲库数据库已成功清除！"
            })
        else:
            return JSONResponse({"status": "error", "message": "清除本地数据库失败"}, status_code=500)
    except Exception as e:
        return JSONResponse({"status": "error", "message": f"清除数据库失败：{str(e)}"}, status_code=500)


async def api_cloud_connect(request):
    try:
        data = {}
        if request.headers.get("content-length", "0") != "0":
            try:
                data = await request.json()
            except Exception:
                data = {}

        raw_input = (data.get("token") or data.get("token_or_userdata") or "").strip()
        token = extract_session_token(raw_input) if raw_input else None

        # No token supplied: fall back to the one we saved last time.
        if not token:
            token = load_session_token()

        if not token:
            return JSONResponse({
                "status": "error",
                "message": "未能识别出有效的 sessionToken。请使用扫码登录，或手动粘贴 sessionToken。"
            }, status_code=400)

        user = connect_with_token(token)
        display_name = user.get("nickname") or user.get("username", "未知用户")
        return JSONResponse({
            "status": "ok",
            "message": f"成功连接至 LeanCloud！玩家：{display_name}（登录凭证已保存，下次启动自动登录）",
            "token": token,
            "user": user,
            "saves": cloud_state["saves"],
            "selected_profile": cloud_state["selected_profile"]
        })
    except Exception as e:
        return JSONResponse({"status": "error", "message": f"云端连接失败：{str(e)}"}, status_code=500)


async def api_cloud_qr_start(request):
    """
    Starts a TapTap QR login.

    Returns the QR code as inline SVG plus the device code the browser
    polls with. The player scans it in the TapTap app; nothing is stored
    until they approve.
    """
    try:
        qr = taptap_login.request_qrcode()
        device_code = qr["device_code"]
        qr_login_state[device_code] = qr

        # Drop anything that has already expired, so the dict cannot grow
        # without bound across a long session.
        now = time.time()
        for code, entry in list(qr_login_state.items()):
            if entry.get("_created", now) + entry.get("expires_in", 300) < now:
                qr_login_state.pop(code, None)
        qr["_created"] = now

        return JSONResponse({
            "status": "ok",
            "device_code": device_code,
            "qrcode_url": qr.get("qrcode_url") or qr.get("verification_url"),
            "qrcode_svg": taptap_login.render_qrcode_svg(qr["qrcode_url"]),
            "interval": qr.get("interval", 5),
            "expires_in": qr.get("expires_in", 300),
            "message": "请使用 TapTap App 扫描二维码并确认登录。",
        })
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "message": f"获取登录二维码失败：{str(e)}",
        }, status_code=500)


async def api_cloud_qr_poll(request):
    """
    Checks whether a QR login has been approved.

    On approval this completes the whole exchange: TapTap profile ->
    LeanCloud sessionToken -> saved credential -> connected client.
    """
    try:
        data = await request.json()
        device_code = (data.get("device_code") or "").strip()
        qr = qr_login_state.get(device_code)
        if not qr:
            return JSONResponse({
                "status": "error",
                "state": "expired",
                "message": "登录会话不存在或已过期，请重新获取二维码。",
            }, status_code=400)

        result = taptap_login.poll_qrcode(qr)
        state = result["state"]

        if state == "pending":
            return JSONResponse({"status": "ok", "state": "pending"})

        if state in ("denied", "expired"):
            qr_login_state.pop(device_code, None)
            return JSONResponse({
                "status": "ok",
                "state": state,
                "message": result.get("message", "登录未完成。"),
            })

        # Approved: finish the exchange and persist the credential.
        qr_login_state.pop(device_code, None)
        record = taptap_login.complete_login(qr, result["token"])
        token = record["sessionToken"]
        user = connect_with_token(token)

        display_name = user.get("nickname") or user.get("username", "未知用户")
        return JSONResponse({
            "status": "ok",
            "state": "ok",
            "message": f"扫码登录成功！玩家：{display_name}（凭证已保存，下次启动自动登录）",
            "token": token,
            "user": user,
            "saves": cloud_state["saves"],
            "selected_profile": cloud_state["selected_profile"],
        })
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "state": "error",
            "message": f"扫码登录失败：{str(e)}",
        }, status_code=500)


async def api_catalog_options(request):
    """
    Lists the avatar and illustration IDs available for the name card.

    Everything here is read out of the user's locally-extracted APK
    database; the program ships no game data of its own.
    """
    db = load_database()
    return JSONResponse({
        "status": "ok",
        "avatars": db.get("avatars", []),
        "illustrations": db.get("illustration_keys", []),
        "songs": sorted((db.get("songs") or {}).keys()),
    })


async def api_cloud_download(request):
    """Pulls the remote .save, backs it up locally, and parses it."""
    guard = require_catalog()
    if guard:
        return guard
    if not cloud_state.get("client") or not cloud_state.get("selected_profile"):
        return JSONResponse({"status": "error", "message": "尚未连接云端或无可用存档"}, status_code=400)
    try:
        pkg = pull_remote_package()
        backup_name = write_backup(cloud_state["raw_save_bytes"], "cloud_backup")
        return JSONResponse({
            "status": "ok",
            "message": f"云存档下载成功！已自动备份至 backups/{backup_name}",
            "backup_file": backup_name,
            "summary": pkg.get_inspect_summary(),
            "progress_supported": pkg.progress_supported,
            "progress_error": pkg.progress_error,
        })
    except Exception as e:
        return JSONResponse({"status": "error", "message": f"下载云存档失败：{str(e)}"}, status_code=500)


async def api_cloud_export_save(request):
    """
    Downloads the current save as an encrypted .save archive.

    Prefers the editor's live state (so table edits are included); falls
    back to the last raw archive pulled from the cloud.
    """
    save_bytes = editor_save_bytes() or cloud_state.get("raw_save_bytes")
    if not save_bytes:
        return JSONResponse({"status": "error", "message": "当前没有云格式存档，请先从云端载入或上传 .save 文件"}, status_code=400)
    return Response(
        save_bytes,
        media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="com.PigeonGames.Phigros.save"'}
    )


async def api_cloud_export_json(request):
    """Downloads the fully decrypted save as JSON."""
    pkg = cloud_state.get("parsed_package")
    if not pkg:
        return JSONResponse({"status": "error", "message": "尚未下载或解析云存档"}, status_code=400)
    return Response(
        json.dumps(pkg.export_decrypted_json(), ensure_ascii=False, indent=2).encode("utf-8"),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="phigros_cloud_save_decrypted.json"'}
    )


async def api_cloud_export_records_json(request):
    """Downloads just gameRecord + gameKey as decrypted JSON."""
    pkg = cloud_state.get("parsed_package")
    if not pkg:
        return JSONResponse({"status": "error", "message": "尚未下载或解析云存档"}, status_code=400)
    return Response(
        json.dumps(pkg.export_records_json(), ensure_ascii=False, indent=2).encode("utf-8"),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="phigros_gameRecord_gameKey.json"'}
    )


async def api_cloud_profile(request):
    """
    Reads (GET) or updates (POST) the player's name card and game settings.
    Covers selfIntro / avatar / background plus the full settings block.
    """
    guard = require_catalog()
    if guard:
        return guard
    client = cloud_state.get("client")

    try:
        # Reads work from whatever save the editor holds (cloud or local
        # .save). Writes always re-pull, so they land on live remote data.
        if request.method == "POST":
            if not client:
                return JSONResponse({"status": "error", "message": "尚未连接云端账号，请先连接"}, status_code=400)
            pkg = pull_remote_package()
        else:
            pkg = active_package()
            if pkg is None:
                if not client:
                    return JSONResponse({"status": "error", "message": "尚未载入任何云格式存档"}, status_code=400)
                pkg = pull_remote_package()

        if request.method == "POST":
            data = await request.json()
            if data.get("user"):
                pkg.update_user(data["user"])
            if data.get("settings"):
                pkg.update_settings(data["settings"])

            if data.get("upload", True):
                new_save = pkg.repack()
                summary_bytes = build_summary_for_package(
                    pkg, (cloud_state.get("selected_profile") or {}).get("parsed_summary")
                )
                write_backup(cloud_state.get("raw_save_bytes"), "pre_profile_backup")
                client.upload_save(new_save, summary_bytes, cloud_state["selected_profile"])
                cloud_state["raw_save_bytes"] = new_save
                refresh_profiles(client)

        return JSONResponse({
            "status": "ok",
            "message": "个人资料已保存并同步至云端" if request.method == "POST" else "ok",
            "user": pkg.user(),
            "settings": pkg.settings(),
        })
    except Exception as e:
        return JSONResponse({"status": "error", "message": f"个人资料操作失败：{str(e)}"}, status_code=500)


async def api_cloud_progress(request):
    """
    Reads (GET) or updates (POST) gameProgress.

    When the entry uses a layout this build does not recognise, the story
    has moved on ahead of us: we report supported=false so the UI hides the
    editor, and the original bytes are preserved on every repack.
    """
    guard = require_catalog()
    if guard:
        return guard
    client = cloud_state.get("client")

    try:
        if request.method == "POST":
            if not client:
                return JSONResponse({"status": "error", "message": "尚未连接云端账号，请先连接"}, status_code=400)
            pkg = pull_remote_package()
        else:
            pkg = active_package()
            if pkg is None:
                if not client:
                    return JSONResponse({"status": "error", "message": "尚未载入任何云格式存档"}, status_code=400)
                pkg = pull_remote_package()

        if not pkg.progress_supported:
            return JSONResponse({
                "status": "ok",
                "supported": False,
                "progress": None,
                "message": (
                    "检测到 gameProgress 为本程序未知的新版本结构"
                    f"（{pkg.progress_error}）。游戏剧情内容可能已更新，"
                    "剧情进度编辑功能已自动隐藏。其余部分（成绩、解锁、设置）不受影响，"
                    "上传时该部分会原样保留，不会损坏存档。"
                ),
            })

        if request.method == "POST":
            data = await request.json()
            pkg.update_progress(data.get("progress") or {})

            if data.get("upload", True):
                new_save = pkg.repack()
                summary_bytes = build_summary_for_package(
                    pkg, (cloud_state.get("selected_profile") or {}).get("parsed_summary")
                )
                write_backup(cloud_state.get("raw_save_bytes"), "pre_progress_backup")
                client.upload_save(new_save, summary_bytes, cloud_state["selected_profile"])
                cloud_state["raw_save_bytes"] = new_save
                refresh_profiles(client)

        return JSONResponse({
            "status": "ok",
            "supported": True,
            "version": pkg.versions.get("gameProgress"),
            "progress": pkg.game_progress(),
            "field_types": pkg.progress_field_types(),
            "bits_widths": pkg.progress_bits_widths(),
            "message": "剧情进度已保存并同步至云端" if request.method == "POST" else "ok",
        })
    except Exception as e:
        return JSONResponse({"status": "error", "message": f"剧情进度操作失败：{str(e)}"}, status_code=500)


async def api_cloud_pull_to_editor(request):
    """Downloads the remote save and loads gameRecord + gameKey into the editor."""
    guard = require_catalog()
    if guard:
        return guard
    client = cloud_state.get("client")
    if not client:
        return JSONResponse({"status": "error", "message": "尚未连接云端账号，请先连接"}, status_code=400)

    try:
        pkg = pull_remote_package()
        backup_name = write_backup(cloud_state["raw_save_bytes"], "cloud_pull_backup")

        load_package_into_editor(pkg, cloud_state["raw_save_bytes"], "云端存档 (TapTap Cloud Save)")

        message = f"成功从云端载入档案！已导入 {len(current_save.songs)} 首曲目，可直接在下方编辑。"
        if not pkg.progress_supported:
            message += "（注意：gameProgress 结构为未知新版本，剧情进度编辑已隐藏，上传时将原样保留。）"

        return JSONResponse({
            "status": "ok",
            "message": message,
            "backup_file": backup_name,
            "summary": current_save.get_summary(),
            "is_cloud_save": True,
            "progress_supported": pkg.progress_supported,
            "progress_error": pkg.progress_error,
        })
    except Exception as e:
        return JSONResponse({"status": "error", "message": f"从云端载入档案失败：{str(e)}"}, status_code=500)


async def api_cloud_push_from_editor(request):
    """
    Uploads the editor's current save to the cloud.

    Always pulls the live remote save first and merges gameRecord + gameKey
    onto it, so a local XML/JSON save (which carries no settings, user or
    gameProgress) can be pushed without destroying those sections.
    """
    guard = require_catalog()
    if guard:
        return guard
    client = cloud_state.get("client")
    if not client:
        return JSONResponse({"status": "error", "message": "尚未连接云端账号，请先连接并登录"}, status_code=400)
    if not current_save.data_map:
        return JSONResponse({"status": "error", "message": "当前编辑器中没有已加载的存档，无法上传"}, status_code=400)

    try:
        # Step 1: always start from the authoritative remote state.
        base_pkg = pull_remote_package()
        backup_name = write_backup(cloud_state["raw_save_bytes"], "pre_push_backup")

        # Step 2: project local gameRecord + gameKey onto it and repack.
        new_save_bytes, summary_bytes, stats = editor_save_to_cloud_package_bytes(
            save=current_save,
            base_pkg=base_pkg,
            base_summary=(cloud_state.get("selected_profile") or {}).get("parsed_summary"),
        )

        # Step 3: upload.
        client.upload_save(new_save_bytes, summary_bytes, cloud_state["selected_profile"])

        cloud_state["raw_save_bytes"] = new_save_bytes
        cloud_state["parsed_package"] = CloudSavePackage(new_save_bytes)
        refresh_profiles(client)
        current_save.modified = False

        message = (
            "🎉 存档已成功同步上传至 TapTap 云端！"
            f"（合并 {stats['records']} 条成绩、{stats['keys']} 项解锁）"
            "请打开游戏点击【设置 → 云存档 → 从云端同步】下载覆盖。"
        )
        if not base_pkg.progress_supported:
            message += "（gameProgress 为未知新版本结构，已原样保留未作改动。）"

        return JSONResponse({
            "status": "ok",
            "message": message,
            "stats": stats,
            "backup_file": backup_name,
            "selected_profile": cloud_state.get("selected_profile"),
        })
    except Exception as e:
        return JSONResponse({"status": "error", "message": f"上传至云端失败：{str(e)}"}, status_code=500)


async def api_cloud_logout(request):
    """
    Logs out of the cloud account.

    Also deletes the saved sessionToken, so logging out really means
    logging out: the next start will not silently reconnect.
    """
    cloud_state["client"] = None
    cloud_state["user"] = None
    cloud_state["saves"] = []
    cloud_state["selected_profile"] = None
    cloud_state["raw_save_bytes"] = None
    cloud_state["parsed_package"] = None
    qr_login_state.clear()
    removed = clear_credentials()
    return JSONResponse({
        "status": "ok",
        "message": "已成功登出云端账号" + ("，并已删除本地保存的登录凭证" if removed else ""),
    })


routes = [
    Route("/", index, methods=["GET"]),
    Route("/api/status", api_status, methods=["GET"]),
    Route("/api/apk_status", api_apk_status, methods=["GET"]),
    Route("/api/songs", api_songs, methods=["GET"]),
    Route("/api/analysis", api_analysis, methods=["GET"]),
    Route("/api/load_path", api_load_path, methods=["POST"]),
    Route("/api/upload", api_upload, methods=["POST"]),
    Route("/api/upload_apk", api_upload_apk, methods=["POST"]),
    Route("/api/load_apk_path", api_load_apk_path, methods=["POST"]),
    Route("/api/clear_database", api_clear_database, methods=["POST"]),
    Route("/api/update_record", api_update_record, methods=["POST"]),
    Route("/api/delete_record", api_delete_record, methods=["POST"]),
    Route("/api/batch_records", api_batch_records, methods=["POST"]),
    Route("/api/batch_unlock", api_batch_unlock, methods=["POST"]),
    Route("/api/currencies", api_currencies, methods=["POST"]),
    Route("/api/add_song", api_add_song, methods=["POST"]),
    Route("/api/export_xml", api_export_xml, methods=["GET"]),
    Route("/api/export_json", api_export_json, methods=["GET"]),
    Route("/api/catalog_options", api_catalog_options, methods=["GET"]),
    Route("/api/cloud/connect", api_cloud_connect, methods=["POST"]),
    Route("/api/cloud/qr_start", api_cloud_qr_start, methods=["POST"]),
    Route("/api/cloud/qr_poll", api_cloud_qr_poll, methods=["POST"]),
    Route("/api/cloud/download", api_cloud_download, methods=["POST"]),
    Route("/api/cloud/pull_to_editor", api_cloud_pull_to_editor, methods=["POST"]),
    Route("/api/cloud/push_from_editor", api_cloud_push_from_editor, methods=["POST"]),
    Route("/api/cloud/logout", api_cloud_logout, methods=["POST"]),
    Route("/api/cloud/export_save", api_cloud_export_save, methods=["GET"]),
    Route("/api/cloud/export_json", api_cloud_export_json, methods=["GET"]),
    Route("/api/cloud/export_records_json", api_cloud_export_records_json, methods=["GET"]),
    Route("/api/cloud/profile", api_cloud_profile, methods=["GET", "POST"]),
    Route("/api/cloud/progress", api_cloud_progress, methods=["GET", "POST"])
]

app = Starlette(debug=True, routes=routes)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"============================================================")
    print(f" The Universal Phigros Save Manager (UPSM) at http://127.0.0.1:{port}")
    print(f"============================================================")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
