import os
"""
現場フォト＋資料共有アプリ v3
- 現場ごとにランダムURLを発行
- 写真アップロード・ギャラリー表示
- 図面・仕様書などPDF資料のアップロード＆ダウンロード

Usage: python app.py
管理画面: http://サーバーIP:5000/admin
"""

# ===== v1.11.1 (2026-05-01 クララ): 起動時依存ライブラリ自動補完 =====
# 旧イメージは google-genai 等が入っていないことがある (requirements.txt 追加後に再ビルドされていないケース)。
# 不足を検出したら pip install で補う。 google-genai が無いと purchase_manager の Gemini OCR と
# /ai_detect_rooms /ai3d /generate_3d_render が "No module named 'google'" で失敗する。
def _ensure_runtime_deps():
    import subprocess, sys
    needed_packages = []
    checks = [
        ('google.genai',      'google-genai>=1.0.0'),
        ('openpyxl',          'openpyxl>=3.1.0'),
        ('pillow_heif',       'pillow-heif>=0.16.0'),
        ('imagehash',         'ImageHash>=4.3.1'),
    ]
    for mod, pkg in checks:
        try:
            __import__(mod)
        except ImportError:
            needed_packages.append(pkg)
    if needed_packages:
        print(f"[startup-deps] installing missing: {needed_packages}", flush=True)
        try:
            subprocess.run(
                [sys.executable, '-m', 'pip', 'install', '--quiet', '--no-cache-dir', *needed_packages],
                check=False, timeout=300,
            )
            print("[startup-deps] install complete", flush=True)
        except Exception as _e:
            print(f"[startup-deps] install failed: {_e}", flush=True)

_ensure_runtime_deps()
del _ensure_runtime_deps


import json, uuid, secrets, smtplib, threading, re, csv, io, html as html_lib
import tempfile
import urllib.request, urllib.parse, urllib.error
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, date, timedelta
from pathlib import Path
from collections import defaultdict
from flask import (Flask, render_template, request, redirect,
                   url_for, send_from_directory, send_file, jsonify, abort, session)
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
# v1.8.1: Cloudflare Tunnel など逆プロキシ経由で X-Forwarded-Proto/Host を信頼。
# これがないと https で来てもFlaskは http と認識し、リダイレクトが http に落ちて
# 黒板カメラなど getUserMedia が「HTTPS必須」エラーで弾かれる。
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
# v1.8.3: 末尾スラッシュの有無どちらも受け付ける(/p/<token>/ も /p/<token> もOK)。
# 業者がブックマークした「/」付きURLが404になる問題を防ぐ。
app.url_map.strict_slashes = False
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['MAX_CONTENT_LENGTH'] = 1000 * 1024 * 1024  # v1.8.10: 1000MB (CG画像高解像度対応)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = True
app.permanent_session_lifetime = timedelta(days=int(os.environ.get("LINE_LOGIN_SESSION_DAYS", "180")))

def _load_or_create_flask_secret():
    env_secret = os.environ.get("FLASK_SECRET_KEY") or os.environ.get("SECRET_KEY")
    if env_secret:
        return env_secret
    fp = Path(os.environ.get("FLASK_SECRET_FILE", ".flask_secret_key"))
    try:
        if fp.exists():
            secret = fp.read_text(encoding="utf-8").strip()
            if secret:
                return secret
        secret = secrets.token_urlsafe(48)
        fp.write_text(secret, encoding="utf-8")
        return secret
    except Exception as e:
        print(f"[SESSION_SECRET] fallback volatile secret: {e}", flush=True)
        return secrets.token_urlsafe(48)

app.secret_key = _load_or_create_flask_secret()

@app.before_request
def force_https_on_public_host():
    """v1.8.2: 公開ドメインに http:// で来たら https:// に301リダイレクト。
    黒板カメラの getUserMedia は HTTPS 必須のため。
    LAN直アクセス (192.168.3.167:5000) は対象外。"""
    if request.is_secure:
        return None
    host = (request.headers.get('Host') or '').split(':')[0].lower()
    if host == 'photo.j-cb.com':
        url = request.url.replace('http://', 'https://', 1)
        return redirect(url, code=301)
    return None

@app.after_request
def add_no_cache_headers(response):
    """HTMLレスポンスは絶対にキャッシュさせない(アップデート時のキャッシュ問題対策)"""
    if response.mimetype == 'text/html':
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

UPLOAD_BASE = Path("uploads")
DATA_FILE   = Path("sites.json")

# ====== レイアウト v2 (2026-04-29) — タブ⇔フォルダ 1:1 ======
# UPLOAD_BASE / token / PATHS["photos"] のように使う。URL側は旧名 (photos, thumbs等) のまま据え置き。
PATHS = {
    "photos":           "写真",
    "complete_photos":  "完工/写真",
    "complete_report":  "完工/報告書",
    "complete_attach":  "完工/添付",
    "hero":             "ヒーロー",
    "memo_images":      "メモ",
    "panoramas":        "3D/パノラマ",
    "ai3d":             "3D/AI生成",
    "thumbs":           "_thumbs",
    "invoices":         "請求",
    "videos":           "ライブ/動画",
    "docs":             "資料",
    "schedule.json":    "工程/schedule.json",
    "attendance.jsonl": "工程/attendance.jsonl",
    "subscribers.json": "_meta/subscribers.json",
    "memos.json":       "_meta/memos.json",
    "parking.json":     "地図/parking.json",
    "toilets.json":     "地図/toilets.json",
    "survey_photos":    "現場調査/写真",
    "survey_docs":      "現場調査/資料",
    "survey_memo":      "現場調査/memo.txt",
    "site_videos":      "動画",
}


# ===== メール通知設定 =====
NOTIFY_EMAIL   = os.environ.get("NOTIFY_EMAIL", "cbhatake@gmail.com")
SMTP_USER      = os.environ.get("SMTP_USER", "")      # Gmail: cbhatake@gmail.com
SMTP_PASS      = os.environ.get("SMTP_PASS", "")      # Gmailアプリパスワード
SMTP_HOST      = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "587"))

SITE_BASE_URL  = os.environ.get("SITE_BASE_URL", "https://photo.j-cb.com")

# ===== LINEログイン設定 =====
# 環境変数または line_login.json から読む。SecretはGitへ入れない。
# line_login.json 例:
# {"channel_id":"2009985135","channel_secret":"...","redirect_uri":"https://photo.j-cb.com/auth/line/callback"}
LINE_LOGIN_CONFIG_FILE = Path(os.environ.get("LINE_LOGIN_CONFIG_FILE", "line_login.json"))

def _line_login_config():
    raw = {}
    try:
        if LINE_LOGIN_CONFIG_FILE.exists():
            raw = json.loads(LINE_LOGIN_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[LINE_LOGIN_CONFIG] read error: {e}", flush=True)
    channel_id = (os.environ.get("LINE_LOGIN_CHANNEL_ID") or raw.get("channel_id") or "").strip()
    channel_secret = (os.environ.get("LINE_LOGIN_CHANNEL_SECRET") or raw.get("channel_secret") or "").strip()
    redirect_uri = (os.environ.get("LINE_LOGIN_REDIRECT_URI") or raw.get("redirect_uri") or f"{SITE_BASE_URL}/auth/line/callback").strip()
    return {
        "channel_id": channel_id,
        "channel_secret": channel_secret,
        "redirect_uri": redirect_uri,
        "enabled": bool(channel_id and channel_secret),
    }

# ====== v1.8.7 メール購読機能 ======
def _subscribers_file(token):
    return UPLOAD_BASE / token / PATHS["subscribers.json"]

def load_subscribers(token):
    """現場の購読者一覧を返す: [{email, unsub_token, subscribed_at}]"""
    fp = _subscribers_file(token)
    if not fp.exists():
        return []
    try:
        return json.loads(fp.read_text(encoding='utf-8'))
    except Exception:
        return []

def save_subscribers(token, subs):
    fp = _subscribers_file(token)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(subs, ensure_ascii=False, indent=2), encoding='utf-8')

def add_subscriber(token, email):
    """重複なく追加。既登録でも構わず unsub_token を返す"""
    subs = load_subscribers(token)
    email = (email or '').strip().lower()
    if not email or '@' not in email:
        return None
    for s in subs:
        if s.get('email','').lower() == email:
            return s.get('unsub_token')
    unsub = secrets.token_urlsafe(16)
    subs.append({
        "email": email,
        "unsub_token": unsub,
        "subscribed_at": datetime.now().strftime("%Y/%m/%d %H:%M")
    })
    save_subscribers(token, subs)
    return unsub

def find_subscriber_by_unsub(unsub_token):
    """全現場をスキャンして unsub_token に一致する購読者を返す"""
    if not unsub_token:
        return None
    sites = load_sites()
    for token in sites:
        subs = load_subscribers(token)
        for i, s in enumerate(subs):
            if s.get('unsub_token') == unsub_token:
                return {"token": token, "index": i, "subscriber": s}
    return None

def send_notify(subject, body, site_name="", token=None):
    """通知メール送信 (管理者 + 現場購読者全員に配信、全員に個別の解除URL付き)
    v1.8.17: 重複排除 + 全員に配信停止リンクを必ず付与"""
    if not SMTP_USER or not SMTP_PASS:
        return  # SMTP未設定時はスキップ
    base_body = body
    site_url = ""
    if token:
        site_url = f"{SITE_BASE_URL}/p/{token}"
        base_body = base_body + f"\n\n▼ 現場ページを開く\n{site_url}"
    subj = f"[現場フォト] {subject} - {site_name}"

    # 配信リスト構築 (email -> unsub_url の dict で重複排除)
    by_email = {}  # email_lower -> unsub_url(str or None)
    if NOTIFY_EMAIL:
        by_email[NOTIFY_EMAIL.lower()] = None  # 管理者は購読者に上書きされる可能性あり
    if token:
        for s in load_subscribers(token):
            em = s.get('email')
            ut = s.get('unsub_token')
            if em and ut:
                # 購読者なら必ず解除リンク付与 (管理者と同じemailでも上書き)
                unsub_url = f"{SITE_BASE_URL}/unsubscribe/{ut}"
                by_email[em.lower()] = unsub_url
    if not by_email:
        return

    def _send():
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASS)
                for email_lower, unsub_url in by_email.items():
                    body_for_user = base_body
                    if unsub_url:
                        body_for_user += (
                            "\n\n----------------------------------------\n"
                            "📧 この現場の通知が不要な場合は↓のリンクをタップしてください\n"
                            "▼ 配信停止 (ワンクリック)\n"
                            f"{unsub_url}\n"
                        )
                    else:
                        # 管理者宛 (購読登録なし) には参考まで管理画面へのリンクを添える
                        body_for_user += (
                            "\n\n----------------------------------------\n"
                            "[管理者] このメールは管理者通知です。購読者管理は管理画面から:\n"
                            f"{SITE_BASE_URL}/admin\n"
                        )
                    msg = MIMEMultipart()
                    msg['From'] = SMTP_USER
                    msg['To'] = email_lower
                    msg['Subject'] = subj
                    msg.attach(MIMEText(body_for_user, 'plain', 'utf-8'))
                    try:
                        s.send_message(msg)
                    except Exception as e_each:
                        print(f"[NOTIFY ERROR each→{email_lower}] {e_each}")
        except Exception as e:
            print(f"[NOTIFY ERROR] {e}")
    threading.Thread(target=_send, daemon=True).start()

# Google Places API キー（環境変数 or ファイルから読み込み）
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY', '')
if not GOOGLE_API_KEY:
    _key_file = Path("google_api_key.txt")
    if _key_file.exists():
        GOOGLE_API_KEY = _key_file.read_text(encoding='utf-8').strip()

IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic', '.heif',
             '.bmp', '.tif', '.tiff'}
# v1.7.1: CG(レンダリング)はBMP/TIFFも多いので追加
# ブラウザで直接表示できない拡張子 → upload時にJPGサムネを自動生成
NON_INLINE_IMAGE_EXT = {'.bmp', '.tif', '.tiff', '.heic', '.heif'}
DOC_EXT   = {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.xlsm', '.ppt', '.pptx',
             '.txt', '.csv', '.zip', '.dwg', '.dxf', '.numbers',
             '.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic', '.heif',
             '.bmp', '.tif', '.tiff'}

DOC_CATEGORIES = ["図面", "建築図面", "仕様書", "見積書", "CG", "割付", "採寸", "その他"]

# ---- データ管理 ----

# =====================================================================
# v1.8.6 schedule.json 並行アクセス安全化 (race condition によるデータ損失対策)
# - 現場ごとの threading.Lock で同時書き込みを直列化
# - 一時ファイル+rename による atomic write
# - 破損 JSON は raw_decode で自動復旧してリライト
# =====================================================================
_schedule_locks = {}
_schedule_locks_master = threading.Lock()

def _get_schedule_lock(token):
    """現場(token)ごとのロックを取得。master_lock で辞書アクセス自体も保護。"""
    with _schedule_locks_master:
        if token not in _schedule_locks:
            _schedule_locks[token] = threading.Lock()
        return _schedule_locks[token]

def _atomic_write_json(path, data):
    """同一フォルダ内の一時ファイルに書いて rename(同一FS なら atomic)。
    クラッシュや並行書きで破損したファイルが残らないようにする。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix='.tmp_', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, str(path))
    except Exception:
        try: os.unlink(tmp_path)
        except: pass
        raise

def _read_schedule_safe(token):
    """token の schedule.json を読む。破損していたら自動復旧。
    呼び出し側はロックを取ってから呼ぶ前提。"""
    sched_file = UPLOAD_BASE / token / PATHS["schedule.json"]
    if not sched_file.exists():
        return {"tasks": []}
    text = sched_file.read_text(encoding='utf-8')
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        try:
            d, _end = json.JSONDecoder().raw_decode(text)
            print(f"[SCHEDULE RECOVER] {token}: corrupted JSON, recovered head ({len(d.get('tasks',[]))} tasks)")
            _atomic_write_json(sched_file, d)
            return d
        except Exception as ee:
            print(f"[SCHEDULE FATAL] {token}: cannot recover ({ee}). Initializing empty.")
            _atomic_write_json(sched_file, {"tasks": []})
            return {"tasks": []}

# =====================================================================
# v1.8.7 attendance.jsonl - append-only ログ。出退勤の高頻度書込を lock不要にする。
#   - 1書込 = 1行 (POSIX append は atomic, Windows でも 4KB 未満は atomic)
#   - 取消は tombstone 行 ({"id":"...","deleted":true}) を追記
#   - schedule.json (gantt等) と分離。schedule.json は低頻度なので従来 lock+atomic で十分
# =====================================================================
_attendance_locks = {}
_attendance_locks_master = threading.Lock()
def _get_attendance_lock(token):
    with _attendance_locks_master:
        if token not in _attendance_locks:
            _attendance_locks[token] = threading.Lock()
        return _attendance_locks[token]

def _attendance_jsonl_path(token):
    return UPLOAD_BASE / token / PATHS["attendance.jsonl"]

def _attendance_append(token, record):
    """attendance.jsonl に 1 行追記。短時間ロックで書込の重複行を防ぐ程度。
    全体 lock せず append のみなので 1ms 以下の競合で済む。"""
    path = _attendance_jsonl_path(token)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    # short-lived lock
    with _get_attendance_lock(token):
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line)
            f.flush()

def _attendance_read_all(token, only_today=False):
    """attendance.jsonl を全件読んで tombstone を反映した有効レコードを返す。
    旧 schedule.json (is_attendance) からも読む(後方互換)。"""
    today_prefix = datetime.now().strftime("%Y-%m-%d")
    records = {}  # id -> record
    deleted = set()
    # 1. attendance.jsonl から
    path = _attendance_jsonl_path(token)
    if path.exists():
        try:
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get('deleted') and r.get('id'):
                        deleted.add(r['id'])
                        records.pop(r['id'], None)
                        continue
                    rid = r.get('id')
                    if not rid or rid in deleted:
                        continue
                    if only_today:
                        ts = r.get('timestamp', '')
                        if not ts.startswith(today_prefix):
                            continue
                    records[rid] = r
        except Exception as e:
            print(f"[ATTENDANCE_JSONL READ ERROR] {token}: {e}")
    # 2. schedule.json からの後方互換 (旧データを混ぜて読む)
    sched_file = UPLOAD_BASE / token / PATHS["schedule.json"]
    if sched_file.exists():
        try:
            with _get_schedule_lock(token):
                sd = _read_schedule_safe(token)
            for t in sd.get('tasks', []):
                if not t.get('is_attendance'):
                    continue
                rid = t.get('id')
                if not rid or rid in deleted or rid in records:
                    continue
                if only_today:
                    ts = t.get('timestamp', '')
                    if not ts.startswith(today_prefix):
                        continue
                records[rid] = t
        except Exception as e:
            print(f"[ATTENDANCE_JSONL legacy READ ERROR] {token}: {e}")
    # ソート (新→古)
    return sorted(records.values(), key=lambda r: r.get('timestamp', ''), reverse=True)

def _attendance_cancel(token, task_id):
    """tombstone を append。"""
    if not task_id:
        return False
    rec = {'id': task_id, 'deleted': True, 'deleted_at': datetime.now().isoformat()}
    _attendance_append(token, rec)
    # 後方互換: schedule.json にも同じ id があれば削除
    sched_file = UPLOAD_BASE / token / PATHS["schedule.json"]
    if sched_file.exists():
        try:
            with _get_schedule_lock(token):
                sd = _read_schedule_safe(token)
                tasks = sd.get('tasks', [])
                new_tasks = [t for t in tasks if not (t.get('is_attendance') and t.get('id') == task_id)]
                if len(new_tasks) != len(tasks):
                    sd['tasks'] = new_tasks
                    _atomic_write_json(sched_file, sd)
        except Exception as e:
            print(f"[ATTENDANCE_CANCEL legacy ERROR] {token}: {e}")
    return True

# v1.8.7 attendance.jsonl のバックアップを毎時取る
def _attendance_backup_loop():
    """1時間ごとに各 token の attendance.jsonl をバックアップフォルダにコピー。"""
    import shutil
    while True:
        try:
            time.sleep(3600)  # 1時間
            ts = datetime.now().strftime("%Y%m%d_%H%M")
            for token_dir in UPLOAD_BASE.iterdir():
                if not token_dir.is_dir():
                    continue
                p = token_dir / PATHS["attendance.jsonl"]
                if not p.exists():
                    continue
                bak_dir = token_dir / "_meta" / "_bak" / "attendance_backups"
                bak_dir.mkdir(exist_ok=True)
                bak = bak_dir / f"attendance_{ts}.jsonl"
                shutil.copy2(p, bak)
                # 古いバックアップ削除 (24時間以上前)
                for old in bak_dir.iterdir():
                    age = time.time() - old.stat().st_mtime
                    if age > 86400 * 7:  # 7日
                        old.unlink()
        except Exception as e:
            print(f"[ATTENDANCE_BACKUP] {e}")

import time as _time_for_backup
threading.Thread(target=_attendance_backup_loop, daemon=True).start()

def load_sites():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding='utf-8'))
    return {}

def save_sites(data):
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

LINE_USERS_FILE = UPLOAD_BASE / "_meta" / "line_users.json"
_line_users_lock = threading.Lock()

def _safe_next_url(value, default="/"):
    value = (value or "").strip()
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return default
    return value

def _line_users_load():
    try:
        if LINE_USERS_FILE.exists():
            data = json.loads(LINE_USERS_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[LINE_USERS] load error: {e}", flush=True)
    return {}

def _line_users_save(data):
    _atomic_write_json(LINE_USERS_FILE, data)

def _line_current_user():
    user_id = session.get("line_user_id")
    if not user_id:
        return None
    users = _line_users_load()
    rec = users.get(user_id, {})
    return {
        "user_id": user_id,
        "display_name": rec.get("display_name") or session.get("line_display_name") or "",
        "picture_url": rec.get("picture_url") or session.get("line_picture_url") or "",
        "nickname": rec.get("nickname") or session.get("line_nickname") or "",
        "updated_at": rec.get("updated_at") or "",
    }

def _line_current_worker_name():
    user = _line_current_user()
    if not user:
        return ""
    return (user.get("nickname") or user.get("display_name") or "").strip()

def _line_upsert_user(user_id, display_name="", picture_url="", nickname=None):
    now = datetime.now().isoformat()
    with _line_users_lock:
        users = _line_users_load()
        rec = users.get(user_id, {})
        rec.update({
            "user_id": user_id,
            "display_name": display_name or rec.get("display_name", ""),
            "picture_url": picture_url or rec.get("picture_url", ""),
            "updated_at": now,
        })
        if "created_at" not in rec:
            rec["created_at"] = now
        if nickname is not None:
            rec["nickname"] = nickname.strip()
            rec["nickname_updated_at"] = now
        users[user_id] = rec
        _line_users_save(users)
        return rec

def _http_post_form_json(url, form):
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))

def _http_get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))

def _line_config_missing_response():
    return (
        "<h2>LINEログイン設定待ち</h2>"
        "<p>LINE_LOGIN_CHANNEL_ID と LINE_LOGIN_CHANNEL_SECRET、または line_login.json を設定してください。</p>"
        "<p>コールバックURL: https://photo.j-cb.com/auth/line/callback</p>",
        503,
    )

@app.route("/auth/line/login")
def line_login():
    cfg = _line_login_config()
    if not cfg["enabled"]:
        return _line_config_missing_response()
    next_url = _safe_next_url(request.args.get("next") or request.referrer or "/")
    state = secrets.token_urlsafe(24)
    session.permanent = True
    session["line_oauth_state"] = state
    session["line_next"] = next_url
    params = {
        "response_type": "code",
        "client_id": cfg["channel_id"],
        "redirect_uri": cfg["redirect_uri"],
        "state": state,
        "scope": "profile openid",
        "bot_prompt": "normal",
    }
    return redirect("https://access.line.me/oauth2/v2.1/authorize?" + urllib.parse.urlencode(params))

@app.route("/auth/line/callback")
def line_callback():
    cfg = _line_login_config()
    if not cfg["enabled"]:
        return _line_config_missing_response()
    if request.args.get("error"):
        return f"LINEログインがキャンセルされました: {request.args.get('error_description') or request.args.get('error')}", 400
    state = request.args.get("state", "")
    if not state or state != session.get("line_oauth_state"):
        return "LINEログインのstateが一致しません。もう一度ログインしてください。", 400
    code = request.args.get("code", "")
    if not code:
        return "LINEログインコードがありません。", 400
    try:
        token_data = _http_post_form_json("https://api.line.me/oauth2/v2.1/token", {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cfg["redirect_uri"],
            "client_id": cfg["channel_id"],
            "client_secret": cfg["channel_secret"],
        })
        access_token = token_data.get("access_token")
        if not access_token:
            return "LINEアクセストークンを取得できませんでした。", 400
        profile = _http_get_json("https://api.line.me/v2/profile", {
            "Authorization": f"Bearer {access_token}",
        })
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        print(f"[LINE_LOGIN] HTTPError {e.code}: {body}", flush=True)
        return "LINEログイン連携に失敗しました。Channel Secret とコールバックURLを確認してください。", 400
    except Exception as e:
        print(f"[LINE_LOGIN] error: {e}", flush=True)
        return "LINEログイン連携に失敗しました。", 400

    user_id = profile.get("userId") or ""
    display_name = profile.get("displayName") or ""
    picture_url = profile.get("pictureUrl") or ""
    if not user_id:
        return "LINEユーザーIDを取得できませんでした。", 400

    rec = _line_upsert_user(user_id, display_name, picture_url)
    session.permanent = True
    session["line_user_id"] = user_id
    session["line_display_name"] = display_name
    session["line_picture_url"] = picture_url
    session["line_nickname"] = rec.get("nickname", "")
    session.pop("line_oauth_state", None)
    next_url = _safe_next_url(session.pop("line_next", None), "/")
    if not rec.get("nickname"):
        return redirect(url_for("line_nickname", next=next_url))
    return redirect(next_url)

@app.route("/auth/line/nickname", methods=["GET", "POST"])
def line_nickname():
    user_id = session.get("line_user_id")
    if not user_id:
        return redirect(url_for("line_login", next=_safe_next_url(request.args.get("next"), "/")))
    next_url = _safe_next_url(request.values.get("next"), "/")
    if request.method == "POST":
        if request.is_json:
            data = request.get_json(silent=True) or {}
            nickname = (data.get("nickname") or "").strip()
        else:
            nickname = (request.form.get("nickname") or "").strip()
        if not nickname:
            if request.is_json:
                return jsonify({"error": "ニックネームを入力してください"}), 400
            return "ニックネームを入力してください", 400
        if len(nickname) > 40:
            if request.is_json:
                return jsonify({"error": "ニックネームは40文字以内にしてください"}), 400
            return "ニックネームは40文字以内にしてください", 400
        rec = _line_upsert_user(
            user_id,
            session.get("line_display_name", ""),
            session.get("line_picture_url", ""),
            nickname=nickname,
        )
        session["line_nickname"] = rec.get("nickname", nickname)
        if request.is_json:
            return jsonify({"ok": True, "nickname": session["line_nickname"]})
        return redirect(next_url)

    user = _line_current_user() or {}
    current = html_lib.escape(user.get("nickname") or user.get("display_name") or "", quote=True)
    next_escaped = html_lib.escape(next_url, quote=True)
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LINEニックネーム設定</title>
<style>body{{font-family:system-ui,-apple-system,sans-serif;background:#f6f7f8;margin:0;padding:24px;color:#222}}main{{max-width:440px;margin:6vh auto;background:#fff;border:1px solid #ddd;border-radius:12px;padding:22px;box-shadow:0 8px 24px rgba(0,0,0,.08)}}label{{display:block;font-weight:800;margin-bottom:8px}}input{{width:100%;box-sizing:border-box;padding:14px;border:2px solid #d7d7d7;border-radius:10px;font-size:18px}}button{{width:100%;margin-top:14px;padding:14px;border:0;border-radius:10px;background:#06c755;color:white;font-size:17px;font-weight:800}}p{{line-height:1.6;color:#555}}</style></head>
<body><main><h1>ニックネーム設定</h1><p>出退勤・作業報告に表示する名前です。例: クロス 三好</p>
<form method="post"><input type="hidden" name="next" value="{next_escaped}"><label>表示名</label><input name="nickname" value="{current}" maxlength="40" required autofocus><button>この名前で使う</button></form></main></body></html>"""

@app.route("/auth/line/me")
def line_me():
    cfg = _line_login_config()
    next_url = _safe_next_url(request.args.get("next"), "/")
    user = _line_current_user()
    if not user:
        return jsonify({
            "logged_in": False,
            "configured": cfg["enabled"],
            "login_url": url_for("line_login", next=next_url),
        })
    return jsonify({
        "logged_in": True,
        "configured": cfg["enabled"],
        "user": {
            "display_name": user.get("display_name", ""),
            "picture_url": user.get("picture_url", ""),
            "nickname": user.get("nickname", ""),
        },
        "nickname": user.get("nickname", ""),
        "nickname_url": url_for("line_nickname", next=next_url),
        "logout_url": url_for("line_logout", next=next_url),
    })

@app.route("/auth/line/logout")
def line_logout():
    next_url = _safe_next_url(request.args.get("next"), "/")
    for key in ("line_user_id", "line_display_name", "line_picture_url", "line_nickname", "line_oauth_state", "line_next"):
        session.pop(key, None)
    return redirect(next_url)

def get_site_by_token(token):
    return load_sites().get(token)

def get_photos(token):
    photo_dir = UPLOAD_BASE / token / PATHS["photos"]
    if not photo_dir.exists():
        return []
    photos = []
    for p in sorted(photo_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.suffix.lower() in IMAGE_EXT:
            st = p.stat()
            dt = datetime.fromtimestamp(st.st_mtime)
            photos.append({
                "filename":   p.name,
                "date_label": dt.strftime("%Y年%m月%d日"),
                "date_sort":  dt.strftime("%Y%m%d"),
                "time":       dt.strftime("%H:%M"),
            })
    return photos

def get_complete_photos(token):
    photo_dir = UPLOAD_BASE / token / PATHS["complete_photos"]
    if not photo_dir.exists():
        return []
    photos = []
    for p in sorted(photo_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.suffix.lower() in IMAGE_EXT:
            st = p.stat()
            dt = datetime.fromtimestamp(st.st_mtime)
            photos.append({
                "filename":   p.name,
                "date_label": dt.strftime("%Y年%m月%d日"),
                "date_sort":  dt.strftime("%Y%m%d"),
                "time":       dt.strftime("%H:%M"),
            })
    return photos

def get_docs(token):
    docs_base = UPLOAD_BASE / token / PATHS["docs"]
    result = {}
    for cat in DOC_CATEGORIES:
        cat_dir = docs_base / cat
        if not cat_dir.exists():
            result[cat] = []
            continue
        # v8.0: このカテゴリ内の全stem→dxfマップを先に作る(PDFと同名のDXFを検出するため)
        dxf_stems = {}
        for p in cat_dir.iterdir():
            if p.suffix.lower() == '.dxf':
                dxf_stems[p.stem] = p.name
        files = []
        for p in sorted(cat_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if p.suffix.lower() in DOC_EXT:
                st = p.stat()
                dt = datetime.fromtimestamp(st.st_mtime)
                size = st.st_size
                if size >= 1024 * 1024:
                    size_str = f"{size/1024/1024:.1f}MB"
                else:
                    size_str = f"{size//1024}KB"
                ext_lower = p.suffix.lower()
                is_pdf = ext_lower == '.pdf'
                is_dxf = ext_lower == '.dxf'
                # v1.7.1: BMP/TIFF/HEIC も画像扱い。ただし直接表示できないものはサムネ必須
                is_image = ext_lower in IMAGE_EXT
                is_inline_image = ext_lower in {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
                # サムネイルURL
                thumb_url = ""
                if is_image:
                    # サムネファイルが存在すればそれを使う(BMP/TIFF等・または将来的な全画像統一)
                    thumb_jpg = UPLOAD_BASE / token / PATHS["thumbs"] / f"{p.stem}.jpg"
                    if thumb_jpg.exists():
                        thumb_url = f"/uploads/{token}/thumbs/{p.stem}.jpg"
                    elif is_inline_image:
                        # 旧データで直表示できるものはサムネ不在でも原本で代用
                        thumb_url = f"/p/{token}/download/{cat}/{p.name}"
                elif is_pdf:
                    # v9.7.1: .jpg と .png どちらも対応
                    thumb_jpg = UPLOAD_BASE / token / PATHS["thumbs"] / f"{p.stem}.jpg"
                    thumb_png = UPLOAD_BASE / token / PATHS["thumbs"] / f"{p.stem}.png"
                    if thumb_jpg.exists():
                        thumb_url = f"/uploads/{token}/thumbs/{p.stem}.jpg"
                    elif thumb_png.exists():
                        thumb_url = f"/uploads/{token}/thumbs/{p.stem}.png"
                # v8.0: PDFに紐付いているDXFがあるか
                linked_dxf = None
                if is_pdf and p.stem in dxf_stems:
                    linked_dxf = dxf_stems[p.stem]
                files.append({
                    "filename":  p.name,
                    "ext":       p.suffix.lower().lstrip('.').upper(),
                    "size":      size_str,
                    "date":      dt.strftime("%Y/%m/%d"),
                    "is_pdf":    is_pdf,
                    "is_dxf":    is_dxf,
                    "is_image":  is_image,
                    "thumb_url": thumb_url,
                    "linked_dxf": linked_dxf,
                })
        result[cat] = files
    return result

def get_memos(token):
    sites = load_sites()
    site = sites.get(token, {})
    return site.get("memos", [])

def get_hero_images(token):
    """資料タブのCGフォルダから画像を取得 → ヒーロースライドショーに使用
    CG = パース / 外観イメージ画像。資料タブでCGをアップすれば自動でトップに反映される。
    v1.7.1: BMP/TIFF/HEIC 等ブラウザで直接表示できない形式はサムネURLに差し替える。"""
    cg_dir = UPLOAD_BASE / token / PATHS["docs"] / "CG"
    if not cg_dir.exists():
        return []
    thumb_dir = UPLOAD_BASE / token / PATHS["thumbs"]
    images = []
    for p in sorted(cg_dir.iterdir(), key=lambda x: x.stat().st_mtime):
        ext = p.suffix.lower()
        if ext not in IMAGE_EXT:
            continue
        if ext in NON_INLINE_IMAGE_EXT:
            # TIFF/HEIC 等はサムネがあればそれを使い、なければスキップ
            thumb = thumb_dir / f"{p.stem}.jpg"
            if thumb.exists():
                images.append({"filename": p.name, "url": f"/uploads/{token}/thumbs/{p.stem}.jpg"})
            # サムネがなければ表示できないので除外
        else:
            images.append({"filename": p.name, "url": f"/p/{token}/view_doc/CG/{p.name}"})
    return images

def make_site_slug(site_info, token):
    """ファイル名用のslug (英数字)を生成
    優先順位: 1) site_info['name_en'] (手動設定) → 2) site_info['name']から英数字抽出 → 3) token先頭8文字"""
    slug = (site_info.get('name_en') or '').strip()
    if not slug:
        name = (site_info.get('name') or '').strip()
        # 英数字・ハイフン・アンダースコアのみ残す
        slug = re.sub(r'[^a-zA-Z0-9_\-]', '', name)
    if not slug:
        slug = (token[:8] or 'site').replace('/', '_').replace('\\', '_')
    return slug

def group_by_date(photos):
    groups = {}
    for p in photos:
        key = p['date_label']
        groups.setdefault(key, {'label': key, 'sort': p['date_sort'], 'photos': []})
        groups[key]['photos'].append(p)
    return sorted(groups.values(), key=lambda x: x['sort'], reverse=True)

# ---- ルーティング ----

@app.route("/")
def root():
    return redirect(url_for('admin'))

# v9.2: 職人・取引先向け現場一覧ページ
@app.route("/my-sites")
def my_sites():
    """全現場一覧 (シンプル版、スマホ向け)"""
    sites_dict = load_sites()
    site_list = []
    for token, info in sites_dict.items():
        site_list.append({
            "token": token,
            "name": info.get("name", ""),
            "address": info.get("address", ""),
            "memo": info.get("memo", ""),
            "created": info.get("created", ""),
            "url": url_for('site_page', token=token, _external=True),
        })
    # v1.8.9: アーカイブ・公開停止は my_sites に表示しない (active のみ)
    sites_dict = load_sites()
    site_list = [s for s in site_list if sites_dict.get(s['token'], {}).get('status', 'active') == 'active']
    site_list.sort(key=lambda x: x['created'], reverse=True)
    return render_template("my_sites.html", sites=site_list)

@app.route("/admin")
def admin():
    sites = load_sites()
    enriched = []
    for token, info in sites.items():
        photos    = get_photos(token)
        docs      = get_docs(token)
        doc_count = sum(len(v) for v in docs.values())
        enriched.append({
            "token":       token,
            "name":        info["name"],
            "name_en":     info.get("name_en", ""),  # v1.8.14
            "address":     info.get("address", ""),
            "memo":        info.get("memo", ""),
            "created":     info.get("created", ""),
            "status":      info.get("status", "active"),  # v1.8.9
            "photo_count": len(photos),
            "doc_count":   doc_count,
            "url": url_for('site_page', token=token, _external=True),
        })
    # active → archived → closed の順、各内では作成日新しい順
    status_order = {"active": 0, "archived": 1, "closed": 2}
    enriched.sort(key=lambda x: (status_order.get(x['status'], 9), x['created']), reverse=False)
    enriched.sort(key=lambda x: (status_order.get(x['status'], 9), -ord(x['created'][0]) if x['created'] else 0))
    # シンプルに status 優先 + 作成日降順
    enriched.sort(key=lambda x: (status_order.get(x['status'], 9), x['created'].replace('/','')), reverse=False)
    # 作成日降順にしたいので作り直し
    active = sorted([s for s in enriched if s['status']=='active'], key=lambda x: x['created'], reverse=True)
    archived = sorted([s for s in enriched if s['status']=='archived'], key=lambda x: x['created'], reverse=True)
    closed = sorted([s for s in enriched if s['status']=='closed'], key=lambda x: x['created'], reverse=True)
    enriched = active + archived + closed
    return render_template("admin.html", sites=enriched)

@app.route("/admin/new", methods=["POST"])
def new_site():
    name = request.form.get("site_name", "").strip()
    if not name:
        return redirect(url_for('admin'))
    # v1.8.8 任意: 既存現場の基本情報をコピー (同じマンションの別号室を素早く作る)
    copy_from = request.form.get("copy_from", "").strip()
    base = {}
    if copy_from:
        sites_now = load_sites()
        src = sites_now.get(copy_from)
        if src:
            for key in ('address','name_en','memo','matterport_url','lat','lng','lat_lng_set_at'):
                if key in src:
                    base[key] = src[key]
    token = secrets.token_urlsafe(10)
    sites = load_sites()
    sites[token] = {"name": name, "created": datetime.now().strftime("%Y/%m/%d %H:%M"), **base}
    save_sites(sites)
    # v2 layout: タブ⇔フォルダ 1:1
    (UPLOAD_BASE / token / PATHS["photos"]).mkdir(parents=True, exist_ok=True)
    (UPLOAD_BASE / token / PATHS["complete_photos"]).mkdir(parents=True, exist_ok=True)
    (UPLOAD_BASE / token / PATHS["complete_report"]).mkdir(parents=True, exist_ok=True)
    (UPLOAD_BASE / token / PATHS["complete_attach"]).mkdir(parents=True, exist_ok=True)
    (UPLOAD_BASE / token / PATHS["hero"]).mkdir(parents=True, exist_ok=True)
    (UPLOAD_BASE / token / PATHS["memo_images"]).mkdir(parents=True, exist_ok=True)
    (UPLOAD_BASE / token / PATHS["panoramas"]).mkdir(parents=True, exist_ok=True)
    (UPLOAD_BASE / token / PATHS["videos"]).mkdir(parents=True, exist_ok=True)
    (UPLOAD_BASE / token / PATHS["site_videos"]).mkdir(parents=True, exist_ok=True)
    (UPLOAD_BASE / token / PATHS["survey_photos"]).mkdir(parents=True, exist_ok=True)
    (UPLOAD_BASE / token / PATHS["survey_docs"]).mkdir(parents=True, exist_ok=True)
    (UPLOAD_BASE / token / PATHS["invoices"]).mkdir(parents=True, exist_ok=True)
    (UPLOAD_BASE / token / PATHS["thumbs"]).mkdir(parents=True, exist_ok=True)
    (UPLOAD_BASE / token / "工程").mkdir(parents=True, exist_ok=True)
    (UPLOAD_BASE / token / "地図").mkdir(parents=True, exist_ok=True)
    (UPLOAD_BASE / token / "_meta").mkdir(parents=True, exist_ok=True)
    for cat in DOC_CATEGORIES:
        (UPLOAD_BASE / token / PATHS["docs"] / cat).mkdir(parents=True, exist_ok=True)
    # v9.2: 現場追加時にメール通知(一覧URLも併記)
    site_url = f"{SITE_BASE_URL}/p/{token}"
    list_url = f"{SITE_BASE_URL}/my-sites"
    body = f"""新しい現場が追加されました。

現場名: {name}
日時: {sites[token]['created']}

▼ 現場ページ
{site_url}

▼ 現場一覧(全現場を確認)
{list_url}
"""
    send_notify("🆕 新規現場追加", body, name)
    return redirect(url_for('admin'))

@app.route("/admin/delete/<token>", methods=["POST"])
def delete_site(token):
    sites = load_sites()
    if token in sites:
        import shutil
        shutil.rmtree(UPLOAD_BASE / token, ignore_errors=True)
        del sites[token]
        save_sites(sites)
    return redirect(url_for('admin'))

@app.route("/admin/update/<token>", methods=["POST"])
def update_site_info(token):
    sites = load_sites()
    if token not in sites:
        abort(404)
    sites[token]["name"]    = request.form.get("name", sites[token]["name"]).strip()
    sites[token]["name_en"] = request.form.get("name_en", sites[token].get("name_en","")).strip()  # v1.8.14
    sites[token]["address"] = request.form.get("address", "").strip()
    sites[token]["memo"]    = request.form.get("memo", "").strip()
    save_sites(sites)
    return redirect(url_for('admin'))

@app.route("/admin/generate_thumbs")
def generate_thumbs():
    """既存PDFと画像のサムネイルを一括生成 (v1.7.1: BMP/TIFF/HEIC等の画像も対象)"""
    try:
        import fitz
    except ImportError:
        return "❌ PyMuPDF (fitz) がインストールされていません。requirements.txt に pymupdf を追加して docker compose build --no-cache してください。", 500
    try:
        from PIL import Image
    except ImportError:
        Image = None
    sites = load_sites()
    generated_pdf = 0
    generated_img = 0
    errors = 0
    for token in sites:
        docs_base = UPLOAD_BASE / token / PATHS["docs"]
        thumb_dir = UPLOAD_BASE / token / PATHS["thumbs"]
        thumb_dir.mkdir(parents=True, exist_ok=True)
        for cat in DOC_CATEGORIES:
            cat_dir = docs_base / cat
            if not cat_dir.exists():
                continue
            for p in cat_dir.iterdir():
                ext = p.suffix.lower()
                thumb_path = thumb_dir / f"{p.stem}.jpg"
                if thumb_path.exists():
                    continue  # 既にサムネあり
                if ext == '.pdf':
                    try:
                        doc = fitz.open(str(p))
                        page = doc[0]
                        mat = fitz.Matrix(2, 2)
                        pix = page.get_pixmap(matrix=mat)
                        pix.save(str(thumb_path))
                        doc.close()
                        generated_pdf += 1
                    except Exception:
                        errors += 1
                elif ext in IMAGE_EXT and Image is not None:
                    try:
                        with Image.open(str(p)) as img:
                            if img.mode in ('RGBA', 'LA', 'P'):
                                img = img.convert('RGB')
                            img.thumbnail((1200, 1200), Image.LANCZOS)
                            img.save(str(thumb_path), 'JPEG', quality=85)
                            generated_img += 1
                    except Exception:
                        errors += 1
    return f"✅ サムネイル生成完了: PDF {generated_pdf}件, 画像 {generated_img}件, エラー {errors}件"

@app.route("/p/<token>")
def site_page(token):
    site = get_site_by_token(token)
    if not site:
        abort(404)
    # v1.8.9: ステータスチェック (admin=1 ならどんな状態でも閲覧可)
    is_admin_view = request.args.get('admin') == '1'
    status = site.get('status', 'active')
    if status != 'active' and not is_admin_view:
        return _site_blocked_html(site, status), 200
    # v1.8.14: 英語表記 (name_en) を template に渡す
    photos = get_photos(token)
    groups = group_by_date(photos)
    complete_photos = get_complete_photos(token)
    complete_groups = group_by_date(complete_photos)
    docs   = get_docs(token)
    hero_images = get_hero_images(token)
    sites_data = load_sites()
    gantt_data = sites_data.get(token, {}).get('gantt', {})
    memos = get_memos(token)
    is_admin = request.args.get('admin') == '1'
    matterport_url = site.get('matterport_url', '')
    # v1.8.8: panoramas をヒーローに表示するため取得
    hero_panoramas = []
    try:
        pano_dir = UPLOAD_BASE / token / PATHS["panoramas"]
        if pano_dir.exists():
            for p in sorted(pano_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                if p.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp'):
                    continue
                stem = p.stem
                parts = stem.split('_', 2)
                title = parts[2].replace('_', ' ') if len(parts) >= 3 else stem
                hero_panoramas.append({
                    'filename': p.name,
                    'title': title,
                    'url': f"/uploads/{token}/panoramas/{p.name}",
                })
    except Exception as e:
        print(f"[site_page panoramas] {e}")
    return render_template("site.html",
                           token=token,
                           site_name=site['name'],
                           site_name_en=site.get('name_en',''),
                           site_address=site.get('address',''),
                           site_memo=site.get('memo',''),
                           hero_images=hero_images,
                           hero_panoramas=hero_panoramas,
                           groups=groups,
                           total_photos=len(photos),
                           complete_groups=complete_groups,
                           total_complete_photos=len(complete_photos),
                           docs=docs,
                           memos=memos,
                           matterport_url=matterport_url,
                           is_admin=is_admin,
                           gantt_json=json.dumps(gantt_data),
                           doc_categories=DOC_CATEGORIES)

@app.route("/p/<token>/post_memo", methods=["POST"])
def post_memo(token):
    sites = load_sites()
    if token not in sites:
        abort(404)
    text = request.form.get("text", "").strip()
    # LINEログイン済みならサーバー側のニックネームを優先する。
    # 未ログイン時だけ従来の手入力名を使う。
    line_author = _line_current_worker_name()
    author = line_author or request.form.get("author", "").strip()
    if not author or author == "名前なし":
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"error": "名前が空です。出退勤タブで名前を登録してください。"}), 400
        abort(400, "name_required")
    image_path = ""
    # 画像添付処理
    if "image" in request.files:
        img = request.files["image"]
        if img.filename:
            ext = os.path.splitext(img.filename)[1].lower()
            if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"):
                fname = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6] + ext
                memo_dir = str(UPLOAD_BASE / token / PATHS["memo_images"])
                os.makedirs(memo_dir, exist_ok=True)
                save_path = os.path.join(memo_dir, fname)
                img.save(save_path)
                image_path = f"/uploads/{token}/memo_images/{fname}"
    if text or image_path:
        memo = {
            "id": uuid.uuid4().hex[:8],
            "author": author,
            "text": text or "（画像のみ）",
            "created": datetime.now().strftime("%Y/%m/%d %H:%M"),
        }
        if image_path:
            memo["image"] = image_path
        sites[token].setdefault("memos", []).insert(0, memo)
        save_sites(sites)
        send_notify(f"📝 連絡メモ投稿 ({author})", f"現場: {sites[token].get('name','')}\n投稿者: {author}\n内容: {text[:100]}\n{datetime.now().strftime('%Y/%m/%d %H:%M')}", sites[token].get('name',''), token=token)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(memo if (text or image_path) else {})
    return redirect(url_for('site_page', token=token))

@app.route("/p/<token>/delete_memo/<memo_id>", methods=["POST"])
def delete_memo(token, memo_id):
    sites = load_sites()
    if token not in sites:
        abort(404)
    sites[token]["memos"] = [m for m in sites[token].get("memos", []) if m["id"] != memo_id]
    save_sites(sites)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True})
    return redirect(url_for('site_page', token=token))

# ===== メモ反応 =====
@app.route("/p/<token>/memo/<memo_id>/react", methods=["POST"])
def memo_react(token, memo_id):
    sites = load_sites()
    if token not in sites:
        abort(404)
    author = _line_current_worker_name() or request.form.get("author", "").strip()
    rtype = request.form.get("type", "").strip()
    if not author or not rtype:
        return jsonify({"error": "missing fields"}), 400
    memos = sites[token].get("memos", [])
    for m in memos:
        if m["id"] == memo_id:
            reactions = m.setdefault("reactions", [])
            # 同じ人の同じ反応は重複しない
            if not any(r["author"] == author and r["type"] == rtype for r in reactions):
                reactions.append({
                    "author": author,
                    "type": rtype,
                    "time": datetime.now().strftime("%H:%M")
                })
                save_sites(sites)
            return jsonify({"ok": True, "reactions": m.get("reactions", [])})
    return jsonify({"error": "memo not found"}), 404

@app.route("/p/<token>/memo/<memo_id>/unreact", methods=["POST"])
def memo_unreact(token, memo_id):
    sites = load_sites()
    if token not in sites:
        abort(404)
    author = _line_current_worker_name() or request.form.get("author", "").strip()
    rtype = request.form.get("type", "").strip()
    memos = sites[token].get("memos", [])
    for m in memos:
        if m["id"] == memo_id:
            m["reactions"] = [r for r in m.get("reactions", []) if not (r["author"] == author and r["type"] == rtype)]
            save_sites(sites)
            return jsonify({"ok": True, "reactions": m.get("reactions", [])})
    return jsonify({"error": "memo not found"}), 404

# ===== メモ返信 =====
@app.route("/p/<token>/memo/<memo_id>/reply", methods=["POST"])
def memo_reply(token, memo_id):
    sites = load_sites()
    if token not in sites:
        abort(404)
    author = _line_current_worker_name() or request.form.get("author", "").strip() or "名前なし"
    text = request.form.get("text", "").strip()
    if not text:
        return jsonify({"error": "empty text"}), 400
    memos = sites[token].get("memos", [])
    for m in memos:
        if m["id"] == memo_id:
            reply = {
                "id": uuid.uuid4().hex[:8],
                "author": author,
                "text": text,
                "created": datetime.now().strftime("%Y/%m/%d %H:%M"),
            }
            m.setdefault("replies", []).append(reply)
            save_sites(sites)
            return jsonify(reply)
    return jsonify({"error": "memo not found"}), 404

@app.route("/p/<token>/memo/<memo_id>/delete_reply/<reply_id>", methods=["POST"])
def delete_reply(token, memo_id, reply_id):
    sites = load_sites()
    if token not in sites:
        abort(404)
    memos = sites[token].get("memos", [])
    for m in memos:
        if m["id"] == memo_id:
            m["replies"] = [r for r in m.get("replies", []) if r["id"] != reply_id]
            save_sites(sites)
            return jsonify({"ok": True})
    return jsonify({"error": "not found"}), 404

@app.route("/p/<token>/gantt")
def get_gantt(token):
    site = get_site_by_token(token)
    if not site:
        abort(404)
    sites = load_sites()
    return jsonify(sites[token].get("gantt", {}))

@app.route("/p/<token>/gantt", methods=["POST"])
def save_gantt(token):
    sites = load_sites()
    if token not in sites:
        abort(404)
    data = request.get_json()
    sites[token]["gantt"] = data
    save_sites(sites)
    return jsonify({"ok": True})

# ===== 工程表スケジュール（ganttApp用） =====
@app.route("/p/<token>/schedule", methods=["GET"])
def get_schedule(token):
    site = get_site_by_token(token)
    if not site:
        abort(404)
    with _get_schedule_lock(token):
        return jsonify(_read_schedule_safe(token))

@app.route("/p/<token>/schedule", methods=["POST"])
def save_schedule(token):
    site = get_site_by_token(token)
    if not site:
        abort(404)
    proj_path = UPLOAD_BASE / token
    proj_path.mkdir(parents=True, exist_ok=True)
    sched_file = proj_path / PATHS["schedule.json"]
    data = request.get_json()
    with _get_schedule_lock(token):
        _atomic_write_json(sched_file, data)
    sites = load_sites()
    send_notify("📅 工程表更新", f"現場: {sites.get(token,{}).get('name','')}\n工程表が更新されました\n{datetime.now().strftime('%Y/%m/%d %H:%M')}", sites.get(token,{}).get('name',''), token=token)
    return jsonify({"ok": True})

# ===== 出退勤報告 (v9.1) =====
def _haversine_m(lat1, lng1, lat2, lng2):
    """2点間の距離(メートル)。GPSの誤差検証用、簡易版で十分"""
    import math
    R = 6371000.0
    p1 = math.radians(lat1); p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1); dl = math.radians(lng2 - lng1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(a))

@app.route("/p/<token>/attendance", methods=["POST"])
def report_attendance(token):
    """職人の現場入り/完了退勤を記録。工程表に追加+管理者にメール通知。
    v1.8.5: GPS座標も任意で受け取り、初回をサイト基準点として保存する自動ジオフェンス。"""
    site = get_site_by_token(token)
    if not site:
        abort(404)
    data = request.get_json(force=True, silent=True) or {}
    line_worker = _line_current_worker_name()
    worker = line_worker or (data.get('worker') or '').strip()
    action = data.get('action')  # 'in' or 'out'
    if not worker:
        return jsonify({'error': '作業者名が必要です'}), 400
    if len(worker) > 100:
        # v1.8.7: 異常な長さを拒否(攻撃/バグ防止)
        return jsonify({'error': '作業者名が長すぎます (100文字以内)'}), 400
    if action not in ('in', 'out'):
        return jsonify({'error': 'アクションが不正'}), 400

    # v1.8.5 GPS: 任意。初回は現場基準点として記録、2回目以降は距離も返す
    gps_lat = data.get('lat'); gps_lng = data.get('lng'); gps_acc = data.get('accuracy')
    distance_m = None
    site_lat = site.get('lat'); site_lng = site.get('lng')
    try:
        if gps_lat is not None and gps_lng is not None:
            gps_lat = float(gps_lat); gps_lng = float(gps_lng)
            if -90<=gps_lat<=90 and -180<=gps_lng<=180:
                if site_lat is None or site_lng is None:
                    # 初回: サイト位置として記録
                    sites = load_sites()
                    if token in sites:
                        sites[token]['lat'] = gps_lat
                        sites[token]['lng'] = gps_lng
                        sites[token]['lat_lng_set_at'] = datetime.now().isoformat()
                        save_sites(sites)
                else:
                    distance_m = round(_haversine_m(float(site_lat), float(site_lng), gps_lat, gps_lng))
    except Exception as _e:
        print(f"[ATTENDANCE GPS] {_e}")

    now = datetime.now()
    action_label = '🚶 現場入り' if action == 'in' else '🏁 完了・退勤'
    site_name = site.get('name', '')
    
    # ---- v1.8.8: 工程表に該当業者のタスクが今日無ければ自動追加 ----
    if action == 'in':
        try:
            _ensure_gantt_task_for_worker(token, worker, now.strftime("%Y-%m-%d"))
        except Exception as _e:
            print(f"[ENSURE_GANTT] {_e}")

    # ---- v1.8.7: attendance.jsonl に append-only で追記 (lock 直列化を廃止) ----
    gantt_added = False
    try:
        today_str = now.strftime("%Y-%m-%d")
        task_name = f"{worker}: {action_label} ({now.strftime('%H:%M')})"
        new_task = {
            "id": f"att_{uuid.uuid4().hex[:10]}",
            "name": task_name,
            "start": today_str,
            "end": today_str,
            "progress": 100 if action == 'out' else 0,
            "color": "#4caf50" if action == 'in' else "#ff5a1f",
            "is_attendance": True,
            "worker": worker,
            "action": action,
            "timestamp": now.isoformat(),
            "lat": gps_lat if gps_lat is not None else None,
            "lng": gps_lng if gps_lng is not None else None,
            "gps_accuracy": gps_acc,
            "distance_m": distance_m,
            "line_user_id": session.get("line_user_id") if line_worker else None,
        }
        _attendance_append(token, new_task)
        gantt_added = True
    except Exception as e:
        print(f"[ATTENDANCE ERROR - jsonl] {e}")
    
    # ---- 管理者にメール通知 ----
    mail_sent = False
    try:
        subject = f"{action_label} - {worker}"
        body = f"""現場: {site_name}
作業者: {worker}
ステータス: {action_label}
日時: {now.strftime('%Y/%m/%d %H:%M')}

"""
        if action == 'in':
            body += "職人さんが現場入りしました。"
        else:
            body += "職人さんが本日の作業を完了し、退勤しました。"
        # send_notifyはSMTP_USER/PASS設定時のみ実際に送る(失敗しても例外は投げない)
        send_notify(subject, body, site_name, token=token)
        mail_sent = bool(os.environ.get('SMTP_USER') or True)  # SMTP設定されてれば送信試みる
    except Exception as e:
        print(f"[ATTENDANCE ERROR - mail] {e}")
        mail_sent = False
    
    return jsonify({
        'ok': True,
        'worker': worker,
        'action': action,
        'timestamp': now.isoformat(),
        'gantt_added': gantt_added,
        'mail_sent': mail_sent,
        'distance_m': distance_m,
        'site_pinned': site_lat is not None and site_lng is not None,
        'line_authenticated': bool(line_worker),
    })

@app.route("/p/<token>/attendance/today", methods=["GET"])
def attendance_today(token):
    """v1.8.7: 今日の出退勤エントリ一覧 (jsonl + 旧schedule.json後方互換)"""
    site = get_site_by_token(token)
    if not site:
        abort(404)
    today_str = datetime.now().strftime("%Y-%m-%d")
    items = []
    try:
        for t in _attendance_read_all(token, only_today=True):
            ts = t.get("timestamp", "")
            items.append({
                "id": t.get("id"),
                "worker": t.get("worker", ""),
                "action": t.get("action", ""),
                "timestamp": ts,
                "time": ts[11:16] if len(ts) >= 16 else ""
            })
    except Exception as e:
        print(f"[ATTENDANCE_TODAY ERROR] {e}")
    items.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return jsonify({"items": items, "date": today_str})

@app.route("/p/<token>/attendance/<task_id>/cancel", methods=["POST"])
def cancel_attendance(token, task_id):
    """v1.8.4: 出退勤の誤報告を取消(該当タスクを schedule.json から削除)。"""
    site = get_site_by_token(token)
    if not site:
        abort(404)
    if '/' in task_id or '\\' in task_id or '..' in task_id:
        abort(400)
    try:
        all_recs = _attendance_read_all(token, only_today=False)
        target = next((r for r in all_recs if r.get('id') == task_id), None)
        if not target:
            return jsonify({"error": "該当の出退勤履歴が見つかりません"}), 404
        _attendance_cancel(token, task_id)
        # v1.8.8 取消を管理者にメール通知 (誤取消検出のため)
        try:
            site_name = site.get('name', '')
            worker = target.get('worker', '?')
            action = target.get('action', '?')
            action_label = '🚶 現場入り' if action == 'in' else '🏁 完了・退勤'
            ts = target.get('timestamp', '?')[:19]
            subject = f"⚠️ 取消: {worker} の {action_label}"
            body = f"""【出退勤の取消が発生しました】

現場: {site_name}
作業者: {worker}
取消対象: {action_label}
打刻日時: {ts}
取消日時: {datetime.now().strftime('%Y/%m/%d %H:%M:%S')}

⚠️ 業者が誤って取消した可能性があります。必要に応じて業者に確認してください。
※ 復活が必要な場合は、サーバー側で attendance.jsonl の tombstone 行を削除すれば元に戻ります。
"""
            send_notify(subject, body, site_name, token=token)
        except Exception as _e:
            print(f"[CANCEL_NOTIFY] {_e}")
        return jsonify({"ok": True, "removed_id": task_id})
    except Exception as e:
        print(f"[CANCEL_ATTENDANCE ERROR] {e}")
        return jsonify({"error": str(e)}), 500

_weather_cache = {}  # token -> {data, fetched_at}
_WEATHER_TTL_SEC = 30 * 60  # 30 分キャッシュ (Open-Meteo へのアクセス過多防止)

# v_workermerge_20260505: 業者名統合用ノーマライズ関数
def _norm_worker_name(s):
    """業者名の正規化: 括弧内補足/空白/スラッシュ/アンダースコア除去 + 小文字化。
    「クロス三好」「クロス 三好」「クロス　三好」「クロス/三好」「クロス三好(クロスパテ)」 → 全て同一とみなす"""
    import re as _re_w
    if not s: return ''
    s = _re_w.sub(r'[(（].*?[)）]', '', s)
    return s.replace('/', '').replace(' ', '').replace('　','').replace('_','').lower().strip()

def _ensure_gantt_task_for_worker(token, worker, today_str):
    """v_workermerge_20260505: 業者が現場入りした時、工程表(gantt)に当該業者の今日のタスクが無ければ自動追加。
    括弧内補足(クロスパテ等)・空白・スラッシュを無視して同一業者と判定。"""
    if not worker: return
    norm = _norm_worker_name(worker)
    if not norm: return
    AUTO_COLORS = ['#6F4D5B','#C68874','#5C7993','#7E8B6C','#C9A75C','#5C2A2E','#8C7E72','#6E7C5C','#455563']
    with _get_schedule_lock(token):
        sched_data = _read_schedule_safe(token)
        tasks = sched_data.get('tasks', [])
        # 既存タスクで今日がレンジ内、worker と部分一致するものを探す
        for t in tasks:
            if t.get('is_attendance'): continue
            tname = _norm_worker_name(t.get('name') or '')
            if not tname: continue
            if (norm in tname) or (tname in norm) or norm == tname:
                start = t.get('start',''); end = t.get('end','')
                if start and end and start <= today_str <= end:
                    return  # 既に存在
        # 自動追加
        color = AUTO_COLORS[len(tasks) % len(AUTO_COLORS)]
        new_task = {
            'id': f'auto_{uuid.uuid4().hex[:10]}',
            'name': worker,
            'desc': '(自動追加)',
            'start': today_str,
            'end': today_str,
            'color': color,
            'auto_added': True,
        }
        tasks.append(new_task)
        sched_data['tasks'] = tasks
        _atomic_write_json(UPLOAD_BASE / token / PATHS["schedule.json"], sched_data)

def _heatstroke_level(apparent_temp_c, humidity_pct):
    """環境省 暑さ指数(WBGT) の運用区分に近い5段階を体感温度から簡易判定。
    注意! 屋外炎天下と日陰では誤差大、あくまで目安。
    返す: (level 0-4, label, color, advice)"""
    t = apparent_temp_c if apparent_temp_c is not None else 0
    if t >= 35:
        return (4, '🚨 危険', '#7b1fa2', '原則屋外作業中止。やむを得ない場合は厳重監視と頻繁な水分塩分補給。')
    if t >= 31:
        return (3, '⚠️ 厳重警戒', '#c62828', '激しい運動/作業は中止。30分毎に水分補給と休憩。')
    if t >= 28:
        return (2, '🟠 警戒', '#ef6c00', '激しい作業時は休憩を多めに。1時間に1回以上水分補給。')
    if t >= 25:
        return (1, '🟡 注意', '#f9a825', '通常の作業はOK。長時間作業時は水分補給を意識。')
    return (0, '🟢 ほぼ安全', '#2e7d32', '通常通りでOK。')

def _weather_code_to_jp(code):
    """Open-Meteo の WMO weather code を日本語+絵文字に。"""
    if code is None: return ('—', '❓')
    m = {
        0:('快晴','☀️'),1:('晴れ','🌤'),2:('一部曇り','⛅'),3:('曇り','☁️'),
        45:('霧','🌫'),48:('着氷霧','🌫'),
        51:('小雨','🌦'),53:('雨','🌦'),55:('強い雨','🌧'),
        56:('凍雨','🌧'),57:('凍雨','🌧'),
        61:('小雨','🌦'),63:('雨','🌧'),65:('大雨','🌧'),
        66:('みぞれ','🌨'),67:('みぞれ','🌨'),
        71:('小雪','🌨'),73:('雪','❄️'),75:('大雪','❄️'),
        77:('雪粒','❄️'),
        80:('にわか雨','🌦'),81:('にわか雨','🌧'),82:('豪雨','⛈'),
        85:('にわか雪','❄️'),86:('にわか雪','❄️'),
        95:('雷雨','⛈'),96:('雹混じり雷雨','⛈'),99:('激しい雷雨','⛈'),
    }
    return m.get(code, ('—', '❓'))

@app.route("/p/<token>/weather", methods=["GET"])
def site_weather(token):
    """v1.8.6: 現場 GPS から Open-Meteo を叩いて天気と熱中症指数を返す。
    - 緯度経度は出退勤打刻で自動学習されたものを使う(なければ住所のジオコーディング簡易フォールバック)
    - 30分キャッシュでAPI叩き過ぎ防止
    - AI 不使用 (オープンデータの天気APIのみ)
    """
    site = get_site_by_token(token)
    if not site:
        abort(404)
    # キャッシュ確認
    now_ts = datetime.now().timestamp()
    cached = _weather_cache.get(token)
    if cached and now_ts - cached['fetched_at'] < _WEATHER_TTL_SEC:
        return jsonify(cached['data'])

    lat = site.get('lat'); lng = site.get('lng')
    if lat is None or lng is None:
        return jsonify({
            'available': False,
            'reason': 'GPS未設定（誰か1人でも現場入りすればGPS自動取得して天気が表示されます）'
        })
    try:
        url = (
            'https://api.open-meteo.com/v1/forecast'
            f'?latitude={float(lat):.4f}&longitude={float(lng):.4f}'
            '&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day'
            '&hourly=temperature_2m,apparent_temperature,weather_code,precipitation_probability'
            '&forecast_hours=12'
            '&timezone=Asia%2FTokyo'
        )
        with urllib.request.urlopen(url, timeout=6) as r:
            wd = json.loads(r.read().decode('utf-8'))
        cur = wd.get('current', {})
        temp = cur.get('temperature_2m')
        app_t = cur.get('apparent_temperature')
        hum = cur.get('relative_humidity_2m')
        wcode = cur.get('weather_code')
        wind = cur.get('wind_speed_10m')
        is_day = cur.get('is_day', 1)
        hs_level, hs_label, hs_color, hs_advice = _heatstroke_level(app_t, hum)
        wname, wicon = _weather_code_to_jp(wcode)
        # 12時間先まで時系列(雨予報を業者にプッシュするための予備データ)
        hourly = wd.get('hourly', {})
        forecast = []
        for i, t in enumerate(hourly.get('time', [])[:12]):
            forecast.append({
                'time': t[-5:],  # HH:MM
                'temp': hourly.get('temperature_2m', [None]*99)[i],
                'app_temp': hourly.get('apparent_temperature', [None]*99)[i],
                'precip_prob': hourly.get('precipitation_probability', [None]*99)[i],
                'code': hourly.get('weather_code', [None]*99)[i],
            })
        result = {
            'available': True,
            'fetched_at': datetime.now().isoformat(),
            'temp': temp,
            'apparent_temp': app_t,
            'humidity': hum,
            'wind_kmh': wind,
            'is_day': bool(is_day),
            'weather_code': wcode,
            'weather_name': wname,
            'weather_icon': wicon,
            'heatstroke': {
                'level': hs_level,         # 0-4
                'label': hs_label,
                'color': hs_color,
                'advice': hs_advice,
            },
            'forecast_12h': forecast,
            'lat': float(lat), 'lng': float(lng),
        }
        _weather_cache[token] = {'data': result, 'fetched_at': now_ts}
        return jsonify(result)
    except Exception as e:
        print(f"[WEATHER ERROR] {e}")
        return jsonify({'available': False, 'reason': f'天気API取得失敗: {e}'}), 200

@app.route("/p/<token>/ar_measure")
def ar_measure(token):
    """v1.8.6: WebXR (Chrome Android) で 2点タップ採寸。
    iOS Safari は WebXR 非対応。"""
    site = get_site_by_token(token)
    if not site:
        abort(404)
    return render_template("ar_measure.html", token=token, site_name=site.get("name", ""))

@app.route("/p/<token>/online_workers", methods=["GET"])
def online_workers(token):
    """schedule.json の is_attendance タスクから、今日 in したけど out していない作業者を返す"""
    site = get_site_by_token(token)
    if not site:
        abort(404)
    today_str = datetime.now().strftime("%Y-%m-%d")
    workers_state = {}  # worker -> (last_action, last_ts)
    try:
        for t in _attendance_read_all(token, only_today=True):
            ts = t.get("timestamp", "")
            w = (t.get("worker") or "").strip()
            a = t.get("action")
            if not w or a not in ("in", "out"):
                continue
            prev = workers_state.get(w)
            if prev is None or ts > prev[1]:
                workers_state[w] = (a, ts)
    except Exception as e:
        print(f"[ONLINE_WORKERS ERROR] {e}")
    # v1.8.8 アクティブ/非アクティブ集計
    online = sorted([w for w, (a, _) in workers_state.items() if a == "in"])
    offline = sorted([w for w, (a, _) in workers_state.items() if a == "out"])
    total = len(workers_state)
    active = len(online)
    rate = round(active / total * 100) if total > 0 else 0
    return jsonify({
        "count": active,
        "workers": online,
        "active_count": active,
        "active_workers": online,
        "inactive_count": len(offline),
        "inactive_workers": offline,
        "total_today": total,
        "rate_pct": rate,  # 稼働率(%)
        "date": today_str
    })

# ===== Matterport 3Dウォークスルー =====
@app.route("/p/<token>/matterport", methods=["POST"])
def save_matterport(token):
    sites = load_sites()
    if token not in sites:
        abort(404)
    data = request.get_json()
    url = data.get('url', '').strip()
    # MatterportのシェアURLを埋め込み用に変換
    if url and 'my.matterport.com/show' in url and '&play=1' not in url:
        if '?' in url:
            url += '&play=1'
        else:
            url += '?play=1'
    sites[token]['matterport_url'] = url
    save_sites(sites)
    if url:
        send_notify("🏠 3Dウォークスルー設定", f"現場: {sites[token].get('name','')}\nMatterport URLが設定されました", sites[token].get('name',''), token=token)
    return jsonify({"ok": True})

@app.route("/p/<token>/upload_hero", methods=["POST"])
def upload_hero(token):
    if not get_site_by_token(token):
        abort(404)
    hero_dir = UPLOAD_BASE / token / PATHS["hero"]
    hero_dir.mkdir(parents=True, exist_ok=True)
    uploaded = 0
    for f in request.files.getlist("hero"):
        if f and f.filename and Path(f.filename).suffix.lower() in IMAGE_EXT:
            ext = Path(secure_filename(f.filename)).suffix.lower()
            ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
            f.save(hero_dir / f"{ts}_{uuid.uuid4().hex[:6]}{ext}")
            uploaded += 1
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"uploaded": uploaded})
    return redirect(url_for('site_page', token=token))

@app.route("/p/<token>/delete_hero/<filename>", methods=["POST"])
def delete_hero(token, filename):
    if not get_site_by_token(token):
        abort(404)
    fp = UPLOAD_BASE / token / PATHS["hero"] / secure_filename(filename)
    if fp.exists():
        fp.unlink()
    return redirect(url_for('site_page', token=token))

@app.route("/uploads/<token>/hero/<filename>")
def serve_hero(token, filename):
    return send_from_directory(UPLOAD_BASE / token / PATHS["hero"], filename)

@app.route("/p/<token>/upload_photo", methods=["POST"])
def upload_photo(token):
    if not get_site_by_token(token):
        abort(404)
    photo_dir = UPLOAD_BASE / token / PATHS["photos"]
    photo_dir.mkdir(parents=True, exist_ok=True)
    uploaded = 0
    for f in request.files.getlist("photos"):
        if f and f.filename and Path(f.filename).suffix.lower() in IMAGE_EXT:
            ext = Path(secure_filename(f.filename)).suffix.lower()
            ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
            f.save(photo_dir / f"{ts}_{uuid.uuid4().hex[:6]}{ext}")
            uploaded += 1
    if uploaded > 0:
        site = load_sites().get(token, {})
        send_notify(f"📷 写真{uploaded}枚追加", f"現場: {site.get('name','')}\n{uploaded}枚の写真がアップロードされました\n{datetime.now().strftime('%Y/%m/%d %H:%M')}", site.get('name',''), token=token)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"uploaded": uploaded})
    return redirect(url_for('site_page', token=token))

# ===== 完工写真 =====
@app.route("/p/<token>/upload_complete_photo", methods=["POST"])
def upload_complete_photo(token):
    if not get_site_by_token(token):
        abort(404)
    photo_dir = UPLOAD_BASE / token / PATHS["complete_photos"]
    photo_dir.mkdir(parents=True, exist_ok=True)
    uploaded = 0
    for f in request.files.getlist("photos"):
        if f and f.filename and Path(f.filename).suffix.lower() in IMAGE_EXT:
            ext = Path(secure_filename(f.filename)).suffix.lower()
            ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
            f.save(photo_dir / f"{ts}_{uuid.uuid4().hex[:6]}{ext}")
            uploaded += 1
    if uploaded > 0:
        site = load_sites().get(token, {})
        send_notify(f"✅ 完工写真{uploaded}枚追加", f"現場: {site.get('name','')}\n{uploaded}枚の完工写真がアップロードされました\n{datetime.now().strftime('%Y/%m/%d %H:%M')}", site.get('name',''), token=token)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"uploaded": uploaded})
    return redirect(url_for('site_page', token=token))

@app.route("/p/<token>/complete_photos_pdf")
def complete_photos_pdf(token):
    """v1.8.8 完工写真をA4 PDFアルバムとして書き出す。納品レポート用。
    解像度キープのため、各画像は元サイズで埋め込む(reportlab に任せて自動 LZW 圧縮)。"""
    site = get_site_by_token(token)
    if not site:
        abort(404)
    photo_dir = UPLOAD_BASE / token / PATHS["complete_photos"]
    if not photo_dir.exists():
        return jsonify({'error': '完工写真がありません'}), 404
    photos = sorted(photo_dir.iterdir(), key=lambda p: p.stat().st_mtime)
    photos = [p for p in photos if p.is_file() and p.suffix.lower() in IMAGE_EXT]
    if not photos:
        return jsonify({'error': '完工写真がありません'}), 404
    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.pdfgen import canvas as rlc
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.lib.utils import ImageReader
        try:
            pdfmetrics.registerFont(UnicodeCIDFont('HeiseiKakuGo-W5'))
            font_jp = 'HeiseiKakuGo-W5'
        except Exception:
            font_jp = 'Helvetica'
        from PIL import Image
        import io as _io
        buf = _io.BytesIO()
        # v1.8.8: A4 横向き + 1ページ2枚 (横並び) で写真を最大サイズに
        page_size = landscape(A4)
        page_w, page_h = page_size
        c = rlc.Canvas(buf, pagesize=page_size)
        c.setPageCompression(1)  # PDFストリーム圧縮 (JPEG画像はそのまま)
        site_name = site.get('name', '')
        cols, rows = 2, 1
        per_page = cols * rows
        margin = 10*mm; gutter = 6*mm
        header_h = 14*mm
        cell_w = (page_w - 2*margin - (cols-1)*gutter) / cols
        cell_h = page_h - 2*margin - header_h - 8*mm  # キャプション 8mm
        total_pages = (len(photos)+per_page-1)//per_page
        for idx, p in enumerate(photos):
            page_idx = idx // per_page
            slot = idx % per_page
            if slot == 0 and idx > 0:
                c.showPage()
            if slot == 0:
                # ヘッダ
                c.setFont(font_jp, 13)
                c.drawString(margin, page_h - margin - 6*mm, f'完工写真アルバム  ·  {site_name}')
                c.setFont(font_jp, 9)
                c.setFillGray(0.4)
                c.drawString(margin, page_h - margin - 11*mm,
                             f'{datetime.now().strftime("%Y/%m/%d %H:%M")} 出力  ·  ページ {page_idx+1}/{total_pages}  ·  全 {len(photos)} 枚')
                c.setFillGray(0)
            col = slot
            x = margin + col * (cell_w + gutter)
            y = margin + 8*mm  # 下にキャプション余白
            try:
                # 元画像のアスペクト比でフィット (reportlab は JPEG をそのまま埋込み = 無劣化)
                with Image.open(p) as img:
                    img_w, img_h = img.size
                aspect = img_w / img_h
                if aspect > cell_w / cell_h:
                    draw_w = cell_w; draw_h = cell_w / aspect
                else:
                    draw_h = cell_h; draw_w = cell_h * aspect
                dx = x + (cell_w - draw_w) / 2
                dy = y + (cell_h - draw_h) / 2
                # ImageReader で渡すと reportlab が JPEG をそのまま埋込み → 画質劣化なし
                img_reader = ImageReader(str(p))
                c.drawImage(img_reader, dx, dy, draw_w, draw_h,
                            preserveAspectRatio=True, anchor='c', mask='auto')
                # キャプション
                c.setFont(font_jp, 8)
                c.setFillGray(0.3)
                cap = datetime.fromtimestamp(p.stat().st_mtime).strftime('%Y/%m/%d %H:%M')
                c.drawString(dx, y - 4*mm, f'#{idx+1}  ·  {cap}  ·  {img_w}×{img_h}px')
                c.setFillGray(0)
            except Exception as e:
                print(f"[COMPLETE_PDF] {p.name}: {e}")
        c.save()
        buf.seek(0)
        fname = f'完工写真_{site_name}_{datetime.now().strftime("%Y%m%d")}.pdf'
        return send_file(buf, mimetype='application/pdf', as_attachment=True, download_name=fname)
    except Exception as e:
        print(f"[COMPLETE_PDF FATAL] {e}")
        return jsonify({'error': str(e)}), 500

@app.route("/p/<token>/photo/<filename>/copy_to_complete", methods=["POST"])
def copy_photo_to_complete(token, filename):
    """v1.8.8 既存の通常写真を完工写真に複製する。
    既に完工側に同名があれば上書きはせず別名(suffix _copy)で保存。"""
    if not get_site_by_token(token):
        abort(404)
    safe = _safe_basename(filename)
    src = UPLOAD_BASE / token / PATHS["photos"] / safe
    if not src.exists():
        return jsonify({"ok": False, "error": "元の写真が見つかりません"}), 404
    dst_dir = UPLOAD_BASE / token / PATHS["complete_photos"]
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / safe
    if dst.exists():
        # 既存があれば別名で
        stem = src.stem; suf = src.suffix
        i = 1
        while True:
            candidate = dst_dir / f"{stem}_copy{i}{suf}"
            if not candidate.exists():
                dst = candidate; break
            i += 1
            if i > 99:
                return jsonify({"ok": False, "error": "コピー先が多すぎます"}), 500
    try:
        import shutil
        shutil.copy2(src, dst)
        return jsonify({"ok": True, "copied_to": dst.name})
    except Exception as e:
        print(f"[COPY_TO_COMPLETE ERROR] {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/uploads/<token>/complete_photos/<filename>")
def serve_complete_photo(token, filename):
    return send_from_directory(UPLOAD_BASE / token / PATHS["complete_photos"], filename)

@app.route("/p/<token>/delete_complete_photo/<filename>", methods=["POST"])
def delete_complete_photo(token, filename):
    if not get_site_by_token(token):
        abort(404)
    fp = UPLOAD_BASE / token / PATHS["complete_photos"] / secure_filename(filename)
    if fp.exists():
        fp.unlink()
    return redirect(url_for('site_page', token=token))

@app.route("/p/<token>/upload_doc", methods=["POST"])
def upload_doc(token):
    if not get_site_by_token(token):
        abort(404)
    category = request.form.get("category", "その他")
    if category not in DOC_CATEGORIES:
        category = "その他"
    cat_dir = UPLOAD_BASE / token / PATHS["docs"] / category
    cat_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir = UPLOAD_BASE / token / PATHS["thumbs"]
    thumb_dir.mkdir(parents=True, exist_ok=True)
    uploaded = 0
    # v8.0: 同一リクエスト内のファイルは同じstem(ts+uuid)を共有して自動ペアリング
    shared_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    shared_uuid = uuid.uuid4().hex[:6]
    files_list = request.files.getlist("docs")
    # 同じ拡張子が複数含まれる場合は個別uuidを振る必要がある
    from collections import Counter
    ext_counts = Counter(Path(f.filename).suffix.lower() for f in files_list if f and f.filename)
    use_shared_stem = all(c == 1 for c in ext_counts.values())  # 同一拡張子重複なし → 共有stem使用
    
    for f in files_list:
        if not f or not f.filename:
            continue
        original = f.filename
        ext = Path(original).suffix.lower()
        if ext not in DOC_EXT:
            continue
        # 同一アップロードで異なる拡張子(例: pdf+dxf)ならstem共有、同一拡張子が複数あれば個別
        if use_shared_stem:
            safe_name = f"{shared_ts}_{shared_uuid}{ext}"
        else:
            safe_name = f"{shared_ts}_{uuid.uuid4().hex[:6]}{ext}"
        dest = cat_dir / safe_name
        f.save(dest)
        uploaded += 1
        # PDFサムネイル生成
        if ext == '.pdf':
            try:
                import fitz
                doc = fitz.open(str(dest))
                page = doc[0]
                mat = fitz.Matrix(2, 2)
                pix = page.get_pixmap(matrix=mat)
                thumb_path = thumb_dir / f"{dest.stem}.jpg"
                pix.save(str(thumb_path))
                doc.close()
            except Exception:
                pass
        # v1.7.1: 画像ファイル(CG等)のサムネイル生成 — BMP/TIFF/HEICのようにブラウザ表示できないものは
        # Pillowで変換してJPGサムネ化。JPG/PNG等はそのままでも表示できるが統一のためサムネ生成
        elif ext in IMAGE_EXT:
            try:
                from PIL import Image
                with Image.open(str(dest)) as img:
                    if img.mode in ('RGBA', 'LA', 'P'):
                        img = img.convert('RGB')
                    # サムネは最大1200px (長辺基準) — 一覧表示用なので画質より軽さ優先
                    img.thumbnail((1200, 1200), Image.LANCZOS)
                    thumb_path = thumb_dir / f"{dest.stem}.jpg"
                    img.save(str(thumb_path), 'JPEG', quality=85)
            except Exception as e:
                print(f"[THUMB ERROR - image] {dest.name}: {e}")
    if uploaded > 0:
        site = load_sites().get(token, {})
        send_notify(f"📄 資料{uploaded}件追加 ({category})", f"現場: {site.get('name','')}\nカテゴリ: {category}\n{uploaded}件の資料がアップロードされました\n{datetime.now().strftime('%Y/%m/%d %H:%M')}", site.get('name',''), token=token)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"uploaded": uploaded})
    return redirect(url_for('site_page', token=token))

@app.route("/p/<token>/delete_photo/<filename>", methods=["POST"])
def delete_photo(token, filename):
    if not get_site_by_token(token):
        abort(404)
    fp = UPLOAD_BASE / token / PATHS["photos"] / secure_filename(filename)
    if fp.exists():
        fp.unlink()
    return redirect(url_for('site_page', token=token))

def _safe_basename(name):
    """v1.8.3: secure_filename は日本語を全部削るので、
    日本語ファイル名(均等配置_xxx.pdf 等)に対応した安全化関数。
    パストラバーサル('../') と空文字だけは弾く。"""
    import os as _os
    name = _os.path.basename(name or '')
    if not name or name in ('.', '..') or '/' in name or '\\' in name:
        abort(400)
    return name

@app.route("/p/<token>/delete_doc/<category>/<filename>", methods=["POST"])
def delete_doc(token, category, filename):
    if not get_site_by_token(token):
        abort(404)
    if category not in DOC_CATEGORIES:
        abort(400)
    fp = UPLOAD_BASE / token / PATHS["docs"] / category / _safe_basename(filename)
    if fp.exists():
        fp.unlink()
    return redirect(url_for('site_page', token=token))

@app.route("/p/<token>/download/<category>/<filename>")
def download_doc(token, category, filename):
    if not get_site_by_token(token):
        abort(404)
    if category not in DOC_CATEGORIES:
        abort(400)
    cat_dir = UPLOAD_BASE / token / PATHS["docs"] / category
    return send_from_directory(cat_dir, _safe_basename(filename), as_attachment=True)

@app.route("/p/<token>/view/<category>/<filename>")
def view_doc(token, category, filename):
    """PDFや画像をブラウザ内でインライン表示"""
    if not get_site_by_token(token):
        abort(404)
    if category not in DOC_CATEGORIES:
        abort(400)
    cat_dir = UPLOAD_BASE / token / PATHS["docs"] / category
    return send_from_directory(cat_dir, _safe_basename(filename), as_attachment=False)

# v9.2: Gemini AIで寸法数字を検出
@app.route("/p/<token>/ocr_dimensions", methods=["POST"])
def ocr_dimensions(token):
    """PDF画像から寸法数字をGemini AIで検出して座標付きで返す"""
    if not get_site_by_token(token):
        abort(404)
    if not GOOGLE_API_KEY:
        return jsonify({'ok': False, 'error': 'GOOGLE_API_KEY未設定'}), 500
    data = request.get_json(force=True, silent=True) or {}
    image_b64 = data.get('image_b64')
    image_w = data.get('image_w', 0)
    image_h = data.get('image_h', 0)
    pdf_w = data.get('pdf_w', 0)
    pdf_h = data.get('pdf_h', 0)
    if not image_b64:
        return jsonify({'ok': False, 'error': '画像データなし'}), 400
    try:
        import base64
        img_bytes = base64.b64decode(image_b64)
        
        from google import genai
        from google.genai import types
        
        client = genai.Client(api_key=GOOGLE_API_KEY)
        
        prompt = """建築図面画像から、寸法を示す数字(mm単位)を全て検出してください。

検出対象: 100〜99999 の数字(寸法値)。部屋番号、品番、ページ番号、年月日などは除外。
寸法線の両端の矢印の間に書かれている数字、または寸法補助線の横/上下に書かれている数字が対象です。

以下のJSON形式のみで応答(markdown、説明なし):
{
  "dimensions": [
    {"val": 数字, "x": 左端X座標, "y": 上端Y座標, "w": 幅, "h": 高さ, "rot": 縦書きならtrue else false},
    ...
  ]
}

座標は画像のピクセル値(左上原点)。rotは縦書き(読む向きが90度回転)ならtrue。
見つからない数字があっても構いません。確実に寸法と判断できるものだけ返してください。"""
        
        image_part = types.Part.from_bytes(data=img_bytes, mime_type='image/jpeg')
        
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[prompt, image_part],
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type='application/json'
            )
        )
        
        text = response.text or ''
        # JSON抽出
        import json as json_mod
        try:
            result = json_mod.loads(text)
        except Exception:
            # 余分な文字除去
            text = text.strip()
            if text.startswith('```'):
                text = text.split('```',2)[1]
                if text.startswith('json'):
                    text = text[4:]
            result = json_mod.loads(text.strip())
        
        dims = result.get('dimensions', [])
        # 座標系を PDF(pdfW × pdfH)に変換
        # 画像は image_w × image_h、PDF座標は pdf_w × pdf_h
        if pdf_w > 0 and pdf_h > 0 and image_w > 0 and image_h > 0:
            sx = pdf_w / image_w
            sy = pdf_h / image_h
            for d in dims:
                d['x'] = float(d.get('x', 0)) * sx
                d['y'] = float(d.get('y', 0)) * sy
                d['w'] = float(d.get('w', 30)) * sx
                d['h'] = float(d.get('h', 14)) * sy
                d['rot'] = bool(d.get('rot', False))
                d['val'] = int(d.get('val', 0))
        
        # 100〜99999の範囲だけ許可
        dims = [d for d in dims if 100 <= d.get('val', 0) <= 99999]
        
        return jsonify({'ok': True, 'dimensions': dims, 'count': len(dims)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


# v9.7: Gemini AIで部屋の輪郭を検出
@app.route("/p/<token>/ai_detect_rooms", methods=["POST"])
def ai_detect_rooms(token):
    """画像から部屋の輪郭ポリゴンをGemini AIで検出"""
    if not get_site_by_token(token):
        abort(404)
    if not GOOGLE_API_KEY:
        return jsonify({'ok': False, 'error': 'GOOGLE_API_KEY未設定'}), 500
    data = request.get_json(force=True, silent=True) or {}
    image_b64 = data.get('image_b64')
    image_w = data.get('image_w', 0)
    image_h = data.get('image_h', 0)
    pdf_w = data.get('pdf_w', 0)
    pdf_h = data.get('pdf_h', 0)
    # クリック位置があれば、その部屋だけを返す
    click_x = data.get('click_x')
    click_y = data.get('click_y')
    if not image_b64:
        return jsonify({'ok': False, 'error': '画像データなし'}), 400
    try:
        import base64
        img_bytes = base64.b64decode(image_b64)
        
        from google import genai
        from google.genai import types
        
        client = genai.Client(api_key=GOOGLE_API_KEY)
        
        if click_x is not None and click_y is not None:
            # 画像座標系(image_w × image_h)にクリック位置を変換
            # 送られてきた click_x, click_y は PDF座標系 (0〜pdf_w, 0〜pdf_h)
            if pdf_w > 0 and pdf_h > 0 and image_w > 0 and image_h > 0:
                img_click_x = click_x * image_w / pdf_w
                img_click_y = click_y * image_h / pdf_h
            else:
                img_click_x = click_x
                img_click_y = click_y
            prompt = f"""建築図面の平面図です。
座標 ({int(img_click_x)}, {int(img_click_y)}) にある部屋の輪郭を抽出してください。
座標は画像の左上原点のピクセル値です。

部屋とは、壁で囲まれた居住空間です(LDK、寝室、浴室、玄関、トイレ、廊下、倉庫、シューズクローゼットなど)。
壁・ドア・窓・寸法線・家具・設備は部屋ではなく、部屋の輪郭を構成する境界です。

出力形式(JSONのみ、markdownなし):
{{
  "room": {{
    "label": "部屋の用途(LDK/寝室/浴室/トイレ/玄関/倉庫など、不明なら空欄)",
    "polygon": [[x1,y1], [x2,y2], ...]
  }}
}}

polygon は部屋の輪郭を10〜40点程度のポリゴンで表現。壁に沿った正確な座標を返してください。
座標は画像のピクセル値(左上原点)で返してください。"""
        else:
            prompt = """建築図面の平面図です。
全ての部屋(LDK、寝室、浴室、トイレ、玄関、倉庫など)の輪郭を抽出してください。

壁・ドア・窓・寸法線・家具・設備は部屋ではなく、部屋の輪郭を構成する境界です。

出力形式(JSONのみ、markdownなし):
{
  "rooms": [
    {
      "label": "部屋の用途",
      "polygon": [[x1,y1], [x2,y2], ...]
    }
  ]
}

各部屋の polygon は10〜40点程度で壁に沿った正確な座標。
座標は画像のピクセル値(左上原点)で返してください。"""
        
        image_part = types.Part.from_bytes(data=img_bytes, mime_type='image/jpeg')
        
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[prompt, image_part],
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type='application/json'
            )
        )
        
        text = response.text or ''
        import json as json_mod
        try:
            result = json_mod.loads(text)
        except Exception:
            text = text.strip()
            if text.startswith('```'):
                text = text.split('```',2)[1]
                if text.startswith('json'):
                    text = text[4:]
            result = json_mod.loads(text.strip())
        
        # 座標系を PDF(pdfW × pdfH)に変換
        rooms = []
        if 'room' in result:
            rooms = [result['room']]
        elif 'rooms' in result:
            rooms = result['rooms']
        
        if pdf_w > 0 and pdf_h > 0 and image_w > 0 and image_h > 0:
            sx = pdf_w / image_w
            sy = pdf_h / image_h
            for r in rooms:
                poly = r.get('polygon', [])
                r['polygon'] = [[float(p[0]) * sx, float(p[1]) * sy] for p in poly if len(p) >= 2]
        
        # 3頂点以上のポリゴンだけ残す
        rooms = [r for r in rooms if len(r.get('polygon', [])) >= 3]
        
        return jsonify({'ok': True, 'rooms': rooms, 'count': len(rooms)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route("/p/<token>/save_layout_pdf", methods=["POST"])
def save_layout_pdf(token):
    """割付計算結果をPDFで保存 (v9.0)"""
    if not get_site_by_token(token):
        abort(404)
    data = request.get_json(force=True, silent=True) or {}
    result = data.get('result')
    if not result:
        return jsonify({'error': 'データなし'}), 400
    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.pdfgen import canvas as rlcanvas
        from reportlab.lib.units import mm as RL_MM
        from reportlab.lib.colors import HexColor
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        
        # 日本語フォント(v9.2: CID + ファイル探索 + 自動DL)
        jp_font = 'Helvetica'
        try:
            # 最も確実な方法: reportlab内蔵のCID日本語フォント(HeiseiKakuGo-W5)
            pdfmetrics.registerFont(UnicodeCIDFont('HeiseiKakuGo-W5'))
            jp_font = 'HeiseiKakuGo-W5'
        except Exception as e:
            print(f"[CID font failed] {e}")
            # フォールバック: システムフォントを探す
            for f in ['/usr/share/fonts/opentype/ipafont-gothic/ipagp.ttf',
                      '/usr/share/fonts/truetype/fonts-japanese-gothic.ttf',
                      '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
                      '/usr/share/fonts/opentype/ipaexfont-gothic/ipaexg.ttf',
                      '/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc',
                      '/tmp/NotoSansJP-Regular.ttf']:
                try:
                    if Path(f).exists():
                        pdfmetrics.registerFont(TTFont('JP', f))
                        jp_font = 'JP'
                        break
                except Exception:
                    pass
            # 最終手段: Google FontsからNoto Sans JPをダウンロード
            if jp_font == 'Helvetica':
                try:
                    font_cache = Path('/tmp/NotoSansJP-Regular.ttf')
                    if not font_cache.exists():
                        import urllib.request
                        url = 'https://github.com/notofonts/noto-cjk/raw/main/Sans/OTF/Japanese/NotoSansJP-Regular.otf'
                        urllib.request.urlretrieve(url, str(font_cache))
                    if font_cache.exists():
                        pdfmetrics.registerFont(TTFont('JP', str(font_cache)))
                        jp_font = 'JP'
                except Exception as e:
                    print(f"[Font download failed] {e}")
        
        cat_dir = UPLOAD_BASE / token / PATHS["docs"] / "割付"
        cat_dir.mkdir(parents=True, exist_ok=True)
        # サムネディレクトリ(既存構造に合わせる)
        thumb_dir = UPLOAD_BASE / token / PATHS["thumbs"]
        thumb_dir.mkdir(parents=True, exist_ok=True)
        
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        uid = uuid.uuid4().hex[:6]
        rtype = result.get('type', 'layout')
        type_label = {'downlight':'均等配置','tile':'タイル割付','flooring':'フローリング割付','wallpaper':'クロス割付','generic':'汎用均等割付','room':'部屋割付'}.get(rtype, '割付計算')
        label = result.get('label', '').strip()
        site_info = get_site_by_token(token) or {}
        site_name = site_info.get('name', '')
        
        # v1.2: タイトルを取得してファイル名に反映 (日本語OK、FSでは扱える)
        import re
        ts_short = datetime.now().strftime("%Y%m%d_%H%M")
        def safe_fs(s):
            s = re.sub(r'[\\/:*?"<>|\n\r\t]', '_', s or '').strip()
            s = s[:60]
            return s
        title_part = safe_fs(label) if label else ''
        type_part = safe_fs(type_label)
        if title_part:
            safe_name = f"{title_part}_{type_part}_{ts_short}.pdf"
        else:
            safe_name = f"{type_part}_{ts_short}.pdf"
        # 重複回避
        i = 2
        while (cat_dir / safe_name).exists():
            stem = safe_name.rsplit('.', 1)[0]
            safe_name = f"{stem}_{i}.pdf"
            i += 1
        file_path = cat_dir / safe_name
        
        page_w, page_h = landscape(A4)
        c = rlcanvas.Canvas(str(file_path), pagesize=landscape(A4))
        
        # ==== v9.7.1: ミニマル・モダン デザイン ====
        COLOR_DARK = HexColor('#2d3142')      # 濃紺(タイトル・重要情報)
        COLOR_ACCENT = HexColor('#ef8354')    # アクセントオレンジ
        COLOR_TEXT = HexColor('#4f5d75')      # 本文グレー
        COLOR_LIGHT = HexColor('#bfc0c0')     # ライトグレー(罫線)
        COLOR_BG_ZEBRA = HexColor('#f8f7f5')  # ゼブラ背景
        COLOR_LINE = HexColor('#e0e0e0')
        
        margin_x = 18*RL_MM
        margin_y = 14*RL_MM
        
        # ---- ヘッダーブロック ----
        header_h = 18*RL_MM
        # 左端のアクセントバー
        c.setFillColor(COLOR_ACCENT)
        c.rect(margin_x, page_h-margin_y-header_h, 4*RL_MM, header_h, fill=1, stroke=0)
        # タイプラベル(小)
        c.setFillColor(COLOR_ACCENT)
        c.setFont(jp_font, 8)
        c.drawString(margin_x+7*RL_MM, page_h-margin_y-5*RL_MM, "LAYOUT CALCULATION")
        # メインタイトル
        c.setFillColor(COLOR_DARK)
        c.setFont(jp_font, 18)
        title_text = type_label
        c.drawString(margin_x+7*RL_MM, page_h-margin_y-11*RL_MM, title_text)
        # ラベル(あれば)
        if label:
            c.setFillColor(COLOR_TEXT)
            c.setFont(jp_font, 11)
            c.drawString(margin_x+7*RL_MM, page_h-margin_y-16*RL_MM, label)
        # 右上: 日時 + 現場名
        c.setFillColor(COLOR_TEXT)
        c.setFont(jp_font, 8)
        right_y = page_h-margin_y-5*RL_MM
        c.drawRightString(page_w-margin_x, right_y, datetime.now().strftime("%Y / %m / %d   %H:%M"))
        if site_name:
            c.setFillColor(COLOR_DARK)
            c.setFont(jp_font, 10)
            c.drawRightString(page_w-margin_x, right_y-5*RL_MM, site_name)
        
        # ヘッダー下の細い罫線
        c.setStrokeColor(COLOR_LINE)
        c.setLineWidth(0.5)
        c.line(margin_x, page_h-margin_y-header_h-2*RL_MM, page_w-margin_x, page_h-margin_y-header_h-2*RL_MM)
        
        # ---- 図エリア ----
        drawing_top = page_h - margin_y - header_h - 6*RL_MM
        drawing_height = 95*RL_MM
        drawing_left = margin_x
        drawing_right = page_w - margin_x
        drawing_width = drawing_right - drawing_left
        
        # 図の背景(薄いゼブラ)
        c.setFillColor(COLOR_BG_ZEBRA)
        c.rect(drawing_left, drawing_top-drawing_height, drawing_width, drawing_height, fill=1, stroke=0)
        
        if rtype in ('downlight', 'generic'):
            _draw_equal_pdf(c, result, drawing_left, drawing_top, drawing_width, drawing_height, jp_font)
        elif rtype == 'tile':
            _draw_tile_pdf(c, result, drawing_left, drawing_top, drawing_width, drawing_height, jp_font)
        elif rtype == 'flooring':
            _draw_flooring_pdf(c, result, drawing_left, drawing_top, drawing_width, drawing_height, jp_font)
        elif rtype == 'wallpaper':
            _draw_wallpaper_pdf(c, result, drawing_left, drawing_top, drawing_width, drawing_height, jp_font)
        elif rtype == 'room':
            _draw_room_pdf(c, result, drawing_left, drawing_top, drawing_width, drawing_height, jp_font)
        
        # ---- データテーブル(下部) ----
        table_top = drawing_top - drawing_height - 8*RL_MM
        _draw_data_table(c, result, margin_x, table_top, page_w-margin_x*2, jp_font)
        
        # ---- フッタ ----
        c.setFillColor(COLOR_LIGHT)
        c.setFont(jp_font, 7)
        c.drawCentredString(page_w/2, 7*RL_MM, f"現場フォトアプリ  ·  割付計算 v9.7.1  ·  {safe_name}")
        
        c.save()
        
        # ==== サムネイル生成 (v9.7.1: 堅牢化) ====
        thumb_filename = None
        thumb_error = None
        try:
            import fitz
            doc = fitz.open(str(file_path))
            page = doc[0]
            zoom = 2.0
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat)
            thumb_path = thumb_dir / f"{file_path.stem}.jpg"
            
            # JPG保存を試す
            try:
                pix.save(str(thumb_path))
            except Exception as e1:
                # PNGで保存 → PIL でJPGに変換
                png_path = thumb_dir / f"{file_path.stem}.png"
                pix.save(str(png_path))
                try:
                    from PIL import Image
                    img = Image.open(str(png_path)).convert('RGB')
                    img.save(str(thumb_path), 'JPEG', quality=85)
                    png_path.unlink(missing_ok=True)
                except Exception as e2:
                    # PILも失敗 → PNGのままサムネにする
                    thumb_path = png_path
            
            doc.close()
            if thumb_path.exists() and thumb_path.stat().st_size > 0:
                thumb_filename = thumb_path.name
                print(f"[THUMB OK] {thumb_path.name} ({thumb_path.stat().st_size} bytes)")
            else:
                thumb_error = 'file not created or empty'
                print(f"[THUMB FAIL] empty or missing")
        except Exception as e:
            import traceback
            thumb_error = str(e)
            traceback.print_exc()
            print(f"[THUMB ERROR] {e}")
        
        filename_display = safe_name  # v1.2: 実ファイル名と同じに
        
        return jsonify({
            'ok': True,
            'filename': filename_display,
            'safe_name': safe_name,
            'download_url': f"/p/{token}/download/割付/{safe_name}",
            'view_url': f"/p/{token}/view/割付/{safe_name}",
            'thumb_url': f"/uploads/{token}/thumbs/{thumb_filename}" if thumb_filename else None,
            'thumb_error': thumb_error
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


def _draw_equal_pdf(c, r, x0, y_top, w, h, font):
    from reportlab.lib.units import mm as RL_MM
    from reportlab.lib.colors import HexColor
    L = r.get('L', 3500)
    positions = r.get('positions', [])
    diameter = r.get('diameter', 100)
    
    c.saveState()  # v9.7.1.1: 状態隔離
    
    # v9.7.1: モダンパレット
    COLOR_DARK = HexColor('#2d3142')
    COLOR_ACCENT = HexColor('#ef8354')
    COLOR_DIM = HexColor('#6b7a95')
    COLOR_WALL = HexColor('#95a3b8')
    
    y_center = y_top - h*0.45
    scale = (w - 50*RL_MM) / L
    draw_x0 = x0 + 25*RL_MM
    draw_x1 = draw_x0 + L*scale
    
    # 天井線(太め、濃紺)
    c.setStrokeColor(COLOR_DARK)
    c.setLineWidth(1.8)
    c.line(draw_x0, y_center, draw_x1, y_center)
    
    # 両端壁(濃いグレー、斜線パターン風に)
    wall_w = 10
    wall_h = 22
    c.setFillColor(COLOR_WALL)
    c.setStrokeColor(COLOR_WALL)
    c.rect(draw_x0-wall_w, y_center-wall_h/2, wall_w, wall_h, fill=1, stroke=0)
    c.rect(draw_x1, y_center-wall_h/2, wall_w, wall_h, fill=1, stroke=0)
    # 壁ラベル
    c.setFillColor(HexColor('#ffffff'))
    c.setFont(font, 6)
    c.drawCentredString(draw_x0-wall_w/2, y_center-1.5, "壁")
    c.drawCentredString(draw_x1+wall_w/2, y_center-1.5, "壁")
    
    # 器具(ダウンライト)
    icon_r = min(9, max(6, diameter*scale/2))
    for i, p in enumerate(positions):
        cx = draw_x0 + p*scale
        # 外円(影)
        c.setFillColor(HexColor('#fff1e8'))
        c.setStrokeColor(COLOR_ACCENT)
        c.setLineWidth(1.5)
        c.circle(cx, y_center, icon_r, stroke=1, fill=1)
        # 内円(番号背景)
        c.setFillColor(COLOR_ACCENT)
        c.setStrokeColor(COLOR_ACCENT)
        c.circle(cx, y_center, icon_r*0.55, stroke=0, fill=1)
        # 番号
        c.setFillColor(HexColor('#ffffff'))
        c.setFont(font, 7)
        c.drawCentredString(cx, y_center-2, str(i+1))
    
    # 寸法線
    dim_y = y_center - 22
    edge1 = r.get('edge1', 0)
    pitch = r.get('pitch', 0)
    edge2 = r.get('edge2', 0)
    if positions:
        _draw_dim(c, draw_x0, dim_y, draw_x0+positions[0]*scale, f"{round(edge1)}", font)
        for i in range(1, len(positions)):
            _draw_dim(c, draw_x0+positions[i-1]*scale, dim_y, draw_x0+positions[i]*scale, f"{round(pitch)}", font)
        _draw_dim(c, draw_x0+positions[-1]*scale, dim_y, draw_x1, f"{round(edge2)}", font)
    # 全長
    _draw_dim(c, draw_x0, dim_y-14, draw_x1, f"全長 {L}mm", font, main=True)
    
    c.restoreState()


def _draw_dim(c, x1, y, x2, txt, font, main=False):
    """寸法線を描画 v9.7.1.1: 背景青ベタ問題を完全解決"""
    from reportlab.lib.colors import HexColor, black, white
    
    # まず状態をリセット
    c.saveState()
    
    # 色を明示的に定義(濃紺 for main, グレー for sub)
    line_color = HexColor('#2d3142') if main else HexColor('#8a95a8')
    text_color = HexColor('#1a1a1a')  # ほぼ黒、確実に可読
    bg_color = HexColor('#ffffff')     # 純白
    
    line_w = 0.8 if main else 0.5
    
    # --- 1. 寸法線を描く ---
    c.setStrokeColor(line_color)
    c.setFillColor(line_color)  # Fill もラインと同じ色に(矢印tick用)
    c.setLineWidth(line_w)
    c.line(x1, y, x2, y)
    
    # 矢印(短い縦線)
    tick = 3.5 if main else 2.8
    c.line(x1, y-tick, x1, y+tick)
    c.line(x2, y-tick, x2, y+tick)
    
    # --- 2. ラベルエリアをクリア(白背景) ---
    fsize = 9 if main else 8
    tw = c.stringWidth(txt, font, fsize)
    label_x = (x1+x2)/2
    # ラベル中心 y (寸法線の上に配置)
    label_center_y = y + fsize/2 + 2
    # 白背景 rect (文字サイズ + 余白)
    pad_x = 4
    pad_y = 2
    rect_x = label_x - tw/2 - pad_x
    rect_y = label_center_y - fsize/2 - pad_y
    rect_w = tw + pad_x*2
    rect_h = fsize + pad_y*2
    c.setFillColor(bg_color)
    c.setStrokeColor(bg_color)  # stroke も白で
    c.rect(rect_x, rect_y, rect_w, rect_h, fill=1, stroke=0)
    
    # --- 3. テキストを描画(黒文字) ---
    c.setFillColor(text_color)
    c.setStrokeColor(text_color)
    c.setFont(font, fsize)
    # drawCentredString の y はベースライン基準。下側にdescenderぶんの余白
    c.drawCentredString(label_x, label_center_y - fsize*0.33, txt)
    
    c.restoreState()


def _draw_tile_pdf(c, r, x0, y_top, w, h, font):
    from reportlab.lib.units import mm as RL_MM
    from reportlab.lib.colors import HexColor
    W = r.get('W', 5000)
    H = r.get('H', 3000)
    tW = r.get('tW', 600)
    tH = r.get('tH', 300)
    joint = r.get('joint', 3)
    pattern = r.get('pattern', 'grid')
    # 描画
    scale = min((w-40*RL_MM)/W, (h-30*RL_MM)/H)
    dW = W*scale
    dH = H*scale
    draw_x0 = x0 + (w-dW)/2
    draw_y0 = y_top - 10*RL_MM
    draw_y1 = draw_y0 - dH
    
    # 外枠
    c.setStrokeColor(HexColor('#333'))
    c.setLineWidth(1.5)
    c.rect(draw_x0, draw_y1, dW, dH, stroke=1, fill=0)
    
    # タイル
    uW = (tW+joint)*scale
    uH = (tH+joint)*scale
    tileW = tW*scale
    tileH = tH*scale
    
    rows = int(dH/uH)+2
    cols = int(dW/uW)+2
    
    # クリッピング領域設定(Reportlab)
    c.saveState()
    p = c.beginPath()
    p.rect(draw_x0, draw_y1, dW, dH)
    c.clipPath(p, stroke=0)
    
    c.setStrokeColor(HexColor('#8b6f47'))
    c.setLineWidth(0.3)
    for row in range(-1, rows):
        offset = 0
        if pattern == 'brick':
            offset = (row%2)*uW/2
        elif pattern == 'third':
            offset = (row%3)*uW/3
        for col in range(-1, cols):
            x = draw_x0 + col*uW + offset
            y = draw_y0 - (row+1)*uH
            color = HexColor('#f0e3d4') if (row+col)%2==0 else HexColor('#e6d5c2')
            c.setFillColor(color)
            c.rect(x, y, tileW, tileH, stroke=1, fill=1)
    
    c.restoreState()
    # 外枠再描画
    c.setStrokeColor(HexColor('#333'))
    c.setLineWidth(1.5)
    c.rect(draw_x0, draw_y1, dW, dH, stroke=1, fill=0)
    
    # 寸法
    _draw_dim(c, draw_x0, draw_y1-8, draw_x0+dW, f"{W}mm", font)
    c.saveState()
    c.translate(draw_x0+dW+15, draw_y1+dH/2)
    c.rotate(90)
    _draw_dim(c, -dH/2, 0, dH/2, f"{H}mm", font)
    c.restoreState()


def _draw_flooring_pdf(c, r, x0, y_top, w, h, font):
    from reportlab.lib.units import mm as RL_MM
    from reportlab.lib.colors import HexColor
    W = r.get('W', 4550)
    H = r.get('H', 3640)
    bW = r.get('bW', 1820)
    bH = r.get('bH', 303)
    pattern = r.get('pattern', 'brick')
    scale = min((w-40*RL_MM)/W, (h-30*RL_MM)/H)
    dW = W*scale
    dH = H*scale
    draw_x0 = x0 + (w-dW)/2
    draw_y0 = y_top - 10*RL_MM
    draw_y1 = draw_y0 - dH
    
    c.setStrokeColor(HexColor('#333'))
    c.setLineWidth(1.5)
    c.rect(draw_x0, draw_y1, dW, dH, stroke=1, fill=0)
    
    boardW = bW*scale
    boardH = bH*scale
    rows = int(dH/boardH)+2
    cols = int(dW/boardW)+2
    shades = [HexColor('#c9a878'), HexColor('#b9955f'), HexColor('#d4b787'), HexColor('#a98656'), HexColor('#c2a06b')]
    
    c.saveState()
    p = c.beginPath()
    p.rect(draw_x0, draw_y1, dW, dH)
    c.clipPath(p, stroke=0)
    
    c.setStrokeColor(HexColor('#7a5a3a'))
    c.setLineWidth(0.3)
    for row in range(-1, rows):
        offset = 0
        if pattern == 'brick':
            offset = (row%2)*boardW/2
        elif pattern == 'third':
            offset = (row%3)*boardW/3
        elif pattern == 'random':
            offset = (row*0.37*boardW) % boardW
        for col in range(-1, cols):
            x = draw_x0 + col*boardW + offset
            y = draw_y0 - (row+1)*boardH
            c.setFillColor(shades[(row*7+col*3)%5])
            c.rect(x, y, boardW, boardH, stroke=1, fill=1)
    
    c.restoreState()
    c.setStrokeColor(HexColor('#333'))
    c.setLineWidth(1.5)
    c.rect(draw_x0, draw_y1, dW, dH, stroke=1, fill=0)
    
    _draw_dim(c, draw_x0, draw_y1-8, draw_x0+dW, f"{W}mm (張り方向)", font)


def _draw_wallpaper_pdf(c, r, x0, y_top, w, h, font):
    from reportlab.lib.units import mm as RL_MM
    from reportlab.lib.colors import HexColor
    W = r.get('W', 4000)
    H = r.get('H', 2400)
    rW = r.get('rW', 920)
    sheets = r.get('sheets', 5)
    scale = min((w-40*RL_MM)/W, (h-30*RL_MM)/H)
    dW = W*scale
    dH = H*scale
    draw_x0 = x0 + (w-dW)/2
    draw_y0 = y_top - 10*RL_MM
    draw_y1 = draw_y0 - dH
    
    # 壁背景
    c.setFillColor(HexColor('#f5f0e5'))
    c.setStrokeColor(HexColor('#333'))
    c.setLineWidth(1.5)
    c.rect(draw_x0, draw_y1, dW, dH, stroke=1, fill=1)
    
    roll_w_px = rW*scale
    colors = [HexColor('#d4e5f5'), HexColor('#b5d4ee'), HexColor('#a3c9e8')]
    
    c.saveState()
    p = c.beginPath()
    p.rect(draw_x0, draw_y1, dW, dH)
    c.clipPath(p, stroke=0)
    
    for i in range(sheets):
        x = draw_x0 + i*roll_w_px
        c.setFillColor(colors[i%3])
        c.setFillAlpha(0.5)
        c.rect(x, draw_y1, roll_w_px, dH, stroke=0, fill=1)
        c.setFillAlpha(1.0)
        c.setStrokeColor(HexColor('#2980b9'))
        c.setDash(2, 1)
        c.line(x+roll_w_px, draw_y1, x+roll_w_px, draw_y1+dH)
        c.setDash()
        c.setFillColor(HexColor('#2980b9'))
        c.setFont(font, 8)
        c.drawCentredString(x+roll_w_px/2, draw_y1+dH/2, f"#{i+1}")
    
    c.restoreState()
    c.setStrokeColor(HexColor('#333'))
    c.setLineWidth(1.5)
    c.rect(draw_x0, draw_y1, dW, dH, stroke=1, fill=0)
    
    _draw_dim(c, draw_x0, draw_y1-8, draw_x0+dW, f"{W}mm", font)


def _draw_room_pdf(c, r, x0, y_top, w, h, font):
    """部屋から割付 PDF描画 (v1.3)"""
    from reportlab.lib.units import mm as RL_MM
    from reportlab.lib.colors import HexColor

    pts = r.get('roomPoints', [])
    tiles = r.get('tiles', [])
    if not pts:
        return

    minX = min(p['x'] for p in pts)
    maxX = max(p['x'] for p in pts)
    minY = min(p['y'] for p in pts)
    maxY = max(p['y'] for p in pts)
    rW = max(maxX - minX, 1)
    rH = max(maxY - minY, 1)
    pad_mm = 15 * RL_MM
    avail_w = w - pad_mm * 2
    avail_h = h - pad_mm * 2
    scale = min(avail_w / rW, avail_h / rH)
    draw_w = rW * scale
    draw_h = rH * scale
    draw_x0 = x0 + (w - draw_w) / 2
    draw_y1 = y_top - (h - draw_h) / 2 - draw_h

    def X(xmm):
        return draw_x0 + (xmm - minX) * scale

    def Y(ymm):
        return draw_y1 + (maxY - ymm) * scale

    c.saveState()
    p = c.beginPath()
    p.moveTo(X(pts[0]['x']), Y(pts[0]['y']))
    for pt in pts[1:]:
        p.lineTo(X(pt['x']), Y(pt['y']))
    p.close()
    c.clipPath(p, stroke=0, fill=0)

    for idx, t in enumerate(tiles):
        tx = X(t['x'])
        ty = Y(t['y'] + t['h'])
        tw = t['w'] * scale
        th = t['h'] * scale
        if t.get('full'):
            color = HexColor('#f0e3d4') if idx % 2 == 0 else HexColor('#e6d5c2')
        else:
            color = HexColor('#ffe8c0')
        c.setFillColor(color)
        c.setStrokeColor(HexColor('#8b6f47'))
        c.setLineWidth(0.3)
        c.rect(tx, ty, tw, th, stroke=1, fill=1)

    c.restoreState()

    c.setStrokeColor(HexColor('#2d2520'))
    c.setLineWidth(2.0)
    path = c.beginPath()
    path.moveTo(X(pts[0]['x']), Y(pts[0]['y']))
    for pt in pts[1:]:
        path.lineTo(X(pt['x']), Y(pt['y']))
    path.close()
    c.drawPath(path, stroke=1, fill=0)

    c.setFillColor(HexColor('#2d2520'))
    c.setFont(font, 8)
    for i in range(1, len(pts)):
        p1 = pts[i - 1]
        p2 = pts[i]
        mx = (X(p1['x']) + X(p2['x'])) / 2
        my = (Y(p1['y']) + Y(p2['y'])) / 2
        dx = p2['x'] - p1['x']
        dy = p2['y'] - p1['y']
        length = int(round((dx * dx + dy * dy) ** 0.5))
        if abs(dx) > abs(dy):
            ty = my - 5
            tx = mx
        else:
            tx = mx + 6
            ty = my
        c.drawCentredString(tx, ty, f"{length}")


def _draw_data_table(c, r, x0, y_top, w, font):
    from reportlab.lib.units import mm as RL_MM
    from reportlab.lib.colors import HexColor
    rtype = r.get('type')
    rows = []
    if rtype in ('downlight', 'generic'):
        mode_txt = {'equal':'両端余白均等', 'edge':'端からの距離指定', 'pitch':'ピッチ指定'}.get(r.get('mode','equal'))
        rows = [
            ('タイプ', f"{r.get('itemName','設備')} × {r.get('n')}個"),
            ('区間', f"{r.get('L')} mm"),
            ('方式', mode_txt),
            ('端から(両端)', f"{round(r.get('edge1',0))} mm"),
            ('ピッチ(間隔)', f"{round(r.get('pitch',0))} mm"),
            ('位置リスト', ' / '.join([str(round(p)) for p in r.get('positions',[])]) + ' mm'),
        ]
    elif rtype == 'tile':
        pat = {'grid':'通し目地','brick':'馬貼り(1/2)','third':'三ッ割り(1/3)'}.get(r.get('pattern','grid'))
        rows = [
            ('床サイズ', f"{r.get('W')} × {r.get('H')} mm ({r.get('area_m2',0):.2f} m²)"),
            ('タイル', f"{r.get('tW')} × {r.get('tH')} mm (目地 {r.get('joint')}mm)"),
            ('割付方法', pat),
            ('枚数(割付上)', f"{r.get('cols')} × {r.get('rows')} = {r.get('total')} 枚"),
            (f"必要枚数(ロス{int(r.get('loss',0)*100)}%込み)", f"{r.get('totalWithLoss')} 枚"),
            ('端材(最右列の幅)', f"{round(r.get('endW',0))} mm"),
            ('端材(最下行の高さ)', f"{round(r.get('endH',0))} mm"),
        ]
    elif rtype == 'flooring':
        pat = {'random':'ランダム','brick':'馬貼り','third':'三ッ割り'}.get(r.get('pattern','brick'))
        rows = [
            ('部屋サイズ', f"{r.get('W')} × {r.get('H')} mm ({r.get('area_m2',0):.2f} m²)"),
            ('板サイズ', f"{r.get('bW')} × {r.get('bH')} mm"),
            ('割付方法', pat),
            ('枚数(割付上)', f"{r.get('colsPerRow')} × {r.get('rows')}列 = {r.get('total')} 枚"),
            (f"必要枚数(ロス{int(r.get('loss',0)*100)}%込み)", f"{r.get('totalWithLoss')} 枚"),
            ('1ケース', f"{r.get('perCase')} 枚"),
            ('必要ケース', f"{r.get('cases')} ケース"),
        ]
    elif rtype == 'wallpaper':
        rows = [
            ('壁サイズ', f"{r.get('W')} × {r.get('H')} mm ({r.get('area_m2',0):.2f} m²)"),
            ('クロス幅', f"{r.get('rW')} mm"),
            ('割り枚数', f"{r.get('sheets')} 枚"),
            ('1枚の長さ', f"{r.get('pieceLen')} mm (上下余り {r.get('margin')}mm×2)"),
            ('総必要長', f"{r.get('totalLen_m',0):.2f} m"),
            (f"必要量(ロス{int(r.get('loss',0)*100)}%込み)", f"{r.get('totalLenWithLoss_m',0):.2f} m"),
        ]
    elif rtype == 'room':
        pat = {'grid':'通し目地','brick':'馬貼り(1/2)','third':'三ツ割り(1/3)'}.get(r.get('pattern','grid'), r.get('pattern','-'))
        align_txt = {'tl':'左上','tc':'上中央','tr':'右上','ml':'左中央','mc':'中央','mr':'右中央','bl':'左下','bc':'下中央','br':'右下'}.get(r.get('align','tl'), r.get('align','-'))
        mat_type_txt = {'tile':'タイル','flooring':'フローリング','carpet':'カーペットタイル','custom':'カスタム'}.get(r.get('matType','tile'), '-')
        rows = [
            ('部屋形状', f"{len(r.get('roomPoints',[]))}角形 · 面積 {r.get('area_m2',0):.2f} m²"),
            ('商材', f"{mat_type_txt} · {r.get('matW')}×{r.get('matH')}mm (目地 {r.get('joint',0)}mm)"),
            ('割付方法', f"{pat} · 基準: {align_txt}"),
            ('オフセット', f"横 {r.get('offX',0)}mm / 縦 {r.get('offY',0)}mm"),
            ('カット最小幅', f"{r.get('minCut',0)} mm"),
            ('枚数内訳', f"フル {r.get('fullTiles',0)}枚 + カット {r.get('cutTiles',0)}枚 = {r.get('total',0)}枚"),
            (f"必要枚数(ロス{int(r.get('loss',0)*100)}%込み)", f"{r.get('totalWithLoss',0)} 枚"),
        ]
    
    # v9.7.1: モダンなデータテーブル
    COLOR_DARK = HexColor('#2d3142')
    COLOR_ACCENT = HexColor('#ef8354')
    COLOR_TEXT = HexColor('#4f5d75')
    COLOR_LIGHT = HexColor('#e8e8e8')
    COLOR_ZEBRA = HexColor('#fafafa')
    
    # セクションタイトル
    c.setFillColor(COLOR_ACCENT)
    c.setFont(font, 8)
    c.drawString(x0, y_top+1*RL_MM, "CALCULATION DETAILS")
    c.setFillColor(COLOR_DARK)
    c.setFont(font, 11)
    c.drawString(x0, y_top-4*RL_MM, "計算詳細")
    
    y = y_top - 8*RL_MM
    row_h = 6.5*RL_MM
    col_w = 60*RL_MM
    
    # テーブル上罫線
    c.setStrokeColor(COLOR_DARK)
    c.setLineWidth(1.0)
    c.line(x0, y, x0+w, y)
    
    for i, (k, v) in enumerate(rows):
        # ゼブラ縞
        if i % 2 == 0:
            c.setFillColor(COLOR_ZEBRA)
            c.rect(x0, y-row_h, w, row_h, fill=1, stroke=0)
        # ラベル(左側)
        c.setFillColor(COLOR_TEXT)
        c.setFont(font, 9)
        c.drawString(x0+3*RL_MM, y-row_h+2.3*RL_MM, str(k))
        # 値(右側、強調)
        c.setFillColor(COLOR_DARK)
        c.setFont(font, 10)
        c.drawString(x0+col_w+3*RL_MM, y-row_h+2.3*RL_MM, str(v))
        # 行間の薄い罫線
        c.setStrokeColor(COLOR_LIGHT)
        c.setLineWidth(0.3)
        c.line(x0, y-row_h, x0+w, y-row_h)
        y -= row_h
    
    # テーブル下罫線
    c.setStrokeColor(COLOR_DARK)
    c.setLineWidth(1.0)
    c.line(x0, y, x0+w, y)


@app.route("/p/<token>/measure/<category>/<filename>")
def measure_doc(token, category, filename):
    """PDF図面の計測ツール画面"""
    site = get_site_by_token(token)
    if not site:
        abort(404)
    if category not in DOC_CATEGORIES:
        abort(400)
    sites = load_sites()
    pdf_url = url_for('view_doc', token=token, category=category, filename=filename)
    has_gemini = bool(GOOGLE_API_KEY)
    # v8.0: 同名DXFを探す → 優先度順
    # 1. 同じstem(タイムスタンプ+uuid)
    # 2. 同じカテゴリ内で最新のDXF(1つだけならペア扱い)
    # 3. リクエストパラメータで明示指定 ?dxf=xxx.dxf
    dxf_filename = request.args.get('dxf')  # 明示指定優先
    pdf_base = Path(filename).stem
    cat_dir = UPLOAD_BASE / token / PATHS["docs"] / category
    if not dxf_filename and cat_dir.exists():
        # 1. 完全一致stem
        for p in cat_dir.iterdir():
            if p.suffix.lower() == '.dxf' and p.stem == pdf_base:
                dxf_filename = p.name; break
        # 2. カテゴリ内のDXFが1つだけならペア扱い
        if not dxf_filename:
            dxfs = [p for p in cat_dir.iterdir() if p.suffix.lower() == '.dxf']
            if len(dxfs) == 1:
                dxf_filename = dxfs[0].name
    return render_template("measure.html",
                           token=token,
                           site_name=sites[token].get('name', ''),
                           pdf_url=pdf_url,
                           category=category,
                           filename=filename,
                           dxf_filename=dxf_filename,
                           has_gemini=has_gemini)


@app.route("/p/<token>/list_dxfs/<category>")
def list_dxfs(token, category):
    """同カテゴリ内のDXFファイル一覧 (v8.0)"""
    if not get_site_by_token(token):
        abort(404)
    if category not in DOC_CATEGORIES:
        abort(400)
    cat_dir = UPLOAD_BASE / token / PATHS["docs"] / category
    dxfs = []
    if cat_dir.exists():
        for p in cat_dir.iterdir():
            if p.suffix.lower() == '.dxf':
                dxfs.append(p.name)
    return jsonify({'dxfs': dxfs})


@app.route("/p/<token>/link_dxf", methods=["POST"])
def link_dxf(token):
    """PDFにDXFを紐付ける(DXFをPDFと同じstemにリネーム) (v8.0)"""
    if not get_site_by_token(token):
        abort(404)
    data = request.get_json(force=True, silent=True) or {}
    category = data.get('category')
    pdf_filename = data.get('pdf_filename')
    dxf_filename = data.get('dxf_filename')
    if not all([category, pdf_filename, dxf_filename]) or category not in DOC_CATEGORIES:
        return jsonify({'error': 'パラメータ不正'}), 400
    cat_dir = UPLOAD_BASE / token / PATHS["docs"] / category
    pdf_path = cat_dir / pdf_filename
    dxf_path = cat_dir / dxf_filename
    if not pdf_path.exists() or pdf_path.suffix.lower() != '.pdf':
        return jsonify({'error': 'PDF not found'}), 404
    if not dxf_path.exists() or dxf_path.suffix.lower() != '.dxf':
        return jsonify({'error': 'DXF not found'}), 404
    new_dxf_path = cat_dir / (pdf_path.stem + '.dxf')
    # 既に同名DXFがある場合は上書きせずにエラー
    if new_dxf_path.exists() and new_dxf_path != dxf_path:
        # 既存の紐付けを解除: 一旦リネームして退避
        import time
        bak = cat_dir / (pdf_path.stem + f'_bak{int(time.time())}.dxf')
        new_dxf_path.rename(bak)
    dxf_path.rename(new_dxf_path)
    return jsonify({'ok': True, 'new_filename': new_dxf_path.name})


@app.route("/p/<token>/unlink_dxf", methods=["POST"])
def unlink_dxf(token):
    """PDFのDXF紐付けを解除(DXFを元のタイムスタンプ名に戻す) (v8.0)"""
    if not get_site_by_token(token):
        abort(404)
    data = request.get_json(force=True, silent=True) or {}
    category = data.get('category')
    pdf_filename = data.get('pdf_filename')
    if not category or not pdf_filename or category not in DOC_CATEGORIES:
        return jsonify({'error': 'パラメータ不正'}), 400
    cat_dir = UPLOAD_BASE / token / PATHS["docs"] / category
    pdf_path = cat_dir / pdf_filename
    if not pdf_path.exists():
        return jsonify({'error': 'PDF not found'}), 404
    linked_dxf = cat_dir / (pdf_path.stem + '.dxf')
    if not linked_dxf.exists():
        return jsonify({'error': 'DXF紐付けが見つかりません'}), 404
    # 新しいタイムスタンプベースの名前に戻す
    new_name = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6] + ".dxf"
    linked_dxf.rename(cat_dir / new_name)
    return jsonify({'ok': True, 'new_filename': new_name})


@app.route("/p/<token>/dxf_data/<category>/<filename>")
def dxf_data(token, category, filename):
    """DXFを解析してJSON形式で返す (v8.0)"""
    if not get_site_by_token(token):
        abort(404)
    if category not in DOC_CATEGORIES:
        abort(400)
    file_path = UPLOAD_BASE / token / PATHS["docs"] / category / filename
    if not file_path.exists() or file_path.suffix.lower() != '.dxf':
        return jsonify({'error': 'DXFが見つかりません'}), 404
    try:
        import ezdxf
        doc = ezdxf.readfile(str(file_path))
        msp = doc.modelspace()
    except Exception as e:
        return jsonify({'error': f'DXF読込エラー: {e}'}), 500
    
    lines = []       # {x1,y1,x2,y2,layer}
    arcs = []        # {cx,cy,r,start,end,layer}
    circles = []     # {cx,cy,r,layer}
    polylines = []   # {points:[[x,y],...], closed, layer}
    splines = []     # v8.2: {points, layer} スプラインを近似点列に
    dimensions = []  # {text, measurement, layer, ...}
    texts = []       # {text, x, y, layer, height}
    
    minX, minY, maxX, maxY = float('inf'), float('inf'), float('-inf'), float('-inf')
    
    def update_bounds(x, y):
        nonlocal minX, minY, maxX, maxY
        if x < minX: minX = x
        if y < minY: minY = y
        if x > maxX: maxX = x
        if y > maxY: maxY = y
    
    for e in msp:
        try:
            layer = e.dxf.layer
            t = e.dxftype()
            if t == 'LINE':
                s, ed = e.dxf.start, e.dxf.end
                lines.append({'x1': s.x, 'y1': s.y, 'x2': ed.x, 'y2': ed.y, 'layer': layer})
                update_bounds(s.x, s.y); update_bounds(ed.x, ed.y)
            elif t == 'LWPOLYLINE':
                pts = [[p[0], p[1]] for p in e.get_points()]
                if pts:
                    polylines.append({'points': pts, 'closed': e.closed, 'layer': layer})
                    for p in pts: update_bounds(p[0], p[1])
            elif t == 'POLYLINE':
                pts = []
                try:
                    for v in e.vertices:
                        loc = v.dxf.location
                        pts.append([loc.x, loc.y])
                except:
                    pass
                if pts:
                    polylines.append({'points': pts, 'closed': e.is_closed, 'layer': layer})
                    for p in pts: update_bounds(p[0], p[1])
            elif t == 'CIRCLE':
                c = e.dxf.center
                r = e.dxf.radius
                circles.append({'cx': c.x, 'cy': c.y, 'r': r, 'layer': layer})
                update_bounds(c.x - r, c.y - r); update_bounds(c.x + r, c.y + r)
            elif t == 'ARC':
                c = e.dxf.center
                r = e.dxf.radius
                arcs.append({
                    'cx': c.x, 'cy': c.y, 'r': r,
                    'start': e.dxf.start_angle, 'end': e.dxf.end_angle,
                    'layer': layer
                })
                update_bounds(c.x - r, c.y - r); update_bounds(c.x + r, c.y + r)
            elif t == 'SPLINE':
                # v8.2: スプラインを折れ線近似して送る
                try:
                    # flattening - 一定のサグ値で近似点列を取得
                    pts = []
                    try:
                        for p in e.flattening(distance=50):  # 50mm間隔で近似
                            pts.append([p.x, p.y])
                    except:
                        # fallback: 制御点をそのまま使用
                        try:
                            for cp in e.control_points:
                                pts.append([cp[0], cp[1]])
                        except:
                            pass
                    if pts and len(pts) >= 2:
                        splines.append({'points': pts, 'layer': layer})
                        for p in pts: update_bounds(p[0], p[1])
                except Exception:
                    pass
            elif t == 'DIMENSION':
                try:
                    m = e.get_measurement()
                except:
                    m = 0
                txt = ''
                try:
                    txt = e.dxf.text if e.dxf.hasattr('text') else ''
                except:
                    pass
                # dimension の配置点
                ix, iy = 0, 0
                try:
                    if e.dxf.hasattr('defpoint'):
                        ix = e.dxf.defpoint.x; iy = e.dxf.defpoint.y
                except: pass
                dimensions.append({'text': txt, 'measurement': float(m), 'x': ix, 'y': iy, 'layer': layer})
            elif t in ('TEXT', 'MTEXT'):
                try:
                    if t == 'TEXT':
                        txt = e.dxf.text
                        x, y = e.dxf.insert.x, e.dxf.insert.y
                        h = e.dxf.height
                    else:
                        txt = e.text if hasattr(e, 'text') else e.dxf.text
                        x, y = e.dxf.insert.x, e.dxf.insert.y
                        h = e.dxf.char_height
                    texts.append({'text': txt, 'x': x, 'y': y, 'h': h, 'layer': layer})
                    update_bounds(x, y)
                except:
                    pass
        except Exception:
            continue
    
    if minX == float('inf'):
        minX, minY, maxX, maxY = 0, 0, 100, 100
    
    return jsonify({
        'bounds': {'minX': minX, 'minY': minY, 'maxX': maxX, 'maxY': maxY},
        'lines': lines,
        'arcs': arcs,
        'circles': circles,
        'polylines': polylines,
        'splines': splines,
        'dimensions': dimensions,
        'texts': texts,
        'layers': sorted(set([l.dxf.name for l in doc.layers])),
        'counts': {
            'lines': len(lines), 'arcs': len(arcs), 'circles': len(circles),
            'polylines': len(polylines), 'splines': len(splines),
            'dimensions': len(dimensions), 'texts': len(texts)
        }
    })

@app.route("/p/<token>/generate_3d_render", methods=["POST"])
def generate_3d_render(token):
    """PDFの平面図をGemini画像生成APIで3Dパース風にレンダリング"""
    if not get_site_by_token(token):
        abort(404)
    api_key = GOOGLE_API_KEY.strip()
    # 【v6.4】キーに日本語文字等が含まれていないかチェック&ASCII化
    try:
        api_key.encode('ascii')
    except UnicodeEncodeError:
        return jsonify({'error': 'APIキーにASCII以外の文字が含まれています。google_api_key.txtを確認してください'}), 500
    if not api_key:
        return jsonify({'error': 'GOOGLE_API_KEY未設定。\ndocker-compose.yml または google_api_key.txt ファイルで設定してください'}), 500
    # サーバーログ用にキーの先頭と長さを出力(キー全体は出さない)
    import sys as _sys
    _sys.stderr.write(f'[GEN_3D] api_key_prefix={api_key[:8]}... len={len(api_key)}\n')
    _sys.stderr.flush()

    data = request.get_json(silent=True) or {}
    category = data.get('category', '')
    filename = data.get('filename', '')
    style = data.get('style', 'modern')
    if category not in DOC_CATEGORIES:
        return jsonify({'error': 'invalid category'}), 400

    file_path = UPLOAD_BASE / token / PATHS["docs"] / category / filename
    if not file_path.exists():
        return jsonify({'error': f'ファイルが見つかりません: {filename}'}), 404

    # PDF → 第1ページをPNG化（v6.5 高解像度化でAIが詳細を読めるように）
    try:
        import fitz
        pdf = fitz.open(str(file_path))
        page = pdf.load_page(0)
        # v6.5: 3倍解像度にアップ、AIが壁位置や部屋を正確に認識できるように
        mat = fitz.Matrix(3.0, 3.0)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img_bytes = pix.tobytes("png")
        pdf.close()
    except Exception as e:
        return jsonify({'error': f'PDF読込エラー: {e}'}), 500

    # 【v6.5】忠実な3D化プロンプト - 平面図を正確に再現
    prompt = (
        "CRITICAL TASK: Convert this exact 2D floor plan into a 3D isometric visualization. "
        "You MUST preserve the EXACT layout shown in the floor plan — do NOT invent or modify anything. "
        "\n\n"
        "REQUIREMENTS:\n"
        "1. COUNT and position every room EXACTLY as shown in the plan (same number of rooms, same positions)\n"
        "2. The overall shape and proportions of the building must match the plan precisely\n"
        "3. Every wall shown in the plan must appear in the 3D render at the same position\n"
        "4. Every door opening and window must be placed exactly where shown\n"
        "5. Bathroom fixtures (tub, toilet, sink) if shown, must be preserved in the exact room\n"
        "6. Kitchen fixtures (sink, counter) if shown, must be preserved in their exact position\n"
        "\n"
        "VISUAL STYLE:\n"
        "- Isometric view from 45 degrees angle, slightly tilted (bird's-eye perspective)\n"
        "- No ceiling shown (top-down cutaway revealing all rooms)\n"
        "- Walls extruded upward as simple 3D volumes, matte white finish\n"
        "- Light wood-tone flooring\n"
        "- No furniture other than what is explicitly drawn in the plan (bath tub, toilet, kitchen sink if visible)\n"
        "- No decorative elements, no plants, no people\n"
        "- Soft natural lighting, clean architectural visualization\n"
        "- Style: clean SketchUp / Vectorworks / ArchiCAD 3D export aesthetic\n"
        "\n"
        "CRITICAL: The 3D view must look like the SAME building as the floor plan — if the plan shows 2 main rooms at the top and a bathroom/kitchen area at the bottom, your 3D must show EXACTLY that. Do not swap, add, remove, or rearrange rooms.\n"
        "\n"
        "Output only the rendered image with no text, labels, or annotations."
    )

    # Gemini画像生成
    try:
        from google import genai
        from google.genai import types
        # 環境変数の影響を除外するため、明示的にapi_keyのみ使う
        client = genai.Client(api_key=api_key)
        _sys.stderr.write(f'[GEN_3D] client created OK, img_size={len(img_bytes)}bytes, prompt_len={len(prompt)}\n')
        _sys.stderr.flush()
        response = client.models.generate_content(
            model='gemini-2.5-flash-image',
            contents=[
                types.Part.from_bytes(data=img_bytes, mime_type='image/png'),
                prompt,
            ],
        )
        import base64
        for part in response.candidates[0].content.parts:
            if getattr(part, 'inline_data', None) and part.inline_data.data:
                mime = part.inline_data.mime_type or 'image/png'
                b64 = base64.b64encode(part.inline_data.data).decode('ascii')
                return jsonify({'image': f'data:{mime};base64,{b64}'})
        # テキストのみ返ってきた場合
        text_parts = []
        for part in response.candidates[0].content.parts:
            if getattr(part, 'text', None):
                text_parts.append(part.text)
        msg = ' / '.join(text_parts) if text_parts else 'no image in response'
        return jsonify({'error': f'画像生成失敗: {msg}'}), 500
    except ImportError:
        return jsonify({'error': 'google-genai未インストール。requirements.txtにgoogle-genaiを追加してコンテナ再ビルドしてください'}), 500
    except Exception as e:
        err_str = str(e)
        import traceback
        tb = traceback.format_exc()
        _sys.stderr.write(f'[GEN_3D ERROR] {err_str}\n{tb}\n')
        _sys.stderr.flush()
        # エラーを分類して分かりやすいメッセージにする
        if 'ascii' in err_str.lower() and 'codec' in err_str.lower():
            return jsonify({'error': (
                f'文字エンコーディングエラー: {err_str}\n\n'
                'google-genaiライブラリのバージョンが古い可能性があります。\n'
                'requirements.txt を確認してください。'
            )}), 500
        if 'PERMISSION_DENIED' in err_str or 'API_KEY_SERVICE_BLOCKED' in err_str:
            return jsonify({'error': (
                'APIキーで画像生成モデル(Gemini Image)が許可されていません。\n'
                '解決方法:\n'
                '1) https://aistudio.google.com/apikey にアクセス\n'
                '2) 現在のAPIキーを削除し、「制限なし」で新しいAPIキーを作成\n'
                '3) docker-compose.yml の GOOGLE_API_KEY を新しいキーに更新\n'
                '4) sudo docker compose up -d で再起動'
            )}), 500
        if 'API_KEY_INVALID' in err_str or 'API key not valid' in err_str:
            return jsonify({'error': 'APIキーが無効です。docker-compose.ymlのGOOGLE_API_KEYを確認してください'}), 500
        if 'RESOURCE_EXHAUSTED' in err_str or 'quota' in err_str.lower():
            return jsonify({'error': '無料枠の使用量上限に達しました。Google Cloud Consoleで課金を有効化するか、時間を置いて再試行してください'}), 500
        if 'model' in err_str.lower() and 'not found' in err_str.lower():
            return jsonify({'error': 'gemini-2.5-flash-imageモデルが利用できません。Google AI Studioでモデルの有効化を確認してください'}), 500
        return jsonify({'error': f'Gemini APIエラー: {err_str[:500]}'}), 500


@app.route("/uploads/<token>/photos/<filename>")
def serve_photo(token, filename):
    return send_from_directory(UPLOAD_BASE / token / PATHS["photos"], filename)

@app.route("/uploads/<token>/thumbs/<filename>")
def serve_thumb(token, filename):
    return send_from_directory(UPLOAD_BASE / token / PATHS["thumbs"], filename)

@app.route("/uploads/<token>/memo_images/<filename>")
def serve_memo_image(token, filename):
    return send_from_directory(UPLOAD_BASE / token / PATHS["memo_images"], filename)

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)

# ====== v1.8 PWA (Androidアプリ化) ======
@app.route("/manifest.webmanifest")
def pwa_manifest():
    """PWAマニフェスト - これを <link> で読み込ませると "ホーム画面に追加" でアプリ化される"""
    from flask import jsonify as _jsonify
    return _jsonify({
        "name": "現場フォト管理 - シービー合同会社",
        "short_name": "現場フォト",
        "description": "現場管理・写真共有・出退勤・割付計算",
        "start_url": "/my-sites",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#f5f3ee",
        "theme_color": "#2d2520",
        "icons": [
            {"src": "/pwa_icon/192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/pwa_icon/512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
        ],
        "shortcuts": [
            {"name": "現場一覧", "url": "/my-sites", "icons": [{"src": "/pwa_icon/192.png", "sizes": "192x192"}]},
            {"name": "管理画面", "url": "/admin", "icons": [{"src": "/pwa_icon/192.png", "sizes": "192x192"}]},
            {"name": "全体レポート", "url": "/admin/report", "icons": [{"src": "/pwa_icon/192.png", "sizes": "192x192"}]}
        ]
    })

@app.route("/pwa_icon/<int:size>.png")
def pwa_icon(size):
    """PWA用アプリアイコンを Pillow で動的生成 (現場フォト管理 ロゴ風)"""
    from flask import Response
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return Response("Pillow not installed", status=500)
    if size not in (192, 512, 144, 96, 72, 48):
        size = 192
    # ベース: 暖色グラデの正方形 + 中央に "📍" + "現場" 風テキスト
    img = Image.new('RGB', (size, size), '#2d2520')
    d = ImageDraw.Draw(img)
    # グラデ風 (上→下で2色塗り重ね)
    for y in range(size):
        # 暖色グラデ
        t = y / size
        r = int(0x2d + (0xff - 0x2d) * t * 0.4)
        g = int(0x25 + (0x6b - 0x25) * t * 0.6)
        b = int(0x20 + (0x35 - 0x20) * t * 0.6)
        d.line([(0, y), (size, y)], fill=(r, g, b))
    # 中央に大きい📍 (Pillowはemoji描画が環境依存なので、代わりに幾何アイコン)
    # オレンジの円 + 白い "現場" 文字
    cx, cy = size // 2, size // 2
    r1 = int(size * 0.32)
    d.ellipse([cx-r1, cy-r1, cx+r1, cy+r1], fill='#ff6b35', outline='#fff', width=max(3, size // 64))
    # 中央に文字
    label = "現場"
    try:
        # 一般的に存在しそうなフォントを試す
        font_paths = [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
        font = None
        for fp in font_paths:
            try:
                font = ImageFont.truetype(fp, int(size * 0.22))
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()
    try:
        bbox = d.textbbox((0, 0), label, font=font)
        tw = bbox[2] - bbox[0]; th = bbox[3] - bbox[1]
    except Exception:
        tw, th = font.getsize(label) if hasattr(font, 'getsize') else (size//4, size//5)
    d.text((cx - tw//2, cy - th//2 - bbox[1] if 'bbox' in dir() else cy - th//2), label, fill='#ffffff', font=font)
    # PNG出力
    import io as _io
    buf = _io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return Response(buf.getvalue(), mimetype='image/png',
                    headers={'Cache-Control': 'public, max-age=86400'})

# ====== v1.8 360°パノラマ写真 ======
@app.route("/p/<token>/pano_camera")
def pano_camera_page(token):
    """サイト内で360°撮影する専用カメラページ"""
    site = get_site_by_token(token)
    if not site:
        abort(404)
    return render_template("pano_camera.html", token=token, site_name=site.get('name', ''))

@app.route("/p/<token>/tour")
def panorama_tour(token):
    """v1.9: 360°パノラマ写真をPannellumで連結ツアー表示 (Matterport風)"""
    site = get_site_by_token(token)
    if not site:
        abort(404)
    return render_template("tour.html", token=token, site_name=site.get('name', ''))

def _to_equirectangular_2x1(img_path: Path):
    """v1.9 任意のパノラマ画像を 2:1 equirectangular に変換 (上下スマートパディング)。
    - 既に 2:1 (1.95-2.05) → そのまま (4096上限にリサイズ)
    - それ以上に横長 (iPhone標準パノラマ等 ~6:1) → 上下を空色・床色グラデで自動補完
    - それ未満 → そのまま保存 (ツアー表示時に多少違和感あるが破綻はしない)
    """
    from PIL import Image, ImageOps
    img = Image.open(str(img_path))
    img = ImageOps.exif_transpose(img)
    img = img.convert('RGB')
    w, h = img.size
    aspect = w / h if h else 0

    TW, TH = 4096, 2048

    if 1.95 <= aspect <= 2.05:
        # 既に正規 2:1
        if w > TW:
            img.thumbnail((TW, TH), Image.LANCZOS)
        img.save(str(img_path), 'JPEG', quality=90, optimize=True, progressive=True)
        return

    if aspect <= 1.95:
        # 縦長 / 通常写真 - 触らずそのまま (Pannellumで部分パノラマとして見える)
        img.save(str(img_path), 'JPEG', quality=90, optimize=True, progressive=True)
        return

    # ここから: 横長パノラマを 2:1 equirectangular にスマートパディング
    # 1) 幅TWに合わせてリサイズ (高さは比例)
    new_w = TW
    new_h = int(round(h * (TW / w)))
    if new_h >= TH:
        # こんな横長まれだが、安全側で TH に収める
        scale = TH / new_h
        new_w = int(new_w * scale)
        new_h = TH
    src = img.resize((new_w, new_h), Image.LANCZOS)

    # 2) 上下端の代表色をサンプリング (中央を避けて両端から)
    def avg_strip(im, top_or_bot, n_rows=24):
        px = im.load()
        ww, hh = im.size
        ys = range(n_rows) if top_or_bot == 'top' else range(hh - n_rows, hh)
        rs, gs, bs, cnt = 0, 0, 0, 0
        for y in ys:
            for x in range(0, ww, 8):
                r, g, b = px[x, y]
                rs += r; gs += g; bs += b; cnt += 1
        return (rs // cnt, gs // cnt, bs // cnt) if cnt else (200, 220, 240)

    top_color = avg_strip(src, 'top')
    bot_color = avg_strip(src, 'bottom')

    # 3) 天頂(zenith)・天底(nadir)の理想色 (空っぽい色 / 床っぽい色)
    # 天頂は明るい青、天底は中庸グレー (現場用途で違和感少ない無難な色)
    ZENITH = (210, 225, 240)
    NADIR  = (60, 55, 50)

    canvas = Image.new('RGB', (TW, TH), top_color)

    # 上下パディング高さ
    y_offset = (TH - new_h) // 2
    x_offset = (TW - new_w) // 2

    # 4) 上パディング: zenith → top_color のグラデ
    if y_offset > 0:
        try:
            import numpy as np
            t = np.linspace(0, 1, y_offset)[:, None]
            top_arr = (1 - t) * np.array(ZENITH) + t * np.array(top_color)
            top_strip = np.repeat(top_arr[:, None, :], TW, axis=1).astype('uint8')
            canvas.paste(Image.fromarray(top_strip, 'RGB'), (0, 0))
        except Exception:
            for y in range(y_offset):
                t = y / max(1, y_offset - 1)
                c = tuple(int((1-t)*z + t*tc) for z, tc in zip(ZENITH, top_color))
                Image.new('RGB', (TW, 1), c).paste(canvas, (0, y))  # fallback

    # 5) 中央: 元画像
    canvas.paste(src, (x_offset, y_offset))

    # 6) 下パディング: bot_color → nadir のグラデ
    bot_pad_y = y_offset + new_h
    bot_pad_h = TH - bot_pad_y
    if bot_pad_h > 0:
        try:
            import numpy as np
            t = np.linspace(0, 1, bot_pad_h)[:, None]
            bot_arr = (1 - t) * np.array(bot_color) + t * np.array(NADIR)
            bot_strip = np.repeat(bot_arr[:, None, :], TW, axis=1).astype('uint8')
            canvas.paste(Image.fromarray(bot_strip, 'RGB'), (0, bot_pad_y))
        except Exception:
            pass

    canvas.save(str(img_path), 'JPEG', quality=90, optimize=True, progressive=True)




# ========================================================================
# v2.0 (2026-04-28) ジャイロ既知 球面投影合成 (B19パターン) 追加
# ========================================================================

# =============================================================================
# v2.0 ジャイロ既知 球面投影合成 (B19パターン)
#
# 既存の `_to_equirectangular_2x1` の直前か直後に挿入してください。
# 新エンドポイント `/p/<token>/panorama/upload_full` も併せて追加。
#
# 依存: numpy + Pillow (既存の Pillow と一緒に入っているはず)。
# cv2 があれば自動で使って高速化(4K で 0.4s)。無ければ numpy にフォールバック(2K で 5s)。
# requirements.txt も Dockerfile も触らずに、コンテナ「再起動」のみで反映できます。
# =============================================================================

import math


# cv2 (opencv-python) があれば使う、無ければ numpy で
try:
    import cv2 as _cv2
    _HAS_CV2 = True
except Exception:
    _cv2 = None
    _HAS_CV2 = False


def _np_remap_bilinear(img_arr, map_x, map_y):
    """cv2.remap 相当 (bilinear) を numpy 単体で実装。"""
    import numpy as np
    H, W = img_arr.shape[:2]
    map_x = np.clip(map_x, 0, W - 1)
    map_y = np.clip(map_y, 0, H - 1)
    x0 = np.floor(map_x).astype(np.int32)
    y0 = np.floor(map_y).astype(np.int32)
    x1 = np.minimum(x0 + 1, W - 1)
    y1 = np.minimum(y0 + 1, H - 1)
    fx = (map_x - x0).astype(np.float32)
    fy = (map_y - y0).astype(np.float32)
    p00 = img_arr[y0, x0].astype(np.float32)
    p10 = img_arr[y0, x1].astype(np.float32)
    p01 = img_arr[y1, x0].astype(np.float32)
    p11 = img_arr[y1, x1].astype(np.float32)
    p0 = p00 * (1 - fx)[..., None] + p10 * fx[..., None]
    p1 = p01 * (1 - fx)[..., None] + p11 * fx[..., None]
    return p0 * (1 - fy)[..., None] + p1 * fy[..., None]


def _stitch_known_rotations_sphere(frames_with_meta, eq_w=4096, eq_h=2048):
    """
    [B19方式] ジャイロ角度既知のフレーム群を equirectangular へ球面投影合成。
    特徴点マッチング不要 → 白壁・低照度でも安定。

    frames_with_meta: list of dict {
        'image': np.ndarray (HxWx3, uint8, RGB),
        'yaw': float (deg),
        'pitch': float (deg),
        'fov': float (deg, 水平視野角),
    }
    """
    import numpy as np
    if not frames_with_meta:
        return None

    canvas = np.zeros((eq_h, eq_w, 3), dtype=np.float32)
    weights = np.zeros((eq_h, eq_w), dtype=np.float32)

    UU, VV = np.meshgrid(np.arange(eq_w, dtype=np.float32),
                         np.arange(eq_h, dtype=np.float32))
    lon = (UU / eq_w - 0.5) * 2 * math.pi
    lat = (0.5 - VV / eq_h) * math.pi
    cl = np.cos(lat); sl = np.sin(lat)
    world = np.stack([cl * np.sin(lon), sl, cl * np.cos(lon)], axis=-1)

    for fm in frames_with_meta:
        img = fm.get('image')
        if img is None:
            continue
        H, W = img.shape[:2]
        fov_h = math.radians(fm['fov'])
        fov_v = 2 * math.atan(math.tan(fov_h / 2) * H / W)
        yaw = math.radians(fm['yaw'])
        pitch = math.radians(fm['pitch'])
        Rp = np.array([[1, 0, 0],
                       [0, math.cos(pitch), -math.sin(pitch)],
                       [0, math.sin(pitch),  math.cos(pitch)]], dtype=np.float32)
        Ry = np.array([[math.cos(yaw), 0, math.sin(yaw)],
                       [0, 1, 0],
                       [-math.sin(yaw), 0, math.cos(yaw)]], dtype=np.float32)
        R_inv = (Ry @ Rp).T
        cam = world @ R_inv.T
        cz = cam[..., 2]
        in_front = cz > 0.01
        cz_safe = np.where(in_front, cz, 1.0)
        cx = cam[..., 0] / cz_safe
        cy = cam[..., 1] / cz_safe
        xn = cx / math.tan(fov_h / 2)
        yn = cy / math.tan(fov_v / 2)
        in_fov = in_front & (np.abs(xn) <= 1) & (np.abs(yn) <= 1)
        px = ((xn + 1) / 2 * (W - 1)).astype(np.float32)
        py = ((yn + 1) / 2 * (H - 1)).astype(np.float32)
        # cv2 があれば 60倍速い
        if _HAS_CV2:
            sampled = _cv2.remap(img, px, py, _cv2.INTER_LINEAR,
                                 borderMode=_cv2.BORDER_REPLICATE).astype(np.float32)
        else:
            sampled = _np_remap_bilinear(img, px, py)
        weight = (np.maximum(0, 1 - np.maximum(np.abs(xn), np.abs(yn)) ** 2) * in_fov).astype(np.float32)
        canvas += sampled * weight[..., None]
        weights += weight

    weights_safe = np.maximum(weights, 1e-6)
    out = (canvas / weights_safe[..., None]).clip(0, 255).astype(np.uint8)

    unfilled = weights < 0.01
    if unfilled.any():
        filled = ~unfilled
        if filled.any():
            avg = out[filled].mean(axis=0).astype(np.uint8)
        else:
            avg = np.array([128, 128, 128], dtype=np.uint8)
        out[unfilled] = avg

    return out


@app.route("/p/<token>/panorama/upload_full", methods=["POST"])
def upload_panorama_full(token):
    """
    [B19方式] 複数コマ + 各メタデータ(yaw,pitch,fov)で球面投影合成。
    既存 /panorama/upload は単品アップ用にそのまま残す。
    """
    if not get_site_by_token(token):
        abort(404)

    import numpy as np
    from PIL import Image
    import io
    import time as _t

    # cv2 があれば 4K、無ければ 2K を既定 (numpy だと 4K は20秒以上かかる)
    eq_w = 4096 if _HAS_CV2 else 2048
    eq_h = eq_w // 2

    # メタデータ
    try:
        metas = json.loads(request.form.get('meta', '[]'))
    except Exception as e:
        return jsonify({"ok": False, "error": "メタデータJSONが不正"}), 400

    files = request.files.getlist('frames')
    if not files:
        return jsonify({"ok": False, "error": "framesが空"}), 400
    if len(files) != len(metas):
        return jsonify({"ok": False, "error": f"枚数不一致 frames={len(files)} metas={len(metas)}"}), 400
    if len(files) < 8:
        return jsonify({"ok": False, "error": "最低8枚必要"}), 400

    # 全コマを numpy にデコード(Pillow経由)
    frames_with_meta = []
    for f, m in zip(files, metas):
        try:
            data = f.read()
            pil = Image.open(io.BytesIO(data)).convert('RGB')
            # 大きすぎたら 1280px に縮小(品質ほぼ変わらず高速化)
            if pil.width > 1280:
                ratio = 1280 / pil.width
                pil = pil.resize((1280, int(pil.height * ratio)), Image.LANCZOS)
            arr = np.array(pil)  # HxWx3 RGB
            frames_with_meta.append({
                'image': arr,
                'yaw': float(m.get('yaw', 0)),
                'pitch': float(m.get('pitch', 0)),
                'fov': float(m.get('fov', 90)),
            })
        except Exception as e:
            print(f"[upload_full] frame decode skip: {e}")

    if len(frames_with_meta) < 8:
        return jsonify({"ok": False, "error": "有効フレーム不足"}), 400

    # 合成
    print(f"[upload_full] stitching {len(frames_with_meta)} frames at {eq_w}x{eq_h}, cv2={_HAS_CV2}...")
    t0 = _t.time()
    try:
        pano_rgb = _stitch_known_rotations_sphere(frames_with_meta, eq_w=eq_w, eq_h=eq_h)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "error": f"合成エラー: {e}"}), 500
    if pano_rgb is None:
        return jsonify({"ok": False, "error": "合成失敗"}), 500
    elapsed = _t.time() - t0

    # 保存
    pano_dir = UPLOAD_BASE / token / PATHS["panoramas"]
    pano_dir.mkdir(parents=True, exist_ok=True)
    title = (request.form.get('title') or 'panorama').strip()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_title = re.sub(r'[\\/:*?"<>|\n\r\t ]', '_', title or 'panorama')[:40]
    fname = f"{ts}_{safe_title}.jpg"
    saved = pano_dir / fname
    Image.fromarray(pano_rgb, 'RGB').save(str(saved), 'JPEG', quality=90,
                                           optimize=True, progressive=True)

    # サムネ
    try:
        with Image.open(saved) as img:
            img.thumbnail((1600, 800), Image.LANCZOS)
            (UPLOAD_BASE / token / PATHS["thumbs"]).mkdir(parents=True, exist_ok=True)
            img.save(UPLOAD_BASE / token / PATHS["thumbs"] / f"pano_{Path(fname).stem}.jpg",
                     'JPEG', quality=80)
    except Exception as e:
        print(f"[upload_full] thumb skip: {e}")

    print(f"[upload_full] saved: {fname}  {len(frames_with_meta)} frames  {elapsed:.1f}s")
    return jsonify({"ok": True, "filename": fname, "title": title,
                    "frames_used": len(frames_with_meta),
                    "elapsed_sec": round(elapsed, 1),
                    "resolution": f"{eq_w}x{eq_h}",
                    "engine": "cv2" if _HAS_CV2 else "numpy"})

# ========== ↑ 追加終了 ↓ 既存コード続き ==========

@app.route("/p/<token>/panorama/upload", methods=["POST"])
def upload_panorama(token):
    """360度パノラマ画像をアップロード (uploads/<token>/panoramas/ に保存)
    v1.9: 2:1でない画像は自動的に上下パディングしてequirectangular化"""
    if not get_site_by_token(token):
        abort(404)
    f = request.files.get('panorama')
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "ファイル未指定"}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.webp'):
        return jsonify({"ok": False, "error": "JPG/PNG/WEBPのみ対応"}), 400
    pano_dir = UPLOAD_BASE / token / PATHS["panoramas"]
    pano_dir.mkdir(parents=True, exist_ok=True)
    title = (request.form.get('title') or '').strip()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_title = re.sub(r'[\\/:*?"<>|\n\r\t ]', '_', title or 'panorama')[:40]
    # 統一して .jpg で保存 (PNG/WEBPでも内部で再エンコード)
    fname = f"{ts}_{safe_title}.jpg"
    saved_path = pano_dir / fname
    f.save(saved_path)
    # 2:1 equirectangular へ変換 (横長パノラマは上下パディング)
    try:
        _to_equirectangular_2x1(saved_path)
    except Exception as e:
        print(f"[pano upload] padding skipped: {e}")
    # サムネ生成
    try:
        from PIL import Image
        with Image.open(saved_path) as img:
            img.thumbnail((1600, 800), Image.LANCZOS)
            thumb_dir = UPLOAD_BASE / token / PATHS["thumbs"]
            thumb_dir.mkdir(parents=True, exist_ok=True)
            img.save(thumb_dir / f"pano_{Path(fname).stem}.jpg", 'JPEG', quality=80)
    except Exception:
        pass
    return jsonify({"ok": True, "filename": fname, "title": title})

@app.route("/p/<token>/panoramas")
def list_panoramas(token):
    if not get_site_by_token(token):
        abort(404)
    pano_dir = UPLOAD_BASE / token / PATHS["panoramas"]
    items = []
    if pano_dir.exists():
        for p in sorted(pano_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            ext = p.suffix.lower()
            if ext not in ('.jpg', '.jpeg', '.png', '.webp'):
                continue
            stem = p.stem
            # ファイル名から title を逆算
            parts = stem.split('_', 2)
            title = parts[2].replace('_', ' ') if len(parts) >= 3 else stem
            dt = datetime.fromtimestamp(p.stat().st_mtime)
            thumb = UPLOAD_BASE / token / PATHS["thumbs"] / f"pano_{stem}.jpg"
            items.append({
                "filename": p.name,
                "title": title,
                "url": f"/uploads/{token}/panoramas/{p.name}",
                "thumb_url": f"/uploads/{token}/thumbs/pano_{stem}.jpg" if thumb.exists() else f"/uploads/{token}/panoramas/{p.name}",
                "date": dt.strftime("%Y/%m/%d %H:%M"),
            })
    return jsonify({"items": items})

@app.route("/p/<token>/panorama/delete/<filename>", methods=["POST"])
def delete_panorama(token, filename):
    if not get_site_by_token(token):
        abort(404)
    safe = secure_filename(filename)
    fp = UPLOAD_BASE / token / PATHS["panoramas"] / safe
    if fp.exists():
        fp.unlink()
        thumb = UPLOAD_BASE / token / PATHS["thumbs"] / f"pano_{Path(safe).stem}.jpg"
        if thumb.exists():
            thumb.unlink()
    return jsonify({"ok": True})

@app.route("/uploads/<token>/panoramas/<filename>")
def serve_panorama(token, filename):
    return send_from_directory(UPLOAD_BASE / token / PATHS["panoramas"], filename)

# ====== v1.8.7 購読 API ======
@app.route("/p/<token>/subscribe", methods=["POST"])
def subscribe(token):
    """現場ページから誰でも購読登録できる"""
    if not get_site_by_token(token):
        abort(404)
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get('email') or request.form.get('email') or '').strip().lower()
    if not email or '@' not in email or '.' not in email:
        return jsonify({"ok": False, "error": "メールアドレスが正しくありません"}), 400
    unsub = add_subscriber(token, email)
    if unsub is None:
        return jsonify({"ok": False, "error": "登録失敗"}), 400
    # 確認メール
    site = load_sites().get(token, {})
    confirm_url = f"{SITE_BASE_URL}/unsubscribe/{unsub}"
    body = (
        f"通知の購読を登録しました。\n\n"
        f"現場: {site.get('name','')}\n"
        f"メール: {email}\n"
        f"日時: {datetime.now().strftime('%Y/%m/%d %H:%M')}\n\n"
        f"今後この現場のメモ追加・出退勤・写真追加などのタイミングで通知が届きます。\n\n"
        f"--\n📧 配信停止はいつでも↓のリンクからワンクリックで可能です\n{confirm_url}"
    )
    # 確認メールは購読者本人だけに送る
    if SMTP_USER and SMTP_PASS:
        def _confirm():
            try:
                with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
                    s.starttls()
                    s.login(SMTP_USER, SMTP_PASS)
                    msg = MIMEMultipart()
                    msg['From'] = SMTP_USER
                    msg['To'] = email
                    msg['Subject'] = f"[現場フォト] 通知購読を登録しました - {site.get('name','')}"
                    msg.attach(MIMEText(body, 'plain', 'utf-8'))
                    s.send_message(msg)
            except Exception as e:
                print(f"[SUBSCRIBE CONFIRM ERROR] {e}")
        threading.Thread(target=_confirm, daemon=True).start()
    # 管理者にも通知
    send_notify(f"🔔 通知購読が追加: {email}", f"購読者: {email}\n現場: {site.get('name','')}", site.get('name',''), token=token)
    return jsonify({"ok": True, "email": email})

@app.route("/p/<token>/subscribers")
def list_subscribers(token):
    """現場の購読者一覧 (管理者画面用)"""
    if not get_site_by_token(token):
        abort(404)
    subs = load_subscribers(token)
    # email と subscribed_at だけ返す (unsub_token は内部用のため伏せる)
    return jsonify({"items": [{"email": s.get('email',''), "subscribed_at": s.get('subscribed_at','')} for s in subs]})

@app.route("/p/<token>/subscribe/remove", methods=["POST"])
def admin_remove_subscriber(token):
    """管理者画面から購読者を削除 (?admin=1)"""
    if request.args.get('admin') != '1':
        abort(403)
    if not get_site_by_token(token):
        abort(404)
    data = request.get_json(force=True, silent=True) or {}
    target = (data.get('email') or '').strip().lower()
    subs = load_subscribers(token)
    new_subs = [s for s in subs if s.get('email','').lower() != target]
    if len(new_subs) == len(subs):
        return jsonify({"ok": False, "error": "見つかりませんでした"}), 404
    save_subscribers(token, new_subs)
    return jsonify({"ok": True, "removed": target})

@app.route("/unsubscribe/<unsub_token>")
def unsubscribe(unsub_token):
    """ワンクリック配信停止 (メールから誘導)"""
    info = find_subscriber_by_unsub(unsub_token)
    if not info:
        return _unsub_html("無効または既に削除済みのリンクです", success=False)
    site_token = info['token']
    sub_email = info['subscriber'].get('email','')
    site_name = load_sites().get(site_token, {}).get('name','')
    subs = load_subscribers(site_token)
    new_subs = [s for s in subs if s.get('unsub_token') != unsub_token]
    save_subscribers(site_token, new_subs)
    return _unsub_html(f"配信停止しました。<br><br>メール: <b>{sub_email}</b><br>現場: <b>{site_name}</b><br><br>今後この現場からの通知メールは届きません。", success=True)

def _unsub_html(message, success=True):
    color = '#4caf50' if success else '#c33'
    icon = '✅' if success else '⚠️'
    return f"""<!DOCTYPE html><html lang="ja"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>配信停止 - 現場フォト</title>
<style>body{{font-family:'Noto Sans JP',sans-serif;background:#f5f3ee;color:#2d2520;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;margin:0}}
.box{{background:#fff;border-radius:14px;box-shadow:0 4px 20px rgba(0,0,0,.08);padding:40px 30px;max-width:420px;text-align:center}}
.icon{{font-size:4rem;margin-bottom:14px}}
h1{{font-size:1.2rem;color:{color};margin-bottom:14px}}
p{{font-size:.92rem;line-height:1.8;color:#555}}
a{{display:inline-block;margin-top:24px;padding:10px 24px;background:#2d2520;color:#fff;text-decoration:none;border-radius:8px;font-size:.85rem;font-weight:700}}</style>
</head><body><div class="box"><div class="icon">{icon}</div><h1>配信停止</h1><p>{message}</p><a href="/my-sites">📍 現場一覧へ</a></div></body></html>"""

def _site_blocked_html(site, status):
    """アーカイブ/公開停止された現場の表示用HTML"""
    name = site.get('name','')
    msg_map = {
        'archived': ('📦 アーカイブ済み', 'この現場はアーカイブされています。<br>過去の記録として保存されており、現在は更新できません。', '#5a8fc2'),
        'closed':   ('🚫 公開停止中',     'この現場ページは現在公開停止されています。<br>管理者にお問い合わせください。', '#c33'),
    }
    title, message, color = msg_map.get(status, ('完了', 'この現場は終了しています', '#888'))
    return f"""<!DOCTYPE html><html lang="ja"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} - {name}</title>
<style>body{{font-family:'Noto Sans JP',sans-serif;background:#f5f3ee;color:#2d2520;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;margin:0}}
.box{{background:#fff;border-radius:14px;box-shadow:0 4px 20px rgba(0,0,0,.08);padding:50px 30px;max-width:480px;text-align:center}}
.icon{{font-size:5rem;margin-bottom:14px}}
h1{{font-size:1.4rem;color:{color};margin-bottom:16px}}
.name{{font-size:.95rem;color:#666;margin-bottom:20px;background:#f5f3ee;padding:8px 14px;border-radius:8px;display:inline-block}}
p{{font-size:.95rem;line-height:1.9;color:#555;margin-bottom:20px}}
a{{display:inline-block;padding:10px 24px;background:#2d2520;color:#fff;text-decoration:none;border-radius:8px;font-size:.85rem;font-weight:700}}</style>
</head><body><div class="box"><div class="icon">{title.split(' ')[0]}</div><h1>{title}</h1><div class="name">📍 {name}</div><p>{message}</p><a href="/my-sites">📍 現場一覧へ</a></div></body></html>"""

# ====== v1.8.9 現場ステータス (active / archived / closed) ======
@app.route("/admin/site_status/<token>", methods=["POST"])
def admin_site_status(token):
    """現場のステータスを変更 (active/archived/closed)"""
    sites = load_sites()
    if token not in sites:
        return jsonify({"ok": False, "error": "現場が見つかりません"}), 404
    data = request.get_json(force=True, silent=True) or {}
    new_status = (data.get('status') or '').strip()
    if new_status not in ('active', 'archived', 'closed'):
        return jsonify({"ok": False, "error": "ステータスは active/archived/closed のいずれか"}), 400
    sites[token]['status'] = new_status
    sites[token]['status_changed_at'] = datetime.now().strftime("%Y/%m/%d %H:%M")
    save_sites(sites)
    label = {'active':'✅ アクティブ', 'archived':'📦 アーカイブ', 'closed':'🚫 公開停止'}[new_status]
    send_notify(f"{label}: 現場ステータス変更", f"現場: {sites[token].get('name','')}\n新ステータス: {label}", sites[token].get('name',''), token=token)
    return jsonify({"ok": True, "status": new_status})

UPLOAD_BASE.mkdir(exist_ok=True)

# ===== 地図・駐車場 =====
@app.route('/p/<token>/parking', methods=['GET'])
def get_parking(token):
    site = get_site_by_token(token)
    if not site:
        return jsonify({'error':'not found'}), 404
    proj_path = str(UPLOAD_BASE / token)
    park_file = str(UPLOAD_BASE / token / PATHS["parking.json"])
    if not os.path.exists(park_file):
        return jsonify({'location':None, 'parkings':[]})
    with open(park_file, 'r', encoding='utf-8') as f:
        return jsonify(json.load(f))

@app.route('/p/<token>/parking', methods=['POST'])
def save_parking(token):
    site = get_site_by_token(token)
    if not site:
        return jsonify({'error':'not found'}), 404
    proj_path = str(UPLOAD_BASE / token)
    park_file = str(UPLOAD_BASE / token / PATHS["parking.json"])
    data = request.get_json()
    with open(park_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return jsonify({'ok': True})

def _ensure_site_latlng(token, site):
    """v1.8.8: 現場の lat/lng が無ければ住所から Google Geocoding で取得し、sites.json にキャッシュ。
    出退勤の GPS 自律ジオフェンスでも自動セットされるが、まだ誰も打刻してない場合のフォールバック。"""
    lat = site.get('lat'); lng = site.get('lng')
    if lat is not None and lng is not None:
        return lat, lng
    addr = site.get('address', '').strip()
    if not addr:
        return None, None

    def _save(_lat, _lng, source):
        sites = load_sites()
        if token in sites:
            sites[token]['lat'] = _lat
            sites[token]['lng'] = _lng
            sites[token]['lat_lng_source'] = source
            sites[token]['lat_lng_set_at'] = datetime.now().isoformat()
            save_sites(sites)

    # ① Google Geocoding (APIキーがあれば試行)
    if GOOGLE_API_KEY:
        try:
            geo_url = ('https://maps.googleapis.com/maps/api/geocode/json?address='
                       + urllib.parse.quote(addr) + '&language=ja&key=' + GOOGLE_API_KEY)
            with urllib.request.urlopen(geo_url, timeout=8) as r:
                gd = json.loads(r.read().decode('utf-8'))
            results = gd.get('results') or []
            if results:
                loc = results[0].get('geometry', {}).get('location', {})
                _lat = loc.get('lat'); _lng = loc.get('lng')
                if _lat is not None and _lng is not None:
                    _save(_lat, _lng, 'geocoded_google')
                    return _lat, _lng
            else:
                print(f"[GEOCODE] Google: {gd.get('status')} ({gd.get('error_message','')[:80]})")
        except Exception as e:
            print(f"[GEOCODE] Google fail: {e}")

    # ② フォールバック: 国土地理院 Address Search API (無料・登録不要)
    # https://msearch.gsi.go.jp/address-search/AddressSearch?q=<住所>
    try:
        gsi_url = 'https://msearch.gsi.go.jp/address-search/AddressSearch?q=' + urllib.parse.quote(addr)
        with urllib.request.urlopen(gsi_url, timeout=8) as r:
            data = json.loads(r.read().decode('utf-8'))
        # レスポンス: [{"geometry":{"coordinates":[lng,lat],"type":"Point"},"properties":{"title":"..."},"type":"Feature"},...]
        if data and isinstance(data, list) and len(data) > 0:
            coords = data[0].get('geometry', {}).get('coordinates', [])
            if len(coords) >= 2:
                _lng = float(coords[0]); _lat = float(coords[1])
                _save(_lat, _lng, 'geocoded_gsi')
                print(f"[GEOCODE] GSI fallback OK: {addr} → {_lat},{_lng}")
                return _lat, _lng
    except Exception as e:
        print(f"[GEOCODE] GSI fail: {e}")

    return None, None

@app.route('/p/<token>/search_parking', methods=['GET'])
def search_parking(token):
    """Google Places API で周辺駐車場を検索（バックエンドプロキシ）。
    v1.8.8: lat/lng が省略されたら現場の住所から自動補完する。"""
    site = get_site_by_token(token)
    if not site:
        return jsonify({'error': 'not found'}), 404
    lat = request.args.get('lat', '')
    lng = request.args.get('lng', '')
    if not lat or not lng:
        # 自動取得 (まず保存済 → 住所からジオコード)
        auto_lat, auto_lng = _ensure_site_latlng(token, site)
        if auto_lat is None:
            return jsonify({'error': '現場の住所から座標が取得できません。住所を確認するか、業者の打刻で位置を学習してください', 'results': []}), 200
        lat, lng = str(auto_lat), str(auto_lng)
    # 駐車場検索: Google Places Nearby Search → 失敗(REQUEST_DENIED等)なら OpenStreetMap Overpass API にフォールバック
    google_results = []
    google_status = None
    if GOOGLE_API_KEY:
        try:
            params = urllib.parse.urlencode({
                'location': f'{lat},{lng}',
                'radius': 500,
                'type': 'parking',
                'language': 'ja',
                'key': GOOGLE_API_KEY,
            })
            api_url = f'https://maps.googleapis.com/maps/api/place/nearbysearch/json?{params}'
            with urllib.request.urlopen(urllib.request.Request(api_url), timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            google_status = data.get('status')
            for place in data.get('results', []):
                loc = place.get('geometry', {}).get('location', {})
                google_results.append({
                    'name': place.get('name', 'パーキング'),
                    'address': place.get('vicinity', ''),
                    'lat': loc.get('lat'),
                    'lng': loc.get('lng'),
                    'rating': place.get('rating'),
                    'open_now': place.get('opening_hours', {}).get('open_now'),
                    'total_ratings': place.get('user_ratings_total', 0),
                    'source': 'google',
                })
        except Exception as e:
            print(f"[PARKING] Google fail: {e}")

    if google_results:
        return jsonify({'results': google_results, 'status': google_status or 'OK', 'source': 'google'})

    # フォールバック: OpenStreetMap Overpass API (無料・登録不要)
    try:
        overpass_query = (
            "[out:json][timeout:20];("
            f"node[\"amenity\"=\"parking\"](around:500,{lat},{lng});"
            f"way[\"amenity\"=\"parking\"](around:500,{lat},{lng});"
            f"node[\"amenity\"=\"parking_entrance\"](around:500,{lat},{lng});"
            ");out center 30;"
        )
        op_url = 'https://overpass-api.de/api/interpreter?data=' + urllib.parse.quote(overpass_query)
        with urllib.request.urlopen(op_url, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        osm_results = []
        for el in data.get('elements', []):
            if el.get('type') == 'way':
                center = el.get('center', {})
                e_lat, e_lng = center.get('lat'), center.get('lon')
            else:
                e_lat, e_lng = el.get('lat'), el.get('lon')
            if e_lat is None or e_lng is None: continue
            tags = el.get('tags', {})
            name = tags.get('name') or tags.get('operator') or tags.get('parking', 'パーキング')
            if name == 'surface' or name == 'underground': name = 'パーキング (' + name + ')'
            osm_results.append({
                'name': name,
                'address': tags.get('addr:full') or tags.get('addr:street', ''),
                'lat': e_lat,
                'lng': e_lng,
                'rating': None,
                'open_now': None,
                'total_ratings': 0,
                'source': 'osm',
                'capacity': tags.get('capacity'),
                'fee': tags.get('fee'),
            })
        return jsonify({'results': osm_results, 'status': 'OK_OSM', 'source': 'osm', 'google_status': google_status})
    except Exception as e:
        print(f"[PARKING] OSM fail: {e}")
        return jsonify({'error': str(e), 'results': [], 'google_status': google_status}), 200


# ===== トイレ共有 =====
@app.route('/p/<token>/toilets', methods=['GET'])
def get_toilets(token):
    site = get_site_by_token(token)
    if not site:
        return jsonify({'error': 'not found'}), 404
    toilet_file = UPLOAD_BASE / token / PATHS["toilets.json"]
    if not toilet_file.exists():
        return jsonify({'toilets': []})
    return jsonify(json.loads(toilet_file.read_text(encoding='utf-8')))

@app.route('/p/<token>/toilets', methods=['POST'])
def add_toilet(token):
    site = get_site_by_token(token)
    if not site:
        return jsonify({'error': 'not found'}), 404
    toilet_file = UPLOAD_BASE / token / PATHS["toilets.json"]
    if toilet_file.exists():
        data = json.loads(toilet_file.read_text(encoding='utf-8'))
    else:
        data = {'toilets': []}
    entry = request.get_json()
    data['toilets'].append(entry)
    (UPLOAD_BASE / token).mkdir(parents=True, exist_ok=True)
    toilet_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    sites = load_sites()
    send_notify(f"🚻 トイレ登録 ({entry.get('name','')})", f"現場: {sites.get(token,{}).get('name','')}\n場所: {entry.get('name','')}\n種類: {entry.get('type','')}", sites.get(token,{}).get('name',''), token=token)
    return jsonify({'ok': True})

@app.route('/p/<token>/toilets/<toilet_id>', methods=['DELETE'])
def delete_toilet(token, toilet_id):
    site = get_site_by_token(token)
    if not site:
        return jsonify({'error': 'not found'}), 404
    toilet_file = UPLOAD_BASE / token / PATHS["toilets.json"]
    if not toilet_file.exists():
        return jsonify({'error': 'not found'}), 404
    data = json.loads(toilet_file.read_text(encoding='utf-8'))
    data['toilets'] = [t for t in data['toilets'] if t.get('id') != toilet_id]
    toilet_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return jsonify({'ok': True})


# ===== 現場調査タブ (v2 layout 2026-04-29) =====
@app.route('/p/<token>/survey/photos', methods=['GET'])
def survey_get_photos(token):
    if not get_site_by_token(token):
        return jsonify({'error': 'not found'}), 404
    pdir = UPLOAD_BASE / token / PATHS["survey_photos"]
    if not pdir.exists():
        return jsonify({'photos': []})
    photos = []
    for p in sorted(pdir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.is_file() and p.suffix.lower() in ('.jpg', '.jpeg', '.png', '.heic', '.heif', '.webp'):
            photos.append({'filename': p.name, 'url': f'/uploads/{token}/survey_photos/{p.name}', 'mtime': p.stat().st_mtime})
    return jsonify({'photos': photos})


@app.route('/p/<token>/survey/photos', methods=['POST'])
def survey_upload_photos(token):
    if not get_site_by_token(token):
        return jsonify({'error': 'not found'}), 404
    pdir = UPLOAD_BASE / token / PATHS["survey_photos"]
    pdir.mkdir(parents=True, exist_ok=True)
    saved = 0
    for f in request.files.getlist('photos'):
        if not f.filename: continue
        ext = Path(f.filename).suffix.lower() or '.jpg'
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"{ts}_{uuid.uuid4().hex[:6]}{ext}"
        f.save(str(pdir / fname))
        saved += 1
    return jsonify({'ok': True, 'saved': saved})


@app.route('/uploads/<token>/survey_photos/<filename>')
def serve_survey_photo(token, filename):
    return send_from_directory(UPLOAD_BASE / token / PATHS["survey_photos"], filename)


@app.route('/p/<token>/survey/photo/<filename>', methods=['DELETE'])
def survey_delete_photo(token, filename):
    if not get_site_by_token(token):
        return jsonify({'error': 'not found'}), 404
    fp = UPLOAD_BASE / token / PATHS["survey_photos"] / secure_filename(filename)
    if fp.exists():
        fp.unlink()
    return jsonify({'ok': True})


@app.route('/p/<token>/survey/memo', methods=['GET'])
def survey_get_memo(token):
    if not get_site_by_token(token):
        return jsonify({'error': 'not found'}), 404
    mfile = UPLOAD_BASE / token / PATHS["survey_memo"]
    if not mfile.exists():
        return jsonify({'memo': ''})
    return jsonify({'memo': mfile.read_text(encoding='utf-8')})


@app.route('/p/<token>/survey/memo', methods=['POST'])
def survey_save_memo(token):
    if not get_site_by_token(token):
        return jsonify({'error': 'not found'}), 404
    mfile = UPLOAD_BASE / token / PATHS["survey_memo"]
    mfile.parent.mkdir(parents=True, exist_ok=True)
    data = request.get_json() or {}
    mfile.write_text(data.get('memo', ''), encoding='utf-8')
    return jsonify({'ok': True})


# ===== 現場記録動画 (写真タブで混合表示, 60秒以内) =====
@app.route('/p/<token>/site_videos', methods=['GET'])
def site_videos_list(token):
    if not get_site_by_token(token):
        return jsonify({'error': 'not found'}), 404
    vdir = UPLOAD_BASE / token / PATHS["site_videos"]
    if not vdir.exists():
        return jsonify({'videos': []})
    videos = []
    for p in sorted(vdir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.is_file() and p.suffix.lower() in ('.mp4', '.mov', '.webm', '.m4v'):
            videos.append({
                'filename': p.name,
                'url': f'/uploads/{token}/site_videos/{p.name}',
                'mtime': p.stat().st_mtime,
                'size': p.stat().st_size,
            })
    return jsonify({'videos': videos})


@app.route('/p/<token>/site_videos', methods=['POST'])
def site_videos_upload(token):
    if not get_site_by_token(token):
        return jsonify({'error': 'not found'}), 404
    vdir = UPLOAD_BASE / token / PATHS["site_videos"]
    vdir.mkdir(parents=True, exist_ok=True)
    MAX_BYTES = 200 * 1024 * 1024  # 200MB上限
    saved = 0
    errs = []
    for f in request.files.getlist('videos'):
        if not f.filename: continue
        ext = Path(f.filename).suffix.lower()
        if ext not in ('.mp4', '.mov', '.webm', '.m4v'):
            errs.append(f"{f.filename}: 動画形式ではない (拡張子 {ext})")
            continue
        # サイズチェック (Content-Lengthを取得)
        f.seek(0, 2); size = f.tell(); f.seek(0)
        if size > MAX_BYTES:
            errs.append(f"{f.filename}: {size//1024//1024}MB超過 (上限200MB)")
            continue
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"{ts}_{uuid.uuid4().hex[:6]}{ext}"
        f.save(str(vdir / fname))
        saved += 1
    sites = load_sites()
    if saved:
        send_notify("動画追加", f"現場: {sites.get(token,{}).get('name','')}\n動画 {saved}本を追加しました", sites.get(token,{}).get('name',''), token=token)
    return jsonify({'ok': True, 'saved': saved, 'errors': errs})


@app.route('/uploads/<token>/site_videos/<filename>')
def serve_site_video(token, filename):
    return send_from_directory(UPLOAD_BASE / token / PATHS["site_videos"], filename)


@app.route('/p/<token>/site_video/<filename>', methods=['DELETE'])
def site_videos_delete(token, filename):
    if not get_site_by_token(token):
        return jsonify({'error': 'not found'}), 404
    fp = UPLOAD_BASE / token / PATHS["site_videos"] / secure_filename(filename)
    if fp.exists():
        fp.unlink()
    return jsonify({'ok': True})


# ===== GPS逆ジオコーディング =====
@app.route('/api/reverse_geocode', methods=['POST'])
def reverse_geocode():
    if not GOOGLE_API_KEY:
        return jsonify({'ok': False, 'error': 'GOOGLE_API_KEY未設定'}), 500
    data = request.get_json() or {}
    lat = data.get('lat'); lng = data.get('lng')
    if lat is None or lng is None:
        return jsonify({'ok': False, 'error': 'lat/lng required'}), 400
    try:
        url = f'https://maps.googleapis.com/maps/api/geocode/json?latlng={lat},{lng}&language=ja&key={GOOGLE_API_KEY}'
        with urllib.request.urlopen(url, timeout=8) as r:
            gd = json.loads(r.read().decode('utf-8'))
        results = gd.get('results') or []
        if not results:
            return jsonify({'ok': False, 'error': '住所が見つかりません'}), 200
        addr = results[0].get('formatted_address', '').replace('日本、', '').replace('〒', '').strip()
        import re as _re
        addr = _re.sub(r'^\d{3}-?\d{4}\s+', '', addr)
        return jsonify({'ok': True, 'address': addr, 'lat': lat, 'lng': lng})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ===== 喫煙所 =====

@app.route('/p/<token>/smokespots', methods=['GET'])
def get_smokespots(token):
    site = get_site_by_token(token)
    if not site:
        return jsonify({'error': 'not found'}), 404
    smoke_file = UPLOAD_BASE / token / 'smokespots.json'
    if not smoke_file.exists():
        return jsonify({'spots': []})
    return jsonify(json.loads(smoke_file.read_text(encoding='utf-8')))

@app.route('/p/<token>/smokespots', methods=['POST'])
def add_smokespot(token):
    site = get_site_by_token(token)
    if not site:
        return jsonify({'error': 'not found'}), 404
    smoke_file = UPLOAD_BASE / token / 'smokespots.json'
    if smoke_file.exists():
        data = json.loads(smoke_file.read_text(encoding='utf-8'))
    else:
        data = {'spots': []}
    entry = request.get_json()
    data['spots'].append(entry)
    (UPLOAD_BASE / token).mkdir(parents=True, exist_ok=True)
    smoke_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    sites = load_sites()
    send_notify(f"🚬 喫煙所登録 ({entry.get('name','')})", f"現場: {sites.get(token,{}).get('name','')}\n場所: {entry.get('name','')}", sites.get(token,{}).get('name',''), token=token)
    return jsonify({'ok': True})

@app.route('/p/<token>/smokespots/<spot_id>', methods=['DELETE'])
def delete_smokespot(token, spot_id):
    site = get_site_by_token(token)
    if not site:
        return jsonify({'error': 'not found'}), 404
    smoke_file = UPLOAD_BASE / token / 'smokespots.json'
    if not smoke_file.exists():
        return jsonify({'error': 'not found'}), 404
    data = json.loads(smoke_file.read_text(encoding='utf-8'))
    data['spots'] = [s for s in data['spots'] if s.get('id') != spot_id]
    smoke_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return jsonify({'ok': True})


# ===== 請求書・見積書 =====

@app.route('/p/<token>/invoices', methods=['GET'])
def list_invoices(token):
    site = get_site_by_token(token)
    if not site:
        return jsonify({'error':'not found'}), 404
    proj_path = str(UPLOAD_BASE / token)
    inv_dir = str(UPLOAD_BASE / token / PATHS["invoices"])
    meta_file = os.path.join(inv_dir, '_meta.json')
    if not os.path.exists(meta_file):
        return jsonify({'files':[]})
    with open(meta_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    contractor = request.args.get('contractor','').strip()
    is_admin = request.args.get('admin') == '1'
    if is_admin:
        return jsonify({'files': data.get('files',[])})
    elif contractor:
        filtered = [f for f in data.get('files',[]) if f.get('contractor','') == contractor]
        return jsonify({'files': filtered})
    else:
        return jsonify({'files':[]})

@app.route('/p/<token>/invoice/upload', methods=['POST'])
def upload_invoice(token):
    site = get_site_by_token(token)
    if not site:
        return jsonify({'error':'not found'}), 404
    proj_path = str(UPLOAD_BASE / token)
    inv_dir = str(UPLOAD_BASE / token / PATHS["invoices"])
    os.makedirs(inv_dir, exist_ok=True)
    meta_file = os.path.join(inv_dir, '_meta.json')
    contractor = request.form.get('contractor','').strip()
    category = request.form.get('category','その他')
    if not contractor:
        return jsonify({'error':'業者名が必要です'}), 400
    files = request.files.getlist('files')
    if not files:
        return jsonify({'error':'ファイルが必要です'}), 400
    # Load existing meta
    if os.path.exists(meta_file):
        with open(meta_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    else:
        data = {'files':[]}
    from datetime import datetime
    import uuid
    saved = []
    for file in files:
        if not file.filename:
            continue
        ext = os.path.splitext(file.filename)[1].lower()
        safe_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}{ext}"
        file.save(os.path.join(inv_dir, safe_name))
        entry = {
            'id': uuid.uuid4().hex[:8],
            'filename': safe_name,
            'original_name': file.filename,
            'contractor': contractor,
            'category': category,
            'uploaded': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'size': os.path.getsize(os.path.join(inv_dir, safe_name))
        }
        data['files'].append(entry)
        saved.append(entry)
    with open(meta_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if saved:
        sites = load_sites()
        send_notify(f"📋 請求書等アップ ({contractor})", f"現場: {sites.get(token,{}).get('name','')}\n業者: {contractor}\n種別: {category}\n{len(saved)}件のファイル\n{datetime.now().strftime('%Y/%m/%d %H:%M')}", sites.get(token,{}).get('name',''), token=token)
    return jsonify({'saved': saved})

@app.route('/p/<token>/invoice/download/<file_id>')
def download_invoice(token, file_id):
    site = get_site_by_token(token)
    if not site:
        return '', 404
    # Admin check via query param
    if request.args.get('admin') != '1':
        return '', 403
    proj_path = str(UPLOAD_BASE / token)
    inv_dir = str(UPLOAD_BASE / token / PATHS["invoices"])
    meta_file = os.path.join(inv_dir, '_meta.json')
    if not os.path.exists(meta_file):
        return '', 404
    with open(meta_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    entry = next((f for f in data.get('files',[]) if f['id'] == file_id), None)
    if not entry:
        return '', 404
    from flask import send_from_directory
    return send_from_directory(inv_dir, entry['filename'],
        as_attachment=True, download_name=entry['original_name'])

@app.route('/p/<token>/invoice/delete/<file_id>', methods=['POST'])
def delete_invoice(token, file_id):
    site = get_site_by_token(token)
    if not site:
        return jsonify({'error':'not found'}), 404
    proj_path = str(UPLOAD_BASE / token)
    inv_dir = str(UPLOAD_BASE / token / PATHS["invoices"])
    meta_file = os.path.join(inv_dir, '_meta.json')
    if not os.path.exists(meta_file):
        return jsonify({'error':'not found'}), 404
    with open(meta_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    entry = next((f for f in data.get('files',[]) if f['id'] == file_id), None)
    if not entry:
        return jsonify({'error':'not found'}), 404
    filepath = os.path.join(inv_dir, entry['filename'])
    if os.path.exists(filepath):
        os.remove(filepath)
    data['files'] = [f for f in data['files'] if f['id'] != file_id]
    with open(meta_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return jsonify({'ok': True})


# ===== 完了報告書PDF =====
@app.route('/p/<token>/report/photos')
def photo_report_pdf(token):
    """完了写真をまとめたA4報告書PDFを生成"""
    site = get_site_by_token(token)
    if not site:
        abort(404)
    sites = load_sites()
    site_info = sites.get(token, {})
    site_name = site_info.get('name', '現場')
    site_address = site_info.get('address', '')

    photos = get_complete_photos(token)
    if not photos:
        return "完工写真がありません。「完工」タブから写真をアップロードしてください。", 404

    photo_dir = UPLOAD_BASE / token / PATHS["complete_photos"]

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.pdfgen import canvas
        from reportlab.lib.colors import Color, HexColor
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.lib.utils import ImageReader
        from PIL import Image as PILImage
        import io
        import tempfile
    except ImportError:
        return "❌ reportlab/Pillow が必要です。requirements.txt に追加してください。", 500

    # 日本語フォント登録
    pdfmetrics.registerFont(UnicodeCIDFont('HeiseiKakuGo-W5'))
    pdfmetrics.registerFont(UnicodeCIDFont('HeiseiMin-W3'))

    FONT_GOTHIC = 'HeiseiKakuGo-W5'
    FONT_MINCHO = 'HeiseiMin-W3'

    # カラーパレット
    COLOR_WINE   = HexColor('#722F37')
    COLOR_DARK   = HexColor('#2a2a2e')
    COLOR_GRAY   = HexColor('#888888')
    COLOR_LIGHT  = HexColor('#f5f3ee')
    COLOR_WHITE  = HexColor('#ffffff')

    W, H = A4  # 595.27 x 841.89
    MARGIN = 18 * mm
    CONTENT_W = W - 2 * MARGIN

    # 写真グリッド設定: 2列 x 3行 = 6枚/ページ
    COLS = 2
    ROWS = 3
    PHOTO_GAP = 5 * mm
    HEADER_H = 35 * mm
    FOOTER_H = 15 * mm
    PHOTO_AREA_H = H - HEADER_H - FOOTER_H - MARGIN * 2
    PHOTO_W = (CONTENT_W - PHOTO_GAP * (COLS - 1)) / COLS
    PHOTO_H = (PHOTO_AREA_H - PHOTO_GAP * (ROWS - 1) - 8 * mm * ROWS) / ROWS

    # PDF生成
    tmp = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
    c = canvas.Canvas(tmp.name, pagesize=A4)
    c.setTitle(f'完了報告書 - {site_name}')

    total_pages = (len(photos) + COLS * ROWS - 1) // (COLS * ROWS) + 1  # +1 for cover
    page_num = 0

    def draw_header(c, page):
        # 上部バー
        c.setFillColor(COLOR_WINE)
        c.rect(0, H - 12 * mm, W, 12 * mm, fill=1, stroke=0)
        # 会社名
        c.setFillColor(COLOR_WHITE)
        c.setFont(FONT_GOTHIC, 7)
        c.drawString(MARGIN, H - 8 * mm, 'シービー合同会社')
        # 物件名
        c.setFillColor(COLOR_DARK)
        c.setFont(FONT_GOTHIC, 10)
        c.drawString(MARGIN, H - 22 * mm, site_name)
        # 住所
        if site_address:
            c.setFillColor(COLOR_GRAY)
            c.setFont(FONT_GOTHIC, 7)
            c.drawString(MARGIN, H - 28 * mm, site_address)
        # 区切り線
        c.setStrokeColor(COLOR_WINE)
        c.setLineWidth(0.5)
        c.line(MARGIN, H - HEADER_H, W - MARGIN, H - HEADER_H)

    def draw_footer(c, page, total):
        y = MARGIN - 2 * mm
        c.setStrokeColor(HexColor('#dddddd'))
        c.setLineWidth(0.3)
        c.line(MARGIN, y + FOOTER_H, W - MARGIN, y + FOOTER_H)
        c.setFillColor(COLOR_GRAY)
        c.setFont(FONT_GOTHIC, 7)
        c.drawString(MARGIN, y + 4 * mm, f'完了報告書 - {site_name}')
        c.drawRightString(W - MARGIN, y + 4 * mm, f'{page} / {total}')
        # 出力日
        c.setFont(FONT_GOTHIC, 6)
        c.drawRightString(W - MARGIN, y + 1 * mm, f'出力日: {datetime.now().strftime("%Y年%m月%d日")}')

    # ===== 表紙 =====
    page_num += 1
    # 背景グラデーション風
    c.setFillColor(COLOR_LIGHT)
    c.rect(0, 0, W, H, fill=1, stroke=0)
    # 上部アクセント
    c.setFillColor(COLOR_WINE)
    c.rect(0, H - 4 * mm, W, 4 * mm, fill=1, stroke=0)
    # 中央タイトル
    center_y = H * 0.55
    c.setFillColor(COLOR_WINE)
    c.setFont(FONT_GOTHIC, 28)
    c.drawCentredString(W / 2, center_y + 15 * mm, '完了報告書')
    # サブタイトル
    c.setFillColor(COLOR_DARK)
    c.setFont(FONT_GOTHIC, 14)
    c.drawCentredString(W / 2, center_y - 5 * mm, site_name)
    if site_address:
        c.setFillColor(COLOR_GRAY)
        c.setFont(FONT_GOTHIC, 9)
        c.drawCentredString(W / 2, center_y - 18 * mm, site_address)
    # 区切り線
    c.setStrokeColor(COLOR_WINE)
    c.setLineWidth(1)
    c.line(W / 2 - 60 * mm, center_y - 25 * mm, W / 2 + 60 * mm, center_y - 25 * mm)
    # 日付・会社名
    c.setFillColor(COLOR_DARK)
    c.setFont(FONT_GOTHIC, 10)
    c.drawCentredString(W / 2, center_y - 40 * mm, datetime.now().strftime('%Y年%m月%d日'))
    c.setFont(FONT_GOTHIC, 12)
    c.drawCentredString(W / 2, center_y - 55 * mm, 'シービー合同会社')
    # 写真枚数
    c.setFillColor(COLOR_GRAY)
    c.setFont(FONT_GOTHIC, 8)
    c.drawCentredString(W / 2, center_y - 70 * mm, f'写真枚数: {len(photos)}枚')
    # 下部アクセント
    c.setFillColor(COLOR_WINE)
    c.rect(0, 0, W, 4 * mm, fill=1, stroke=0)

    c.showPage()

    # ===== 写真ページ =====
    photos_per_page = COLS * ROWS
    for page_start in range(0, len(photos), photos_per_page):
        page_num += 1
        page_photos = photos[page_start:page_start + photos_per_page]

        draw_header(c, page_num)
        draw_footer(c, page_num, total_pages)

        for idx, photo in enumerate(page_photos):
            row = idx // COLS
            col = idx % COLS

            x = MARGIN + col * (PHOTO_W + PHOTO_GAP)
            y_top = H - HEADER_H - 2 * mm - row * (PHOTO_H + 8 * mm + PHOTO_GAP)
            y = y_top - PHOTO_H

            # 写真読み込み・描画（強力圧縮で軽量化）
            photo_path = photo_dir / photo['filename']
            try:
                img = PILImage.open(str(photo_path))
                try:
                    from PIL import ImageOps
                    img = ImageOps.exif_transpose(img)
                except Exception:
                    pass
                # PDF用リサイズ（客提出用・画面拡大対応: 600DPI / JPEG品質92）
                # PHOTO_W/H はpt(1/72inch)。pt × DPI / 72 で必要px数を算出。
                # 旧: PHOTO_W*0.7 (168px) + quality=30 → スマホ拡大時に画素荒い問題のため修正。
                PDF_PHOTO_DPI = 600
                max_px_w = int(PHOTO_W * PDF_PHOTO_DPI / 72)
                max_px_h = int(PHOTO_H * PDF_PHOTO_DPI / 72)
                img.thumbnail((max_px_w, max_px_h), PILImage.LANCZOS)
                img_buf = io.BytesIO()
                img.convert('RGB').save(
                    img_buf, format='JPEG',
                    quality=92, optimize=True, progressive=True,
                    subsampling=0,
                )
                img_buf.seek(0)
                img_reader = ImageReader(img_buf)

                img_w, img_h = img.size
                scale = min(PHOTO_W / img_w, PHOTO_H / img_h)
                draw_w = img_w * scale
                draw_h = img_h * scale
                draw_x = x + (PHOTO_W - draw_w) / 2
                draw_y = y + (PHOTO_H - draw_h) / 2

                c.setFillColor(HexColor('#f0f0f0'))
                c.roundRect(x - 1 * mm, y - 1 * mm, PHOTO_W + 2 * mm, PHOTO_H + 2 * mm, 2 * mm, fill=1, stroke=0)
                c.setStrokeColor(HexColor('#e0e0e0'))
                c.setLineWidth(0.3)
                c.roundRect(x - 1 * mm, y - 1 * mm, PHOTO_W + 2 * mm, PHOTO_H + 2 * mm, 2 * mm, fill=0, stroke=1)

                c.drawImage(img_reader, draw_x, draw_y, draw_w, draw_h, preserveAspectRatio=True)
            except Exception:
                # 読み込めない場合はプレースホルダー
                c.setFillColor(HexColor('#f5f5f5'))
                c.roundRect(x, y, PHOTO_W, PHOTO_H, 2 * mm, fill=1, stroke=0)
                c.setFillColor(COLOR_GRAY)
                c.setFont(FONT_GOTHIC, 8)
                c.drawCentredString(x + PHOTO_W / 2, y + PHOTO_H / 2, '読込不可')

            # キャプション（日時）
            c.setFillColor(COLOR_DARK)
            c.setFont(FONT_GOTHIC, 6.5)
            caption = f'{photo["date_label"]} {photo["time"]}'
            c.drawString(x, y - 6 * mm, caption)

        c.showPage()

    c.save()

    # v2: 完了報告書を 完工/報告書/ にも実ファイル保存 (Finder/Explorerで直接見られる)
    slug = make_site_slug(site_info, token)
    download_name = f'{slug}_finish_{datetime.now().strftime("%Y%m%d")}.pdf'
    try:
        report_dir = UPLOAD_BASE / token / PATHS["complete_report"]
        report_dir.mkdir(parents=True, exist_ok=True)
        saved_path = report_dir / f'{datetime.now().strftime("%Y%m%d_%H%M%S")}_完了報告書_{slug}.pdf'
        import shutil as _sh
        _sh.copy2(tmp.name, saved_path)
        print(f"[REPORT_SAVED] {saved_path}")
    except Exception as e:
        print(f"[REPORT_SAVE_ERR] {e}")

    # PDFを返す (ストリーム配信は従来どおり)
    return send_file(
        tmp.name,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=download_name
    )


# ========== AI 3D パース生成 (Gemini 2.5 Flash Image) ==========
@app.route("/p/<token>/ai3d/<category>/<filename>", methods=["POST"])
def ai_generate_3d(token, category, filename):
    """PDF図面から3Dパース風画像をAIで生成"""
    site = get_site_by_token(token)
    if not site:
        return jsonify({'error': 'サイトが見つかりません'}), 404
    if category not in DOC_CATEGORIES:
        return jsonify({'error': 'カテゴリが不正です'}), 400
    if not GOOGLE_API_KEY:
        return jsonify({'error': 'Google APIキー未設定'}), 500

    pdf_path = UPLOAD_BASE / token / PATHS["docs"] / category / filename
    if not pdf_path.exists():
        return jsonify({'error': 'PDFが見つかりません'}), 404

    style = request.json.get('style', 'natural') if request.is_json else 'natural'

    # スタイル別プロンプト
    style_prompts = {
        'natural': 'ナチュラルで温かみのある内装。明るい木目のフローリング、白壁、間接照明、観葉植物を配置',
        'modern': 'モダンでスタイリッシュな内装。ダークグレーの壁、黒のアクセント、大理石風の床、ダウンライト',
        'nordic': '北欧風の内装。白を基調とした壁、薄いオーク材の床、柔らかい自然光、シンプルな家具',
        'luxury': '高級感のある内装。ダークウォールナット材、大理石のアクセント、シャンデリア、ゴールドのディテール',
    }
    style_desc = style_prompts.get(style, style_prompts['natural'])

    try:
        import fitz  # pymupdf
        import base64
        from io import BytesIO

        # PDFを画像に変換(1ページ目)
        pdf_doc = fitz.open(str(pdf_path))
        page = pdf_doc[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
        img_bytes = pix.tobytes("png")
        pdf_doc.close()

        # Gemini 2.5 Flash Image を呼び出し
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=GOOGLE_API_KEY)

        prompt = f"""この建築平面図を元に、その間取りの内装を3D空間で描いたリアルな室内パース風イラストを生成してください。
- 視点: 部屋の一角から見た室内の見下ろし視点(アイソメトリック風または透視投影)
- スタイル: {style_desc}
- 図面の間取り・壁の配置を忠実に再現
- 窓から自然光が差し込む雰囲気
- 建築パース・インテリアパースとして成立する品質
- 写真ではなくイラスト風で、手描き感と清潔感のバランス

画像のみを生成してください。"""

        response = client.models.generate_content(
            model='gemini-2.5-flash-image',
            contents=[
                prompt,
                types.Part.from_bytes(data=img_bytes, mime_type='image/png')
            ],
        )

        # 生成画像を取り出す
        generated_png = None
        for part in response.candidates[0].content.parts:
            if part.inline_data is not None:
                generated_png = part.inline_data.data
                break

        if not generated_png:
            return jsonify({'error': 'AI画像生成失敗: 画像データが返されませんでした'}), 500

        # 保存
        ai3d_dir = UPLOAD_BASE / token / PATHS["ai3d"]
        ai3d_dir.mkdir(parents=True, exist_ok=True)
        safe_stem = Path(filename).stem
        out_name = f"{safe_stem}_{style}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        out_path = ai3d_dir / out_name
        out_path.write_bytes(generated_png)

        return jsonify({
            'ok': True,
            'url': f'/uploads/{token}/ai3d/{out_name}',
            'style': style,
        })

    except ImportError as e:
        return jsonify({'error': f'ライブラリ不足: {str(e)} (pip install google-genai)'}), 500
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'AI生成失敗: {str(e)}'}), 500


@app.route("/uploads/<token>/ai3d/<filename>")
def serve_ai3d(token, filename):
    return send_from_directory(UPLOAD_BASE / token / PATHS["ai3d"], filename)


# ============================================================
# 🖼 黒板カメラ (v1.1) + 📋 割付履歴 (v1.2) + CGアップロード修正
# ============================================================

@app.route("/p/<token>/kokuban_camera")
def kokuban_camera(token):
    """黒板カメラページ
    ?mode=complete を付けると完工写真として保存される
    """
    site = get_site_by_token(token)
    if not site:
        abort(404)
    mode = request.args.get("mode", "normal")
    if mode not in ("normal", "complete"):
        mode = "normal"
    return render_template(
        "kokuban_camera.html",
        token=token,
        site_name=site.get("name", ""),
        mode=mode,
    )


@app.route("/p/<token>/layout_history")
def layout_history(token):
    """割付履歴一覧API - 割付カテゴリのPDFをリストで返す"""
    if not get_site_by_token(token):
        abort(404)
    layout_dir = UPLOAD_BASE / token / PATHS["docs"] / "割付"
    items = []
    type_labels_known = ("均等配置", "タイル割付", "フローリング割付", "クロス割付", "汎用均等割付", "部屋割付")
    if layout_dir.exists():
        for p in sorted(layout_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if p.suffix.lower() != '.pdf':
                continue
            stem = p.stem
            title = ""
            type_label = "割付計算"
            matched = False
            for tl in type_labels_known:
                if f"_{tl}_" in stem:
                    type_label = tl
                    parts = stem.split(f"_{tl}_")
                    if len(parts) == 2 and parts[0]:
                        title = parts[0]
                    matched = True
                    break
            if not matched:
                for tl in type_labels_known:
                    if stem.startswith(tl + "_"):
                        type_label = tl
                        break
            dt = datetime.fromtimestamp(p.stat().st_mtime)
            items.append({
                "safe_name": p.name,
                "title": title,
                "type_label": type_label,
                "display_date": dt.strftime("%Y/%m/%d %H:%M"),
                "mtime": p.stat().st_mtime,
                "view_url": f"/p/{token}/view/割付/{p.name}",
                "download_url": f"/p/{token}/download/割付/{p.name}",
            })
    # 互換: 旧 layout_*.pdf が 図面 カテゴリに残っている場合も拾う
    old_dir = UPLOAD_BASE / token / PATHS["docs"] / "図面"
    if old_dir.exists():
        for p in sorted(old_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if p.suffix.lower() != '.pdf':
                continue
            if not p.stem.startswith("layout_"):
                continue
            dt = datetime.fromtimestamp(p.stat().st_mtime)
            items.append({
                "safe_name": p.name,
                "title": "(旧形式)",
                "type_label": "割付計算",
                "display_date": dt.strftime("%Y/%m/%d %H:%M"),
                "mtime": p.stat().st_mtime,
                "view_url": f"/p/{token}/doc/図面/{p.name}",
                "download_url": f"/p/{token}/download/図面/{p.name}",
                "legacy": True,
            })
    items.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    for it in items:
        it.pop("mtime", None)
    return jsonify({"items": items})


@app.route("/p/<token>/delete_layout/<path:safe_name>", methods=["POST"])
def delete_layout(token, safe_name):
    """割付PDFの削除"""
    if not get_site_by_token(token):
        abort(404)
    safe_name = Path(safe_name).name
    if not safe_name or safe_name in ('.', '..'):
        return jsonify({"ok": False, "error": "invalid name"}), 400
    for cat in ("割付", "図面"):
        fp = UPLOAD_BASE / token / PATHS["docs"] / cat / safe_name
        if fp.exists():
            try:
                fp.unlink()
                thumb_jpg = UPLOAD_BASE / token / PATHS["thumbs"] / f"{Path(safe_name).stem}.jpg"
                thumb_png = UPLOAD_BASE / token / PATHS["thumbs"] / f"{Path(safe_name).stem}.png"
                for tp in (thumb_jpg, thumb_png):
                    if tp.exists():
                        try:
                            tp.unlink()
                        except Exception:
                            pass
                return jsonify({"ok": True})
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": False, "error": "file not found"}), 404


@app.route("/admin/merge_duplicate_workers/<token>", methods=["POST"])
def merge_duplicate_workers(token):
    """v_workermerge_20260505: 工程表内の重複業者(空白/括弧違いで別entry化されたもの)を統合。
    各 norm group 内で最も短い name を代表に、同 group の他タスクの name を代表 name に書き換える。"""
    site = get_site_by_token(token)
    if not site:
        abort(404)
    is_admin = request.args.get('admin') == '1'
    if not is_admin:
        return jsonify({'error': 'admin required'}), 403
    with _get_schedule_lock(token):
        sched = _read_schedule_safe(token)
        tasks = sched.get('tasks', [])
        # group 化
        groups = {}
        for t in tasks:
            if t.get('is_attendance'): continue
            n = _norm_worker_name(t.get('name') or '')
            if not n: continue
            groups.setdefault(n, []).append(t)
        merged = 0
        renamed = []
        for norm_key, ts in groups.items():
            if len(ts) <= 1: continue
            # 代表は一番短い name (補足括弧なし)
            rep_name = sorted([x.get('name','') for x in ts], key=lambda s: len(s))[0]
            for t in ts:
                if t.get('name') != rep_name:
                    renamed.append({'old': t.get('name'), 'new': rep_name, 'id': t.get('id')})
                    t['name'] = rep_name
                    merged += 1
        if merged > 0:
            sched['tasks'] = tasks
            _atomic_write_json(UPLOAD_BASE / token / PATHS["schedule.json"], sched)
        return jsonify({'ok': True, 'merged_count': merged, 'renamed': renamed})

@app.route("/admin/migrate_layout_pdfs")
def migrate_layout_pdfs():
    """旧 docs/図面/layout_*.pdf を docs/割付/ へ移動 (一度だけ実行)"""
    moved = 0
    errors = []
    for token_dir in UPLOAD_BASE.iterdir():
        if not token_dir.is_dir():
            continue
        old_dir = token_dir / PATHS["docs"] / "図面"
        new_dir = token_dir / PATHS["docs"] / "割付"
        if not old_dir.exists():
            continue
        new_dir.mkdir(parents=True, exist_ok=True)
        for p in list(old_dir.iterdir()):
            if p.suffix.lower() == '.pdf' and p.stem.startswith("layout_"):
                try:
                    dest = new_dir / p.name
                    if not dest.exists():
                        p.rename(dest)
                        moved += 1
                except Exception as e:
                    errors.append(f"{p}: {e}")
    return jsonify({"moved": moved, "errors": errors})


@app.route("/p/<token>/save_photo_measure", methods=["POST"])
def save_photo_measure(token):
    """写真採寸ツールで作成した画像を保存"""
    if not get_site_by_token(token):
        abort(404)
    try:
        f = request.files.get('image')
        if not f:
            return jsonify({"ok": False, "error": "no image"}), 400
        title = (request.form.get('title') or '写真採寸').strip()
        import re
        def safe_fs(s):
            s = re.sub(r'[\\/:*?"<>|\n\r\t]', '_', s or '').strip()
            return s[:60]
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        safe_name = f"{safe_fs(title)}_{ts}.jpg"
        dest_dir = UPLOAD_BASE / token / PATHS["docs"] / "採寸"
        dest_dir.mkdir(parents=True, exist_ok=True)
        i = 2
        while (dest_dir / safe_name).exists():
            stem = safe_name.rsplit('.', 1)[0]
            safe_name = f"{stem}_{i}.jpg"
            i += 1
        fp = dest_dir / safe_name
        f.save(str(fp))
        return jsonify({
            "ok": True,
            "filename": safe_name,
            "view_url": f"/p/{token}/doc/採寸/{safe_name}",
            "download_url": f"/p/{token}/download/採寸/{safe_name}"
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


# ==================================================================
# 管理者レポート (v1.6) - 出退勤集計 + 請求突合
# ==================================================================
# データ源:
#   - 各現場の uploads/<token>/schedule.json の tasks[] のうち
#     is_attendance: true のもの
#   - worker フィールドは "業者名/個人名" のスラッシュ区切り想定
#     (旧形式の素の人名もフォールバックで業者="未設定"として扱う)
# 請求突合:
#   - billing.json に { "業者名": { "YYYY-MM": 請求日数 } } を保存

BILLING_FILE = Path("billing.json")

def load_billing():
    if BILLING_FILE.exists():
        try:
            return json.loads(BILLING_FILE.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}

def save_billing(data):
    BILLING_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

def split_worker(raw):
    """worker 文字列を (業者名, 個人名) に分解。スラッシュ区切り優先、
    無ければ全体を個人名・業者は「未設定」とする。全角/半角スラッシュ両対応。"""
    if not raw:
        return ("未設定", "")
    s = str(raw).strip()
    # 全角スラッシュも半角に統一
    for sep in ('/', '／', '／'):
        if sep in s:
            parts = s.split(sep, 1)
            company = parts[0].strip() or "未設定"
            person = parts[1].strip()
            return (company, person)
    return ("未設定", s)

def aggregate_attendance(date_from=None, date_to=None, token_filter=None):
    """全現場の schedule.json を走査して出退勤を集計する。

    戻り値(dict):
      period: {from, to}
      by_person: [{key, company, person, days, sites:[...], late_count, early_count, dates:[...]}]
      by_company: [{company, days, people_count, sites_count}]
      by_date:   [{date, workers:[{company, person, sites:[...]}], total}]
      by_site:   [{token, name, total_days, by_company:[{company, days}]}]
      raw_count: 集計対象の"現場入り"イベント数
    """
    sites = load_sites()

    # person_key = (company, person, site_token, date) で重複排除
    # -> 同じ人が同じ日に in を複数回押しても 1日 としてカウント
    person_days = defaultdict(set)          # (company, person) -> {(token, date)}
    company_days = defaultdict(set)          # company -> {(person, token, date)}
    company_people = defaultdict(set)        # company -> {person}
    company_sites = defaultdict(set)         # company -> {token}
    date_workers = defaultdict(list)         # date -> [{company, person, token, time}]
    site_days = defaultdict(lambda: defaultdict(set))  # token -> company -> {(person, date)}
    site_totals = defaultdict(set)           # token -> {(company, person, date)}

    # 遅刻・早退判定の閾値
    LATE_AFTER = "09:00"   # これより後に in なら遅刻
    EARLY_BEFORE = "16:00" # これより前に out なら早退
    person_late = defaultdict(int)           # (company, person) -> 遅刻回数
    person_early = defaultdict(int)          # (company, person) -> 早退回数
    person_dates = defaultdict(set)          # (company, person) -> {date}

    raw_count = 0

    for token, info in sites.items():
        if token_filter and token != token_filter:
            continue
        sched_file = UPLOAD_BASE / token / PATHS["schedule.json"]
        if not sched_file.exists():
            continue
        try:
            data = json.loads(sched_file.read_text(encoding='utf-8'))
        except Exception:
            continue
        for task in (data.get("tasks") or []):
            if not task.get("is_attendance"):
                continue
            action = task.get("action")
            worker_raw = task.get("worker") or ""
            ts = task.get("timestamp") or ""
            day = (task.get("start") or "")[:10]
            if not day and ts:
                day = ts[:10]
            if not day:
                continue
            # 期間フィルタ
            if date_from and day < date_from:
                continue
            if date_to and day > date_to:
                continue
            company, person = split_worker(worker_raw)
            key = (company, person)
            # 時刻
            hhmm = ""
            if ts and "T" in ts:
                hhmm = ts.split("T", 1)[1][:5]

            if action == 'in':
                raw_count += 1
                person_days[key].add((token, day))
                company_days[company].add((person, token, day))
                company_people[company].add(person)
                company_sites[company].add(token)
                date_workers[day].append({
                    "company": company, "person": person, "token": token,
                    "site_name": info.get("name", ""), "time": hhmm, "action": "in"
                })
                site_days[token][company].add((person, day))
                site_totals[token].add((company, person, day))
                person_dates[key].add(day)
                if hhmm and hhmm > LATE_AFTER:
                    person_late[key] += 1
            elif action == 'out':
                date_workers[day].append({
                    "company": company, "person": person, "token": token,
                    "site_name": info.get("name", ""), "time": hhmm, "action": "out"
                })
                if hhmm and hhmm < EARLY_BEFORE:
                    person_early[key] += 1

    # ---- by_person ----
    by_person = []
    for (company, person), day_set in person_days.items():
        sites_worked = sorted({t for (t, d) in day_set})
        by_person.append({
            "company": company,
            "person": person,
            "days": len(day_set),
            "sites": [{"token": t, "name": sites.get(t, {}).get("name", "")} for t in sites_worked],
            "late_count": person_late.get((company, person), 0),
            "early_count": person_early.get((company, person), 0),
            "dates": sorted(person_dates.get((company, person), set())),
        })
    by_person.sort(key=lambda x: (-x["days"], x["company"], x["person"]))

    # ---- by_company ----
    by_company = []
    for company, triple_set in company_days.items():
        unique_days = len({(t, d) for (_p, t, d) in triple_set})  # 業者の延べ出勤日(現場×日)
        person_day_total = len(triple_set)                          # 人×現場×日 の延べ
        by_company.append({
            "company": company,
            "days": person_day_total,      # 人日
            "unique_days": unique_days,    # 業者として現場に入った日数(請求突合用)
            "people_count": len(company_people.get(company, set())),
            "sites_count": len(company_sites.get(company, set())),
            "people": sorted(company_people.get(company, set())),
        })
    by_company.sort(key=lambda x: (-x["days"], x["company"]))

    # ---- by_date ----
    by_date = []
    for day in sorted(date_workers.keys(), reverse=True):
        entries = date_workers[day]
        # 同じ人・現場のin/outは1件にまとめる
        seen = {}
        for e in entries:
            k = (e["company"], e["person"], e["token"])
            if k not in seen:
                seen[k] = {"company": e["company"], "person": e["person"],
                           "token": e["token"], "site_name": e["site_name"],
                           "in_time": "", "out_time": ""}
            if e["action"] == "in":
                seen[k]["in_time"] = e["time"]
            else:
                seen[k]["out_time"] = e["time"]
        workers = list(seen.values())
        workers.sort(key=lambda x: (x["company"], x["person"]))
        by_date.append({"date": day, "workers": workers, "total": len(workers)})

    # ---- by_site ----
    by_site = []
    for token, triple_set in site_totals.items():
        company_counts = defaultdict(int)
        for (company, person, day) in triple_set:
            company_counts[company] += 1
        by_site.append({
            "token": token,
            "name": sites.get(token, {}).get("name", ""),
            "total_days": len(triple_set),
            "by_company": sorted(
                [{"company": c, "days": n} for c, n in company_counts.items()],
                key=lambda x: -x["days"]
            ),
        })
    by_site.sort(key=lambda x: -x["total_days"])

    return {
        "period": {"from": date_from or "", "to": date_to or ""},
        "by_person": by_person,
        "by_company": by_company,
        "by_date": by_date,
        "by_site": by_site,
        "raw_count": raw_count,
        "generated_at": datetime.now().isoformat(),
    }

def _parse_period_args():
    """リクエストの from/to/month を (from, to) 文字列に整える"""
    month = (request.args.get('month') or '').strip()
    if month and re.match(r'^\d{4}-\d{2}$', month):
        y, m = map(int, month.split('-'))
        first = date(y, m, 1)
        if m == 12:
            last = date(y + 1, 1, 1) - timedelta(days=1)
        else:
            last = date(y, m + 1, 1) - timedelta(days=1)
        return (first.isoformat(), last.isoformat())
    df = (request.args.get('from') or '').strip() or None
    dt = (request.args.get('to') or '').strip() or None
    return (df, dt)

@app.route("/admin/report")
def admin_report():
    """管理者レポート画面 (請求突合含む)"""
    # 初期表示は当月
    today = date.today()
    default_month = today.strftime("%Y-%m")
    return render_template("admin_report.html", default_month=default_month)

@app.route("/admin/report/api/summary")
def admin_report_summary():
    df, dt = _parse_period_args()
    token_filter = (request.args.get('token') or '').strip() or None
    data = aggregate_attendance(df, dt, token_filter)
    # 請求データも一緒に返す (該当月があれば)
    billing = load_billing()
    month = (request.args.get('month') or '').strip()
    data["billing"] = {"month": month, "claims": billing.get(month, {}) if month else {}}
    # 現場一覧 (フィルタUI用)
    sites = load_sites()
    data["sites"] = [
        {"token": t, "name": s.get("name", "")}
        for t, s in sorted(sites.items(), key=lambda kv: kv[1].get("created", ""), reverse=True)
    ]
    return jsonify(data)

@app.route("/admin/report/api/billing", methods=["POST"])
def admin_report_save_billing():
    """請求日数の保存。{month: 'YYYY-MM', claims: {業者名: 日数}}"""
    payload = request.get_json(force=True, silent=True) or {}
    month = (payload.get('month') or '').strip()
    claims = payload.get('claims') or {}
    if not re.match(r'^\d{4}-\d{2}$', month):
        return jsonify({"ok": False, "error": "month は YYYY-MM 形式"}), 400
    if not isinstance(claims, dict):
        return jsonify({"ok": False, "error": "claims は辞書"}), 400
    # 値を整数化
    normalized = {}
    for k, v in claims.items():
        k = str(k).strip()
        if not k:
            continue
        try:
            n = int(float(v))
            if n < 0:
                n = 0
        except Exception:
            continue
        normalized[k] = n
    billing = load_billing()
    billing[month] = normalized
    save_billing(billing)
    return jsonify({"ok": True, "month": month, "claims": normalized})

@app.route("/admin/report/csv")
def admin_report_csv():
    """集計CSVダウンロード。?type=person|company|date|site"""
    df, dt = _parse_period_args()
    data = aggregate_attendance(df, dt)
    kind = (request.args.get('type') or 'person').strip()

    buf = io.StringIO()
    buf.write('\ufeff')  # Excel向けBOM
    w = csv.writer(buf)

    if kind == 'person':
        w.writerow(['業者', '個人名', '出勤日数(延べ)', '遅刻回数', '早退回数', '入った現場数'])
        for p in data['by_person']:
            w.writerow([p['company'], p['person'], p['days'], p['late_count'], p['early_count'], len(p['sites'])])
    elif kind == 'company':
        w.writerow(['業者', '人日(延べ)', '出勤日数(業者単位)', '人数', '現場数'])
        for c in data['by_company']:
            w.writerow([c['company'], c['days'], c['unique_days'], c['people_count'], c['sites_count']])
    elif kind == 'date':
        w.writerow(['日付', '出勤者数', '内訳(業者/個人)'])
        for d in data['by_date']:
            names = ' / '.join(f"{x['company']}:{x['person']}" for x in d['workers'])
            w.writerow([d['date'], d['total'], names])
    elif kind == 'site':
        w.writerow(['現場', '延べ人日', '業者別内訳'])
        for s in data['by_site']:
            breakdown = ' / '.join(f"{x['company']}:{x['days']}" for x in s['by_company'])
            w.writerow([s['name'], s['total_days'], breakdown])
    else:
        return jsonify({"error": "type は person/company/date/site"}), 400

    csv_bytes = buf.getvalue().encode('utf-8')
    filename = f"genba_report_{kind}_{df or 'all'}_{dt or 'all'}.csv"
    from flask import Response
    return Response(
        csv_bytes,
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


# ===== v1.10/v1.11 仕入・経費管理 Blueprint 登録 =====
# 管理者画面 /admin/purchases/?admin=1 で利用 (Gemini Vision API)
try:
    from purchase_manager import purchase_bp
    app.register_blueprint(purchase_bp)
    print("[purchase_manager] Blueprint registered")
except Exception as _e:
    import traceback
    print("[purchase_manager] Blueprint登録に失敗(機能無効化):", _e)
    traceback.print_exc()


# ====================================================================
# 音声メモ・数量計算 (voice_memo_pack v1, 2026-05-02 統合)
# 管理者専用 ( ?admin=1 必須 )。 token単位でJSON保存。
# ====================================================================
import uuid as _vmem_uuid
from pathlib import Path as _VmemPath

VOICE_MEMOS_DIR = _VmemPath("/data/voice_memos")
VOICE_MEMOS_DIR.mkdir(parents=True, exist_ok=True)


def _voice_memos_file(token):
    d = VOICE_MEMOS_DIR / str(token)
    d.mkdir(parents=True, exist_ok=True)
    return d / "memos.json"


def _load_voice_memos(token):
    fp = _voice_memos_file(token)
    if not fp.exists():
        return []
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[voice_memos] load error token={token}: {e}", flush=True)
        return []


def _save_voice_memos(token, memos):
    fp = _voice_memos_file(token)
    fp.write_text(
        json.dumps(memos, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def _vmem_clean_items(raw_items):
    clean = []
    for it in raw_items or []:
        try:
            size = float(it.get('size', 0))
            count = int(it.get('count', 0))
            if size > 0 and count > 0:
                clean.append({'size': size, 'count': count})
        except (TypeError, ValueError):
            continue
    return clean


@app.route('/p/<token>/voice-memos', methods=['GET'])
def api_voice_memos_list(token):
    return jsonify({'memos': _load_voice_memos(token)})


@app.route('/p/<token>/voice-memos', methods=['POST'])
def api_voice_memos_create(token):
    data = request.get_json(silent=True) or {}
    items = _vmem_clean_items(data.get('items'))
    if not items:
        return jsonify({'error': 'no valid items'}), 400
    memo = {
        'id': str(_vmem_uuid.uuid4()),
        'token': token,
        'type': str(data.get('type', 'other'))[:32],
        'type_label': str(data.get('type_label', ''))[:32],
        'items': items,
        'total': float(data.get('total', 0)),
        'unit': str(data.get('unit', ''))[:8],
        'memo': str(data.get('memo', ''))[:2000],
        'created_at': datetime.now().isoformat(),
        'created_by': 'admin',
    }
    memos = _load_voice_memos(token)
    memos.insert(0, memo)
    _save_voice_memos(token, memos)
    return jsonify({'ok': True, 'memo': memo})


@app.route('/p/<token>/voice-memos/<memo_id>', methods=['DELETE'])
def api_voice_memos_delete(token, memo_id):
    memos = _load_voice_memos(token)
    new_memos = [m for m in memos if m.get('id') != memo_id]
    if len(new_memos) == len(memos):
        return jsonify({'error': 'not found'}), 404
    _save_voice_memos(token, new_memos)
    return jsonify({'ok': True})


@app.route('/p/<token>/voice-memos/pdf', methods=['GET'])
def api_voice_memos_pdf(token):
    """音声メモを A4 PDF で出力"""
    site_info = get_site_by_token(token) or {}
    site_name = site_info.get('name', token)
    memos = _load_voice_memos(token)

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as rlc
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    try:
        pdfmetrics.registerFont(UnicodeCIDFont('HeiseiKakuGo-W5'))
        font_jp = 'HeiseiKakuGo-W5'
    except Exception:
        font_jp = 'Helvetica'

    buf = io.BytesIO()
    c = rlc.Canvas(buf, pagesize=A4)
    page_w, page_h = A4
    margin = 15 * mm
    line_h = 6.5 * mm

    def draw_header(page_no):
        c.setFont(font_jp, 16)
        c.drawString(margin, page_h - margin - 4 * mm, f"🎙️ 音声メモ集計 — {site_name}")
        c.setFont(font_jp, 9)
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
        c.drawString(margin, page_h - margin - 11 * mm, f"出力: {now_str}   登録件数: {len(memos)}件   ページ {page_no}")
        c.line(margin, page_h - margin - 13 * mm, page_w - margin, page_h - margin - 13 * mm)
        return page_h - margin - 18 * mm

    page_no = 1
    y = draw_header(page_no)

    if not memos:
        c.setFont(font_jp, 12)
        c.drawString(margin, y - 10 * mm, "保存された音声メモはまだありません。")
    else:
        # 種別ごとにグループ化
        by_type = {}
        for m in memos:
            key = m.get('type_label') or m.get('type') or 'その他'
            by_type.setdefault(key, []).append(m)

        for type_label, group in by_type.items():
            type_total = sum(float(m.get('total') or 0) for m in group)
            unit = group[0].get('unit', '') if group else ''
            if y < margin + 30 * mm:
                c.showPage(); page_no += 1; y = draw_header(page_no)
            c.setFont(font_jp, 13)
            c.setFillColorRGB(0.05, 0.4, 0.2)
            c.drawString(margin, y, f"■ {type_label}")
            c.setFont(font_jp, 11)
            c.drawRightString(page_w - margin, y, f"小計: {type_total:,.2f} {unit}".rstrip('0').rstrip('.') + f" {unit}" if False else f"小計: {type_total:,.2f} {unit}")
            c.setFillColorRGB(0, 0, 0)
            y -= line_h

            for m in group:
                if y < margin + 25 * mm:
                    c.showPage(); page_no += 1; y = draw_header(page_no)
                items = m.get('items') or []
                items_str = ', '.join(f"{it.get('size')}×{it.get('count')}" for it in items)
                created = (m.get('created_at') or '')[:16].replace('T', ' ')
                memo_text = m.get('memo') or ''
                total = float(m.get('total') or 0)

                c.setFont(font_jp, 10)
                # 1行目: 内訳 + 合計
                line1 = f"  {items_str}"
                if len(line1) > 70:
                    line1 = line1[:67] + '...'
                c.drawString(margin, y, line1)
                c.drawRightString(page_w - margin, y, f"= {total:,.2f} {unit}")
                y -= line_h - 2 * mm
                # 2行目: 作成日 + メモ
                c.setFont(font_jp, 8)
                c.setFillColorRGB(0.4, 0.4, 0.4)
                meta = f"  {created}"
                if memo_text:
                    meta += f"  /  {memo_text[:60]}"
                c.drawString(margin, y, meta)
                c.setFillColorRGB(0, 0, 0)
                y -= line_h
            y -= 2 * mm

        # 全体合計
        if y < margin + 20 * mm:
            c.showPage(); page_no += 1; y = draw_header(page_no)
        grand_total_by_unit = {}
        for m in memos:
            u = m.get('unit', '')
            grand_total_by_unit[u] = grand_total_by_unit.get(u, 0) + float(m.get('total') or 0)
        c.line(margin, y + 2 * mm, page_w - margin, y + 2 * mm)
        c.setFont(font_jp, 12)
        c.drawString(margin, y - 3 * mm, "■ 単位別 総合計")
        y -= line_h + 2 * mm
        c.setFont(font_jp, 11)
        for u, t in grand_total_by_unit.items():
            c.drawString(margin + 5 * mm, y, f"{u or '(単位なし)'}: {t:,.2f} {u}")
            y -= line_h

    c.save()
    buf.seek(0)
    safe_name = re.sub(r'[\\/:*?"<>|\s]+', '_', site_name)
    filename = f"voice_memos_{safe_name}.pdf"
    return send_file(
        buf,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=filename,
    )


@app.route('/p/<token>/voice-memos/<memo_id>', methods=['PATCH'])
def api_voice_memos_update(token, memo_id):
    data = request.get_json(silent=True) or {}
    memos = _load_voice_memos(token)
    target = next((m for m in memos if m.get('id') == memo_id), None)
    if not target:
        return jsonify({'error': 'not found'}), 404
    if 'memo' in data:
        target['memo'] = str(data['memo'])[:2000]
    if 'items' in data:
        target['items'] = _vmem_clean_items(data['items'])
    if 'total' in data:
        target['total'] = float(data['total'])
    target['updated_at'] = datetime.now().isoformat()
    _save_voice_memos(token, memos)
    return jsonify({'ok': True, 'memo': target})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
