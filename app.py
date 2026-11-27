import os
import json
import re
import time
import tempfile
from flask import Flask, request, jsonify, render_template
from locketlam import LocketClient, NoteType
from locketlam.exceptions import LocketError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(BASE_DIR, "locketlam_session.json")
PRESETS_FILE = os.path.join(BASE_DIR, "locketlam", "caption_presets.json")
CUSTOM_META_FILE = os.path.join(BASE_DIR, "user_custom_meta.json")

TEMPLATE_DIR = (
    os.path.join(BASE_DIR, "mẫu")
    if os.path.isdir(os.path.join(BASE_DIR, "mẫu"))
    else os.path.join(BASE_DIR, "templates")
)

app = Flask(__name__, template_folder=TEMPLATE_DIR)
app.config["MAX_CONTENT_LENGTH"] = 120 * 1024 * 1024

DEFAULT_META = {
    "birthday": "",
    "email_display": "",
    "surface_style": "glass",
    "theme_id": "default",
    "lite_mode": "auto",
}

QUICK_CAPTIONS = [
    {"id": "standard", "label": "Thêm một tin nhắn", "icon": "Aa", "colors": [], "text_color": "#FFFFFF"},
    {"id": NoteType.REVIEW, "label": "Review", "icon": "⭐", "colors": ["#2B2A24", "#3B382A"], "text_color": "#FFD43B"},
    {"id": NoteType.LOCATION, "label": "Location", "icon": "📍", "colors": ["#1E252B", "#2C3842"], "text_color": "#FFFFFF"},
    {"id": NoteType.WEATHER, "label": "Weather", "icon": "☁️", "colors": ["#4A6FA5", "#7790A6"], "text_color": "#FFFFFF"},
    {"id": NoteType.TIME, "label": "13:22", "icon": "🕒", "colors": ["#22252A", "#32363E"], "text_color": "#FFFFFF"},
    {"id": NoteType.STREAK, "label": "1", "icon": "🔥", "colors": ["#3A2416", "#5E3519"], "text_color": "#FF9F1C"},
    {"id": NoteType.PARTY_TIME, "label": "Party Time!", "icon": "🪩", "colors": ["#1EA866", "#2BD984"], "text_color": "#FFFFFF"},
    {"id": NoteType.OOTD, "label": "OOTD", "icon": "🕶", "colors": ["#1F2224", "#35393C"], "text_color": "#FFFFFF"},
    {"id": NoteType.MISS_YOU, "label": "Miss you", "icon": "🥰", "colors": ["#E62E4D", "#FF5E7E"], "text_color": "#FFFFFF"},
    {"id": NoteType.DANG_BAN_TUONG_TU, "label": "Đang bận tương tư", "icon": "💭", "colors": ["#FF758C", "#FF7EB3"], "text_color": "#FFFFFF"},
    {"id": NoteType.DOI_ROI, "label": "Dỗi rồi", "icon": "😤", "colors": ["#FF416C", "#FF4B2B"], "text_color": "#FFFFFF"},
    {"id": NoteType.MUON_DUOC_OM, "label": "Muốn được ôm", "icon": "🫂", "colors": ["#667EEA", "#764BA2"], "text_color": "#FFFFFF"},
]

IMAGE_URL_RE = re.compile(r"(https?://[^\s]+(?:firebasestorage\.googleapis\.com[^\s]*|\.(?:webp|png|jpg|jpeg|gif)(?:\?[^\s]*)?))", re.IGNORECASE)


def restore_session_from_env():
    env_json = (os.environ.get("LOCKET_SESSION_JSON") or "").strip()
    if env_json and not os.path.exists(SESSION_FILE):
        try:
            parsed = json.loads(env_json)
            if isinstance(parsed, dict):
                with open(SESSION_FILE, "w", encoding="utf-8") as f:
                    json.dump(parsed, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


restore_session_from_env()
client = LocketClient(session_path=SESSION_FILE)


def with_cache_bust(url: str) -> str:
    if not url:
        return ""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}cb={int(time.time())}"


def load_all_meta() -> dict:
    if os.path.exists(CUSTOM_META_FILE):
        try:
            with open(CUSTOM_META_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}


def get_user_meta(uid: str) -> dict:
    all_m = load_all_meta()
    key = uid or "default"
    entry = all_m.get(key) if isinstance(all_m.get(key), dict) else {}
    merged = dict(DEFAULT_META)
    merged.update(entry)
    return merged


def save_user_meta(uid: str, patch: dict) -> dict:
    all_m = load_all_meta()
    key = uid or "default"
    cur = get_user_meta(key)
    cur.update(patch)
    all_m[key] = cur
    try:
        with open(CUSTOM_META_FILE, "w", encoding="utf-8") as f:
            json.dump(all_m, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return cur


def load_presets() -> list:
    items = list(QUICK_CAPTIONS)
    if os.path.exists(PRESETS_FILE):
        try:
            with open(PRESETS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            seen = {x["id"] for x in items}
            for k, v in raw.items():
                if not isinstance(v, dict):
                    continue
                oid = (v.get("overlay_id") or k).replace("caption:", "")
                if oid in seen or k in seen:
                    continue
                seen.add(oid)
                items.append({
                    "id": oid,
                    "label": v.get("text") or k.replace("_", " ").title(),
                    "icon": v.get("icon") or "",
                    "colors": v.get("colors") or [],
                    "text_color": v.get("text_color") or "#FFFFFF",
                })
                if len(items) >= 32:
                    break
        except Exception:
            pass
    return items


def resolve_conversation_uid(other_user_id: str, provided_conv_id: str = "") -> str:
    if provided_conv_id:
        return provided_conv_id
    if not other_user_id:
        return ""
    try:
        convs = client.chat.get_conversations() or []
        for c in convs:
            if not isinstance(c, dict):
                continue
            peer = c.get("peer_uid") or c.get("other_user_id") or c.get("recipient_id") or ""
            members = c.get("members") or []
            if peer == other_user_id or other_user_id in members:
                cid = c.get("conversation_id") or c.get("id") or ""
                if cid:
                    return cid
    except Exception:
        pass
    return ""


def split_message_media(raw_text: str):
    txt = (raw_text or "").strip()
    match = IMAGE_URL_RE.search(txt)
    if not match:
        return txt, ""
    img_url = match.group(1)
    clean_txt = txt.replace(img_url, "").strip()
    return clean_txt, img_url


@app.route("/")
@app.route("/u/<slug>")
@app.route("/profile/<slug>")
@app.route("/terms")
@app.route("/privacy")
def index(slug=None):
    return render_template("index.html")


@app.errorhandler(404)
def spa_fallback(e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "API endpoint not found"}), 404
    return render_template("index.html"), 200


@app.route("/api/boot", methods=["GET"])
def api_boot():
    authed = client.is_authenticated()
    uid = getattr(client.session, "uid", "") if authed else ""
    meta = get_user_meta(uid)
    profile = {}
    friends = []
    conversations = []

    if authed:
        try:
            p = client.user.get_profile()
            if isinstance(p, dict):
                profile = p
        except Exception:
            profile = {}
        try:
            raw_f = client.friends.get_all_friends() or []
            for item in raw_f:
                if isinstance(item, dict):
                    f_uid = item.get("uid") or item.get("id") or ""
                    fn = item.get("first_name") or item.get("firstName") or ""
                    ln = item.get("last_name") or item.get("lastName") or ""
                    full = f"{fn} {ln}".strip() or item.get("username") or f_uid[:8]
                    av = (
                        item.get("profile_picture_url")
                        or item.get("profile_pic")
                        or item.get("avatar_url")
                        or item.get("photo_url")
                        or ""
                    )
                    if f_uid:
                        friends.append({"uid": f_uid, "name": full, "avatar": av})
        except Exception:
            friends = []
        try:
            conversations = client.chat.get_conversations() or []
        except Exception:
            conversations = []

    username = profile.get("username") or (uid[:10] if uid else "")
    raw_avatar = (
        profile.get("profile_picture_url")
        or profile.get("profile_pic")
        or profile.get("avatar_url")
        or ""
    )
    share_slug = username or uid or "profile"
    return jsonify({
        "authenticated": authed,
        "uid": uid,
        "share_slug": share_slug,
        "profile": {
            "first_name": profile.get("first_name") or "",
            "last_name": profile.get("last_name") or "",
            "username": username,
            "email": meta.get("email_display") or profile.get("email") or getattr(client.session, "email", "") or "",
            "birthdate": profile.get("birthdate") or meta.get("birthday") or "",
            "avatar": with_cache_bust(raw_avatar) if raw_avatar else "",
        },
        "meta": meta,
        "friends": friends,
        "conversations": conversations,
        "presets": load_presets(),
    })


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    identifier = (data.get("identifier") or data.get("email") or data.get("phone") or "").strip()
    password = data.get("password") or ""
    if not identifier or not password:
        return jsonify({"ok": False, "error": "Vui lòng nhập đầy đủ Email và Mật khẩu."}), 400
    if "@" not in identifier:
        return jsonify({"ok": False, "error": "Vui lòng nhập địa chỉ Email hợp lệ để đăng nhập."}), 400
    try:
        client.login(identifier, password)
        return jsonify({"ok": True, "uid": getattr(client.session, "uid", "")})
    except LocketError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    try:
        client.session.id_token = None
        client.session.refresh_token = None
        client.session.uid = None
        if os.path.exists(SESSION_FILE):
            os.remove(SESSION_FILE)
    except Exception:
        pass
    return jsonify({"ok": True})


@app.route("/api/profile/update", methods=["POST"])
def api_profile_update():
    if not client.is_authenticated():
        return jsonify({"ok": False, "error": "Chưa đăng nhập."}), 401
    uid = getattr(client.session, "uid", "")
    data = request.get_json(silent=True) or {}
    first_name = (data.get("first_name") or "").strip()
    last_name = (data.get("last_name") or "").strip()
    email_val = (data.get("email") or "").strip()
    birthday_val = (data.get("birthday") or "").strip()
    surface_style = (data.get("surface_style") or "").strip()
    theme_id = (data.get("theme_id") or "").strip()
    lite_mode = (data.get("lite_mode") or "").strip()

    try:
        if first_name or last_name:
            client.user.update_name(first_name=first_name, last_name=last_name)
        if birthday_val:
            try:
                client.user.update_birthdate(birthday_val)
            except Exception:
                pass
        patch = {}
        if email_val:
            patch["email_display"] = email_val
        if birthday_val:
            patch["birthday"] = birthday_val
        if surface_style in ("glass", "solid"):
            patch["surface_style"] = surface_style
        if theme_id:
            patch["theme_id"] = theme_id
        if lite_mode in ("off", "lite", "auto"):
            patch["lite_mode"] = lite_mode
        saved = save_user_meta(uid, patch)
        return jsonify({"ok": True, "meta": saved})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/profile/avatar", methods=["POST"])
def api_profile_avatar():
    if not client.is_authenticated():
        return jsonify({"ok": False, "error": "Chưa đăng nhập."}), 401
    file = request.files.get("avatar")
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "Chưa chọn ảnh đại diện."}), 400
    ext = os.path.splitext(file.filename)[1] or ".jpg"
    fd, tmp_path = tempfile.mkstemp(suffix=ext)
    os.close(fd)
    try:
        file.save(tmp_path)
        upd_res = client.user.update_avatar(tmp_path)
        fresh = {}
        try:
            fresh = client.user.get_profile() or {}
        except Exception:
            fresh = {}
        final_url = (
            fresh.get("profile_picture_url")
            or fresh.get("profile_pic")
            or fresh.get("avatar_url")
            or (upd_res.get("profile_picture_url") if isinstance(upd_res, dict) else "")
            or ""
        )
        return jsonify({
            "ok": True,
            "avatar_url": with_cache_bust(final_url) if final_url else ""
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


@app.route("/api/post", methods=["POST"])
def api_post():
    if not client.is_authenticated():
        return jsonify({"ok": False, "error": "Vui lòng đăng nhập trước khi gửi."}), 401

    temp_files = []
    try:
        caption = (request.form.get("caption") or "").strip()
        preset_id = (request.form.get("preset_id") or "standard").strip()
        icon = (request.form.get("icon") or "").strip() or None
        colors_raw = (request.form.get("colors") or "").strip()
        text_color = (request.form.get("text_color") or "").strip() or None
        audience = (request.form.get("audience") or "all").strip()
        recipients_raw = (request.form.get("recipients") or "").strip()

        colors = [c.strip() for c in colors_raw.split(",") if c.strip()] or None
        is_private = audience == "private"
        recipients = None
        if audience == "selected" and recipients_raw:
            recipients = [u.strip() for u in recipients_raw.split(",") if u.strip()]

        image_input = None
        video_input = None

        img_file = request.files.get("image")
        vid_file = request.files.get("video")

        if img_file and img_file.filename:
            ext = os.path.splitext(img_file.filename)[1] or ".jpg"
            fd, tmp_img = tempfile.mkstemp(suffix=ext)
            os.close(fd)
            img_file.save(tmp_img)
            temp_files.append(tmp_img)
            image_input = tmp_img

        if vid_file and vid_file.filename:
            ext = os.path.splitext(vid_file.filename)[1] or ".mp4"
            fd, tmp_vid = tempfile.mkstemp(suffix=ext)
            os.close(fd)
            vid_file.save(tmp_vid)
            temp_files.append(tmp_vid)
            video_input = tmp_vid

        if not image_input and not video_input:
            return jsonify({"ok": False, "error": "Chụp ảnh, quay video hoặc chọn từ thư viện trước khi gửi."}), 400

        res = client.moments.post(
            image=image_input,
            video=video_input,
            caption=caption,
            note_type=preset_id,
            icon=icon,
            colors=colors,
            text_color=text_color,
            is_private=is_private,
            recipients=recipients,
            auto_compress=True,
            priority="balanced",
        )
        return jsonify({"ok": True, "moment": res})
    except LocketError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        for p in temp_files:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


@app.route("/api/feed", methods=["GET"])
def api_feed():
    if not client.is_authenticated():
        return jsonify({"ok": False, "error": "Chưa đăng nhập."}), 401
    try:
        items = client.moments.get_latest() or []
        return jsonify({"ok": True, "items": items})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/memories", methods=["GET"])
def api_memories():
    if not client.is_authenticated():
        return jsonify({"ok": False, "error": "Chưa đăng nhập."}), 401
    try:
        items = client.moments.get_all_my_moments(limit=31) or []
        return jsonify({"ok": True, "items": items})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/react", methods=["POST"])
def api_react():
    if not client.is_authenticated():
        return jsonify({"ok": False, "error": "Chưa đăng nhập."}), 401
    data = request.get_json(silent=True) or {}
    mid = data.get("moment_uid")
    emo = data.get("reaction") or "💛"
    if not mid:
        return jsonify({"ok": False, "error": "Thiếu moment_uid."}), 400
    try:
        res = client.moments.react(moment_uid=mid, reaction=emo)
        return jsonify({"ok": True, "result": res})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/comment", methods=["POST"])
def api_comment():
    if not client.is_authenticated():
        return jsonify({"ok": False, "error": "Chưa đăng nhập."}), 401
    data = request.get_json(silent=True) or {}
    mid = data.get("moment_uid")
    uid = data.get("receiver_uid")
    text = (data.get("text") or "").strip()
    if not mid or not uid or not text:
        return jsonify({"ok": False, "error": "Thiếu nội dung tin nhắn."}), 400
    try:
        res = client.moments.comment(moment_uid=mid, receiver_uid=uid, text=text)
        return jsonify({"ok": True, "result": res})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/chat/messages", methods=["GET"])
def api_chat_messages():
    if not client.is_authenticated():
        return jsonify({"ok": False, "error": "Chưa đăng nhập."}), 401
    other_uid = (request.args.get("other_user_id") or "").strip()
    if not other_uid:
        return jsonify({"ok": False, "error": "Thiếu other_user_id."}), 400
    try:
        conv_uid = resolve_conversation_uid(other_uid)
        try:
            client.chat.mark_as_read(
                conversation_uid=conv_uid or None,
                other_user_id=other_uid,
                send_read_receipt=False
            )
        except Exception:
            pass

        msgs = client.chat.get_messages(
            other_user_id=other_uid,
            conversation_id=conv_uid or None,
            fetch_server=True
        ) or []

        normalized = []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            mid = m.get("message_id") or m.get("id") or m.get("client_token") or ""
            cid = m.get("conversation_id") or conv_uid
            if cid and not conv_uid:
                conv_uid = cid
            raw_body = m.get("text") or ""
            clean_txt, img_url = split_message_media(raw_body)
            normalized.append({
                "id": mid,
                "conversation_id": cid,
                "sender_id": m.get("sender_uid") or m.get("sender_id") or "",
                "is_me": bool(m.get("is_me")),
                "text": clean_txt,
                "image_url": img_url,
                "reaction": m.get("reaction") or "",
                "timestamp": m.get("timestamp") or "",
            })

        return jsonify({
            "ok": True,
            "my_uid": getattr(client.session, "uid", ""),
            "conversation_id": conv_uid,
            "messages": normalized,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/chat/send", methods=["POST"])
def api_chat_send():
    if not client.is_authenticated():
        return jsonify({"ok": False, "error": "Chưa đăng nhập."}), 401

    temp_files = []
    try:
        json_body = request.get_json(silent=True) or {}
        uid = (request.form.get("receiver_uid") or json_body.get("receiver_uid") or "").strip()
        text = (request.form.get("text") or json_body.get("text") or "").strip()
        img_file = request.files.get("image")

        if not uid:
            return jsonify({"ok": False, "error": "Thiếu người nhận."}), 400

        if img_file and img_file.filename:
            ext = os.path.splitext(img_file.filename)[1] or ".jpg"
            fd, tmp_img = tempfile.mkstemp(suffix=ext)
            os.close(fd)
            img_file.save(tmp_img)
            temp_files.append(tmp_img)
            img_url = client.moments.upload_image(tmp_img)
            payload_txt = f"{text} {img_url}".strip() if text else img_url
            res = client.chat.send_message(receiver_uid=uid, text=payload_txt)
            return jsonify({"ok": True, "image_url": img_url, "result": res})

        if not text:
            return jsonify({"ok": False, "error": "Thiếu nội dung tin nhắn."}), 400

        res = client.chat.send_message(receiver_uid=uid, text=text)
        return jsonify({"ok": True, "result": res})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        for p in temp_files:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


@app.route("/api/chat/reaction", methods=["POST"])
def api_chat_reaction():
    if not client.is_authenticated():
        return jsonify({"ok": False, "error": "Chưa đăng nhập."}), 401
    data = request.get_json(silent=True) or {}
    other_uid = (data.get("other_user_id") or "").strip()
    conv_id = resolve_conversation_uid(other_uid, (data.get("conversation_id") or "").strip())
    msg_id = (data.get("message_id") or "").strip()
    emoji = (data.get("emoji") or "❤️").strip()

    if not msg_id:
        return jsonify({"ok": False, "error": "Thiếu message_id."}), 400
    if not conv_id:
        return jsonify({"ok": False, "error": "Không tìm thấy conversation_id của cuộc trò chuyện."}), 400

    try:
        res = client.chat.send_reaction(
            conversation_id=conv_id,
            message_id=msg_id,
            emoji=emoji,
            validate_receiver=False
        )
        if isinstance(res, dict) and res.get("error"):
            return jsonify({"ok": False, "error": str(res.get("error"))}), 400
        return jsonify({"ok": True, "result": res})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Thả icon lỗi: {e}"}), 400


@app.route("/api/chat/delete", methods=["POST"])
def api_chat_delete():
    if not client.is_authenticated():
        return jsonify({"ok": False, "error": "Chưa đăng nhập."}), 401
    data = request.get_json(silent=True) or {}
    other_uid = (data.get("other_user_id") or "").strip()
    conv_uid = resolve_conversation_uid(other_uid, (data.get("conversation_id") or "").strip())
    msg_id = (data.get("message_id") or "").strip()

    if not msg_id:
        return jsonify({"ok": False, "error": "Thiếu message_id."}), 400
    if not conv_uid:
        return jsonify({"ok": False, "error": "Không tìm thấy conversation_uid của cuộc trò chuyện."}), 400

    try:
        res = client.chat.delete_message(
            conversation_uid=conv_uid,
            message_uid=msg_id,
            validate_ownership=True
        )
        if isinstance(res, dict) and res.get("error"):
            return jsonify({"ok": False, "error": str(res.get("error"))}), 400
        return jsonify({"ok": True, "result": res})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"Thu hồi lỗi: {e}"}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
