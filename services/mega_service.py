import os
import re
import subprocess
import sys
import tempfile

from services.audio_utils import create_tagged

# Sur Windows, MEGAcmd installe des .bat — subprocess a besoin de shell=True
# et du PATH MEGAcmd pour les trouver
SHELL = sys.platform == "win32"
if SHELL:
    megacmd_dir = os.path.expandvars(r"%LOCALAPPDATA%\MEGAcmd")
    if megacmd_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = megacmd_dir + os.pathsep + os.environ.get("PATH", "")
    ffmpeg_dir = os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"
        r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
        r"\ffmpeg-8.1.2-full_build\bin"
    )
    if ffmpeg_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")


class MegaService:
    TIMEOUT = 60

    def __init__(self, beats_root_path: str):
        # Strips slashes — la racine Mega est représentée par une chaîne vide
        self.beats_root_path = beats_root_path.strip("/")

    def process_beat_files(
        self, titre_atomique: str, local_tagged_dir: str,
        beat_tag_path: str | None = None, beat_tag_interval: int = 30
    ) -> dict | None:
        """
        Pour un beat :
        - Télécharge le fichier tagged dans local_tagged_dir
        - Génère des liens de partage pour mp3, wav, stems
        Retourne None si le dossier est introuvable.
        """
        # Résoudre le nom exact du dossier (insensible à la casse)
        actual_folder = self._find_folder_name(titre_atomique)
        if not actual_folder:
            return None

        if self.beats_root_path:
            remote_path = f"{self.beats_root_path}/{actual_folder}"
        else:
            remote_path = actual_folder

        files = self._list_files(remote_path)
        if not files:
            print("\npas de fichiers trouvés sur Mega pour", titre_atomique)
            return None

        result = {}
        wav_filename = None

        tagged_filename, mp3_filename = self._classify_mp3s(files)

        for filename in files:
            lower       = filename.lower()
            remote_file = f"{remote_path}/{filename}"

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
            remote_wav = f"{remote_path}/{wav_filename}"
            mp3_name   = f"{titre_atomique}Untagged.mp3"
            mp3_link   = self._convert_wav_to_mp3_and_upload(
                remote_wav, remote_path, mp3_name, local_tagged_dir
            )
            if mp3_link:
                result["mp3"] = mp3_link

        # Pas de fichier tagged → le créer depuis l'untagged (WAV ou MP3)
        if "tagged" not in result and beat_tag_path and os.path.exists(beat_tag_path):
            untagged_remote = None
            untagged_local_name = None
            if wav_filename:
                untagged_remote     = f"{remote_path}/{wav_filename}"
                untagged_local_name = wav_filename
            elif mp3_filename:
                untagged_remote     = f"{remote_path}/{mp3_filename}"
                untagged_local_name = mp3_filename

            if untagged_remote:
                tagged_name = f"{titre_atomique}.mp3"
                link = self._create_tagged_and_upload(
                    untagged_remote, remote_path, tagged_name,
                    beat_tag_path, beat_tag_interval, local_tagged_dir
                )
                if link:
                    result["tagged"]         = tagged_name
                    result["tagged_created"] = True

        # Pas de .rar/.zip → lien du dossier comme stems
        if "stems" not in result:
            link = self._get_or_create_share_link(remote_path)
            if link:
                result["stems"] = link

        return result if result else None

    def _find_folder_name(self, titre_atomique: str) -> str | None:
        """
        Cherche le dossier du beat en 2 niveaux (racine → sous-dossiers genre).
        Retourne le chemin exact avec la bonne casse, ex: "Instrumentale/Godzilla II".
        MEGAcmd est case-sensitive : le chemin retourné doit être exact.
        """
        import unicodedata

        def _norm(s: str) -> str:
            return unicodedata.normalize("NFC", s).lower()

        def _mega_ls(path: str | None) -> list[str]:
            cmd = f'mega-ls "{path}"' if path else "mega-ls"
            try:
                r = subprocess.run(
                    cmd, capture_output=True, text=True, encoding="utf-8",
                    timeout=self.TIMEOUT, shell=SHELL
                )
                if r.returncode != 0:
                    return []
                return [l.strip() for l in r.stdout.splitlines() if l.strip()]
            except Exception:
                return []

        print(f"   [Mega] Recherche du dossier pour '{titre_atomique}'...")
        root = self.beats_root_path if self.beats_root_path else None

        # Niveau 1 : racine (ou beats_root_path)
        root_entries = _mega_ls(root)
        for entry in root_entries:
            if _norm(entry) == _norm(titre_atomique):
                print(f"   [Mega] Trouvé à la racine : {entry}")
                return entry

        # Niveau 2 : sous-dossiers genre
        for genre in root_entries:
            genre_path = f"{root}/{genre}" if root else genre
            beat_entries = _mega_ls(genre_path)
            for beat in beat_entries:
                if _norm(beat) == _norm(titre_atomique):
                    path = f"{genre_path}/{beat}"
                    print(f"   [Mega] Trouvé dans '{genre}' : {beat}")
                    return path

        print(f"   [Mega] Dossier introuvable pour '{titre_atomique}'")
        return None

    @staticmethod
    def _classify_mp3s(files: list[str]) -> tuple[str | None, str | None]:
        """
        Détermine quel fichier MP3 est le tagged et lequel est l'untagged.

        Structure 1 : nom.mp3 (tagged) + nomUntagged.mp3 (untagged)
        Structure 2 : nomTagged.mp3 (tagged) + nom.mp3 (untagged)
        Structure 3 : nom.mp3 seul (considéré comme tagged)
        """
        mp3s = [f for f in files if f.lower().endswith(".mp3")]

        if not mp3s:
            return None, None

        has_untagged = any("untagged" in f.lower() for f in mp3s)

        if has_untagged:
            # Structure 1
            tagged   = next((f for f in mp3s if "untagged" not in f.lower() and "tagged" not in f.lower()), None)
            untagged = next((f for f in mp3s if "untagged" in f.lower()), None)
        elif any("tagged" in f.lower() for f in mp3s):
            # Structure 2
            tagged   = next((f for f in mp3s if "tagged" in f.lower()), None)
            untagged = next((f for f in mp3s if "tagged" not in f.lower()), None)
        else:
            # Structure 3 : un seul MP3 sans marqueur → c'est le tagged
            tagged   = mp3s[0]
            untagged = None

        return tagged, untagged

    def retag_beat(
        self, titre_atomique: str, tag_path: str,
        local_dir: str, interval: int = 90
    ) -> bool:
        """
        Recrée le fichier tagged d'un beat depuis son untagged source et remplace
        l'ancien tagged sur Mega.
        Retourne True si le remplacement a réussi.
        """
        actual_folder = self._find_folder_name(titre_atomique)
        if not actual_folder:
            return False

        remote_path = f"{self.beats_root_path}/{actual_folder}" if self.beats_root_path else actual_folder
        files = self._list_files(remote_path)
        if not files:
            return False

        tagged_filename, mp3_filename = self._classify_mp3s(files)
        wav_filename = next((f for f in files if f.lower().endswith(".wav")), None)

        # Choisir la meilleure source : WAV > untagged MP3 > tagged MP3
        if wav_filename:
            src_remote = f"{remote_path}/{wav_filename}"
        elif mp3_filename:
            src_remote = f"{remote_path}/{mp3_filename}"
        elif tagged_filename:
            src_remote = f"{remote_path}/{tagged_filename}"
        else:
            return False

        tagged_name = f"{titre_atomique}.mp3" if not tagged_filename else tagged_filename

        return self._create_tagged_and_upload(
            src_remote, remote_path, tagged_name,
            tag_path, interval, local_dir
        )

    def _list_files(self, remote_path: str) -> list[str]:
        try:
            result = subprocess.run(
                ["mega-ls", remote_path],
                capture_output=True, text=True, timeout=self.TIMEOUT, shell=SHELL
            )
            if result.returncode != 0:
                return []
            return [line.strip() for line in result.stdout.splitlines() if line.strip()]
        except Exception:
            return []

    def _create_tagged_and_upload(
        self, remote_untagged: str, remote_folder: str, tagged_name: str,
        tag_path: str, interval: int, local_dir: str
    ) -> bool:
        """Télécharge l'untagged, crée le tagged avec ffmpeg, uploade sur Mega."""
        with tempfile.TemporaryDirectory() as conv_dir:
            if not self._download_file(remote_untagged, conv_dir):
                print("   [Mega] Echec telechargement untagged pour creation tagged")
                return False
            src_files = [f for f in os.listdir(conv_dir) if os.path.isfile(os.path.join(conv_dir, f))]
            if not src_files:
                return False
            src_path    = os.path.join(conv_dir, src_files[0])
            tagged_path = os.path.join(local_dir, tagged_name)
            if not create_tagged(src_path, tagged_path, tag_path, interval):
                print("   [Mega] Echec creation tagged")
                return False
            print(f"   [Mega] Tagged cree : {tagged_name}")
            if not self._upload_file(tagged_path, remote_folder):
                print("   [Mega] Echec upload tagged")
                return False
            return True

    def _convert_wav_to_mp3_and_upload(
        self, remote_wav: str, remote_folder: str, mp3_name: str, tmp_dir: str
    ) -> str | None:
        """Télécharge le WAV, le convertit en MP3, l'uploade dans le même dossier Mega et retourne le lien."""
        import tempfile
        with tempfile.TemporaryDirectory() as conv_dir:
            # 1. Télécharger le WAV
            if not self._download_file(remote_wav, conv_dir):
                print("   [Mega] Échec téléchargement WAV pour conversion")
                return None
            wav_files = [f for f in os.listdir(conv_dir) if f.lower().endswith(".wav")]
            if not wav_files:
                return None
            wav_path = os.path.join(conv_dir, wav_files[0])
            mp3_path = os.path.join(conv_dir, mp3_name)

            # 2. Convertir WAV → MP3
            if not self._ffmpeg_convert(wav_path, mp3_path):
                print("   [Mega] Echec conversion WAV->MP3")
                return None
            print(f"   [Mega] WAV converti en MP3 : {mp3_name}")

            # 3. Uploader le MP3 sur Mega
            remote_mp3 = f"{remote_folder}/{mp3_name}"
            if not self._upload_file(mp3_path, remote_folder):
                print("   [Mega] Échec upload MP3 converti")
                return None

            # 4. Créer et retourner le lien
            return self._get_or_create_share_link(remote_mp3)

    def _upload_file(self, local_path: str, remote_folder: str) -> bool:
        try:
            result = subprocess.run(
                ["mega-put", local_path, remote_folder],
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
                ["mega-get", remote_file, local_dir],
                capture_output=True, timeout=self.TIMEOUT, shell=SHELL
            )
            return result.returncode == 0
        except Exception:
            return False

    def _get_or_create_share_link(self, remote_file: str) -> str | None:
        # Essayer de récupérer un lien existant
        try:
            result = subprocess.run(
                ["mega-export", remote_file],
                capture_output=True, text=True, timeout=self.TIMEOUT, shell=SHELL
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
                capture_output=True, text=True, timeout=self.TIMEOUT, shell=SHELL
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
