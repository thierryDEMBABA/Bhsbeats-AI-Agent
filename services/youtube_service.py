import re
import requests


def _parse_iso8601_duration(duration: str) -> int:
    """Convertit une durée ISO 8601 (ex: PT3M45S) en secondes."""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration)
    if not m:
        return 0
    h, mn, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mn * 60 + s


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
            "part": "snippet,contentDetails",
            "id": ",".join(video_ids),
            "key": self.api_key,
        })
        r.raise_for_status()
        items = r.json().get("items", [])
        # Exclure les Shorts (durée < 60 secondes)
        return [v for v in items if not self._is_short(v)]

    @staticmethod
    def _is_short(video: dict) -> bool:
        """Retourne True si la vidéo est un Short (durée < 90s).
        Si la durée est absente ou 0, on garde la vidéo par défaut."""
        duration = video.get("contentDetails", {}).get("duration", "")
        if not duration:
            return False
        seconds = _parse_iso8601_duration(duration)
        return seconds > 0 and seconds < 90

    _STRIP_TAGS = re.compile(r'\[(FREE|free|Free|SOLD|sold|Sold)\]', re.IGNORECASE)

    @staticmethod
    def extract_titre_atomique(youtube_title: str) -> str:
        """Extrait le texte entre les premières guillemets doubles, sans les tags [FREE]/[SOLD]."""
        m = re.search(r'"([^"]+)"', youtube_title)
        if not m:
            return ""
        titre = YouTubeService._STRIP_TAGS.sub("", m.group(1)).strip()
        return titre

    @staticmethod
    def is_sold(youtube_title: str) -> bool:
        """Retourne True si le titre contient [SOLD] (insensible à la casse)."""
        return bool(re.search(r'\[sold\]', youtube_title, re.IGNORECASE))

    @staticmethod
    def format_title(youtube_title: str) -> str:
        """
        Reformate le titre YouTube en : "titre_atomique - reste"
        1. Retire les tags [FREE], [SOLD], etc.
        2. Extrait le titre_atomique (entre guillemets doubles)
        3. Retire la partie entre guillemets du titre pour obtenir le "reste"
        4. Retourne "titre_atomique - reste" (nettoyé)
        Si pas de guillemets, retourne le titre nettoyé tel quel.
        """
        # 1. Retirer les tags
        cleaned = YouTubeService._STRIP_TAGS.sub("", youtube_title).strip()

        # 2. Extraire le titre_atomique entre guillemets
        m = re.search(r'"([^"]+)"', cleaned)
        if not m:
            return re.sub(r'\s+', ' ', cleaned).strip()

        titre_atomique = m.group(1).strip()

        # 3. Retirer la partie entre guillemets (inclus les guillemets) du titre
        reste = cleaned[:m.start()] + cleaned[m.end():]
        # Nettoyer séparateurs résiduels en début/fin (|, -, espace)
        reste = re.sub(r'^[\s|,\-]+|[\s|,\-]+$', '', reste)
        reste = re.sub(r'\s+', ' ', reste).strip()

        # 4. Si pas de reste, retourner juste le titre_atomique
        if not reste:
            return titre_atomique

        return f'"{titre_atomique}" - {reste}'

    @staticmethod
    def parse_description(description: str, title: str = "") -> tuple[int, str, str]:
        """Extrait BPM, tonalité et genre depuis la description et le titre."""
        haystack = title + " " + description

        # BPM
        tempo = 0
        m = re.search(r"bpm\s*[-:]?\s*(\d{2,3})|(\d{2,3})\s*bpm", haystack, re.IGNORECASE)
        if m:
            tempo = int(m.group(1) or m.group(2))

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
