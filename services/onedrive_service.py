import os
import re
import subprocess
import sys
import tempfile
import unicodedata

from services.audio_utils import create_tagged

SHELL = sys.platform == "win32"
if SHELL:
    rclone_dir = r"C:\Coding\rclone-v1.74.3-windows-amd64"
    if rclone_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = rclone_dir + os.pathsep + os.environ.get("PATH", "")
    ffmpeg_dir = os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"
        r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
        r"\ffmpeg-8.1.2-full_build\bin"
    )
    if ffmpeg_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")


def _normalize(s: str) -> str:
    """Normalise une chaîne en NFC lowercase pour comparer des noms avec accents."""
    return unicodedata.normalize("NFC", s).lower()


class OneDriveService:
    """
    Interagit avec OneDrive via rclone.
    Remote configuré : ondrive
    Syntaxe rclone : ondrive:"chemin/vers/dossier"
    """

    TIMEOUT     = 60
    REMOTE_NAME = "ondrive"

    def __init__(self, beats_root_path: str):
        self.beats_root_path = beats_root_path.strip("/")

    def _rclone_path(self, *parts: str) -> str:
        """Construit un chemin rclone : "ondrive:root/part1/part2" (compatible cmd.exe) """
        segments = [self.beats_root_path] + list(parts)
        path     = "/".join(s.strip("/") for s in segments if s)
        return f'"{self.REMOTE_NAME}:{path}"'

    def process_beat_files(
        self, titre_atomique: str, local_tagged_dir: str,
        beat_tag_path: str | None = None, beat_tag_interval: int = 90
    ) -> dict | None:
        """
        Pour un beat :
        - Télécharge le fichier tagged dans local_tagged_dir
        - Génère des liens de partage pour mp3, wav, stems
        Retourne None si le dossier est introuvable.
        """
        actual_folder = self._find_folder_name(titre_atomique)
        if not actual_folder:
            return None

        files, files_folder = self._list_files_with_path(actual_folder)
        if not files:
            return None

        result = {}
        wav_filename = None
        tagged_filename, mp3_filename = self._classify_mp3s(files)

        for filename in files:
            lower       = filename.lower()
            remote_file = self._rclone_path(files_folder, filename)

            if filename == tagged_filename:
                if self._download_file(remote_file, local_tagged_dir):
                    result["tagged"] = filename

            elif filename == mp3_filename:
                link = self._get_or_create_share_link(remote_file)
                if link:
                    result["mp3"] = link

            elif lower.endswith(".wav") and "wav" not in result:
                wav_filename = filename
                link = self._get_or_create_share_link(remote_file)
                if link:
                    result["wav"] = link

            elif (lower.endswith(".rar") or lower.endswith(".zip")) and "stems" not in result:
                link = self._get_or_create_share_link(remote_file)
                if link:
                    result["stems"] = link

        # Pas de MP3 untagged mais WAV disponible → convertir et uploader
        if "mp3" not in result and wav_filename:
            mp3_name = f"{titre_atomique}Untagged.mp3"
            mp3_link = self._convert_wav_to_mp3_and_upload(
                self._rclone_path(files_folder, wav_filename),
                files_folder, mp3_name, local_tagged_dir
            )
            if mp3_link:
                result["mp3"] = mp3_link

        # Pas de fichier tagged → le créer depuis l'untagged (WAV ou MP3)
        if "tagged" not in result and beat_tag_path and os.path.exists(beat_tag_path):
            if wav_filename:
                untagged_remote = self._rclone_path(files_folder, wav_filename)
            elif mp3_filename:
                untagged_remote = self._rclone_path(files_folder, mp3_filename)
            else:
                untagged_remote = None

            if untagged_remote:
                tagged_name = f"{titre_atomique}.mp3"
                ok = self._create_tagged_and_upload(
                    untagged_remote, files_folder, tagged_name,
                    beat_tag_path, beat_tag_interval, local_tagged_dir
                )
                if ok:
                    result["tagged"]         = tagged_name
                    result["tagged_created"] = True

        # Pas de .rar/.zip → lien du dossier comme stems
        if "stems" not in result:
            link = self._get_or_create_share_link(self._rclone_path(actual_folder))
            if link:
                result["stems"] = link

        return result if result else None

    def retag_beat(
        self, titre_atomique: str, tag_path: str,
        local_dir: str, interval: int = 90
    ) -> bool:
        """
        Recrée le fichier tagged d'un beat depuis son untagged source et remplace
        l'ancien tagged sur OneDrive.
        Retourne True si le remplacement a réussi.
        """
        actual_folder = self._find_folder_name(titre_atomique)
        if not actual_folder:
            return False

        files, files_folder = self._list_files_with_path(actual_folder)
        if not files:
            return False

        tagged_filename, mp3_filename = self._classify_mp3s(files)
        wav_filename = next((f for f in files if f.lower().endswith(".wav")), None)

        # Choisir la meilleure source : WAV > untagged MP3 > tagged MP3
        if wav_filename:
            src_remote = self._rclone_path(files_folder, wav_filename)
        elif mp3_filename:
            src_remote = self._rclone_path(files_folder, mp3_filename)
        elif tagged_filename:
            src_remote = self._rclone_path(files_folder, tagged_filename)
        else:
            return False

        tagged_name = f"{titre_atomique}.mp3" if not tagged_filename else tagged_filename

        return self._create_tagged_and_upload(
            src_remote, files_folder, tagged_name,
            tag_path, interval, local_dir
        )

    def _find_folder_name(self, titre_atomique: str) -> str | None:
        """
        Cherche le dossier du beat dans tous les sous-dossiers genre de beats_root_path.
        Retourne le chemin relatif complet genre/beat (ex: "afro trap/roma").
        """
        # 1. Lister les sous-dossiers genre (afro trap, cyril kamer, etc.)
        root_path = f'"{self.REMOTE_NAME}:{self.beats_root_path}"'
        try:
            result = subprocess.run(
                f"rclone lsf {root_path} --dirs-only",
                capture_output=True, text=True, encoding="utf-8", timeout=self.TIMEOUT, shell=SHELL
            )
            if result.returncode != 0:
                print(f"   [OneDrive] Erreur listing genres: {result.stderr.strip()}")
                return None
            genre_folders = [l.strip().rstrip("/") for l in result.stdout.splitlines() if l.strip()]
        except Exception as e:
            print(f"   [OneDrive] Exception listing genres: {e}")
            return None

        # 2. Pour chaque sous-dossier genre, chercher le beat
        for genre in genre_folders:
            genre_path = f'"{self.REMOTE_NAME}:{self.beats_root_path}/{genre}"'
            try:
                result = subprocess.run(
                    f"rclone lsf {genre_path} --dirs-only",
                    capture_output=True, text=True, encoding="utf-8", timeout=self.TIMEOUT, shell=SHELL
                )
                if result.returncode != 0:
                    continue
                beat_folders = [l.strip().rstrip("/") for l in result.stdout.splitlines() if l.strip()]
                for beat in beat_folders:
                    if _normalize(beat) == _normalize(titre_atomique):
                        print(f"   [OneDrive] Trouvé dans genre '{genre}' : {beat}")
                        return f"{genre}/{beat}"
            except Exception:
                continue

        return None

    def _list_files_with_path(self, folder_name: str) -> tuple[list[str], str]:
        """
        Liste les fichiers audio du dossier beat.
        Fallback sur Audio/ si aucun fichier audio à la racine.
        Retourne (liste_fichiers, chemin_effectif).
        """
        audio_exts = (".mp3", ".wav", ".rar", ".zip")
        files = self._lsf_files(self._rclone_path(folder_name))
        if any(f.lower().endswith(audio_exts) for f in files):
            return files, folder_name
        # Fallback sous-dossier Audio/
        audio_folder = f"{folder_name}/Audio"
        audio_files  = self._lsf_files(self._rclone_path(audio_folder))
        if audio_files:
            return audio_files, audio_folder
        return files, folder_name

    def _lsf_files(self, remote_path: str) -> list[str]:
        try:
            result = subprocess.run(
                f"rclone lsf {remote_path} --files-only",
                capture_output=True, text=True, encoding="utf-8", timeout=self.TIMEOUT, shell=SHELL
            )
            if result.returncode != 0:
                return []
            return [l.strip() for l in result.stdout.splitlines() if l.strip()]
        except Exception:
            return []

    def _create_tagged_and_upload(
        self, remote_untagged: str, remote_folder: str, tagged_name: str,
        tag_path: str, interval: int, local_dir: str
    ) -> bool:
        """Télécharge l'untagged, crée le tagged avec ffmpeg, uploade sur OneDrive."""
        with tempfile.TemporaryDirectory() as conv_dir:
            if not self._download_file(remote_untagged, conv_dir):
                print("   [OneDrive] Echec telechargement untagged pour creation tagged")
                return False
            src_files = [f for f in os.listdir(conv_dir) if os.path.isfile(os.path.join(conv_dir, f))]
            if not src_files:
                return False
            src_path    = os.path.join(conv_dir, src_files[0])
            tagged_path = os.path.join(local_dir, tagged_name)
            if not create_tagged(src_path, tagged_path, tag_path, interval):
                print("   [OneDrive] Echec creation tagged")
                return False
            print(f"   [OneDrive] Tagged cree : {tagged_name}")
            remote_folder_path = self._rclone_path(remote_folder)
            if not self._upload_file(tagged_path, remote_folder_path):
                print("   [OneDrive] Echec upload tagged")
                return False
            return True

    def _convert_wav_to_mp3_and_upload(
        self, remote_wav: str, remote_folder: str, mp3_name: str, tmp_dir: str
    ) -> str | None:
        """Télécharge le WAV, le convertit en MP3, l'uploade dans le même dossier OneDrive et retourne le lien."""
        import tempfile
        with tempfile.TemporaryDirectory() as conv_dir:
            # 1. Télécharger le WAV
            if not self._download_file(remote_wav, conv_dir):
                print("   [OneDrive] Échec téléchargement WAV pour conversion")
                return None
            wav_files = [f for f in os.listdir(conv_dir) if f.lower().endswith(".wav")]
            if not wav_files:
                return None
            wav_path = os.path.join(conv_dir, wav_files[0])
            mp3_path = os.path.join(conv_dir, mp3_name)

            # 2. Convertir WAV → MP3
            if not self._ffmpeg_convert(wav_path, mp3_path):
                print("   [OneDrive] Echec conversion WAV->MP3")
                return None
            print(f"   [OneDrive] WAV converti en MP3 : {mp3_name}")

            # 3. Uploader le MP3 sur OneDrive
            remote_folder_path = self._rclone_path(remote_folder)
            if not self._upload_file(mp3_path, remote_folder_path):
                print("   [OneDrive] Échec upload MP3 converti")
                return None

            # 4. Créer et retourner le lien
            return self._get_or_create_share_link(self._rclone_path(remote_folder, mp3_name))

    def _upload_file(self, local_path: str, remote_folder_path: str) -> bool:
        try:
            result = subprocess.run(
                f'rclone copy "{local_path}" {remote_folder_path}',
                capture_output=True, timeout=self.TIMEOUT, shell=SHELL
            )
            return result.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _ffmpeg_convert(input_path: str, output_path: str) -> bool:
        try:
            cmd = f'ffmpeg -y -i "{input_path}" -codec:a libmp3lame -qscale:a 2 "{output_path}"'
            result = subprocess.run(cmd, capture_output=True, timeout=300, shell=True)
            return result.returncode == 0
        except Exception:
            return False

    def _download_file(self, remote_file: str, local_dir: str) -> bool:
        try:
            result = subprocess.run(
                f"rclone copy {remote_file} \"{local_dir}\"",
                capture_output=True, timeout=self.TIMEOUT, shell=SHELL
            )
            return result.returncode == 0
        except Exception:
            return False

    def _get_or_create_share_link(self, remote_file: str) -> str | None:
        try:
            result = subprocess.run(
                f"rclone link {remote_file}",
                capture_output=True, text=True, encoding="utf-8", timeout=self.TIMEOUT, shell=SHELL
            )
            if result.returncode == 0:
                link = result.stdout.strip()
                if link.startswith("http"):
                    return link
        except Exception:
            pass
        return None

    @staticmethod
    def _classify_mp3s(files: list[str]) -> tuple[str | None, str | None]:
        """
        Structure 1 : nom.mp3 (tagged) + nomUntagged.mp3 (untagged)
        Structure 2 : nomTagged.mp3 (tagged) + nom.mp3 (untagged)
        Structure 3 : nom.mp3 seul (considéré comme tagged)
        """
        mp3s = [f for f in files if f.lower().endswith(".mp3")]

        if not mp3s:
            return None, None

        has_untagged = any("untagged" in f.lower() for f in mp3s)

        if has_untagged:
            tagged   = next((f for f in mp3s if "untagged" not in f.lower() and "tagged" not in f.lower()), None)
            untagged = next((f for f in mp3s if "untagged" in f.lower()), None)
        elif any("tagged" in f.lower() for f in mp3s):
            tagged   = next((f for f in mp3s if "tagged" in f.lower()), None)
            untagged = next((f for f in mp3s if "tagged" not in f.lower()), None)
        else:
            # Structure 3 : un seul MP3 sans marqueur → c'est le tagged
            tagged   = mp3s[0]
            untagged = None

        return tagged, untagged
