#!/usr/bin/env python3
"""
Minimal YouTube Clipper - tylko wycinanie fragmentów wideo.
Bez napisów, TTS, obrazków AI, overlay'ów.
"""

import argparse
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List
from urllib.parse import urlparse, parse_qs

import google.generativeai as genai
from pydantic import BaseModel, Field
from youtube_transcript_api import YouTubeTranscriptApi
import yt_dlp
from moviepy.editor import VideoFileClip
from dotenv import load_dotenv

load_dotenv()


# ----------------------------- Setup & Config -----------------------------

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


@dataclass
class Settings:
    language: str
    output_dir: Path
    download_dir: Path
    max_clips_per_chunk: int = 3
    gemini_model_name: str = "gemini-2.5-pro"


@dataclass
class TranscriptLine:
    index: int
    start: float
    end: float
    text: str


class ViralClip(BaseModel):
    """Struktura odpowiedzi z Gemini (indeksy linii transkryptu)."""
    title: str = Field(..., description="Krótki, chwytliwy tytuł klipu")
    start_index: int = Field(..., description="Numer linii [X] w transkrypcie (start)", ge=1)
    end_index: int = Field(..., description="Numer linii [X] w transkrypcie (koniec)", ge=1)


@dataclass
class TimedClip:
    title: str
    start_time: float
    end_time: float


# ----------------------------- Utility Functions --------------------------

def extract_video_id(youtube_url: str) -> str:
    """Wyciąga ID wideo z URL YouTube."""
    youtube_url = youtube_url.strip()
    
    if re.fullmatch(r"[\w-]{11}", youtube_url):
        return youtube_url
    
    parsed = urlparse(youtube_url)
    if parsed.hostname in ("youtu.be",):
        return parsed.path.lstrip("/")
    
    qs = parse_qs(parsed.query)
    if "v" in qs:
        return qs["v"][0]
    
    m = re.search(r"(?:v=|\/)([\w-]{11})(?:\?|&|$)", youtube_url)
    if m:
        return m.group(1)
    
    raise ValueError(f"Nie można wyciągnąć video_id z URL: {youtube_url}")


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


# ----------------------------- Transcript Fetch --------------------------

def get_transcript(video_id: str, language: str = "en") -> List[TranscriptLine]:
    """Pobiera transkrypt z YouTube."""
    ytt_api = YouTubeTranscriptApi()
    fetched = ytt_api.fetch(video_id, languages=[language])
    transcript = fetched.to_raw_data()


    lines = []
    for idx, item in enumerate(transcript, start=1):
        text = item.get("text", "").replace("\n", " ").strip()
        if not text:
            continue
        start = float(item.get("start", 0.0))
        duration = float(item.get("duration", 0.0))
        end = start + duration
        
        lines.append(TranscriptLine(index=idx, start=start, end=end, text=text))
    
    return lines


# ----------------------------- Gemini Analysis --------------------------

def configure_gemini():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Brak GEMINI_API_KEY w .env")
    
    genai.configure(api_key=api_key)


def analyze_transcript_chunk(
    lines: List[TranscriptLine],
    model_name: str,
    max_clips_per_chunk: int
) -> List[ViralClip]:
    """Analizuje JEDEN chunk transkryptu przez Gemini."""
    configure_gemini()
    
    system_instruction = (
        "ROLE: Viral Content Editor — Aggressive Social Hook Specialist.\n"
        "TASK: From the provided transcript lines, pick best viral 30–50s short-form clips that trigger comments and debate.\n"
        "PRIORITIZE: conflict, bold claims, named entities, numbers, moral language, predictions, contradictions.\n"
        "EACH CLIP MUST START WITH A HOOK THAT TRIGGER EMOTION, that is really important.\n"
        "\n"
        "LANGUAGE RULES:\n"
        "- title in English.\n"
        "\n"
        "OUTPUT (STRICT JSON array only; no markdown, no comments, no trailing commas):\n"
        "IMPORTANT: start_index/end_index MUST be the EXACT numbers shown in brackets [X] in the transcript lines. Do NOT renumber per chunk.\n"
        "\n"
        "[\n"
        '  {"title": "string <= 60 chars", "start_index": int, "end_index": int}\n'
        "]\n"
        "\n"
        "\n"
        )

    transcript_text = "\n".join(
        [f"[{l.index}] ({l.start:.2f}-{l.end:.2f}) {l.text}" for l in lines]
    )
    
    prompt = (
        f"TRANSCRIPT CHUNK (use bracketed indices shown at line start):\n"
        f"CHUNK INDEX RANGE: [{lines[0].index}]..[{lines[-1].index}] (return indices from this range)\n"
        f"{transcript_text}\n\n"
        f"TASK: Select up to {max_clips_per_chunk} viral short-form clips (30–50s).\n"
        "OUTPUT FORMAT (STRICT):\n"
        "- Return a JSON array ONLY (no markdown, no comments, ASCII quotes).\n"
    )

    model = genai.GenerativeModel(model_name=model_name, system_instruction=system_instruction)
    
    try:
        response = model.generate_content(prompt)
        text = response.text.strip()
        
        # Usuń potencjalne ```json ... ```
        text = re.sub(r"^```json\s*", "", text)
        text = re.sub(r"^```\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        
        data = json.loads(text)
        if isinstance(data, dict):
            data = [data]
        
        clips = []
        for item in data:
            try:
                clips.append(ViralClip.model_validate(item))
            except Exception as e:
                logging.error(f"Błąd walidacji: {e}")
        
        return clips[:max_clips_per_chunk]
    except Exception as e:
        logging.error(f"Błąd Gemini: {e}")
        return []


def analyze_transcript_with_gemini(
    lines: List[TranscriptLine],
    model_name: str,
    max_clips_per_chunk: int = 3
) -> List[ViralClip]:
    """
    Dzieli długi transkrypt na chunki i analizuje każdy osobno.
    Dla 2h wywiadu zwróci ~10-20 klipów zamiast tylko 3.
    """
    MAX_LINES_PER_CHUNK = 200  # ~10-15 minut materiału
    all_clips = []
    
    total_chunks = (len(lines) + MAX_LINES_PER_CHUNK - 1) // MAX_LINES_PER_CHUNK
    logging.info(f"Dzielę transkrypt na {total_chunks} chunków (po {MAX_LINES_PER_CHUNK} linijek)")
    
    for chunk_idx in range(total_chunks):
        start_i = chunk_idx * MAX_LINES_PER_CHUNK
        end_i = min((chunk_idx + 1) * MAX_LINES_PER_CHUNK, len(lines))
        chunk = lines[start_i:end_i]
        
        logging.info(f"  Chunk {chunk_idx+1}/{total_chunks}: linie {start_i+1}-{end_i}")
        clips = analyze_transcript_chunk(chunk, model_name, max_clips_per_chunk)
        
        if clips:
            logging.info(f"  ✓ Znaleziono {len(clips)} klipów w tym chunku")
            all_clips.extend(clips)
    
    return all_clips


def resolve_clip_timing(clip: ViralClip, lines: List[TranscriptLine]) -> TimedClip:
    """Konwertuje indeksy z Gemini na czasy wideo.
    Oczekuje, że start_index/end_index to wartości z nawiasów [X] w transkrypcie.
    Jest odporne na 0-based vs 1-based i na indeksy poza zakresem.
    """
    if not lines:
        return TimedClip(title=clip.title, start_time=0.0, end_time=1.0)

    # mapa: indeks z nawiasu -> linia transkryptu
    idx_to_line = {l.index: l for l in lines}
    keys_sorted = sorted(idx_to_line.keys())

    def normalize_idx(x: int) -> int:
        # 1) najpierw spróbuj wprost
        if x in idx_to_line:
            return x
        # 2) jeśli model zwrócił 1-based, spróbuj przesunąć o -1
        if (x - 1) in idx_to_line:
            return x - 1
        # 3) fallback: przytnij do najbliższego istniejącego indeksu
        if x < keys_sorted[0]:
            return keys_sorted[0]
        if x > keys_sorted[-1]:
            return keys_sorted[-1]
        # 4) jeśli “w środku”, ale nie ma takiego klucza (rzadkie) – znajdź najbliższy
        # (tu prosto: wybierz max klucz <= x)
        lo = keys_sorted[0]
        for k in keys_sorted:
            if k <= x:
                lo = k
            else:
                break
        return lo

    start_k = normalize_idx(int(clip.start_index))
    end_k = normalize_idx(int(clip.end_index))

    # jeśli kolejność odwrócona, napraw
    if end_k < start_k:
        start_k, end_k = end_k, start_k

    start_line = idx_to_line[start_k]
    end_line = idx_to_line[end_k]

    # minimalnie 1s długości, żeby moviepy nie wybuchało na 0-length
    start_time = float(start_line.start)
    end_time = float(end_line.end)
    if end_time <= start_time:
        end_time = start_time + 1.0

    return TimedClip(
        title=clip.title,
        start_time=start_time,
        end_time=end_time,
    )


# ----------------------------- Download Video ----------------------------

def download_youtube_video(video_url: str, download_dir: Path) -> Path:
    """Pobiera wideo z YouTube do download_dir, zwraca ścieżkę pliku."""
    ensure_dir(download_dir)
    
    ydl_opts = {
        "outtmpl": str(download_dir / "%(id)s.%(ext)s"),
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=True)
        video_id = info.get("id")
        ext = info.get("ext", "mp4")
        candidate = download_dir / f"{video_id}.{ext}"
        
        # yt-dlp czasem zapisuje jako .mp4 po merge
        if not candidate.exists():
            candidate = download_dir / f"{video_id}.mp4"
        
        if not candidate.exists():
            raise FileNotFoundError(f"Nie znaleziono pobranego pliku dla {video_id}")
        
        return candidate


# ----------------------------- Clip Extraction ---------------------------

def extract_clip(
    video_path: Path,
    clip: TimedClip,
    output_dir: Path,
    clip_index: int
) -> Path:
    """Wycinanie klipu (bez dodatków)."""
    ensure_dir(output_dir)
    
    safe_title = re.sub(r"[^\w\-]+", "_", clip.title.strip())[:60]
    out_path = output_dir / f"{clip_index:02d}_{safe_title}.mp4"
    
    logging.info(f"  -> Wycinam klip {clip_index}: {clip.start_time:.2f}s - {clip.end_time:.2f}s ({clip.title})")
    
    with VideoFileClip(str(video_path)) as video:
        duration = video.duration
        
        start = max(0.0, min(clip.start_time, duration))
        end = max(start + 1.0, min(clip.end_time, duration))
        
        sub = video.subclip(start, end)
        sub.write_videofile(
            str(out_path),
            codec="libx264",
            audio_codec="aac",
            verbose=False,
            logger=None
        )
    
    return out_path


# ----------------------------- Main Processing ---------------------------

def process_video(video_url: str, settings: Settings):
    video_id = extract_video_id(video_url)
    logging.info(f"Przetwarzam wideo: {video_id}")
    
    # 1. Pobierz transkrypt
    lines = get_transcript(video_id, settings.language)
    logging.info(f"Pobrano transkrypt: {len(lines)} linijek")
    
    # 2. Analiza przez Gemini (chunking)
    logging.info("Analizuję transkrypt przez Gemini...")
    clips = analyze_transcript_with_gemini(lines, settings.gemini_model_name, settings.max_clips_per_chunk)
    
    if not clips:
        logging.warning("Gemini nie znalazł żadnych klipów")
        return
    
    timed_clips = [resolve_clip_timing(c, lines) for c in clips]
    
    # 3. Pobierz wideo
    logging.info("Pobieram wideo...")
    try:
        video_path = download_youtube_video(video_url, settings.download_dir)
        logging.info(f"Pobrano wideo: {video_path}")
    except Exception as e:
        logging.error(f"Błąd pobierania: {e}")
        return
    
    # 4. Wytnij klipy
    for idx, clip in enumerate(timed_clips, start=1):
        try:
            extract_clip(video_path, clip, settings.output_dir, idx)
        except Exception as e:
            logging.error(f"Błąd wycinania klipu {idx}: {e}")


def main():
    setup_logging()
    
    parser = argparse.ArgumentParser(description="Minimal YouTube Clipper")
    parser.add_argument("--interviews-file", default="wywiady.txt", help="Plik z linkami YouTube")
    parser.add_argument("-l", "--language", default="en", help="Język transkryptu (en/pl)")
    parser.add_argument("-o", "--output-dir", default="do_obrobki", help="Folder na klipy")
    parser.add_argument("-d", "--download-dir", default="downloads", help="Folder na pobrane wideo")
    parser.add_argument("-n", "--max-clips", type=int, default=4, help="Max klipów na chunk transkryptu (100 linijek)")
    args = parser.parse_args()
    
    # Sprawdź plik z linkami
    interviews_path = Path(args.interviews_file)
    if not interviews_path.exists():
        logging.error(f"Plik {args.interviews_file} nie istnieje!")
        logging.info("Utwórz plik wywiady.txt i dodaj linki YouTube (jeden na linię)")
        return
    
    settings = Settings(
        language=args.language,
        output_dir=Path(args.output_dir),
        download_dir=Path(args.download_dir),
        max_clips_per_chunk=args.max_clips,
    )
    
    # Wczytaj linki
    with open(interviews_path, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip()]
    
    logging.info(f"Znaleziono {len(urls)} filmów do przetworzenia")
    
    for i, url in enumerate(urls, start=1):
        logging.info("\n" + "=" * 60)
        logging.info(f"Film {i}/{len(urls)}: {url}")
        logging.info("=" * 60)
        
        try:
            process_video(url, settings)
        except Exception as e:
            logging.error(f"Błąd przetwarzania {url}: {e}")


if __name__ == "__main__":
    main()
