import re
import subprocess
import tempfile
import os


class MegaService:
    TIMEOUT = 60

    def __init__(self, beats_root_path: str):
        self.beats_root_path = beats_root_path.rstrip("/")

    def process_beat_files(self, titre_atomique: str, local_tagged_dir: str) -> dict | None:
        """
        Pour un beat :
        - Télécharge le fichier tagged dans local_tagged_dir
        - Génère des liens de partage pour mp3, wav, stems
        Retourne None si le dossier est introuvable.
        """
        remote_path = f"{self.beats_root_path}/{titre_atomique}"

        files = self._list_files(remote_path)
        if not files:
            return None

        result = {}

        for filename in files:
            lower = filename.lower()
            remote_file = f"{remote_path}/{filename}"

            is_tagged = "tagged" in lower and lower.endswith(".mp3")
            is_mp3    = not is_tagged and lower.endswith(".mp3")
            is_wav    = lower.endswith(".wav")
            is_stems  = lower.endswith(".rar") or lower.endswith(".zip")

            if is_tagged and "tagged" not in result:
                if self._download_file(remote_file, local_tagged_dir):
                    result["tagged"] = filename

            elif is_mp3 and "mp3" not in result:
                link = self._get_or_create_share_link(remote_file)
                if link:
                    result["mp3"] = link

            elif is_wav and "wav" not in result:
                link = self._get_or_create_share_link(remote_file)
                if link:
                    result["wav"] = link

            elif is_stems and "stems" not in result:
                link = self._get_or_create_share_link(remote_file)
                if link:
                    result["stems"] = link

        return result if result else None

    def _list_files(self, remote_path: str) -> list[str]:
        try:
            result = subprocess.run(
                ["mega-ls", remote_path],
                capture_output=True, text=True, timeout=self.TIMEOUT
            )
            if result.returncode != 0:
                return []
            return [line.strip() for line in result.stdout.splitlines() if line.strip()]
        except Exception:
            return []

    def _download_file(self, remote_file: str, local_dir: str) -> bool:
        try:
            result = subprocess.run(
                ["mega-get", remote_file, local_dir],
                capture_output=True, timeout=self.TIMEOUT
            )
            return result.returncode == 0
        except Exception:
            return False

    def _get_or_create_share_link(self, remote_file: str) -> str | None:
        # Essayer de récupérer un lien existant
        try:
            result = subprocess.run(
                ["mega-export", remote_file],
                capture_output=True, text=True, timeout=self.TIMEOUT
            )
            if result.returncode == 0:
                link = self._parse_link(result.stdout)
                if link:
                    return link
        except Exception:
            pass

        # Créer un nouveau lien
        try:
            result = subprocess.run(
                ["mega-export", "-a", remote_file],
                capture_output=True, text=True, timeout=self.TIMEOUT
            )
            if result.returncode == 0:
                return self._parse_link(result.stdout)
        except Exception:
            pass

        return None

    @staticmethod
    def _parse_link(output: str) -> str | None:
        m = re.search(r"(https://mega\.nz/\S+)", output)
        return m.group(1) if m else None
