"""
BHS Beats Sync Agent
--------------------
Tourne sur ton PC local. Récupère les vidéos YouTube, les fichiers audio
depuis Mega ou OneDrive, puis pousse tout vers l'app Symfony hébergée.

Usage :
    python agent.py            # sync complète
    python agent.py --dry-run  # simulation sans envoi
"""

import argparse
import json
import os
import tempfile
from pathlib import Path

import requests
from dotenv import load_dotenv

from services.youtube_service  import YouTubeService
from services.mega_service     import MegaService
from services.onedrive_service import OneDriveService

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────────

YOUTUBE_API_KEY        = os.environ["YOUTUBE_API_KEY"]
YOUTUBE_CHANNEL_HANDLE = os.getenv("YOUTUBE_CHANNEL_HANDLE", "@bhsbeats")

MEGA_BEATS_ROOT_PATH   = os.environ["MEGA_BEATS_ROOT_PATH"]

ONEDRIVE_TENANT_ID     = os.environ["ONEDRIVE_TENANT_ID"]
ONEDRIVE_CLIENT_ID     = os.environ["ONEDRIVE_CLIENT_ID"]
ONEDRIVE_CLIENT_SECRET = os.environ["ONEDRIVE_CLIENT_SECRET"]
ONEDRIVE_REFRESH_TOKEN = os.environ["ONEDRIVE_REFRESH_TOKEN"]
ONEDRIVE_BEATS_ROOT    = os.environ["ONEDRIVE_BEATS_ROOT_PATH"]

SYMFONY_API_URL        = os.environ["SYMFONY_API_URL"]
SYMFONY_API_KEY        = os.environ["SYMFONY_API_KEY"]

MISSING_LOG_FILE       = Path(__file__).parent / "missing_audio_beats.json"

# ── Main ─────────────────────────────────────────────────────────────────────

def main(dry_run: bool = False):
    print("=" * 60)
    print("BHS Beats Sync Agent")
    print("=" * 60)

    youtube  = YouTubeService(YOUTUBE_API_KEY, YOUTUBE_CHANNEL_HANDLE)
    mega     = MegaService(MEGA_BEATS_ROOT_PATH)
    onedrive = OneDriveService(
        ONEDRIVE_TENANT_ID, ONEDRIVE_CLIENT_ID, ONEDRIVE_CLIENT_SECRET,
        ONEDRIVE_REFRESH_TOKEN, ONEDRIVE_BEATS_ROOT,
    )

    # 1. Récupérer les vidéos YouTube
    print("\n[YouTube] Résolution du channel...")
    channel_id = youtube.get_channel_id()
    if not channel_id:
        print("ERREUR : channel introuvable.")
        return

    print(f"[YouTube] Channel ID : {channel_id}")
    print("[YouTube] Récupération des vidéos...")
    videos = youtube.fetch_all_videos(channel_id)
    print(f"[YouTube] {len(videos)} vidéo(s) trouvée(s)")

    missing_audio = load_missing_log()
    already_missing_urls = {e["youtube_url"] for e in missing_audio}

    created = 0
    skipped = 0
    missing = 0

    with tempfile.TemporaryDirectory() as tmp_dir:
        for video in videos:
            snippet     = video["snippet"]
            youtube_url = f"https://www.youtube.com/watch?v={video['id']}"
            title       = snippet["title"]

            print(f"\n── {title}")

            titre_atomique = YouTubeService.extract_titre_atomique(title)
            if not titre_atomique:
                print(f"   [WARN] Pas de titre atomique entre guillemets, skip audio.")

            desc            = snippet.get("description", "")
            tempo, key, gender = YouTubeService.parse_description(desc, title)
            thumbnail_url   = (
                snippet.get("thumbnails", {}).get("maxres", {}).get("url")
                or snippet.get("thumbnails", {}).get("high", {}).get("url")
                or snippet.get("thumbnails", {}).get("default", {}).get("url")
            )

            # 2. Chercher les fichiers audio
            audio_files  = None
            audio_source = None

            if titre_atomique:
                print(f"   [Mega] Recherche de \"{titre_atomique}\"...")
                audio_files = mega.process_beat_files(titre_atomique, tmp_dir)
                if audio_files:
                    audio_source = "Mega"
                else:
                    print(f"   [OneDrive] Pas trouvé sur Mega, essai OneDrive...")
                    audio_files = onedrive.process_beat_files(titre_atomique, tmp_dir)
                    if audio_files:
                        audio_source = "OneDrive"

            if not audio_files:
                print(f"   [WARN] Audio introuvable.")
                if youtube_url not in already_missing_urls:
                    missing_audio.append({
                        "youtube_url":    youtube_url,
                        "title":          title,
                        "titre_atomique": titre_atomique,
                    })
                missing += 1
            else:
                print(f"   [OK] Audio trouvé sur {audio_source} — tagged: {audio_files.get('tagged', '—')}")

            if dry_run:
                print(f"   [DRY-RUN] Rien envoyé.")
                continue

            # 3. Envoyer vers Symfony
            status = push_to_symfony(
                youtube_url   = youtube_url,
                title         = title,
                tempo         = tempo,
                key           = key,
                gender        = gender,
                description   = desc,
                published_at  = snippet.get("publishedAt", ""),
                thumbnail_url = thumbnail_url,
                audio_files   = audio_files or {},
                tmp_dir       = tmp_dir,
            )

            if status == "created":
                print(f"   [SYMFONY] Créé en base ✓")
                created += 1
            elif status == "skipped":
                print(f"   [SYMFONY] Déjà présent, ignoré.")
                skipped += 1
            else:
                print(f"   [SYMFONY] Erreur lors de l'envoi.")

    save_missing_log(missing_audio)

    print("\n" + "=" * 60)
    if dry_run:
        print("Mode dry-run : aucun envoi effectué.")
    print(f"Créés : {created} | Ignorés : {skipped} | Audio manquant : {missing}")
    if missing_audio:
        print(f"Liste des beats sans audio : {MISSING_LOG_FILE}")


# ── Push vers Symfony ────────────────────────────────────────────────────────

def push_to_symfony(
    youtube_url: str, title: str, tempo: int, key: str, gender: str,
    description: str, published_at: str, thumbnail_url: str | None,
    audio_files: dict, tmp_dir: str,
) -> str:
    headers = {"X-Api-Key": SYMFONY_API_KEY}

    data_payload = {
        "youtube_url":  youtube_url,
        "title":        title,
        "tempo":        tempo,
        "key":          key,
        "gender":       gender,
        "description":  description,
        "published_at": published_at,
        "file_mp3":     audio_files.get("mp3", ""),
        "file_wav":     audio_files.get("wav", ""),
        "file_stems":   audio_files.get("stems", ""),
    }

    files = {"data": (None, json.dumps(data_payload), "application/json")}

    # Fichier tagged
    tagged_filename = audio_files.get("tagged")
    tagged_path     = os.path.join(tmp_dir, tagged_filename) if tagged_filename else None
    tagged_handle   = None

    if tagged_path and os.path.exists(tagged_path):
        tagged_handle         = open(tagged_path, "rb")
        files["tagged_file"]  = (tagged_filename, tagged_handle, "audio/mpeg")

    # Thumbnail
    thumbnail_handle = None
    if thumbnail_url:
        try:
            r = requests.get(thumbnail_url, timeout=15)
            if r.status_code == 200:
                thumb_path = os.path.join(tmp_dir, "thumbnail.jpg")
                with open(thumb_path, "wb") as f:
                    f.write(r.content)
                thumbnail_handle  = open(thumb_path, "rb")
                files["thumbnail"] = ("thumbnail.jpg", thumbnail_handle, "image/jpeg")
        except Exception:
            pass

    try:
        response = requests.post(SYMFONY_API_URL, headers=headers, files=files, timeout=60)
        body     = response.json()
        return body.get("status", "error")
    except Exception as e:
        print(f"   [ERROR] {e}")
        return "error"
    finally:
        if tagged_handle:
            tagged_handle.close()
        if thumbnail_handle:
            thumbnail_handle.close()


# ── Missing log ──────────────────────────────────────────────────────────────

def load_missing_log() -> list:
    if MISSING_LOG_FILE.exists():
        return json.loads(MISSING_LOG_FILE.read_text(encoding="utf-8"))
    return []


def save_missing_log(data: list) -> None:
    MISSING_LOG_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BHS Beats Sync Agent")
    parser.add_argument("--dry-run", action="store_true", help="Simuler sans envoyer")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
