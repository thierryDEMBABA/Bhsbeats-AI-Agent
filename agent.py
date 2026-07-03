"""
BHS Beats Sync Agent
--------------------
Tourne sur ton PC local. Récupère les vidéos YouTube, les fichiers audio
depuis Mega ou OneDrive, puis pousse tout vers l'app Symfony hébergée.

Usage :
    python agent.py              # sync complète
    python agent.py --dry-run    # simulation sans envoi
    python agent.py --retry-only # réessaie uniquement les beats en attente
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 output on Windows to avoid UnicodeEncodeError with special chars
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import requests
from dotenv import load_dotenv

from services.youtube_service  import YouTubeService
from services.mega_service     import MegaService
from services.onedrive_service import OneDriveService

load_dotenv()

# ── Config ───────────────────────────────────────────────────────────────────

YOUTUBE_API_KEY        = os.environ["YOUTUBE_API_KEY"]
YOUTUBE_CHANNEL_HANDLE = os.getenv("YOUTUBE_CHANNEL_HANDLE", "@bhsbeats")
MEGA_BEATS_ROOT_PATH   = os.environ["MEGA_BEATS_ROOT_PATH"]
ONEDRIVE_BEATS_ROOT    = os.environ["ONEDRIVE_BEATS_ROOT_PATH"]
SYMFONY_API_URL        = os.environ["SYMFONY_API_URL"]
SYMFONY_API_KEY        = os.environ["SYMFONY_API_KEY"]
BEAT_TAG_PATH          = os.getenv("BEAT_TAG_PATH", "")
BEAT_TAG_INTERVAL      = int(os.getenv("BEAT_TAG_INTERVAL", "30"))

MISSING_LOG_FILE      = Path(__file__).parent / "missing_audio_beats.json"
RETRY_QUEUE_FILE      = Path(__file__).parent / "retry_queue.json"
CREATED_TAGGED_LOG    = Path(__file__).parent / "created_tagged_log.json"

# ── Main ─────────────────────────────────────────────────────────────────────

def main(dry_run: bool = False, retry_only: bool = False, retag: bool = False):
    print("=" * 60)
    print("BHS Beats Sync Agent")
    print("=" * 60)

    mega     = MegaService(MEGA_BEATS_ROOT_PATH)
    onedrive = OneDriveService(ONEDRIVE_BEATS_ROOT)

    # Vérifier la joignabilité du serveur avant de commencer
    if not dry_run and not is_server_reachable():
        print("\n[ERREUR] Serveur Symfony injoignable.")
        print("         Vérifie ta connexion ou que le serveur est démarré.")
        print("         Les beats déjà dans retry_queue.json seront réessayés au prochain lancement.")
        return

    with tempfile.TemporaryDirectory() as tmp_dir:
        print(f"\nDossier temporaire : {tmp_dir}")

        # Mode --retag : recrée tous les fichiers tagged avec le nouveau tag
        if retag:
            _process_retag(tmp_dir)
            return

        # Mode --retry-only : on ne refait pas YouTube, on réessaie juste la queue
        if retry_only:
            _process_retry_queue(tmp_dir, dry_run)
            return

        # Réinitialiser les logs à chaque sync complète
        save_json(MISSING_LOG_FILE, [])
        save_json(CREATED_TAGGED_LOG, [])

        youtube = YouTubeService(YOUTUBE_API_KEY, YOUTUBE_CHANNEL_HANDLE)

        print("\n[YouTube] Résolution du channel...")
        channel_id = youtube.get_channel_id()
        if not channel_id:
            print("ERREUR : channel introuvable.")
            return

        print(f"[YouTube] Channel ID : {channel_id}")
        print("[YouTube] Récupération des vidéos...")
        videos = youtube.fetch_all_videos(channel_id)
        print(f"[YouTube] {len(videos)} vidéo(s) trouvée(s)")

        missing_audio = load_json(MISSING_LOG_FILE)
        already_missing_urls = {e["youtube_url"] for e in missing_audio}

        created = skipped = missing = queued = 0

        for video in videos:
            snippet     = video["snippet"]
            youtube_url = f"https://www.youtube.com/watch?v={video['id']}"
            title       = snippet["title"]

            print(f"\n── {title}")

            titre_atomique  = YouTubeService.extract_titre_atomique(title)
            solded          = YouTubeService.is_sold(title)
            formatted_title = YouTubeService.format_title(title)
            if not titre_atomique:
                print("   [WARN] Pas de titre atomique entre guillemets, skip audio.")
            if solded:
                print("   [INFO] Beat marqué [SOLD]")

            desc          = snippet.get("description", "")
            tempo, key, gender = YouTubeService.parse_description(desc, title)
            thumbnail_url = (
                snippet.get("thumbnails", {}).get("maxres", {}).get("url")
                or snippet.get("thumbnails", {}).get("high", {}).get("url")
                or snippet.get("thumbnails", {}).get("default", {}).get("url")
            )

            # Recherche audio
            audio_files = audio_source = None
            if titre_atomique:
                print(f"   [Mega] Recherche de \"{titre_atomique}\"...")
                audio_files = mega.process_beat_files(
                    titre_atomique, tmp_dir, BEAT_TAG_PATH, BEAT_TAG_INTERVAL
                )
                if audio_files:
                    audio_source = "Mega"
                else:
                    print("   [OneDrive] Pas trouvé sur Mega, essai OneDrive...")
                    audio_files = onedrive.process_beat_files(
                        titre_atomique, tmp_dir, BEAT_TAG_PATH, BEAT_TAG_INTERVAL
                    )
                    if audio_files:
                        audio_source = "OneDrive"

            if not audio_files:
                print("   [WARN] Audio introuvable.")
                if youtube_url not in already_missing_urls:
                    missing_audio.append({
                        "youtube_url":    youtube_url,
                        "title":          title,
                        "titre_atomique": titre_atomique,
                    })
                missing += 1
            else:
                print(f"   [OK] Audio trouvé sur {audio_source} — tagged: {audio_files.get('tagged', '—')}")
                if audio_files.get("tagged_created"):
                    _log_created_tagged(titre_atomique, audio_source)

            if dry_run:
                print("   [DRY-RUN] Rien envoyé.")
                continue

            # tagged + wav obligatoires (contraintes d'intégrité)
            # wav garantit : untagged mp3 existe, stems existe (dossier si pas de rar/zip)
            af = audio_files or {}
            if not af.get("tagged") or not af.get("wav"):
                print("   [SKIP] tagged ou wav manquant — beat non envoyé.")
                missing += 1
                continue

            # Envoi vers Symfony
            beat_data = build_beat_data(
                youtube_url, formatted_title, tempo, key, gender,
                desc, snippet.get("publishedAt", ""),
                thumbnail_url, audio_files or {}, solded,
            )

            status, error = push_to_symfony(beat_data, audio_files or {}, tmp_dir)

            if status == "created":
                print("   [SYMFONY] Créé en base ✓")
                created += 1
            elif status == "skipped":
                print("   [SYMFONY] Déjà présent, ignoré.")
                skipped += 1
            elif status == "server_error":
                print("   [RETRY] Serveur injoignable — beat ajouté à la file d'attente.")
                enqueue_beat(beat_data, audio_files or {}, tmp_dir)
                queued += 1
            else:
                print(f"   [ERROR] {error}")

        save_json(MISSING_LOG_FILE, missing_audio)

        # Réessayer la queue après la sync normale
        if not dry_run:
            _process_retry_queue(tmp_dir, dry_run)

    print("\n" + "=" * 60)
    if dry_run:
        print("Mode dry-run : aucun envoi effectué.")
    print(f"Créés : {created} | Ignorés : {skipped} | Audio manquant : {missing} | En attente : {queued}")
    if missing_audio:
        print(f"Beats sans audio → {MISSING_LOG_FILE}")


def _process_retag(tmp_dir: str):
    """Recrée les fichiers tagged créés par l'agent (listés dans created_tagged_log.json)."""
    if not BEAT_TAG_PATH or not os.path.exists(BEAT_TAG_PATH):
        print("[RETAG] BEAT_TAG_PATH introuvable. Vérifie ton .env.")
        return

    log = load_json(CREATED_TAGGED_LOG)
    if not log:
        print("[RETAG] Aucun tagged créé par l'agent dans le journal (created_tagged_log.json).")
        return

    print(f"\n[RETAG] Tag utilisé : {BEAT_TAG_PATH}")
    print(f"[RETAG] Intervalle  : {BEAT_TAG_INTERVAL}s")
    print(f"[RETAG] {len(log)} beat(s) à re-tagger\n")

    mega     = MegaService(MEGA_BEATS_ROOT_PATH)
    onedrive = OneDriveService(ONEDRIVE_BEATS_ROOT)
    ok = ko = 0

    for entry in log:
        titre_atomique = entry["titre_atomique"]
        source         = entry.get("source", "")
        created_at     = entry.get("created_at", "")
        print(f"── {titre_atomique}  (créé le {created_at[:10]} sur {source})")

        if source == "Mega":
            done = mega.retag_beat(titre_atomique, BEAT_TAG_PATH, tmp_dir, BEAT_TAG_INTERVAL)
        elif source == "OneDrive":
            done = onedrive.retag_beat(titre_atomique, BEAT_TAG_PATH, tmp_dir, BEAT_TAG_INTERVAL)
        else:
            # Source inconnue : essai Mega puis OneDrive
            done = mega.retag_beat(titre_atomique, BEAT_TAG_PATH, tmp_dir, BEAT_TAG_INTERVAL)
            if not done:
                done = onedrive.retag_beat(titre_atomique, BEAT_TAG_PATH, tmp_dir, BEAT_TAG_INTERVAL)

        if done:
            print(f"   [RETAG] OK")
            ok += 1
        else:
            print(f"   [RETAG] Echec")
            ko += 1

    print(f"\n[RETAG] Terminé — OK : {ok} | Echec : {ko}")


def _process_retry_queue(tmp_dir: str, dry_run: bool):
    queue = load_json(RETRY_QUEUE_FILE)
    if not queue:
        return

    print(f"\n[RETRY] {len(queue)} beat(s) en attente de réenvoi...")

    if not is_server_reachable():
        print("[RETRY] Serveur toujours injoignable, on réessaiera plus tard.")
        return

    remaining = []
    for entry in queue:
        beat_data   = entry["beat_data"]
        audio_files = entry.get("audio_files", {})
        title       = beat_data.get("title", "?")

        print(f"\n── [RETRY] {title}")

        # Le fichier tagged n'est plus dans tmp_dir (session précédente)
        # On le retire de audio_files pour éviter une erreur de fichier manquant
        audio_files_without_tagged = {k: v for k, v in audio_files.items() if k != "tagged"}

        status, error = push_to_symfony(beat_data, audio_files_without_tagged, tmp_dir)

        if status == "created":
            print("   [SYMFONY] Créé en base ✓")
        elif status == "skipped":
            print("   [SYMFONY] Déjà présent, ignoré.")
        elif status == "server_error":
            print("   [RETRY] Toujours injoignable, remis en queue.")
            remaining.append(entry)
        else:
            print(f"   [ERROR] {error} — remis en queue.")
            remaining.append(entry)

    save_json(RETRY_QUEUE_FILE, remaining)
    retried = len(queue) - len(remaining)
    print(f"\n[RETRY] {retried} beat(s) réenvoyé(s), {len(remaining)} encore en attente.")


# ── Push vers Symfony ─────────────────────────────────────────────────────────

def build_beat_data(
    youtube_url, title, tempo, key, gender,
    description, published_at, thumbnail_url, audio_files, solded=False,
) -> dict:
    return {
        "youtube_url":    youtube_url,
        "title":          title,
        "tempo":          tempo,
        "key":            key,
        "gender":         gender,
        "description":    description,
        "published_at":   published_at,
        "thumbnail_url":  thumbnail_url,
        "file_mp3":       audio_files.get("mp3", ""),
        "file_wav":       audio_files.get("wav", ""),
        "file_stems":     audio_files.get("stems", ""),
        "solded":         solded,
    }


def push_to_symfony(beat_data: dict, audio_files: dict, tmp_dir: str) -> tuple[str, str]:
    """
    Retourne (status, error_message).
    status : "created" | "skipped" | "server_error" | "error"
    "server_error" = serveur injoignable → doit aller en retry queue
    """
    headers = {"X-Api-Key": SYMFONY_API_KEY}

    payload = {k: v for k, v in beat_data.items() if k != "thumbnail_url"}
    files   = {"data": (None, json.dumps(payload), "application/json")}

    tagged_handle = thumbnail_handle = None

    try:
        # Fichier tagged
        tagged_filename = audio_files.get("tagged")
        tagged_path     = os.path.join(tmp_dir, tagged_filename) if tagged_filename else None
        if tagged_path and os.path.exists(tagged_path):
            tagged_handle        = open(tagged_path, "rb")
            files["tagged_file"] = (tagged_filename, tagged_handle, "audio/mpeg")

        # Thumbnail
        thumbnail_url = beat_data.get("thumbnail_url")
        if thumbnail_url:
            try:
                r = requests.get(thumbnail_url, timeout=15)
                if r.status_code == 200:
                    thumb_path = os.path.join(tmp_dir, "thumbnail.jpg")
                    with open(thumb_path, "wb") as f:
                        f.write(r.content)
                    thumbnail_handle   = open(thumb_path, "rb")
                    files["thumbnail"] = ("thumbnail.jpg", thumbnail_handle, "image/jpeg")
            except Exception:
                pass

        response = requests.post(SYMFONY_API_URL, headers=headers, files=files, timeout=30)
        body     = response.json()
        return body.get("status", "error"), ""

    except requests.exceptions.ConnectionError:
        return "server_error", "Connection refused"
    except requests.exceptions.Timeout:
        return "server_error", "Timeout"
    except Exception as e:
        return "error", str(e)
    finally:
        if tagged_handle:
            tagged_handle.close()
        if thumbnail_handle:
            thumbnail_handle.close()


def is_server_reachable() -> bool:
    try:
        requests.get(SYMFONY_API_URL.replace("/api/beats/sync", ""), timeout=5)
        return True
    except Exception:
        return False


# ── Retry queue ───────────────────────────────────────────────────────────────

def enqueue_beat(beat_data: dict, audio_files: dict, tmp_dir: str) -> None:
    queue = load_json(RETRY_QUEUE_FILE)
    # On ne stocke pas le chemin du tagged (il sera dans tmp_dir qui sera supprimé)
    audio_files_without_tagged = {k: v for k, v in audio_files.items() if k != "tagged"}
    queue.append({"beat_data": beat_data, "audio_files": audio_files_without_tagged})
    save_json(RETRY_QUEUE_FILE, queue)


# ── Helpers JSON ─────────────────────────────────────────────────────────────

def _log_created_tagged(titre_atomique: str, source: str) -> None:
    log = load_json(CREATED_TAGGED_LOG)
    # Evite les doublons
    existing = {e["titre_atomique"] for e in log}
    if titre_atomique not in existing:
        log.append({
            "titre_atomique": titre_atomique,
            "source":         source,
            "created_at":     datetime.now(timezone.utc).isoformat(),
        })
        save_json(CREATED_TAGGED_LOG, log)


def load_json(path: Path) -> list:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return []


def save_json(path: Path, data: list) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BHS Beats Sync Agent")
    parser.add_argument("--dry-run",    action="store_true", help="Simuler sans envoyer")
    parser.add_argument("--retry-only", action="store_true", help="Réessayer uniquement la file d'attente")
    parser.add_argument("--retag",      action="store_true", help="Recréer tous les tagged avec le nouveau tag")
    args = parser.parse_args()
    main(dry_run=args.dry_run, retry_only=args.retry_only, retag=args.retag)
