import re
import requests


class YouTubeService:
    BASE = "https://www.googleapis.com/youtube/v3"

    def __init__(self, api_key: str, channel_handle: str):
        self.api_key = api_key
        self.channel_handle = channel_handle

    def get_channel_id(self) -> str | None:
        r = requests.get(f"{self.BASE}/channels", params={
            "part": "id",
            "forHandle": self.channel_handle,
            "key": self.api_key,
        })
        r.raise_for_status()
        items = r.json().get("items", [])
        return items[0]["id"] if items else None

    def fetch_all_videos(self, channel_id: str) -> list[dict]:
        videos = []
        page_token = None

        while True:
            params = {
                "part": "snippet",
                "channelId": channel_id,
                "maxResults": 50,
                "type": "video",
                "key": self.api_key,
            }
            if page_token:
                params["pageToken"] = page_token

            r = requests.get(f"{self.BASE}/search", params=params)
            r.raise_for_status()
            data = r.json()

            ids = [item["id"]["videoId"] for item in data.get("items", [])]
            if ids:
                videos.extend(self._fetch_video_details(ids))

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return videos

    def _fetch_video_details(self, video_ids: list[str]) -> list[dict]:
        r = requests.get(f"{self.BASE}/videos", params={
            "part": "snippet",
            "id": ",".join(video_ids),
            "key": self.api_key,
        })
        r.raise_for_status()
        return r.json().get("items", [])

    @staticmethod
    def extract_titre_atomique(youtube_title: str) -> str:
        """Extrait le texte entre les premières guillemets doubles."""
        m = re.search(r'"([^"]+)"', youtube_title)
        return m.group(1).strip() if m else ""

    @staticmethod
    def parse_description(description: str, title: str = "") -> tuple[int, str, str]:
        """Extrait BPM, tonalité et genre depuis la description et le titre."""
        haystack = title + " " + description

        # BPM
        tempo = 0
        m = re.search(r"(\d{2,3})\s*bpm", haystack, re.IGNORECASE)
        if m:
            tempo = int(m.group(1))

        # Key
        key = "N/A"
        m = re.search(r"(?:key\s*[:\-]?\s*)([A-G][#b]?\s*(?:major|minor|maj|min|m)?)", description, re.IGNORECASE)
        if m:
            key = m.group(1).strip()

        # Genre
        genres = ["Afro Trap", "Afrobeat", "Afro Drill", "Drill", "Trap",
                  "Boom Bap", "R&B", "Pop", "Dancehall", "Amapiano", "Hip Hop"]
        gender = "Trap"
        for g in genres:
            if g.lower() in haystack.lower():
                gender = g
                break

        return tempo, key, gender
