# -*- coding: utf-8 -*-
"""
Mellojoy TikTok → Discord 通知
  @mellojoyjapan の新しい投稿（毎日10〜12時ごろのラインナップ）を見張って
  Discord の Webhook に通知する常駐スクリプト。

使い方:
  python mellojoy_tiktok_notify.py          … 常駐して見張る
  python mellojoy_tiktok_notify.py --test   … Discordにテスト通知を送るだけ
  python mellojoy_tiktok_notify.py --check  … 今の最新投稿を1回だけ表示（通知しない）

必要なもの:
  pip install -U yt-dlp
  （予備ルート用・任意）pip install playwright   ※PCのChromeを使うのでブラウザDL不要# -*- coding: utf-8 -*-
"""
Mellojoy TikTok → Discord 通知
  @mellojoyjapan の新しい投稿（毎日10〜12時ごろのラインナップ）を見張って
  Discord の Webhook に通知する常駐スクリプト。

使い方:
  python mellojoy_tiktok_notify.py          … 常駐して見張る
  python mellojoy_tiktok_notify.py --test   … Discordにテスト通知を送るだけ
  python mellojoy_tiktok_notify.py --check  … 今の最新投稿を1回だけ表示（通知しない）

必要なもの:
  pip install -U yt-dlp
  （予備ルート用・任意）pip install playwright   ※PCのChromeを使うのでブラウザDL不要
"""

import json
import os
import re
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone

# ============ 設定 ============
DISCORD_WEBHOOK_URL = "ここにDiscordのWebhook URLを貼る"
MENTION = "@everyone"            # 通知でメンションしない場合は "" にする
TIKTOK_ACCOUNT = "mellojoyjapan"

WATCH_START = (9, 45)            # この時刻〜WATCH_END は短い間隔でチェック
WATCH_END = (12, 30)
INTERVAL_IN_WINDOW = 90          # 秒（時間帯内）
INTERVAL_OUTSIDE = 30 * 60
LOOP_INTERVAL = 60               # GitHub上の朝の見張りでのチェック間隔（秒）       # 秒（時間帯外。それ以外の投稿も一応拾う）
STOP_AFTER_FOUND = False         # True: その日1件通知したら翌日までは時間帯外の間隔に落とす
# ==============================

JST = timezone(timedelta(hours=9))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "mellojoy_tiktok_state.json")
LOG_FILE = os.path.join(BASE_DIR, "mellojoy_tiktok_notify.log")
PROFILE_URL = f"https://www.tiktok.com/@{TIKTOK_ACCOUNT}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")


def log(msg):
    line = f"[{datetime.now(JST):%m/%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"seen": [], "last_found_date": ""}


def save_state(state):
    state["seen"] = state["seen"][-300:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def created_at(video_id):
    """TikTokの投稿IDの上位ビットは投稿時刻（UNIX秒）"""
    try:
        return datetime.fromtimestamp(int(video_id) >> 32, JST)
    except Exception:
        return None


def post_url(vid):
    return f"https://www.tiktok.com/@{TIKTOK_ACCOUNT}/video/{vid}"


# ---------- 取得ルート1: yt-dlp ----------
def fetch_ytdlp():
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
            "playlistend": 12, "skip_download": True,
            "http_headers": {"User-Agent": UA}}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(PROFILE_URL, download=False)
    posts = []
    for e in (info or {}).get("entries") or []:
        vid = str(e.get("id") or "")
        if not vid.isdigit():
            m = re.search(r"/(?:video|photo)/(\d+)", e.get("url") or "")
            vid = m.group(1) if m else ""
        if vid:
            posts.append({"id": vid, "desc": e.get("description") or e.get("title") or "",
                          "cover": e.get("thumbnail") or ""})
    return posts


def detail_ytdlp(vid):
    """新着1件の説明文・サムネを取りに行く（失敗してもOK）"""
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                               "http_headers": {"User-Agent": UA}}) as ydl:
            i = ydl.extract_info(post_url(vid), download=False)
        return {"desc": i.get("description") or i.get("title") or "",
                "cover": i.get("thumbnail") or ""}
    except Exception:
        return {}


# ---------- 取得ルート2: Playwright（PCのChrome） ----------
def fetch_playwright():
    from playwright.sync_api import sync_playwright
    items, posts = [], []

    def on_response(r):
        if "/api/post/item_list" in r.url:
            try:
                items.extend(r.json().get("itemList") or [])
            except Exception:
                pass

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            os.path.join(BASE_DIR, "tiktok_profile"), channel="chrome",
            headless=True, user_agent=UA, locale="ja-JP")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("response", on_response)
        page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)
        if not items:  # APIが拾えなければ画面のリンクから
            hrefs = page.eval_on_selector_all(
                "a[href*='/video/'], a[href*='/photo/']", "els => els.map(e => e.href)")
            for h in hrefs:
                m = re.search(r"/(?:video|photo)/(\d+)", h)
                if m and m.group(1) not in [x["id"] for x in posts]:
                    posts.append({"id": m.group(1), "desc": "", "cover": ""})
        ctx.close()

    for it in items:
        cover = (it.get("video") or {}).get("cover") or ""
        if not cover and it.get("imagePost"):
            try:
                cover = it["imagePost"]["cover"]["imageURL"]["urlList"][0]
            except Exception:
                pass
        posts.append({"id": str(it.get("id")), "desc": it.get("desc") or "", "cover": cover})
    return posts


def fetch_posts():
    errors = []
    for name, fn in (("yt-dlp", fetch_ytdlp), ("playwright", fetch_playwright)):
        try:
            posts = fn()
            if posts:
                return posts
            errors.append(f"{name}: 0件")
        except ImportError:
            errors.append(f"{name}: 未インストール")
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:150]}")
    raise RuntimeError(" / ".join(errors))


# ---------- Discord ----------
def send_discord(content, embed=None):
    body = {"content": content, "allowed_mentions": {"parse": ["everyone"]}}
    if embed:
        body["embeds"] = [embed]
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "MellojoyNotify/1.0"})
    urllib.request.urlopen(req, timeout=20).read()


def notify(post):
    vid = post["id"]
    if not post.get("desc") or not post.get("cover"):
        d = detail_ytdlp(vid)
        post["desc"] = post.get("desc") or d.get("desc", "")
        post["cover"] = post.get("cover") or d.get("cover", "")
    t = created_at(vid)
    tstr = f"{t:%H:%M}" if t else "?"
    embed = {
        "title": "🧸 メロジョイ 今日のラインナップが投稿されました",
        "url": post_url(vid),
        "description": (post.get("desc") or "")[:1500],
        "color": 0xFF7EB6,
        "footer": {"text": f"TikTok @{TIKTOK_ACCOUNT}・{tstr} 投稿"},
    }
    if t:
        embed["timestamp"] = t.isoformat()
    if post.get("cover"):
        embed["image"] = {"url": post["cover"]}
    send_discord(f"{MENTION} メロジョイのTikTokに新しい投稿！\n{post_url(vid)}".strip(), embed)
    log(f"通知しました: {post_url(vid)}")


# ---------- 本体 ----------
def in_window(now):
    s = now.replace(hour=WATCH_START[0], minute=WATCH_START[1], second=0, microsecond=0)
    e = now.replace(hour=WATCH_END[0], minute=WATCH_END[1], second=0, microsecond=0)
    return s <= now <= e


def check_once(state, send=True):
    posts = fetch_posts()
    today = datetime.now(JST).date()
    new = []
    for p in posts:
        t = created_at(p["id"])
        # 固定投稿・昨日以前の投稿は無視。今日の投稿で未通知のものだけ
        if p["id"] not in state["seen"] and t and t.date() == today:
            new.append(p)
    new.sort(key=lambda p: int(p["id"]))
    for p in new:
        if send:
            notify(p)
        state["seen"].append(p["id"])
        state["last_found_date"] = today.isoformat()
    if send:
        save_state(state)
        if new:
            commit_state()
    return posts, new


def commit_state():
    """GitHub Actions上では通知済み記録をすぐリポジトリに保存（二重通知防止）"""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    import subprocess
    cmds = [["git", "config", "user.name", "mellojoy-bot"],
            ["git", "config", "user.email", "bot@users.noreply.github.com"],
            ["git", "add", STATE_FILE],
            ["git", "commit", "-m", "update state"],
            ["git", "push"]]
    for c in cmds:
        subprocess.run(c, cwd=BASE_DIR, capture_output=True)


def main():
    global DISCORD_WEBHOOK_URL
    url_file = os.path.join(BASE_DIR, "webhook_url.txt")
    if os.environ.get("DISCORD_WEBHOOK_URL"):
        DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"].strip()
    elif "ここに" in DISCORD_WEBHOOK_URL and os.path.exists(url_file):
        with open(url_file, encoding="utf-8") as f:
            DISCORD_WEBHOOK_URL = f.read().strip()
    if "--check" not in sys.argv and not DISCORD_WEBHOOK_URL.startswith("https://"):
        if not sys.stdin.isatty():
            raise SystemExit("DISCORD_WEBHOOK_URL が設定されていません（GitHubのSecretsを確認）")
        print("DiscordのWebhook URLをここに貼り付けて(右クリックで貼り付け) Enter:")
        u = input("> ").strip().strip('"')
        if not u.startswith("https://discord"):
            print("Webhook URLではないようです(https://discord.com/api/webhooks/... の形)")
            input("Enterで終了")
            return
        with open(url_file, "w", encoding="utf-8") as f:
            f.write(u)
        DISCORD_WEBHOOK_URL = u
        print("保存しました(webhook_url.txt)。次回からは聞きません。")

    if "--test" in sys.argv:
        send_discord("✅ メロジョイTikTok通知のテストです")
        print("テスト通知を送りました。Discordを確認してください。")
        return

    state = load_state()
    if "--test-latest" in sys.argv:  # テスト：最新投稿を日付関係なく1件通知（記録はしない）
        posts = fetch_posts()
        latest = max(posts, key=lambda p: int(p["id"]))
        log(f"取得 {len(posts)}件 / テストで最新投稿を通知します")
        notify(latest)
        return
    if "--auto" in sys.argv:  # GitHub Actions用：朝は12:30まで居続けて1分ごと、それ以外は1回
        end = datetime.now(JST).replace(hour=WATCH_END[0], minute=WATCH_END[1], second=0)
        start = datetime.now(JST).replace(hour=WATCH_START[0], minute=WATCH_START[1], second=0)
        if datetime.now(JST) >= end:
            sys.argv.append("--once")
        else:
            log(f"朝の見張りモード：{WATCH_END[0]}:{WATCH_END[1]:02d}まで {LOOP_INTERVAL}秒ごとにチェック")
            while datetime.now(JST) < end:
                now = datetime.now(JST)
                if now < start:
                    time.sleep(min(60, (start - now).total_seconds() + 1))
                    continue
                try:
                    posts, new = check_once(state)
                    print(f"  {now:%H:%M:%S} 取得{len(posts)}件 新着{len(new)}件", flush=True)
                except Exception as e:
                    log(f"取得失敗: {e}")
                time.sleep(LOOP_INTERVAL)
            log("12:30になったので終了")
            return
    if "--once" in sys.argv:  # 1回チェックして終了
        posts, new = check_once(state)
        log(f"取得 {len(posts)}件 / 新着通知 {len(new)}件")
        for p in sorted(posts, key=lambda p: int(p["id"]), reverse=True)[:5]:
            t = created_at(p["id"])
            log(f"  最新: {t:%m/%d %H:%M} {p['id']} {p['desc'][:30]!r}")
        return
    if "--check" in sys.argv:
        posts, new = check_once(state, send=False)
        for p in posts[:8]:
            t = created_at(p["id"])
            print(f"{t:%m/%d %H:%M}  {post_url(p['id'])}  {p['desc'][:40]}")
        print(f"→ 今日の未通知: {len(new)}件")
        return

    log(f"見張り開始: {PROFILE_URL}（{WATCH_START[0]}:{WATCH_START[1]:02d}〜"
        f"{WATCH_END[0]}:{WATCH_END[1]:02d} は {INTERVAL_IN_WINDOW}秒ごと）")
    fails = 0
    while True:
        now = datetime.now(JST)
        try:
            _, new = check_once(state)
            if fails >= 3:
                log("取得が復旧しました")
            fails = 0
            if not new:
                print(f"  {now:%H:%M:%S} 新着なし", flush=True)
        except Exception as e:
            fails += 1
            log(f"取得失敗({fails}回目): {e}")
            if fails == 10:
                try:
                    send_discord("⚠️ メロジョイTikTok通知: 投稿の取得に10回連続で失敗しています。"
                                 "`pip install -U yt-dlp` を試してください。")
                except Exception:
                    pass
        found_today = state.get("last_found_date") == now.date().isoformat()
        if in_window(now) and not (STOP_AFTER_FOUND and found_today):
            wait = INTERVAL_IN_WINDOW
        else:
            wait = INTERVAL_OUTSIDE
            # 時間帯開始をまたぐ場合は開始時刻に合わせて起きる
            start = now.replace(hour=WATCH_START[0], minute=WATCH_START[1], second=0, microsecond=0)
            if now < start:
                wait = min(wait, max(5, (start - now).total_seconds()))
        time.sleep(wait)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        log(traceback.format_exc())
        raise

"""

import json
import os
import re
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone

# ============ 設定 ============
DISCORD_WEBHOOK_URL = "ここにDiscordのWebhook URLを貼る"
MENTION = "@everyone"            # 通知でメンションしない場合は "" にする
TIKTOK_ACCOUNT = "mellojoyjapan"

WATCH_START = (9, 45)            # この時刻〜WATCH_END は短い間隔でチェック
WATCH_END = (12, 30)
INTERVAL_IN_WINDOW = 90          # 秒（時間帯内）
INTERVAL_OUTSIDE = 30 * 60       # 秒（時間帯外。それ以外の投稿も一応拾う）
STOP_AFTER_FOUND = False         # True: その日1件通知したら翌日までは時間帯外の間隔に落とす
# ==============================

JST = timezone(timedelta(hours=9))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "mellojoy_tiktok_state.json")
LOG_FILE = os.path.join(BASE_DIR, "mellojoy_tiktok_notify.log")
PROFILE_URL = f"https://www.tiktok.com/@{TIKTOK_ACCOUNT}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")


def log(msg):
    line = f"[{datetime.now(JST):%m/%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"seen": [], "last_found_date": ""}


def save_state(state):
    state["seen"] = state["seen"][-300:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def created_at(video_id):
    """TikTokの投稿IDの上位ビットは投稿時刻（UNIX秒）"""
    try:
        return datetime.fromtimestamp(int(video_id) >> 32, JST)
    except Exception:
        return None


def post_url(vid):
    return f"https://www.tiktok.com/@{TIKTOK_ACCOUNT}/video/{vid}"


# ---------- 取得ルート1: yt-dlp ----------
def fetch_ytdlp():
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
            "playlistend": 12, "skip_download": True,
            "http_headers": {"User-Agent": UA}}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(PROFILE_URL, download=False)
    posts = []
    for e in (info or {}).get("entries") or []:
        vid = str(e.get("id") or "")
        if not vid.isdigit():
            m = re.search(r"/(?:video|photo)/(\d+)", e.get("url") or "")
            vid = m.group(1) if m else ""
        if vid:
            posts.append({"id": vid, "desc": e.get("description") or e.get("title") or "",
                          "cover": e.get("thumbnail") or ""})
    return posts


def detail_ytdlp(vid):
    """新着1件の説明文・サムネを取りに行く（失敗してもOK）"""
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                               "http_headers": {"User-Agent": UA}}) as ydl:
            i = ydl.extract_info(post_url(vid), download=False)
        return {"desc": i.get("description") or i.get("title") or "",
                "cover": i.get("thumbnail") or ""}
    except Exception:
        return {}


# ---------- 取得ルート2: Playwright（PCのChrome） ----------
def fetch_playwright():
    from playwright.sync_api import sync_playwright
    items, posts = [], []

    def on_response(r):
        if "/api/post/item_list" in r.url:
            try:
                items.extend(r.json().get("itemList") or [])
            except Exception:
                pass

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            os.path.join(BASE_DIR, "tiktok_profile"), channel="chrome",
            headless=True, user_agent=UA, locale="ja-JP")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("response", on_response)
        page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)
        if not items:  # APIが拾えなければ画面のリンクから
            hrefs = page.eval_on_selector_all(
                "a[href*='/video/'], a[href*='/photo/']", "els => els.map(e => e.href)")
            for h in hrefs:
                m = re.search(r"/(?:video|photo)/(\d+)", h)
                if m and m.group(1) not in [x["id"] for x in posts]:
                    posts.append({"id": m.group(1), "desc": "", "cover": ""})
        ctx.close()

    for it in items:
        cover = (it.get("video") or {}).get("cover") or ""
        if not cover and it.get("imagePost"):
            try:
                cover = it["imagePost"]["cover"]["imageURL"]["urlList"][0]
            except Exception:
                pass
        posts.append({"id": str(it.get("id")), "desc": it.get("desc") or "", "cover": cover})
    return posts


def fetch_posts():
    errors = []
    for name, fn in (("yt-dlp", fetch_ytdlp), ("playwright", fetch_playwright)):
        try:
            posts = fn()
            if posts:
                return posts
            errors.append(f"{name}: 0件")
        except ImportError:
            errors.append(f"{name}: 未インストール")
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:150]}")
    raise RuntimeError(" / ".join(errors))


# ---------- Discord ----------
def send_discord(content, embed=None):
    body = {"content": content, "allowed_mentions": {"parse": ["everyone"]}}
    if embed:
        body["embeds"] = [embed]
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "MellojoyNotify/1.0"})
    urllib.request.urlopen(req, timeout=20).read()


def notify(post):
    vid = post["id"]
    if not post.get("desc") or not post.get("cover"):
        d = detail_ytdlp(vid)
        post["desc"] = post.get("desc") or d.get("desc", "")
        post["cover"] = post.get("cover") or d.get("cover", "")
    t = created_at(vid)
    tstr = f"{t:%H:%M}" if t else "?"
    embed = {
        "title": "🧸 メロジョイ 今日のラインナップが投稿されました",
        "url": post_url(vid),
        "description": (post.get("desc") or "")[:1500],
        "color": 0xFF7EB6,
        "footer": {"text": f"TikTok @{TIKTOK_ACCOUNT}・{tstr} 投稿"},
    }
    if t:
        embed["timestamp"] = t.isoformat()
    if post.get("cover"):
        embed["image"] = {"url": post["cover"]}
    send_discord(f"{MENTION} メロジョイのTikTokに新しい投稿！\n{post_url(vid)}".strip(), embed)
    log(f"通知しました: {post_url(vid)}")


# ---------- 本体 ----------
def in_window(now):
    s = now.replace(hour=WATCH_START[0], minute=WATCH_START[1], second=0, microsecond=0)
    e = now.replace(hour=WATCH_END[0], minute=WATCH_END[1], second=0, microsecond=0)
    return s <= now <= e


def check_once(state, send=True):
    posts = fetch_posts()
    today = datetime.now(JST).date()
    new = []
    for p in posts:
        t = created_at(p["id"])
        # 固定投稿・昨日以前の投稿は無視。今日の投稿で未通知のものだけ
        if p["id"] not in state["seen"] and t and t.date() == today:
            new.append(p)
    new.sort(key=lambda p: int(p["id"]))
    for p in new:
        if send:
            notify(p)
        state["seen"].append(p["id"])
        state["last_found_date"] = today.isoformat()
    if send:
        save_state(state)
    return posts, new


def main():
    global DISCORD_WEBHOOK_URL
    url_file = os.path.join(BASE_DIR, "webhook_url.txt")
    if os.environ.get("DISCORD_WEBHOOK_URL"):
        DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"].strip()
    elif "ここに" in DISCORD_WEBHOOK_URL and os.path.exists(url_file):
        with open(url_file, encoding="utf-8") as f:
            DISCORD_WEBHOOK_URL = f.read().strip()
    if "--check" not in sys.argv and not DISCORD_WEBHOOK_URL.startswith("https://"):
        if not sys.stdin.isatty():
            raise SystemExit("DISCORD_WEBHOOK_URL が設定されていません（GitHubのSecretsを確認）")
        print("DiscordのWebhook URLをここに貼り付けて(右クリックで貼り付け) Enter:")
        u = input("> ").strip().strip('"')
        if not u.startswith("https://discord"):
            print("Webhook URLではないようです(https://discord.com/api/webhooks/... の形)")
            input("Enterで終了")
            return
        with open(url_file, "w", encoding="utf-8") as f:
            f.write(u)
        DISCORD_WEBHOOK_URL = u
        print("保存しました(webhook_url.txt)。次回からは聞きません。")

    if "--test" in sys.argv:
        send_discord("✅ メロジョイTikTok通知のテストです")
        print("テスト通知を送りました。Discordを確認してください。")
        return

    state = load_state()
    if "--test-latest" in sys.argv:  # テスト：最新投稿を日付関係なく1件通知（記録はしない）
        posts = fetch_posts()
        latest = max(posts, key=lambda p: int(p["id"]))
        log(f"取得 {len(posts)}件 / テストで最新投稿を通知します")
        notify(latest)
        return
    if "--once" in sys.argv:  # GitHub Actions用：1回チェックして終了
        posts, new = check_once(state)
        log(f"取得 {len(posts)}件 / 新着通知 {len(new)}件")
        return
    if "--check" in sys.argv:
        posts, new = check_once(state, send=False)
        for p in posts[:8]:
            t = created_at(p["id"])
            print(f"{t:%m/%d %H:%M}  {post_url(p['id'])}  {p['desc'][:40]}")
        print(f"→ 今日の未通知: {len(new)}件")
        return

    log(f"見張り開始: {PROFILE_URL}（{WATCH_START[0]}:{WATCH_START[1]:02d}〜"
        f"{WATCH_END[0]}:{WATCH_END[1]:02d} は {INTERVAL_IN_WINDOW}秒ごと）")
    fails = 0
    while True:
        now = datetime.now(JST)
        try:
            _, new = check_once(state)
            if fails >= 3:
                log("取得が復旧しました")
            fails = 0
            if not new:
                print(f"  {now:%H:%M:%S} 新着なし", flush=True)
        except Exception as e:
            fails += 1
            log(f"取得失敗({fails}回目): {e}")
            if fails == 10:
                try:
                    send_discord("⚠️ メロジョイTikTok通知: 投稿の取得に10回連続で失敗しています。"
                                 "`pip install -U yt-dlp` を試してください。")
                except Exception:
                    pass
        found_today = state.get("last_found_date") == now.date().isoformat()
        if in_window(now) and not (STOP_AFTER_FOUND and found_today):
            wait = INTERVAL_IN_WINDOW
        else:
            wait = INTERVAL_OUTSIDE
            # 時間帯開始をまたぐ場合は開始時刻に合わせて起きる
            start = now.replace(hour=WATCH_START[0], minute=WATCH_START[1], second=0, microsecond=0)
            if now < start:
                wait = min(wait, max(5, (start - now).total_seconds()))
        time.sleep(wait)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        log(traceback.format_exc())
        raise
