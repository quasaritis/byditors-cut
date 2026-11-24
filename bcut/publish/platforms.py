"""Posting a finished clip through each platform's official API. No browser automation, no password sharing:
every platform here is used the way its docs describe, so accounts don't get flagged.

Each `post_*` takes (account, clip, caption) and returns (remote_id, public_url).
"""
from __future__ import annotations

import time
from pathlib import Path

import httpx

from .. import config, db

V = config.GRAPH_VERSION
T = httpx.Timeout(60, read=600, write=600)


def _check(r: httpx.Response) -> dict:
    try:
        j = r.json()
    except Exception:  # noqa: BLE001
        j = {}
    if r.status_code >= 400 or (isinstance(j, dict) and j.get("error") and j["error"] not in ({}, {"code": "ok"})):
        err = j.get("error") if isinstance(j, dict) else None
        msg = (err.get("message") if isinstance(err, dict) else None) or r.text[:300]
        raise RuntimeError(f"{r.request.url.host}: {msg}")
    return j


def _save_secrets(account: dict, **changes):
    db.update("accounts", account["id"], secrets={**account["secrets"], **changes})


# ---------------------------------------------------------------- Instagram (Reels)

def ig_host(account: dict) -> str:
    return account["secrets"].get("host") or "graph.instagram.com"


def ig_connect(token: str, host: str = "", username: str = "") -> dict:
    """Two kinds of token work:
    - an Instagram-login token (starts with "IG"), used on graph.instagram.com;
    - a Facebook token (user or Page, starts with "EAA") for a Page that has the Instagram account linked, used
      on graph.facebook.com. A user token is swapped for that Page's own token, which doesn't expire when the
      user token was a long-lived one."""
    host = host or ("graph.instagram.com" if token.startswith("IG") else "graph.facebook.com")
    if host == "graph.instagram.com":
        me = _check(httpx.get(f"https://{host}/{V}/me", params={"fields": "user_id,username", "access_token": token}))
        uid = me.get("user_id") or me.get("id")
        return {"name": "@" + me.get("username", uid), "secrets": {"token": token, "user_id": uid, "host": host,
                                                                   "refreshed": time.time()}}
    fields = "id,name,access_token,instagram_business_account{id,username}"
    pages = []
    try:
        pages = _check(httpx.get(f"https://{host}/{V}/me/accounts", params={"fields": fields, "access_token": token,
                                                                          "limit": 100})).get("data", [])
    except RuntimeError:
        pass                                  # a Page token has no /me/accounts: it is the page itself
    if not pages:
        me = _check(httpx.get(f"https://{host}/{V}/me", params={"fields": "id,name,instagram_business_account"
                                                                          "{id,username}", "access_token": token}))
        pages = [{**me, "access_token": token}]
    linked = [p for p in pages if p.get("instagram_business_account")]
    if username:
        want = username.lstrip("@").lower()
        linked = [p for p in linked if p["instagram_business_account"].get("username", "").lower() == want] or linked
    if not linked:
        raise RuntimeError("None of your Facebook Pages has an Instagram account linked. In the Instagram app: "
                           "Edit profile → Page → connect your Facebook Page, then try again.")
    pg = linked[0]
    ig = pg["instagram_business_account"]
    return {"name": "@" + ig.get("username", ig["id"]),
            "secrets": {"token": pg.get("access_token") or token, "user_id": ig["id"], "host": host,
                        "page_id": pg.get("id"), "refreshed": time.time()}}


def ig_refresh(account: dict) -> None:
    """Long-lived Instagram tokens last 60 days; renew them every ~50."""
    s = account["secrets"]
    if ig_host(account) != "graph.instagram.com" or time.time() - s.get("refreshed", 0) < 50 * 86400:
        return
    j = _check(httpx.get("https://graph.instagram.com/refresh_access_token",
                         params={"grant_type": "ig_refresh_token", "access_token": s["token"]}))
    _save_secrets(account, token=j["access_token"], refreshed=time.time())
    account["secrets"]["token"] = j["access_token"]


def _cover_ms(clip: dict) -> int:
    """Where the cover is in the video, in milliseconds (0 is a real answer: the thumbnail is the first frame)."""
    at = (clip.get("guard") or {}).get("cover_at")
    return int(1000 * float(1.0 if at is None else at))


def post_instagram(account: dict, clip: dict, caption: str) -> tuple[str, str]:
    ig_refresh(account)
    s = account["secrets"]
    host = ig_host(account)
    path = Path(clip["file"])
    box = _check(httpx.post(f"https://{host}/{V}/{s['user_id']}/media", timeout=T, data={
        "media_type": "REELS", "upload_type": "resumable", "caption": caption, "share_to_feed": "true",
        # the cover: the frame the editor picked (the biggest reaction), in milliseconds
        "thumb_offset": str(_cover_ms(clip)),
        "access_token": s["token"]}))
    cid = box["id"]
    _check(httpx.post(box.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{V}/{cid}", timeout=T,
                      headers={"Authorization": f"OAuth {s['token']}", "offset": "0",
                               "file_size": str(path.stat().st_size)}, content=path.read_bytes()))
    for _ in range(90):  # Instagram processes the video before it can be published
        st = _check(httpx.get(f"https://{host}/{V}/{cid}", params={"fields": "status_code,status",
                                                                   "access_token": s["token"]}))
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram couldn't process the video: {st.get('status')}")
        time.sleep(10)
    pub = _check(httpx.post(f"https://{host}/{V}/{s['user_id']}/media_publish", timeout=T,
                            data={"creation_id": cid, "access_token": s["token"]}))
    link = _check(httpx.get(f"https://{host}/{V}/{pub['id']}", params={"fields": "permalink",
                                                                       "access_token": s["token"]}))
    return pub["id"], link.get("permalink", "")


# ---------------------------------------------------------------- Facebook Page (Reels)

def fb_connect(page_id: str, page_token: str) -> dict:
    me = _check(httpx.get(f"https://graph.facebook.com/{V}/{page_id}", params={"fields": "name",
                                                                               "access_token": page_token}))
    return {"name": me.get("name", page_id), "secrets": {"page_id": page_id, "token": page_token}}


def post_facebook(account: dict, clip: dict, caption: str) -> tuple[str, str]:
    s = account["secrets"]
    base = f"https://graph.facebook.com/{V}/{s['page_id']}/video_reels"
    path = Path(clip["file"])
    start = _check(httpx.post(base, timeout=T, data={"upload_phase": "start", "access_token": s["token"]}))
    vid = start["video_id"]
    _check(httpx.post(start.get("upload_url") or f"https://rupload.facebook.com/video-upload/{V}/{vid}", timeout=T,
                      headers={"Authorization": f"OAuth {s['token']}", "offset": "0",
                               "file_size": str(path.stat().st_size)}, content=path.read_bytes()))
    _check(httpx.post(base, timeout=T, data={"upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED",
                                             "description": caption, "access_token": s["token"]}))
    return vid, f"https://www.facebook.com/reel/{vid}"


# ---------------------------------------------------------------- YouTube (Shorts)

YT_SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"


def google_client() -> tuple[str, str]:
    """The Google OAuth client: pasted in the app (Accounts → YouTube), or GOOGLE_CLIENT_ID/SECRET in .env."""
    g = db.kv_get("google_client", {}) or {}
    if g.get("id") and g.get("secret"):
        return g["id"], g["secret"]
    return config.GOOGLE_CLIENT_ID, config.GOOGLE_CLIENT_SECRET


def yt_redirect() -> str:
    return f"{config.PUBLIC_URL}/oauth/youtube"


def yt_auth_url(state: str) -> str:
    q = httpx.QueryParams({"client_id": google_client()[0], "redirect_uri": yt_redirect(),
                           "response_type": "code", "scope": YT_SCOPES, "access_type": "offline",
                           # select_account: every Connect shows Google's account/channel picker, so more channels can be added
                           "prompt": "select_account consent", "state": state})
    return f"https://accounts.google.com/o/oauth2/v2/auth?{q}"


def yt_connect(code: str) -> dict:
    tok = _check(httpx.post("https://oauth2.googleapis.com/token", data={
        "code": code, "client_id": google_client()[0], "client_secret": google_client()[1],
        "redirect_uri": yt_redirect(), "grant_type": "authorization_code"}))
    ch = _check(httpx.get("https://www.googleapis.com/youtube/v3/channels", params={"part": "snippet", "mine": "true"},
                          headers={"authorization": f"Bearer {tok['access_token']}"}))
    item = (ch.get("items") or [{}])[0]
    return {"name": item.get("snippet", {}).get("title", "YouTube channel"),
            "secrets": {"refresh_token": tok.get("refresh_token"), "channel_id": item.get("id", "")}}


def _yt_token(account: dict) -> str:
    tok = _check(httpx.post("https://oauth2.googleapis.com/token", data={
        "client_id": google_client()[0], "client_secret": google_client()[1],
        "refresh_token": account["secrets"]["refresh_token"], "grant_type": "refresh_token"}))
    return tok["access_token"]


def post_youtube(account: dict, clip: dict, caption: dict) -> tuple[str, str]:
    token = _yt_token(account)
    path = Path(clip["file"])
    privacy = account.get("settings", {}).get("privacy", "public")
    meta = {"snippet": {"title": caption.get("title", "")[:100], "description": caption.get("description", "")[:4900],
                        "tags": caption.get("tags", [])[:15], "categoryId": "20"},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    r = httpx.post("https://www.googleapis.com/upload/youtube/v3/videos", timeout=T,
                   params={"uploadType": "resumable", "part": "snippet,status"},
                   headers={"authorization": f"Bearer {token}", "X-Upload-Content-Type": "video/mp4",
                            "X-Upload-Content-Length": str(path.stat().st_size)}, json=meta)
    _check(r)
    up = _check(httpx.put(r.headers["location"], timeout=T, content=path.read_bytes(),
                          headers={"authorization": f"Bearer {token}", "content-type": "video/mp4"}))
    if clip.get("cover_file") and Path(clip["cover_file"]).exists():
        yt_thumbnail(token, up["id"], Path(clip["cover_file"]))
    return up["id"], f"https://youtube.com/shorts/{up['id']}"


def yt_thumbnail(token: str, video_id: str, image: Path) -> bool:
    """Set the video's thumbnail. YouTube allows it only on channels verified by phone; when it says no, the video
    keeps the frame YouTube picks, and the post still counts."""
    try:
        data = image.read_bytes()
        if len(data) > 2 * 2**20:
            return False
        r = httpx.post("https://www.googleapis.com/upload/youtube/v3/thumbnails/set", timeout=T,
                       params={"videoId": video_id, "uploadType": "media"}, content=data,
                       headers={"authorization": f"Bearer {token}", "content-type": "image/jpeg"})
        if r.status_code >= 400:
            from .. import db
            db.log(f"YouTube kept its own thumbnail: {r.text[:160]} (custom thumbnails need a phone-verified "
                   "channel: youtube.com/verify)", "warn")
            return False
        return True
    except (OSError, httpx.HTTPError):
        return False


# ---------------------------------------------------------------- TikTok (Direct Post)

TT = "https://open.tiktokapis.com/v2"


def tt_auth_url(state: str) -> str:
    q = httpx.QueryParams({"client_key": config.TIKTOK_CLIENT_KEY, "scope": "user.info.basic,video.publish",
                           "response_type": "code", "redirect_uri": config.TIKTOK_REDIRECT_URI, "state": state})
    return f"https://www.tiktok.com/v2/auth/authorize/?{q}"


def tt_connect(code: str) -> dict:
    tok = _check(httpx.post(f"{TT}/oauth/token/", data={
        "client_key": config.TIKTOK_CLIENT_KEY, "client_secret": config.TIKTOK_CLIENT_SECRET, "code": code,
        "grant_type": "authorization_code", "redirect_uri": config.TIKTOK_REDIRECT_URI}))
    info = _check(httpx.get(f"{TT}/user/info/", params={"fields": "open_id,display_name"},
                            headers={"authorization": f"Bearer {tok['access_token']}"}))
    user = (info.get("data") or {}).get("user") or {}
    return {"name": user.get("display_name", "TikTok"),
            "secrets": {"access_token": tok["access_token"], "refresh_token": tok["refresh_token"],
                        "expires": time.time() + float(tok.get("expires_in", 86400)), "open_id": tok.get("open_id")}}


def _tt_token(account: dict) -> str:
    s = account["secrets"]
    if s.get("expires", 0) > time.time() + 300:
        return s["access_token"]
    tok = _check(httpx.post(f"{TT}/oauth/token/", data={
        "client_key": config.TIKTOK_CLIENT_KEY, "client_secret": config.TIKTOK_CLIENT_SECRET,
        "grant_type": "refresh_token", "refresh_token": s["refresh_token"]}))
    _save_secrets(account, access_token=tok["access_token"], refresh_token=tok.get("refresh_token", s["refresh_token"]),
                  expires=time.time() + float(tok.get("expires_in", 86400)))
    return tok["access_token"]


def post_tiktok(account: dict, clip: dict, caption: str) -> tuple[str, str]:
    token = _tt_token(account)
    h = {"authorization": f"Bearer {token}", "content-type": "application/json; charset=UTF-8"}
    creator = _check(httpx.post(f"{TT}/post/publish/creator_info/query/", headers=h, timeout=T)).get("data", {})
    allowed = creator.get("privacy_level_options") or ["SELF_ONLY"]
    want = account.get("settings", {}).get("privacy", "PUBLIC_TO_EVERYONE")
    privacy = want if want in allowed else allowed[0]
    path = Path(clip["file"])
    size = path.stat().st_size
    chunk = size if size <= 64 * 2**20 else 10 * 2**20
    count = max(1, size // chunk)
    init = _check(httpx.post(f"{TT}/post/publish/video/init/", headers=h, timeout=T, json={
        "post_info": {"title": caption[:2200], "privacy_level": privacy, "disable_duet": False,
                      "disable_comment": False, "disable_stitch": False, "video_cover_timestamp_ms": _cover_ms(clip)},
        "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk,
                        "total_chunk_count": count}}))["data"]
    data = path.read_bytes()
    for i in range(count):
        a = i * chunk
        b = size if i == count - 1 else a + chunk
        r = httpx.put(init["upload_url"], timeout=T, content=data[a:b], headers={
            "content-type": "video/mp4", "content-range": f"bytes {a}-{b - 1}/{size}"})
        if r.status_code >= 400:
            raise RuntimeError(f"TikTok upload refused chunk {i + 1}: {r.text[:200]}")
    pid = init["publish_id"]
    for _ in range(60):
        st = _check(httpx.post(f"{TT}/post/publish/status/fetch/", headers=h, json={"publish_id": pid}))["data"]
        if st.get("status") == "PUBLISH_COMPLETE":
            ids = st.get("publicaly_available_post_id") or st.get("publicly_available_post_id") or []
            return pid, (f"https://www.tiktok.com/@/video/{ids[0]}" if ids else "")
        if st.get("status") == "FAILED":
            raise RuntimeError(f"TikTok: {st.get('fail_reason')}")
        time.sleep(8)
    return pid, ""


POSTERS = {"instagram": post_instagram, "facebook": post_facebook, "youtube": post_youtube, "tiktok": post_tiktok}
