"""[팀 직접 구현] YouTube 업로드 — OAuth 2.0(채널 주인 로그인) + 이어 올리기(resumable upload).

[오픈소스] requests (HTTP). Google 클라이언트 라이브러리 없이 공개 REST API만 쓴다.

준비: Google Cloud 콘솔에서 YouTube Data API v3를 켜고 OAuth 클라이언트(웹 애플리케이션)를 만든 뒤,
      승인된 리디렉션 URI에 http://127.0.0.1:5000/youtube/callback 을 넣고 JSON을 받아 YOUTUBE_CLIENT_SECRETS에 경로를 적는다.
흐름: 앱 화면 'YouTube 계정 연결' → Google 로그인·동의 → 콜백에서 토큰 저장(instance/, git 제외)
      → ③ 확인 단계에서 제작자가 'YouTube에 올리기'를 눌렀을 때만 업로드한다 (자동 업로드 없음).
주의: 감사(audit)를 받지 않은 Google Cloud 프로젝트로 올린 영상은 YouTube가 비공개로 고정한다.
"""
import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
CAPTIONS_URL = "https://www.googleapis.com/upload/youtube/v3/captions"
CAPTION_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"  # 자막 올리기(captions.insert)에 필요
SCOPES = ("https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly",  # readonly: 연결된 채널 이름 표시용
          CAPTION_SCOPE)
PRIVACY = {"private": "비공개", "unlisted": "일부 공개", "public": "공개"}
_CHUNK_UNIT = 256 * 1024  # 이어 올리기 조각은 256KB의 배수여야 한다
_RETRY_STATUS = {500, 502, 503, 504}


class YouTubeError(Exception):
    """사용자에게 그대로 보여 줄 수 있는 업로드 오류."""


# ── 설정·토큰 파일 ────────────────────────────────────────────────────────
def _http(config):
    """HTTP 클라이언트 — 검증 때는 config['_YOUTUBE_HTTP']로 가짜 Google 서버를 넣는다."""
    return config.get("_YOUTUBE_HTTP") or requests


def _client(config):
    path = config.get("YOUTUBE_CLIENT_SECRETS") or ""
    if not path or not os.path.exists(path):
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    info = data.get("web") or data.get("installed") or {}
    if not info.get("client_id") or not info.get("client_secret"):
        return None
    return info


def token_path(config, instance_path):
    return config.get("YOUTUBE_TOKEN_FILE") or os.path.join(instance_path, "youtube_token.json")


def _load_token(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_token(path, token):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(token, ensure_ascii=False), encoding="utf-8")


def status(config, instance_path):
    """화면 표시용: {"configured", "connected", "channel", "captions"}. captions: 자막 올리기 권한을 받은 연결인지
    (자막 기능 전에 연결한 계정은 권한이 없어 다시 연결해야 한다)."""
    token = _load_token(token_path(config, instance_path))
    return {"configured": _client(config) is not None,
            "connected": bool(token and token.get("refresh_token")),
            "channel": (token or {}).get("channel_title", ""),
            "captions": CAPTION_SCOPE in ((token or {}).get("scope") or "").split()}


# ── OAuth (PKCE) ─────────────────────────────────────────────────────────
def authorization(config, redirect_uri):
    """(Google 로그인 URL, state, code_verifier). state·verifier는 세션에 두었다가 콜백에서 확인한다."""
    client = _client(config)
    if client is None:
        raise YouTubeError("YOUTUBE_CLIENT_SECRETS에 OAuth 클라이언트 JSON 경로를 설정하세요.")
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    query = {
        "client_id": client["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
        "include_granted_scopes": "true", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urlencode(query)}", state, verifier


def finish_authorization(config, instance_path, code, redirect_uri, verifier, http=None):
    """콜백의 code → 토큰 저장. 연결된 채널 이름을 돌려준다."""
    http = http or _http(config)
    client = _client(config)
    if client is None:
        raise YouTubeError("OAuth 클라이언트 설정을 찾을 수 없습니다.")
    resp = http.post(TOKEN_URL, data={
        "code": code, "client_id": client["client_id"], "client_secret": client["client_secret"],
        "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier,
    }, timeout=30)
    if resp.status_code != 200:
        raise YouTubeError(f"Google 로그인 토큰을 받지 못했습니다 ({resp.status_code}): {_error_text(resp)}")
    data = resp.json()
    if not data.get("refresh_token"):
        raise YouTubeError("Google이 갱신 토큰을 주지 않았습니다. Google 계정의 '서드파티 앱 액세스'에서 이 앱을 지우고 다시 연결하세요.")
    token = {"refresh_token": data["refresh_token"], "access_token": data.get("access_token", ""),
             "expires_at": time.time() + float(data.get("expires_in", 0)) - 60,
             "scope": data.get("scope") or " ".join(SCOPES)}  # Google이 실제로 허락한 권한
    token["channel_title"] = _channel_title(token["access_token"], http)
    _save_token(token_path(config, instance_path), token)
    return token["channel_title"]


def disconnect(config, instance_path, http=None):
    """토큰을 Google에서 철회하고 파일을 지운다 (철회가 실패해도 파일은 지운다)."""
    http = http or _http(config)
    path = token_path(config, instance_path)
    token = _load_token(path)
    if token and token.get("refresh_token"):
        try:
            http.post(REVOKE_URL, data={"token": token["refresh_token"]}, timeout=15)
        except requests.RequestException:
            pass
    if os.path.exists(path):
        os.remove(path)


def _channel_title(access_token, http):
    try:
        resp = http.get(CHANNELS_URL, params={"part": "snippet", "mine": "true"},
                        headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
        if resp.status_code != 200:
            return ""
        items = resp.json().get("items") or []
        return items[0]["snippet"]["title"] if items else ""
    except (requests.RequestException, ValueError, KeyError):
        return ""


def _access_token(config, instance_path, http, force=False):
    path = token_path(config, instance_path)
    token = _load_token(path)
    if not token or not token.get("refresh_token"):
        raise YouTubeError("YouTube 계정이 연결되지 않았습니다. 'YouTube 계정 연결'을 먼저 하세요.")
    if not force and token.get("access_token") and token.get("expires_at", 0) > time.time():
        return token["access_token"]
    client = _client(config)
    if client is None:
        raise YouTubeError("OAuth 클라이언트 설정을 찾을 수 없습니다 (YOUTUBE_CLIENT_SECRETS).")
    resp = http.post(TOKEN_URL, data={
        "client_id": client["client_id"], "client_secret": client["client_secret"],
        "refresh_token": token["refresh_token"], "grant_type": "refresh_token",
    }, timeout=30)
    if resp.status_code != 200:
        raise YouTubeError("YouTube 연결이 만료되었습니다. 계정을 다시 연결하세요. "
                           f"({resp.status_code}: {_error_text(resp)})")
    data = resp.json()
    token["access_token"] = data["access_token"]
    token["expires_at"] = time.time() + float(data.get("expires_in", 3600)) - 60
    _save_token(path, token)
    return token["access_token"]


def _error_text(resp):
    try:
        data = resp.json()
    except ValueError:
        return (resp.text or "")[:200]
    err = data.get("error")
    if isinstance(err, dict):
        reasons = [e.get("reason", "") for e in err.get("errors") or []]
        return f"{err.get('message', '')} {' '.join(r for r in reasons if r)}".strip()
    return str(data.get("error_description") or err or data)[:200]


def _explain(resp):
    """자주 나는 오류는 무엇을 하면 되는지 알려 준다."""
    text = _error_text(resp)
    if "quotaExceeded" in text or "dailyLimitExceeded" in text:
        return "오늘 YouTube API 할당량을 다 썼습니다 (업로드 1회 = 1,600단위, 기본 하루 10,000). 내일 다시 하세요."
    if "uploadLimitExceeded" in text:
        return "이 채널의 업로드 한도에 걸렸습니다. 잠시 뒤 다시 하세요."
    if resp.status_code == 403:
        return f"업로드 권한이 없습니다. 계정을 다시 연결하거나 YouTube Data API 사용 설정을 확인하세요. ({text})"
    return f"YouTube 응답 {resp.status_code}: {text}"


# ── 업로드 ───────────────────────────────────────────────────────────────
def upload(config, instance_path, video_path, title, description, privacy="private",
           http=None, progress=None, sleep=time.sleep):
    """올릴 영상 파일 → YouTube 영상 ID. progress(fraction) 콜백으로 진행률을 알린다."""
    http = http or _http(config)
    if privacy not in PRIVACY:
        raise YouTubeError(f"알 수 없는 공개 범위: {privacy}")
    if not video_path or not os.path.exists(video_path):
        raise YouTubeError("올릴 영상 파일이 없습니다. 먼저 올릴 영상을 만드세요.")
    total = os.path.getsize(video_path)
    meta = {
        "snippet": {"title": (title or "제목 없음")[:100], "description": (description or "")[:5000],
                    "categoryId": str(config.get("YOUTUBE_CATEGORY_ID", "22")), "defaultLanguage": "ko"},
        # AI로 만든 영상임을 밝힌다 (YouTube의 '변경되거나 합성된 콘텐츠' 표시)
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True},
    }

    def init(access):
        return http.post(UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                         headers={"Authorization": f"Bearer {access}",
                                  "Content-Type": "application/json; charset=UTF-8",
                                  "X-Upload-Content-Type": "video/mp4",
                                  "X-Upload-Content-Length": str(total)},
                         data=json.dumps(meta, ensure_ascii=False).encode("utf-8"), timeout=30)

    access = _access_token(config, instance_path, http)
    resp = init(access)
    if resp.status_code == 401:  # 토큰이 막 만료됨 → 한 번 갱신
        access = _access_token(config, instance_path, http, force=True)
        resp = init(access)
    if resp.status_code != 200 or not resp.headers.get("Location"):
        raise YouTubeError(_explain(resp))
    session_url = resp.headers["Location"]

    chunk = max(1, int(config.get("YOUTUBE_UPLOAD_CHUNK_MB", 8))) * 4 * _CHUNK_UNIT
    offset, failures = 0, 0
    with open(video_path, "rb") as f:
        while True:
            if offset >= total:  # 다 보냈는데 완료 응답을 못 받았으면 상태를 물어본다
                headers = {"Authorization": f"Bearer {access}", "Content-Range": f"bytes */{total}"}
                body = b""
            else:
                f.seek(offset)
                body = f.read(chunk)
                end = offset + len(body) - 1
                headers = {"Authorization": f"Bearer {access}", "Content-Type": "video/mp4",
                           "Content-Range": f"bytes {offset}-{end}/{total}"}
            try:
                resp = http.put(session_url, headers=headers, data=body, timeout=120)
            except requests.RequestException as exc:
                resp, error = None, exc
            else:
                error = None

            if resp is not None and resp.status_code in (200, 201):
                if progress:
                    progress(1.0)
                video_id = resp.json().get("id")
                if not video_id:
                    raise YouTubeError("업로드는 끝났지만 영상 ID를 받지 못했습니다.")
                return video_id
            if resp is not None and resp.status_code == 308:  # 이어 올리기: 받은 곳까지 표시된다
                got = resp.headers.get("Range", "")
                offset = int(got.rsplit("-", 1)[1]) + 1 if got else 0
                failures = 0
                if progress:
                    progress(min(offset / total, 0.99))
                continue
            if resp is not None and resp.status_code == 401:
                access = _access_token(config, instance_path, http, force=True)
                offset = total  # 받은 곳부터 이어서 — 상태부터 물어본다
                continue
            if resp is not None and resp.status_code not in _RETRY_STATUS:
                raise YouTubeError(_explain(resp))
            failures += 1
            if failures > 5:
                raise YouTubeError(f"네트워크 문제로 업로드를 끝내지 못했습니다: {error or resp.status_code}")
            sleep(min(2 ** failures, 30))
            offset = total  # 어디까지 받았는지 물어본 뒤 이어서 보낸다


def upload_captions(config, instance_path, video_id, srt_path, language="ko", name="한국어", http=None):
    """올린 영상에 자막 트랙(.srt)을 붙인다 (captions.insert, 할당량 400). 자막 트랙 ID 반환."""
    http = http or _http(config)
    boundary = "caption-" + secrets.token_hex(8)
    meta = {"snippet": {"videoId": video_id, "language": language, "name": name, "isDraft": False}}
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(meta, ensure_ascii=False)}\r\n--{boundary}\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode("utf-8") \
        + Path(srt_path).read_bytes() + f"\r\n--{boundary}--\r\n".encode("utf-8")

    def send(access):
        return http.post(CAPTIONS_URL, params={"uploadType": "multipart", "part": "snippet"},
                         headers={"Authorization": f"Bearer {access}",
                                  "Content-Type": f"multipart/related; boundary={boundary}"},
                         data=body, timeout=60)

    resp = send(_access_token(config, instance_path, http))
    if resp.status_code == 401:
        resp = send(_access_token(config, instance_path, http, force=True))
    if resp.status_code not in (200, 201):
        text = _error_text(resp)
        if resp.status_code == 403 and ("insufficient" in text.lower() or "scope" in text.lower()
                                        or "forbidden" in text.lower()):
            raise YouTubeError("자막을 올릴 권한이 없습니다. 'YouTube 계정 연결'을 다시 해 주세요 (자막 권한이 새로 필요합니다).")
        raise YouTubeError(_explain(resp))
    try:
        return resp.json().get("id", "")
    except ValueError:
        return ""


def default_description(description_text):
    """[영상 설명] + AI 제작 안내."""
    text = (description_text or "").strip()
    note = "이 영상은 AI 영상 생성 모델로 만들었고, 시청자 의견을 반영해 다음 버전을 만듭니다. 고칠 점을 댓글로 남겨 주세요."
    return f"{text}\n\n{note}" if text else note
