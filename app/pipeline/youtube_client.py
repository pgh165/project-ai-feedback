"""YouTube 업로드·댓글 수집. SIMULATION_MODE=true면 외부 호출 없이 시뮬레이션한다.

실제 모드에서 업로드는 지원하지 않는다(OAuth 필요) — 직접 업로드한 영상의 ID를 입력받고,
댓글만 YOUTUBE_API_KEY로 수집한다.
"""
import random

import requests

_API_URL = "https://www.googleapis.com/youtube/v3/commentThreads"

# 시뮬레이션 댓글 풀: 건설적(카테고리별) + 상반 의견 + 잡담
_SIMULATED_COMMENTS = [
    # story
    "주인공이 갑자기 화해하는 전개가 어색했어요. 이유를 보여주는 장면이 필요해요.",
    "다음 화에서는 두 사람의 과거 이야기를 더 보여주면 좋겠습니다.",
    "여주인공이 왜 떠났는지 다음 화에서 사연을 보여주세요. 궁금해서 답답해요.",
    "결말이 너무 뻔해요. 다음 화에는 반전이 있었으면 좋겠어요.",
    "악역 캐릭터의 사연이 궁금해요. 악역 시점의 장면을 넣어주세요.",
    "I wish the next episode showed why she left him, the backstory is unclear.",
    "The ending was too predictable, please add a twist in the next episode.",
    "The villain needs a motive, it would be better if we saw his backstory.",
    "두 주인공이 다음 화에서 다시 만났으면 좋겠어요. 재회 장면이 필요해요.",
    "I want the two leads to meet again in the next episode, the story feels unfinished.",
    # pacing (상반 의견 포함)
    "전개가 너무 빨라서 따라가기 힘들어요. 장면 전환을 조금 천천히 해주세요.",
    "장면이 너무 급하게 넘어가서 감정을 느낄 시간이 부족해요.",
    "The pacing is too fast, the scenes feel rushed and hard to follow.",
    "중간 카페 장면이 너무 길어서 지루했어요. 조금 줄였으면 좋겠어요.",
    "The cafe scene drags, it is too slow and should be shorter.",
    # visual
    "화면이 너무 어두워서 인물 표정이 안 보여요. 조명을 밝게 해주세요.",
    "밤 장면이 너무 어두워요. 얼굴이 전혀 안 보여서 답답했어요.",
    "The night scene is too dark to see the actors' faces, please brighten it.",
    "주인공 얼굴이 장면마다 달라 보여서 어색해요. 같은 사람처럼 보였으면 좋겠어요.",
    "The main character's face keeps changing between shots, it is distracting.",
    "의상 색감이 배경이랑 비슷해서 인물이 잘 안 보여요.",
    # audio
    "배경음악이 대사보다 커서 잘 안 들립니다. 음악 볼륨을 줄였으면 좋겠어요.",
    "The background music is louder than the voices, it should be quieter.",
    "음악이 계속 너무 커서 매번 볼륨을 줄여야 해요.",
    # subtitle
    "자막이 너무 빨리 지나가서 읽기 힘들어요. 조금 더 오래 띄워주세요.",
    "자막 글씨가 너무 작아서 휴대폰으로 보기 불편해요.",
    "The subtitles disappear too fast, please keep them on screen longer.",
    # technical
    "영상이 중간에 끊기고 화질이 깨져요. 다음 영상에서는 고쳐주세요.",
    "소리와 화면 싱크가 안 맞아서 도저히 못 보겠어요.",
    "The video glitches at 0:12 and the audio is out of sync.",
    # content
    "무슨 상황인지 설명이 부족해서 처음에 이해가 안 됐어요. 도입부에 배경 설명이 필요해요.",
    "분량이 너무 짧아요. 한 화에 내용을 조금 더 담아주세요.",
    # 잡담·비건설적
    "ㅋㅋㅋ 재밌네요", "잘 보고 갑니다", "1등!", "최고예요", "노잼", "다음 화 언제 나와요?",
    "구독하고 갑니다", "lol nice", "first!", "love this so much", "great video",
    "와 대박", "여주 예쁘다", "this is trash", "who else is watching at 3am",
]


def is_simulation(config):
    return bool(config.get("SIMULATION_MODE"))


def upload_video(round_obj, config, manual_video_id=""):
    """업로드 후 video ID 반환. 시뮬레이션이면 가짜 ID."""
    if is_simulation(config):
        return f"sim-{round_obj.session_id}-{round_obj.round_number}"
    if not manual_video_id.strip():
        raise ValueError("실제 모드에서는 YouTube에 직접 업로드한 영상 ID를 입력해야 합니다.")
    return manual_video_id.strip()


def collect_comments(video_id, config, seed=0, count=32):
    """[{author, text, source}] 반환."""
    if is_simulation(config):
        rng = random.Random(seed)
        picked = rng.sample(_SIMULATED_COMMENTS, min(count, len(_SIMULATED_COMMENTS)))
        return [{"author": f"viewer{i:02d}", "text": t, "source": "simulation"}
                for i, t in enumerate(picked, start=1)]

    if not config.get("YOUTUBE_API_KEY"):
        raise ValueError("YOUTUBE_API_KEY가 설정되지 않았습니다.")
    comments, page_token = [], None
    while len(comments) < 500:
        params = {"part": "snippet", "videoId": video_id, "maxResults": 100,
                  "textFormat": "plainText", "key": config["YOUTUBE_API_KEY"]}
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(_API_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("items", []):
            snippet = item["snippet"]["topLevelComment"]["snippet"]
            comments.append({"author": snippet.get("authorDisplayName", ""),
                             "text": snippet.get("textDisplay", ""), "source": "youtube"})
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return comments
