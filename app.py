import os
import json
import time
import secrets
import tempfile
import requests
from flask import Flask, request, jsonify, render_template, Response
from locketlam import LocketClient, NoteType
from locketlam.exceptions import LocketError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(BASE_DIR, "locketlam_session.json")
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

# Bảo mật: Ánh xạ UID thật <-> ID tạm theo phiên & Media Proxy Token
UID_TO_ALIAS = {}
ALIAS_TO_UID = {}
MEDIA_TOKEN_TO_URL = {}
MEDIA_URL_TO_TOKEN = {}


def reset_security_maps():
    UID_TO_ALIAS.clear()
    ALIAS_TO_UID.clear()
    MEDIA_TOKEN_TO_URL.clear()
    MEDIA_URL_TO_TOKEN.clear()


def to_alias_uid(real_uid: str) -> str:
    if not real_uid:
        return ""
    if real_uid not in UID_TO_ALIAS:
        alias = f"u_{secrets.token_urlsafe(8)}"
        UID_TO_ALIAS[real_uid] = alias
        ALIAS_TO_UID[alias] = real_uid
    return UID_TO_ALIAS[real_uid]


def to_real_uid(alias_or_uid: str) -> str:
    if not alias_or_uid:
        return ""
    return ALIAS_TO_UID.get(alias_or_uid, alias_or_uid)


def to_proxy_url(real_url: str) -> str:
    if not real_url or not isinstance(real_url, str):
        return ""
    if real_url.startswith("/api/media/"):
        return real_url
    if real_url not in MEDIA_URL_TO_TOKEN:
        if len(MEDIA_TOKEN_TO_URL) > 2500:
            MEDIA_TOKEN_TO_URL.clear()
            MEDIA_URL_TO_TOKEN.clear()
        tok = secrets.token_urlsafe(12)
        MEDIA_URL_TO_TOKEN[real_url] = tok
        MEDIA_TOKEN_TO_URL[tok] = real_url
    return f"/api/media/{MEDIA_URL_TO_TOKEN[real_url]}"


def mask_email(email: str) -> str:
    if not email or "@" not in email:
        return ""
    name, domain = email.split("@", 1)
    prefix = name[:1] if name else ""
    return f"{prefix}***@{domain}"


def mask_birthdate(bday: str) -> str:
    return "****-**-**" if (bday and str(bday).strip()) else ""


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


# ======================== PWA ROUTES ========================
@app.route("/manifest.json")
def pwa_manifest():
    return jsonify({
        "name": "TLTH LOCKET",
        "short_name": "Locket",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#000000",
        "theme_color": "#000000",
        "orientation": "portrait",
        "icons": [
            {
                "src": "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 192 192'%3E%3Crect width='192' height='192' rx='44' fill='%23f7b500'/%3E%3Ctext x='96' y='128' font-size='96' text-anchor='middle'%3E%F0%9F%92%9B%3C/text%3E%3C/svg%3E",
                "sizes": "192x192",
                "type": "image/svg+xml"
            }
        ]
    })


@app.route("/service-worker.js")
def pwa_sw():
    sw_js = """
const CACHE_NAME = 'tlth-locket-v3';
self.addEventListener('install', () => { self.skipWaiting(); });
self.addEventListener('activate', (e) => { e.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET' || e.request.url.includes('/api/')) return;
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
"""
    return Response(sw_js.strip(), mimetype="application/javascript")


# ======================== MEDIA PROXY ========================
@app.route("/api/media/<token>", methods=["GET"])
def api_media_proxy(token):
    real_url = MEDIA_TOKEN_TO_URL.get(token)
    if not real_url:
        return jsonify({"ok": False, "error": "Media not found"}), 404
    try:
        r = requests.get(real_url, stream=True, timeout=25)
        content_type = r.headers.get("Content-Type", "application/octet-stream")
        headers = {
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        }
        return Response(
            r.iter_content(chunk_size=16384),
            status=r.status_code,
            content_type=content_type,
            headers=headers,
        )
    except Exception:
        return jsonify({"ok": False, "error": "Media proxy error"}), 502


# ======================== SPA ROUTES ========================
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


# ======================== API ENDPOINTS ========================
@app.route("/api/boot", methods=["GET"])
def api_boot():
    authed = client.is_authenticated()
    real_uid = getattr(client.session, "uid", "") if authed else ""
    alias_my_uid = to_alias_uid(real_uid) if real_uid else ""
    meta = get_user_meta(real_uid)
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
                    full = f"{fn} {ln}".strip() or item.get("username") or "Bạn bè"
                    av = (
                        item.get("profile_picture_url")
                        or item.get("profile_pic")
                        or item.get("avatar_url")
                        or item.get("photo_url")
                        or ""
                    )
                    if f_uid:
                        friends.append({
                            "uid": to_alias_uid(f_uid),
                            "name": full,
                            "avatar": to_proxy_url(av) if av else "",
                        })
        except Exception:
            friends = []
        try:
            raw_convs = client.chat.get_conversations() or []
            for c in raw_convs:
                if not isinstance(c, dict):
                    continue
                peer = c.get("peer_uid") or ""
                lm = c.get("latest_message") or {}
                conversations.append({
                    "conversation_id": c.get("conversation_id") or "",
                    "peer_uid": to_alias_uid(peer) if peer else "",
                    "unread_count": c.get("unread_count", 0),
                    "is_read": bool(c.get("is_read", True)),
                    "latest_message": {
                        "text": lm.get("text") or "",
                        "timestamp": lm.get("timestamp") or "",
                    },
                })
        except Exception:
            conversations = []

    username = profile.get("username") or ""
    raw_avatar = (
        profile.get("profile_picture_url")
        or profile.get("profile_pic")
        or profile.get("avatar_url")
        or ""
    )
    raw_email = profile.get("email") or meta.get("email_display") or getattr(client.session, "email", "") or ""
    raw_bday = profile.get("birthdate") or meta.get("birthday") or ""

    return jsonify({
        "authenticated": authed,
        "uid": alias_my_uid,
        "share_slug": username,
        "profile": {
            "first_name": profile.get("first_name") or "",
            "last_name": profile.get("last_name") or "",
            "username": username,
            "email_masked": mask_email(raw_email),
            "birthdate_masked": mask_birthdate(raw_bday),
            "avatar": to_proxy_url(raw_avatar) if raw_avatar else "",
        },
        "meta": {
            "surface_style": meta.get("surface_style", "glass"),
            "theme_id": meta.get("theme_id", "default"),
            "lite_mode": meta.get("lite_mode", "auto"),
        },
        "friends": friends,
        "conversations": conversations,
    })


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    identifier = (data.get("identifier") or data.get("email") or "").strip()
    password = data.get("password") or ""
    if not identifier or not password:
        return jsonify({"ok": False, "error": "Vui lòng nhập đầy đủ Email và Mật khẩu."}), 400
    if "@" not in identifier:
        return jsonify({"ok": False, "error": "Vui lòng nhập địa chỉ Email hợp lệ để đăng nhập."}), 400
    try:
        reset_security_maps()
        client.login(identifier, password)
        return jsonify({"ok": True, "uid": to_alias_uid(getattr(client.session, "uid", ""))})
    except LocketError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    try:
        reset_security_maps()
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
        if birthday_val and "*" not in birthday_val:
            try:
                client.user.update_birthdate(birthday_val)
            except Exception:
                pass
        patch = {}
        if email_val and "*" not in email_val:
            patch["email_display"] = email_val
        if birthday_val and "*" not in birthday_val:
            patch["birthday"] = birthday_val
        if surface_style in ("glass", "solid"):
            patch["surface_style"] = surface_style
        if theme_id:
            patch["theme_id"] = theme_id
        if lite_mode in ("off", "lite", "auto"):
            patch["lite_mode"] = lite_mode
        saved = save_user_meta(uid, patch)
        return jsonify({"ok": True, "meta": {
            "surface_style": saved.get("surface_style", "glass"),
            "theme_id": saved.get("theme_id", "default"),
            "lite_mode": saved.get("lite_mode", "auto"),
        }})
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
            "avatar_url": to_proxy_url(final_url) if final_url else ""
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
            recipients = [to_real_uid(u.strip()) for u in recipients_raw.split(",") if u.strip()]

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
        return jsonify({"ok": True, "moment_id": res.get("canonical_uid") or res.get("id") if isinstance(res, dict) else ""})
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
        sync_token = (request.args.get("sync_token") or "").strip() or None
        filter_alias = (request.args.get("friend_uid") or "").strip()
        payload = {"should_count_missed_moments": True, "include_group_moments": True}
        if sync_token:
            payload["sync_token"] = sync_token

        res = client.post_api("getLatestMomentV2", payload)
        raw_items = res.get("data", []) if isinstance(res, dict) else []
        if not raw_items and not sync_token:
            raw_items = client.moments.get_latest() or []

        filter_real = to_real_uid(filter_alias) if filter_alias else ""
        items = []
        next_sync = None
        for m in raw_items:
            if not isinstance(m, dict):
                continue
            mid = m.get("canonical_uid") or m.get("id") or ""
            if mid:
                next_sync = mid
            author_real = m.get("user") or ""
            if filter_real and author_real != filter_real:
                continue
            thumb = m.get("thumbnail_url") or m.get("image_url") or ""
            vid = m.get("video_url") or ""
            date_field = m.get("date") or {}
            ts_sec = date_field.get("_seconds") if isinstance(date_field, dict) else None
            cap = m.get("caption") or ""
            if not cap and isinstance(m.get("overlays"), list) and m["overlays"]:
                cap = m["overlays"][0].get("alt_text") or ""
            items.append({
                "id": mid,
                "user": to_alias_uid(author_real),
                "thumbnail_url": to_proxy_url(thumb) if thumb else "",
                "video_url": to_proxy_url(vid) if vid else "",
                "caption": cap,
                "timestamp": ts_sec or int(time.time()),
            })
        return jsonify({"ok": True, "items": items, "next_sync": next_sync})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/memories", methods=["GET"])
def api_memories():
    if not client.is_authenticated():
        return jsonify({"ok": False, "error": "Chưa đăng nhập."}), 401
    try:
        raw = client.moments.get_all_my_moments(limit=120) or []
        items = []
        for m in raw:
            if not isinstance(m, dict):
                continue
            thumb = m.get("thumbnail_url") or ""
            items.append({
                "id": m.get("moment_uid") or "",
                "thumbnail_url": to_proxy_url(thumb) if thumb else "",
                "created_at": m.get("created_at") or "",
                "caption": m.get("caption") or "",
            })
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
        client.moments.react(moment_uid=mid, reaction=emo)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/comment", methods=["POST"])
def api_comment():
    if not client.is_authenticated():
        return jsonify({"ok": False, "error": "Chưa đăng nhập."}), 401
    data = request.get_json(silent=True) or {}
    mid = data.get("moment_uid")
    uid = to_real_uid(data.get("receiver_uid") or "")
    text = (data.get("text") or "").strip()
    if not mid or not uid or not text:
        return jsonify({"ok": False, "error": "Thiếu nội dung tin nhắn."}), 400
    try:
        client.moments.comment(moment_uid=mid, receiver_uid=uid, text=text)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/chat/messages", methods=["GET"])
def api_chat_messages():
    if not client.is_authenticated():
        return jsonify({"ok": False, "error": "Chưa đăng nhập."}), 401
    other_alias = (request.args.get("other_user_id") or "").strip()
    other_uid = to_real_uid(other_alias)
    if not other_uid:
        return jsonify({"ok": False, "error": "Thiếu other_user_id."}), 400
    try:
        conv_uid = resolve_conversation_uid(other_uid)
        try:
            client.chat.mark_as_read(conversation_uid=conv_uid or None, other_user_id=other_uid, send_read_receipt=False)
        except Exception:
            pass

        msgs = client.chat.get_messages(other_user_id=other_uid, conversation_id=conv_uid or None, fetch_server=True) or []
        normalized = []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            mid = m.get("message_id") or m.get("id") or m.get("client_token") or ""
            cid = m.get("conversation_id") or conv_uid
            if cid and not conv_uid:
                conv_uid = cid
            txt = m.get("text") or ""
            is_img = txt.strip().startswith("http") and any(ext in txt.lower() for ext in (".webp", ".png", ".jpg", ".jpeg", ".gif"))
            normalized.append({
                "id": mid,
                "is_me": bool(m.get("is_me")),
                "text": "" if is_img else txt,
                "image_url": to_proxy_url(txt.strip()) if is_img else "",
                "reaction": m.get("reaction") or "",
                "timestamp": m.get("timestamp") or "",
            })

        return jsonify({
            "ok": True,
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
        uid = to_real_uid((request.form.get("receiver_uid") or json_body.get("receiver_uid") or "").strip())
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
            client.chat.send_message(receiver_uid=uid, text=payload_txt)
            return jsonify({"ok": True})

        if not text:
            return jsonify({"ok": False, "error": "Thiếu nội dung tin nhắn."}), 400

        client.chat.send_message(receiver_uid=uid, text=text)
        return jsonify({"ok": True})
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
    other_uid = to_real_uid((data.get("other_user_id") or "").strip())
    conv_id = resolve_conversation_uid(other_uid, (data.get("conversation_id") or "").strip())
    msg_id = (data.get("message_id") or "").strip()
    emoji = (data.get("emoji") or "❤️").strip()
    if not msg_id or not conv_id:
        return jsonify({"ok": False, "error": "Thiếu thông tin tin nhắn."}), 400
    try:
        client.chat.send_reaction(conversation_id=conv_id, message_id=msg_id, emoji=emoji, validate_receiver=False)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Thả icon lỗi: {e}"}), 400


@app.route("/api/chat/delete", methods=["POST"])
def api_chat_delete():
    if not client.is_authenticated():
        return jsonify({"ok": False, "error": "Chưa đăng nhập."}), 401
    data = request.get_json(silent=True) or {}
    other_uid = to_real_uid((data.get("other_user_id") or "").strip())
    conv_uid = resolve_conversation_uid(other_uid, (data.get("conversation_id") or "").strip())
    msg_id = (data.get("message_id") or "").strip()
    if not msg_id or not conv_uid:
        return jsonify({"ok": False, "error": "Thiếu thông tin tin nhắn."}), 400
    try:
        client.chat.delete_message(conversation_uid=conv_uid, message_uid=msg_id, validate_ownership=True)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Thu hồi lỗi: {e}"}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
