import requests


class OneDriveService:
    TOKEN_URL  = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    GRAPH_BASE = "https://graph.microsoft.com/v1.0"

    def __init__(self, tenant_id: str, client_id: str, client_secret: str,
                 refresh_token: str, beats_root_path: str):
        self.tenant_id      = tenant_id
        self.client_id      = client_id
        self.client_secret  = client_secret
        self.refresh_token  = refresh_token
        self.beats_root_path = beats_root_path.rstrip("/")
        self._token: str | None = None

    def process_beat_files(self, titre_atomique: str, local_tagged_dir: str) -> dict | None:
        """
        Pour un beat :
        - Télécharge le fichier tagged dans local_tagged_dir
        - Génère des liens de partage pour mp3, wav, stems
        Retourne None si le dossier est introuvable.
        """
        token = self._get_token()
        if not token:
            return None

        remote_path  = f"{self.beats_root_path}/{titre_atomique}"
        encoded_path = requests.utils.quote(remote_path, safe="")

        try:
            r = requests.get(
                f"{self.GRAPH_BASE}/me/drive/root:/{encoded_path}:/children",
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code != 200:
                return None
            items = r.json().get("value", [])
        except Exception:
            return None

        if not items:
            return None

        result = {}

        for item in items:
            filename = item.get("name", "")
            item_id  = item.get("id")
            if not filename or not item_id:
                continue

            lower     = filename.lower()
            is_tagged = "tagged" in lower and lower.endswith(".mp3")
            is_mp3    = not is_tagged and lower.endswith(".mp3")
            is_wav    = lower.endswith(".wav")
            is_stems  = lower.endswith(".rar") or lower.endswith(".zip")

            if is_tagged and "tagged" not in result:
                download_url = item.get("@microsoft.graph.downloadUrl")
                if download_url:
                    local_path = f"{local_tagged_dir}/{filename}"
                    if self._download_file(download_url, local_path):
                        result["tagged"] = filename

            elif is_mp3 and "mp3" not in result:
                link = self._get_or_create_share_link(item_id, token)
                if link:
                    result["mp3"] = link

            elif is_wav and "wav" not in result:
                link = self._get_or_create_share_link(item_id, token)
                if link:
                    result["wav"] = link

            elif is_stems and "stems" not in result:
                link = self._get_or_create_share_link(item_id, token)
                if link:
                    result["stems"] = link

        return result if result else None

    def _get_or_create_share_link(self, item_id: str, token: str) -> str | None:
        headers = {"Authorization": f"Bearer {token}"}

        # Vérifier si un lien view existe déjà
        try:
            r = requests.get(
                f"{self.GRAPH_BASE}/me/drive/items/{item_id}/permissions",
                headers=headers,
            )
            for perm in r.json().get("value", []):
                if perm.get("link", {}).get("type") == "view":
                    return perm["link"]["webUrl"]
        except Exception:
            pass

        # Créer un lien anonyme en lecture seule
        try:
            r = requests.post(
                f"{self.GRAPH_BASE}/me/drive/items/{item_id}/createLink",
                headers=headers,
                json={"type": "view", "scope": "anonymous"},
            )
            return r.json().get("link", {}).get("webUrl")
        except Exception:
            return None

    def _get_token(self) -> str | None:
        if self._token:
            return self._token
        try:
            r = requests.post(
                self.TOKEN_URL.format(tenant=self.tenant_id),
                data={
                    "grant_type":    "refresh_token",
                    "client_id":     self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": self.refresh_token,
                    "scope":         "https://graph.microsoft.com/Files.ReadWrite offline_access",
                },
            )
            self._token = r.json().get("access_token")
            return self._token
        except Exception:
            return None

    @staticmethod
    def _download_file(url: str, local_path: str) -> bool:
        try:
            r = requests.get(url, stream=True)
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True
        except Exception:
            return False
