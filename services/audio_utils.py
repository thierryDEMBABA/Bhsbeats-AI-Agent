import os
import subprocess
import sys

SHELL = sys.platform == "win32"

# Ajoute ffmpeg/ffprobe au PATH sur Windows
if SHELL:
    ffmpeg_dir = os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"
        r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
        r"\ffmpeg-8.1.2-full_build\bin"
    )
    if ffmpeg_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")


def get_duration(audio_path: str) -> float | None:
    """Retourne la durée en secondes d'un fichier audio via ffprobe."""
    cmd = f'ffprobe -v error -show_entries format=duration -of csv=p=0 "{audio_path}"'
    result = subprocess.run(cmd, capture_output=True, text=True, shell=SHELL, timeout=30)
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def create_tagged(
    untagged_path: str,
    output_path: str,
    tag_path: str,
    interval_seconds: int = 90,
) -> bool:
    """
    Crée un fichier MP3 tagué en superposant tag_path toutes les interval_seconds secondes
    sur untagged_path (WAV ou MP3).
    """
    duration = get_duration(untagged_path)
    if duration is None:
        return False

    # Positions du tag en millisecondes
    times_ms = list(range(0, int(duration * 1000), interval_seconds * 1000))
    if not times_ms:
        times_ms = [0]

    # Construit le filtre adelay pour chaque répétition
    filter_parts = []
    tag_refs = []
    for i, t in enumerate(times_ms):
        filter_parts.append(f"[1:a]adelay={t}|{t}[t{i}]")
        tag_refs.append(f"[t{i}]")

    n_inputs = 1 + len(tag_refs)
    filter_complex = (
        ";".join(filter_parts)
        + f";[0:a]{''.join(tag_refs)}amix=inputs={n_inputs}:duration=first:normalize=0[out]"
    )

    cmd = (
        f'ffmpeg -y -i "{untagged_path}" -i "{tag_path}" '
        f'-filter_complex "{filter_complex}" '
        f'-map "[out]" -codec:a libmp3lame -qscale:a 2 "{output_path}"'
    )
    result = subprocess.run(cmd, capture_output=True, shell=SHELL, timeout=600)
    return result.returncode == 0
