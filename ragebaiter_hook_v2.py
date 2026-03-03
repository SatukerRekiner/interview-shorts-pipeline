#!/usr/bin/env python3
"""
Folder of pre-cut RAW .mp4 clips -> subtitles + AI overlays + commentary interrupts + ragebait outro -> final vertical videos.

KEY CHANGE vs original interview_ragebait_v3.py:
- Gemini receives the MP4 video as the PRIMARY input to plan overlays + commentary + ragebait.
- No start/end selection (clips are already cut).
- Transcript (AssemblyAI word-level) is used mainly for subtitles and to constrain overlay keywords.

This file is standalone.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import numpy as np
import requests
import hashlib
import random
import assemblyai as aai
import google.generativeai as genai
from pydantic import BaseModel, Field, ValidationError

# ImageMagick path for Windows (must be set BEFORE importing moviepy TextClip)
if os.name == "nt":
    os.environ.setdefault(
        "IMAGEMAGICK_BINARY",
        r"C:\\Program Files\\ImageMagick-7.1.1-Q16-HDRI\\magick.exe",
    )

from moviepy.editor import (
    VideoFileClip,
    TextClip,
    CompositeVideoClip,
    ColorClip,
    ImageClip,
    AudioFileClip,
    concatenate_videoclips,
)
from moviepy.audio.AudioClip import AudioClip, concatenate_audioclips, CompositeAudioClip
import moviepy.audio.fx.all as afx

from PIL import Image as PILImage
from PIL import ImageFilter
from dotenv import load_dotenv

load_dotenv()

# --- Pillow / moviepy compatibility fix ---
try:
    import PIL.Image as _PIL_Image
    if not hasattr(_PIL_Image, "ANTIALIAS"):
        try:
            from PIL import Image
            _PIL_Image.ANTIALIAS = Image.Resampling.LANCZOS
        except Exception:
            pass
except Exception:
    pass
# --- end fix ---


# ==========================================================
#   RĘCZNIE USTAW TUTAJ (PRZYKŁADOWE NAZWY)
# ==========================================================

INPUT_DIR = Path(r"./do_obrobki")          # tu wrzucasz pre-cut raw .mp4
OUTPUT_DIR = Path(r"./gotowe")             # tu zapisują się finalne klipy
WORK_DIR = Path(r"./work")                # tts + images + temp
GEMINI_CONFIG_PATH = Path(r"./gemini_config.json")  # {"api_key":"..."} lub ustaw GOOGLE_API_KEY
MUSIC_DIR = Path(r"./music")      # albo Path("/music")
BG_MUSIC_VOLUME = 0.06           # startowo 0.03–0.08
BG_MUSIC_FADE = 0.25             # sekundy fade in/out (opcjonalne)


MAX_FILES = 30  # 0 = wszystkie, np. 10 = tylko pierwsze 10, 40-44 to bedzie 10k tokenów

# Gemini models
GEMINI_VIDEO_MODEL = "gemini-2.5-pro"          # plan z wideo
GEMINI_IMAGE_MODEL = "gemini-2.5-flash-image"  # obrazki

# Commentary tuning
COMMENTARY_MAX = 3
COMMENTARY_BG_VOLUME = 0.2             #TODO bylo 0.105
COMMENTARY_BLUR_RADIUS = 5.0            #TODO bylo 10.0
COMMENTARY_TTS_GAIN = 1.35
KEEP_CAPTIONS_DURING_COMMENTARY = False

# Overlays tuning
DEFAULT_OVERLAY_DURATION = 1.5  # sekundy na jeden obrazek
# ==========================================================

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

def load_gemini_api_key(config_path: Path) -> str:
    # 1. Sprawdź ENV
    env_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if env_key:
        return env_key

    # 2. Sprawdź plik JSON
    if not config_path.exists():
        if env_key:
            return env_key
        raise FileNotFoundError(f"Gemini config file not found: {config_path} and no ENV key set.")

    data = json.loads(config_path.read_text(encoding="utf-8"))
    key = data.get("api_key") or data.get("GOOGLE_API_KEY") or data.get("GEMINI_API_KEY")
    if not key:
        raise ValueError(f"No api_key found in {config_path}. Put {{'api_key': '...'}} or set env.")
    return key

def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


# ----------------------------- Imagen -------------------------------------

@dataclass
class Settings:
    language: str
    output_dir: Path
    download_dir: Path
    gemini_config_path: Path
    max_clips: int = 3
    gemini_model_name: str = "gemini-2.5-pro"     # Szybki model do tekstu gemini-3-pro-preview, gemini-2.5-pro
    # ZMIANA: Używamy stabilnego modelu, który nie wyrzuca błędu 404
    imagen_model_name: str = "gemini-2.5-flash-image" #imagen-3.0-generate-002 , imagen-4.0-generate-001

    # Commentary interrupts (middle-of-clip): blur + background ducking + TTS + big center karaoke
    commentary_min: int = 2
    commentary_max: int = 4
    commentary_min_sec: float = 3.0
    commentary_max_sec: float = 8.0
    commentary_background_volume_factor: float = 0.15
    commentary_blur_radius: float = 10.0
    commentary_tts_gain: float = 1.35
    keep_captions_during_commentary: bool = True

@dataclass
class TimedCommentaryBeat:
    start_time: float  # seconds relative to the start of the clip
    text: str
    anchor_index: int = -1

@dataclass
class CaptionSegment:
    start: float
    end: float
    text: str

@dataclass
class VisualOverlay:
    """Stores info where to put an image."""
    asset_path: Path
    start_time: float
    duration: float


# ----------------------------- Utility helpers ----------------------------

def generate_ai_image(
    keyword: str,
    full_clip_text: str,
    person: str,
    out_dir: Path,
    api_key: str,
    model_name: str = "gemini-2.5-flash-image",
) -> Optional[Path]:
    """Generate AI image using Google Gemini image model via REST API (inlineData base64)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", keyword)[:80].strip("_") or "image"
    out_path = out_dir / f"{safe}_{int(time.time())}.png"

    logging.info(f"Generating Gemini Image art for: '{keyword}'...")

    
    prompt = (
        f"PHOTOREALISTIC 8k raw footage style. "
        f"A vertical 9:16 shot appropriate for a high-budget documentary. "
        f"SUBJECT: {keyword} . "
        f"CONTEXT: {full_clip_text[:800]}. "  #TODO BYLO 800
        "MOOD: Dramatic, gritty, high contrast, cinematic lighting. "
        "STYLE: Shot on 35mm film, grain, realistic textures. "
        "NO text. "
        "Make it look like a real photograph from a news archive."
        #f"If '{keyword}' is abstract, make a picture from the CONTEXT"
        f"include {person} lookalike in the scene without stealing focus from the SUBJECT." #
        #f"DO NOT include other people than {person} in the image"
        "COMPOSITION: centered or rule-of-thirds hero subject, shallow depth of field, natural perspective. "
        #"DO: show concrete objects/places tied to the claim; include relevant secondary details suggested by the excerpt. "
        "If PERSON is a public figure, avoid a recognizable facial likeness; depict them generically (e.g., back/side view) or symbolically. "
        #"If PERSON is a public figure, avoid a recognizable facial likeness. "
        "OUTPUT: one polished image."
    )


    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"

    # KLUCZOWA ZMIANA: enums w REST są TEXT/IMAGE (duże litery), a nie "Image".
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {"aspectRatio": "9:16"},
        },
    }

    #NOWE----------------------------------------------------------
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.post(url, json=payload, timeout=120)
            
            data = r.json()

            candidates = data.get("candidates") or []
            if not candidates:
                pf = data.get("promptFeedback") or data.get("prompt_feedback") or {}
                print(f"Próba {attempt}/{max_attempts} nie powiodła się: brak candidates")

                continue

            cand0 = candidates[0] or {}
            finish = cand0.get("finishReason") or cand0.get("finish_reason")
            safety = cand0.get("safetyRatings") or cand0.get("safety_ratings")
            parts = (cand0.get("content") or {}).get("parts") or []

            # Wyciągnij inlineData (czasem camelCase, czasem snake_case w różnych wrapperach)
            b64 = None
            for p in parts:
                inline = p.get("inlineData") or p.get("inline_data") or {}
                # czasem mimeType bywa pominięty, więc nie uzależniaj od niego
                if inline.get("data"):
                    b64 = inline["data"]
                    break
                    
            if not b64:
                print(f"Próba {attempt}/{max_attempts} nie powiodła się: brak b64")
                time.sleep(2)
                continue

            img_bytes = base64.b64decode(b64)
            out_path.write_bytes(img_bytes)
            print(f"Sukces za razem {attempt}")
            return out_path

        except Exception as e:
            print(f"Próba {attempt}/{max_attempts} nie powiodła się: {e}")
            logging.error(f" oppa tutaj Imagen generation failed: {e}")
            return None
    
    #STARE----------------------------------------------------------------------------
    # try:
    #     r = requests.post(url, json=payload, timeout=120)
    #     if r.status_code != 200:
    #         logging.error(f"Imagen request failed {r.status_code}: {r.text[:500]}")
    #         return None

    #     data = r.json()

    #     # Debug: jeśli brak kandydatów, często jest promptFeedback z powodem
    #     candidates = data.get("candidates") or []
    #     if not candidates:
    #         pf = data.get("promptFeedback") or data.get("prompt_feedback") or {}
    #         logging.error(f"No candidates returned. promptFeedback={pf}")
    #         return None

    #     cand0 = candidates[0] or {}
    #     finish = cand0.get("finishReason") or cand0.get("finish_reason")
    #     safety = cand0.get("safetyRatings") or cand0.get("safety_ratings")
    #     parts = (cand0.get("content") or {}).get("parts") or []

    #     # Wyciągnij inlineData (czasem camelCase, czasem snake_case w różnych wrapperach)
    #     b64 = None
    #     for p in parts:
    #         inline = p.get("inlineData") or p.get("inline_data") or {}
    #         # czasem mimeType bywa pominięty, więc nie uzależniaj od niego
    #         if inline.get("data"):
    #             b64 = inline["data"]
    #             break


    #     # if not b64:
    #     #     # --- MINI DEBUG DUMP (bez base64) ---
    #     #     def _redact(obj):
    #     #         if isinstance(obj, dict):
    #     #             out = {}
    #     #             for k, v in obj.items():
    #     #                 if k == "data" and isinstance(v, str) and len(v) > 200:
    #     #                     out[k] = f"<base64 len={len(v)}>"
    #     #                 else:
    #     #                     out[k] = _redact(v)
    #     #             return out
    #     #         if isinstance(obj, list):
    #     #             return [_redact(x) for x in obj]
    #     #         return obj

    #     #     # zredaguj cały response i pokaż ważne fragmenty
    #     #     red = _redact(data)
    #     #     pf = red.get("promptFeedback")
    #     #     cand0_red = (red.get("candidates") or [{}])[0]

    #     #     # krótki, czytelny log do konsoli:
    #     #     logging.error(
    #     #         "Gemini returned no image. "
    #     #         f"finishReason={finish} safetyRatings={safety} "
    #     #         f"promptFeedback={pf} "
    #     #         f"cand0_keys={list((cand0_red or {}).keys())}"
    #     #     )
    #     #     logging.error("Gemini cand0 (redacted)=%s", json.dumps(cand0_red, ensure_ascii=False)[:4000])

    #     #     return None


    #     if not b64:
    #         # pomocniczy log żebyś widział co model zwrócił
    #         texts = [p.get("text") for p in parts if isinstance(p, dict) and p.get("text")]
    #         logging.error(
    #             "No image data returned by Gemini image model. "
    #             f"finishReason={finish} safetyRatings={safety} textParts={texts[:1]}"
    #         )
    #         return None

    #     img_bytes = base64.b64decode(b64)
    #     out_path.write_bytes(img_bytes)
    #     return out_path

    # except Exception as e:
    #     logging.error(f"Imagen generation failed: {e}")
    #     return None

def pick_random_music_file(music_dir: Path) -> Optional[Path]:
    if not music_dir or not music_dir.exists():
        return None

    exts = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
    candidates = [p for p in music_dir.iterdir() if p.is_file() and p.suffix.lower() in exts]
    if not candidates:
        return None
    return random.choice(candidates)

# ----------------------------- Transcript & Gemini -------------------------

def transcribe_clip_with_assemblyai(clip: VideoFileClip, label: str) -> List[CaptionSegment]:
    """
    Word-level transcription using AssemblyAI.

    IMPORTANT (Windows/FFMPEG): we export WAV (PCM) to avoid missing AAC encoders
    like libfdk_aac which can break .m4a exports and result in empty captions.
    """
    if not clip.audio or not aai.settings.api_key:
        return []

    with tempfile.TemporaryDirectory() as tmpdir:
        apath = Path(tmpdir) / f"{label}.wav"
        try:
            # PCM WAV is the most compatible; 16kHz is enough for speech.
            clip.audio.write_audiofile(
                str(apath),
                fps=16000,
                nbytes=2,
                codec="pcm_s16le",
                verbose=False,
                logger=None
            )
        except Exception as e:
            logging.error(f"Audio export for AssemblyAI failed: {e}")
            return []

        try:
            t = aai.Transcriber().transcribe(str(apath))
            segments: List[CaptionSegment] = []
            if not getattr(t, "words", None):
                return []
            for w in t.words:
                segments.append(CaptionSegment(w.start/1000.0, w.end/1000.0, w.text))
            return segments
        except Exception as e:
            logging.error(f"AssemblyAI error: {e}")
            return []

def transcribe_audio_words_assemblyai(audio_path: Path) -> List[CaptionSegment]:
    if not aai.settings.api_key:
        return []
    try:
        t = aai.Transcriber().transcribe(str(audio_path))
        return [CaptionSegment(w.start/1000.0, w.end/1000.0, w.text) for w in t.words]
    except Exception as e:
        logging.error(f"AssemblyAI error: {e}")
        return []

def generate_elevenlabs_tts(text: str, out_dir: Path, label: str) -> Optional[Path]:
    """Generuje plik audio (.mp3) z ElevenLabs. Wymaga ENV: ELEVENLABS_API_KEY + ELEVENLABS_VOICE_ID."""
    text = _normalize_whitespace(text)
    if not text:
        return None

    api_key = os.getenv("ELEVENLABS_API_KEY")
    voice_id = os.getenv("ELEVENLABS_VOICE_ID")
    model_id = os.getenv("ELEVENLABS_MODEL_ID") or "eleven_turbo_v2_5"

    if not api_key or not voice_id:
        logging.warning("ElevenLabs disabled: set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID to enable TTS.")
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "_", label).strip("_")
    h = hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
    out_path = out_dir / f"{safe_label[:60]}_{h}_ragebait.mp3"

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "accept": "audio/mpeg",
        "content-type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": float(os.getenv("ELEVENLABS_STABILITY") or 0.5),
            "similarity_boost": float(os.getenv("ELEVENLABS_SIMILARITY_BOOST") or 0.75),
            "style": float(os.getenv("ELEVENLABS_STYLE") or 0.0),
            "use_speaker_boost": True,
        },
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=90)
        if r.status_code != 200:
            logging.error(f"ElevenLabs error {r.status_code}: {r.text}")
            return None
        out_path.write_bytes(r.content)
        return out_path
    except Exception as e:
        logging.error(f"ElevenLabs request failed: {e}")
        return None

def make_karaoke_segments_for_text(text: str, start_time: float, total_duration: float) -> List[CaptionSegment]:
    """Fallback karaoke segments if no word timings available."""
    words = [w for w in re.split(r"\s+", _normalize_whitespace(text)) if w]
    if not words or total_duration <= 0:
        return []
    per = max(0.05, float(total_duration) / len(words))
    t = float(start_time)
    out = []
    for w in words:
        out.append(CaptionSegment(t, t + per, w))
        t += per
    return out


# ----------------------------- Visual overlays ----------------------------

def match_visuals_to_transcript(
    keywords: List[str],
    captions: Sequence[CaptionSegment],
    full_clip_text: str,
    person: str,
    settings: Settings
) -> List[VisualOverlay]:
    """
    Match each keyword to the earliest occurrence in captions (word-level), generate AI image,
    and schedule overlay at that moment.
    """
    if not keywords:
        return []
    if not captions:
        return []

    key = load_gemini_api_key(settings.gemini_config_path)
    overlays: List[VisualOverlay] = []
    used_times = set()

    out_img_dir = settings.download_dir / "images"

    for kw in keywords:
        kw_norm = kw.lower().strip()
        if not kw_norm:
            continue
        match = None
        for cap in captions:
            if cap.text.lower().strip() == kw_norm:
                match = cap
                break

        if not match:
            continue

        st = float(match.start)
        # avoid stacking overlays at exact same time
        if round(st, 2) in used_times:
            continue
        used_times.add(round(st, 2))

        img_path = generate_ai_image(
            keyword=kw,
            full_clip_text=full_clip_text,
            person=person,
            out_dir=out_img_dir,
            api_key=key,
            model_name=settings.imagen_model_name,
        )
        if not img_path:
            continue

        overlays.append(VisualOverlay(
            asset_path=img_path,
            start_time=st,
            duration=2.5,
        ))
    return overlays


# ----------------------------- Blur helper --------------------------------

def _blur_frame_pil(frame: np.ndarray, radius: float = 10.0) -> np.ndarray:
    im = PILImage.fromarray(frame)
    im = im.filter(ImageFilter.GaussianBlur(radius=float(radius)))
    return np.array(im)

def add_background_music_to_clip(
    clip: CompositeVideoClip,
    music_dir: Path,
    volume: float = 0.06,
    fade: float = 0.25,
) -> CompositeVideoClip:
    mpath = pick_random_music_file(music_dir)
    if not mpath:
        return clip

    try:
        bg_src = AudioFileClip(str(mpath))

        # losowy offset żeby nie zaczynało zawsze od intro
        if bg_src.duration and float(bg_src.duration) > 2.0:
            import random
            off = random.uniform(0.0, max(0.0, float(bg_src.duration) - 1.0))
            bg_src = bg_src.subclip(off)

        bg = bg_src.fx(afx.audio_loop, duration=float(clip.duration))
        bg = bg.fx(afx.volumex, float(volume))

        if fade and float(fade) > 0:
            bg = bg.fx(afx.audio_fadein, float(fade)).fx(afx.audio_fadeout, float(fade))

        base = clip.audio if clip.audio else AudioClip(lambda t: 0.0, duration=float(clip.duration), fps=44100)
        mixed = CompositeAudioClip([base, bg])

        logging.info(f"BG music (late): {mpath.name}")
        return clip.set_audio(mixed)

    except Exception as e:
        logging.error(f"Late BG music failed: {e}")
        return clip

# ----------------------------- Ragebait Outro ------------------------------

def _merge_intervals(intervals: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    clean = [(float(a), float(b)) for a, b in intervals if b > a]
    if not clean:
        return []
    clean.sort(key=lambda x: x[0])
    merged = [clean[0]]
    for a, b in clean[1:]:
        la, lb = merged[-1]
        if a <= lb:
            merged[-1] = (la, max(lb, b))
        else:
            merged.append((a, b))
    return merged

def _attenuate_audio_in_intervals(
    audio: AudioClip,
    intervals: List[Tuple[float, float]],
    factor: float,
    total_duration: float,
) -> AudioClip:
    """
    Returns audio where the original track is ducked to `factor` inside `intervals`,
    and unchanged outside. Implemented by slicing + concatenation (efficient for few intervals).
    """
    intervals = _merge_intervals(intervals)
    if not intervals:
        return audio

    pieces = []
    cur = 0.0
    for a, b in intervals:
        a = max(0.0, min(float(a), total_duration))
        b = max(0.0, min(float(b), total_duration))
        if b <= a:
            continue
        if a > cur + 1e-6:
            pieces.append(audio.subclip(cur, a))
        pieces.append(audio.subclip(a, b).fx(afx.volumex, factor))
        cur = b
    if cur < total_duration - 1e-6:
        pieces.append(audio.subclip(cur, total_duration))

    if pieces:
        return concatenate_audioclips(pieces)
    return audio

def _schedule_commentary_beats(
    beats: List[TimedCommentaryBeat],
    main_duration: float,
    tts_durations: List[float],
    hard_end_time: float,
    min_gap: float = 0.6,
    min_start: float = 1.0,
) -> List[Tuple[TimedCommentaryBeat, float, float]]:
    """
    Given proposed beats (with start_time) and corresponding TTS durations,
    returns a feasible schedule: (beat, start, end) that doesn't overlap and fits before hard_end_time.
    """
    if not beats:
        return []

    paired = list(zip(beats, tts_durations))
    paired.sort(key=lambda x: float(x[0].start_time))

    scheduled = []
    prev_end = 0.0
    for beat, dur in paired:
        dur = float(dur or 0.0)
        if dur <= 0.05:
            continue

        latest_start = max(0.0, float(hard_end_time) - dur)
        start = float(beat.start_time)
        start = max(min_start, min(start, latest_start))

        if scheduled:
            start = max(start, prev_end + min_gap)

        end = start + dur
        if end > hard_end_time + 1e-6:
            start = max(min_start, float(hard_end_time) - dur)
            if scheduled and start < prev_end + min_gap:
                continue
            end = start + dur
            if end > hard_end_time + 1e-6:
                continue

        scheduled.append((beat, start, end))
        prev_end = end

    return scheduled



def build_vertical_video(
    base_clip: VideoFileClip,
    title: str,
    captions: Sequence[CaptionSegment],
    overlays: List[VisualOverlay],
    subtitle_exclude_intervals: Optional[List[Tuple[float, float]]] = None,
    show_subtitles_during_commentary: bool = True,
    target_width: int = 1080,
    target_height: int = 1920,
    sfx_path: Optional[Path] = None,
    music_dir: Optional[Path] = None,
    music_volume: float = BG_MUSIC_VOLUME,
    music_fade: float = BG_MUSIC_FADE,
    title_slide_in_sec: float = 0.7,
    title_sfx_path: Optional[Path] = None,
    title_sfx_volume: float = 0.9,
) -> CompositeVideoClip:

    # 1. Tło i Wideo (Baza - najniżej)
    bg = ColorClip(size=(target_width, target_height), color=(0, 0, 0)).set_duration(base_clip.duration)
    video = base_clip.resize(width=target_width).set_position("center")

    layers = [bg, video]


    # 2. Tytuł (slide-in)
    if title:
        try:
            txt = TextClip(
                title,
                fontsize=70,
                color="white",
                method="caption",
                size=(target_width - 160, None),

                # jeśli chcesz mocniej “tiktokowo” i czytelniej, odkomentuj:
                # stroke_color="black",
                # stroke_width=8,
            ).set_duration(base_clip.duration)

            # --- easing (tiktokowy “wjazd” z lekkim charakterem) ---
            def ease_out_back(x: float) -> float:
                # x w [0..1]
                c1 = 1.70158
                c3 = c1 + 1
                return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2

            final_y = 400
            tw, th = txt.size
            final_x = (target_width - tw) / 2
            start_x = -tw - 60  # start poza ekranem po lewej
            dur = max(0.05, float(title_slide_in_sec))

            def title_pos(t: float):
                if t <= 0:
                    return (start_x, final_y)
                if t < dur:
                    p = ease_out_back(t / dur)
                    x = start_x + (final_x - start_x) * p
                    return (x, final_y)
                return (final_x, final_y)

            txt = txt.set_position(title_pos).crossfadein(0.08)
            layers.append(txt)
        except Exception:
            pass

    # 3. Warstwa Obrazów AI (z efektem Ken Burns / Zoom)
    for item in overlays:
        try:
            if str(item.asset_path).lower().endswith((".jpg", ".jpeg", ".png")):

                #Wczytujemy obrazek, ustawiamy czas
                img_clip = (
                    ImageClip(str(item.asset_path))
                    .set_start(item.start_time)
                    .set_duration(item.duration)

                    # Najpierw dopasuj do szerokości ekranu (stan początkowy)
                    .resize(width=target_width)

                    # TERAZ dodajemy efekt ZOOM (Ken Burns):
                    # lambda t: 1 + 0.04 * t -> Zaczyna od 100% (1.0) i rośnie o 4% na sekundę
                    .resize(lambda t: 1 + 0.04 * t)

                    .set_position(lambda t: ('center', 'center'))
                    #.set_position('center')
                    #.set_position(("center", "center"))
                    .crossfadein(0.3)
                    .crossfadeout(0.3)
                )
                layers.append(img_clip)
        except Exception as e:
            logging.error(f"Blad przy overlay (Ken Burns): {e}")

    # 4. Napisy Karaoke (jak v2)
    overlay_trigger_times = {round(ov.start_time, 2) for ov in overlays}
    subtitle_y = target_height - 600

    def _in_exclude(seg_start: float, seg_end: float) -> bool:
        if show_subtitles_during_commentary:
            return False
        if not subtitle_exclude_intervals:
            return False
        for a, b in subtitle_exclude_intervals:
            a = float(a); b = float(b)
            if seg_start < b and seg_end > a:
                return True
        return False

    for seg in captions:
        if seg.end <= seg.start:
            continue

        if _in_exclude(float(seg.start), float(seg.end)):
            continue

        is_trigger_word = round(seg.start, 2) in overlay_trigger_times
        subtitle_color = "red" if is_trigger_word else "yellow"

        try:
            font = "Arial-Bold" if "Arial-Bold" in TextClip.list('font') else "Arial"
            txt = (
                TextClip(
                    seg.text,
                    fontsize=90,
                    font=font,
                    color=subtitle_color,
                    stroke_color="black",
                    stroke_width=4,
                    method="caption",
                )
                .set_start(seg.start)
                .set_end(seg.end)
                .set_position(("center", subtitle_y))
            )
            layers.append(txt)
        except Exception:
            pass

    comp = CompositeVideoClip(layers, size=(target_width, target_height))

    # ==========================================================
    # NOWE: SFX przy obrazkach
    # ==========================================================
    base_audio = base_clip.audio if base_clip.audio else AudioClip(lambda t: 0.0, duration=float(base_clip.duration), fps=44100)

    audio_tracks = [base_audio]

    # >>> INTRO WOOSH dla tytułu (start 0.0s)
    if title and title_sfx_path:
        try:
            if Path(title_sfx_path).exists():
                whoosh = AudioFileClip(str(title_sfx_path))
                # przytnij, żeby nie ciągnął się za długo (opcjonalnie)
                whoosh = whoosh.subclip(0, min(1.0, float(whoosh.duration)))
                whoosh = whoosh.fx(afx.volumex, float(title_sfx_volume)).set_start(0.0)
                whoosh = whoosh.fx(afx.audio_fadeout, 0.05)
                audio_tracks.append(whoosh)
        except Exception as e:
            logging.error(f"Nie udalo sie dodac TITLE woosh: {e}")


    # ==========================================================
    # NOWE: losowy podkład muzyczny (BG)
    # ==========================================================
    if music_dir:
        mpath = pick_random_music_file(music_dir)
        if mpath:
            try:
                bg_src = AudioFileClip(str(mpath))

                # (opcjonalnie) losowy offset, żeby nie zaczynało się zawsze od intro
                if bg_src.duration and float(bg_src.duration) > 2.0:
                    off = random.uniform(0.0, max(0.0, float(bg_src.duration) - 1.0))
                    bg_src = bg_src.subclip(off)

                # loop do długości klipu
                bg = bg_src.fx(afx.audio_loop, duration=float(base_clip.duration))

                # ściszenie
                bg = bg.fx(afx.volumex, float(music_volume))

                # delikatne fade in/out
                if music_fade and float(music_fade) > 0:
                    bg = bg.fx(afx.audio_fadein, float(music_fade)).fx(afx.audio_fadeout, float(music_fade))

                audio_tracks.append(bg)
                logging.info(f"BG music: {mpath.name}")
            except Exception as e:
                logging.error(f"BG music failed: {e}")
    # ==========================================================


    if sfx_path and os.path.exists(sfx_path) and overlays:
        # Wczytaj dźwięk raz
        try:
            click_sound_src = AudioFileClip(str(sfx_path))
            # Opcjonalnie: ścisz dźwięk kliknięcia, żeby nie zagłuszał mowy (np. 50%)
            click_sound_src = click_sound_src.fx(afx.volumex, 0.5)

            for item in overlays:
                # Stwórz kopię dźwięku dla każdego obrazka
                click_instance = click_sound_src.set_start(item.start_time)
                audio_tracks.append(click_instance)
        except Exception as e:
            logging.error(f"Nie udalo sie dodac SFX: {e}")

    # Miksowanie wszystkiego razem
    final_mixed_audio = CompositeAudioClip(audio_tracks)
    comp = comp.set_audio(final_mixed_audio)
    # ==========================================================
    return comp


# ----------------------------- Main ---------------------------------------

def add_commentary_interleaves(
    clip: CompositeVideoClip,
    beats: List[Tuple[str, Path, float, float]],  # (text, tts_path, start, end)
    blur_lead_seconds: float = 0.2,
    background_volume_factor: float = 0.15,
    blur_radius: float = 10.0,
    text_fontsize: int = 140,
    text_stroke_width: int = 6,
    tts_gain: float = 1.35,
) -> CompositeVideoClip:
    """
    Adds multiple mid-clip commentary segments:
    - blur video during each segment (with optional lead)
    - duck background audio during each segment
    - overlay TTS audio
    - render big center karaoke text for the TTS
    """
    if not beats:
        return clip

    intervals = []
    for _, _, s, e in beats:
        a = max(0.0, float(s) - float(blur_lead_seconds))
        b = min(float(clip.duration), float(e))
        if b > a:
            intervals.append((a, b))
    intervals = _merge_intervals(intervals)

    def _blur_if_needed(get_frame, t):
        frame = get_frame(t)
        for a, b in intervals:
            if a <= t <= b:
                return _blur_frame_pil(frame, radius=blur_radius)
        return frame

    base_video_blurred = clip.without_audio().fl(_blur_if_needed)

    if clip.audio:
        base_audio = clip.audio
    else:
        base_audio = AudioClip(lambda t: 0.0, duration=float(clip.duration), fps=44100)

    ducked = _attenuate_audio_in_intervals(base_audio, intervals, background_volume_factor, float(clip.duration))

    tts_layers = []
    for _, tts_path, s, _ in beats:
        try:
            tts_a = AudioFileClip(str(tts_path)).fx(afx.volumex, tts_gain).set_start(float(s))
            tts_layers.append(tts_a)
        except Exception:
            pass

    mixed_audio = CompositeAudioClip([ducked] + tts_layers) if tts_layers else ducked

    try:
        font = "Arial-Bold" if "Arial-Bold" in TextClip.list('font') else "Arial"
    except Exception:
        font = "Arial"

    karaoke_layers = []
    for text, tts_path, s, e in beats:
        karaoke_raw = transcribe_audio_words_assemblyai(tts_path)
        if karaoke_raw:
            karaoke = [CaptionSegment(float(s) + w.start, float(s) + w.end, w.text) for w in karaoke_raw]
        else:
            karaoke = make_karaoke_segments_for_text(text, start_time=float(s), total_duration=float(e - s))

        for seg in karaoke:
            if seg.end <= seg.start:
                continue
            try:
                txt = (
                    TextClip(
                        seg.text,
                        fontsize=text_fontsize,
                        font=font,
                        color="white",
                        stroke_color="black",
                        stroke_width=text_stroke_width,
                        method="caption",
                    )
                    .set_start(seg.start)
                    .set_end(seg.end)
                    .set_position(("center", "center"))
                )
                karaoke_layers.append(txt)
            except Exception:
                pass

    comp = CompositeVideoClip([base_video_blurred] + karaoke_layers, size=clip.size).set_audio(mixed_audio)
    return comp


# ----------------------------- Compositing -----------------------

def add_ragebait_outro(
    clip: CompositeVideoClip,
    question: str,
    tts_mp3_path: Path,
    question_start: Optional[float] = None,
    blur_lead_seconds: float = 1.0,
    background_volume_factor: float = 0.15,  # ~70% ciszej
    blur_radius: float = 10.0,
    question_fontsize: int = 140,
    question_stroke_width: int = 6,
    tts_gain: float = 1.35,   ##ile ragebait glosniejszy
) -> CompositeVideoClip:
    """
    Nakłada na końcówkę klipu TTS pytanie + 'karaoke' + blur i wyciszenie tła.

    - Jeśli `question_start` jest podane: pytanie startuje w tej sekundzie klipu,
      a blur/wyciszenie zaczyna się `blur_lead_seconds` wcześniej.
    """
    question = _normalize_whitespace(question)
    if not question:
        return clip

    try:
        tts_audio_tmp = AudioFileClip(str(tts_mp3_path))
        tts_dur = float(tts_audio_tmp.duration or 0.0)
        tts_audio_tmp.close()
    except Exception:
        tts_dur = 0.0

    if question_start is None:
        question_start = max(0.0, float(clip.duration) - max(tts_dur, 0.0))

    question_start = float(question_start)
    blur_start = max(0.0, question_start - float(blur_lead_seconds))

    # --- Video (blur tail) ---
    part_a = clip.subclip(0, blur_start)
    part_b = clip.subclip(blur_start, clip.duration).fl_image(lambda fr: _blur_frame_pil(fr, radius=blur_radius))

    video_concat = concatenate_videoclips([
        part_a.without_audio(),
        part_b.without_audio(),
    ], method="compose")

    # --- Audio ---
    base_audio_parts = []
    if clip.audio:
        try:
            a1 = clip.audio.subclip(0, blur_start)
            a2 = clip.audio.subclip(blur_start, clip.duration).fx(afx.volumex, background_volume_factor)
            base_audio_parts = [a1, a2]
        except Exception:
            base_audio_parts = []

    if base_audio_parts:
        base_audio = concatenate_audioclips(base_audio_parts)
    else:
        base_audio = AudioClip(lambda t: 0.0, duration=float(clip.duration), fps=44100)

    tts_audio = AudioFileClip(str(tts_mp3_path)).fx(afx.volumex, tts_gain)
    tts_audio = tts_audio.set_start(question_start)
    mixed_audio = CompositeAudioClip([base_audio, tts_audio])

    # --- Karaoke text ---
    karaoke_raw = transcribe_audio_words_assemblyai(tts_mp3_path)

    if karaoke_raw:
        karaoke = [
            CaptionSegment(question_start + s.start, question_start + s.end, s.text)
            for s in karaoke_raw
        ]
    else:
        karaoke = make_karaoke_segments_for_text(question, start_time=question_start, total_duration=tts_dur)

    q_layers = []
    try:
        font = "Arial-Bold" if "Arial-Bold" in TextClip.list('font') else "Arial"
    except Exception:
        font = "Arial"

    for seg in karaoke:
        if seg.end <= seg.start:
            continue
        try:
            q_txt = (
                TextClip(
                    seg.text,
                    fontsize=question_fontsize,
                    font=font,
                    color="white",
                    stroke_color="black",
                    stroke_width=question_stroke_width,
                    method="caption",
                )
                .set_start(seg.start)
                .set_end(seg.end)
                .set_position(("center", "center"))
            )
            q_layers.append(q_txt)
        except Exception:
            pass

    comp = CompositeVideoClip([video_concat] + q_layers, size=clip.size).set_audio(mixed_audio)
    return comp


# ----------------------------- Gemini plan schema (video -> JSON) -----------------------------

class VisualOverlayOut(BaseModel):
    keyword: str = Field(..., description="lowercase SINGLE WORD, must appear verbatim in transcript")
    start_time_sec: float = Field(..., ge=0, description="overlay start time in seconds")
    duration_sec: float = Field(DEFAULT_OVERLAY_DURATION, ge=0.5, le=6.0)

class BeatOut(BaseModel):
    start_time_sec: float = Field(..., ge=0, description="commentary start time in seconds")
    commentary_text: str = Field(..., description="ENGLISH ONLY, 12–28 words")

class VideoPlan(BaseModel):
    title: str = Field("", description="<= 60 chars, same language as clip")
    person: str = Field("", description="name or short description of the person the clip is about / main speaker")
    visual_overlays: List[VisualOverlayOut] = Field(default_factory=list)
    visual_keywords: List[str] = Field(default_factory=list, description="fallback 5–8 single words lowercase")
    commentary_beats: List[BeatOut] = Field(default_factory=list, description="2–4 beats")
    ragebait_question: str = Field("", description="same language as clip, ends with ?")

def configure_gemini(api_key: str):
    genai.configure(api_key=api_key)

def gemini_upload_video_wait(mp4_path: Path):
    f = genai.upload_file(path=str(mp4_path))
    for _ in range(120):
        f = genai.get_file(f.name)
        state = getattr(f, "state", None)
        state_name = getattr(state, "name", "") if state else ""
        if state_name in ("ACTIVE", ""):
            return f
        if state_name == "FAILED":
            raise RuntimeError("Gemini file processing FAILED for this video.")
        time.sleep(2.0)
    return f

def captions_to_text(captions: List["CaptionSegment"]) -> str:
    return " ".join([c.text for c in captions if c.text]).strip()

def captions_word_set(captions: List["CaptionSegment"]) -> set:
    return {str(c.text).strip().lower() for c in captions if c.text}

def call_gemini_for_video_plan(
    mp4_path: Path,
    duration_sec: float,
    transcript_text: str,
    transcript_words: set,
    api_key: str,
    model_name: str,
    commentary_max: int,
) -> VideoPlan:
    configure_gemini(api_key)

    tx = re.sub(r"\s+", " ", (transcript_text or "").strip())
    tx = tx[:7000] if tx else ""

    system_instruction = (
        "ROLE: Viral Football Analyst & Psychology Expert who reveals uncomfortable truths.\n"
        "PRIMARY INPUT: the VIDEO.\n"
        "SECONDARY INPUT: optional transcript (keyword constraints).\n"
        "OBJECTIVE: Create a 'retention hook' structure for a short video.\n"
        "TONE: High conviction, slightly arrogant, insider knowledge, polarizing but logical.\n"
        "OUTPUT MUST be STRICT JSON only (no markdown):\n"
        "{\n"
        '  \"title\": \"string <= 40 chars\",\n'
        '  \"person\": \"string (name or short description)\",\n'
        '  \"visual_overlays\": [{\"keyword\":\"word\",\"start_time_sec\":12.3,\"duration_sec\":2.5}],\n'
        '  \"visual_keywords\": [\"kw1\",\"kw2\"],\n'
        '  \"commentary_beats\": [{\"start_time_sec\":18.0,\"commentary_text\":\"...\"}],\n'
        '  \"ragebait_question\": \"ends with ?\"\n'
        "}\n\n"
        "RULES FOR COMMENTARY BEATS:\n"
        "- NEVER just insult. Analyze BODY LANGUAGE or HIDDEN MEANING.\n"
        "- Example BAD: 'He is so arrogant.'\n"
        "- Example GOOD: 'Look at his eyes. He actually believes he is above the club.'\n"
        "- Length: Short & Punchy (max 3 seconds of speech).\n"
        "- Timing: Interject exactly when the speaker takes a breath or finishes a sentence.\n"
        "- ragebait_question: Must divide the audience (e.g., 'Is he a genius or a fraud?').\n"
        "\n"
        "RULES FOR VISUAL OVERLAYS:\n"
        "- Keywords must generate PHOTOREALISTIC, DARK, CINEMATIC images.\n"
        "- Keywords can not refer to other people than the main speaker of the clip for example: friends, people.\n"
        "- Avoid fantasy items (no gauntlets, no magic).\n"
        "- Focus on: Cash piles, destroyed stadiums, golden crowns, angry crowds, newspaper headlines.\n"
        "- 8–12 overlays, evenly spaced (avoid stacking), they shoudnt be at the same time as commentary_beats.\n"
        "\n"
        "OTHER RULES"
        "- person: identify the main person on screen or being discussed; use the name only if clearly present, otherwise a short generic description (e.g., \"the host\", \"the guest\").\n"
        "- ragebait_question: 8–10 words, MUST end with '?'.\n"
        "- make 10-16 keywords for overlays.\n"
        #"- prefer 2 commentary beats for videos < 40 sec and 3 for thode over 40 sec.\n"
        #"- try not to put commentary beat in the first 5second of the clip, only if it is perfect opportunity.\n"
        #"- commentary_text: ENGLISH ONLY, 8–16 words, really provocative and ragebaiting sometimes infuriating.\n"
        #"- commentary_text: it should also have a hook to try to make a viewer stick to the end of the video.\n"
        #"- commentary_text: should make emocional hooks so the viewer do not skip the video.\n"
        #"- try to make commentary beats as long as they should be so when they are put onto the video, the new clip could make sense.\n"
        #"- commentary_text should not comment, it should bring your ragebaiting opinion while making things easier to understand. \n"
        "- commentary_text should not be at the same time as the crucial informaction in the clip.\n"
        f"- 2–{int(commentary_max)} commentary beats.\n"
        "- Place last commentary beat at least ~6s before clip end (leave space for outro).\n"
        #"- visual_overlays: 4–8 overlays, spaced (avoid stacking), each duration 2–4s, they shoudnt be at the same time as commentary_beats.\n"
        "- keywords MUST be ALL lowercase, SINGLE WORD.\n"
        "- IMPORTANT: each keyword MUST appear verbatim in transcript as a standalone word.\n"
        "- keywords cannot be abstract.\n"
        "- start_time_sec must be within [0, clip_duration-1.5].\n"
        "- Do NOT invent names/facts not supported.\n"
    )

    prompt = (
        f"CLIP DURATION: {duration_sec:.1f} seconds.\n"
        "Return ONLY the JSON.\n\n"
        "OPTIONAL TRANSCRIPT (for keyword constraint; VIDEO is primary):\n"
        f"{tx}\n"
    )

    uploaded = None
    try:
        uploaded = gemini_upload_video_wait(mp4_path)
        model = genai.GenerativeModel(model_name, system_instruction=system_instruction)
        resp = model.generate_content([uploaded, prompt])
        out = (resp.text or "").strip()
        out = re.sub(r"^```(?:json)?\s*", "", out)
        out = re.sub(r"\s*```$", "", out)
        data = json.loads(out)
        plan = VideoPlan.model_validate(data)

        if transcript_words:
            plan.visual_overlays = [
                ov for ov in (plan.visual_overlays or [])
                if (ov.keyword or "").strip().lower() in transcript_words
            ]
            plan.visual_keywords = [
                k for k in (plan.visual_keywords or [])
                if (k or "").strip().lower() in transcript_words
            ]
        return plan

    except (json.JSONDecodeError, ValidationError) as e:
        logging.error(f"Gemini JSON/validation error: {e}")
    except Exception as e:
        logging.error(f"Gemini video plan error: {e}")
    finally:
        try:
            if uploaded is not None:
                genai.delete_file(uploaded.name)
        except Exception:
            pass
    
    return VideoPlan()


def build_overlays_from_plan(
    plan_overlays: List[VisualOverlayOut],
    full_clip_text: str,
    person: str,
    settings: Settings,
    gemini_key: str,
    clip_duration: float,
) -> List[VisualOverlay]:
    out: List[VisualOverlay] = []
    if not plan_overlays:
        return out

    used = set()
    img_dir = settings.download_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    for ov in plan_overlays:
        kw = (ov.keyword or "").strip().lower()
        if not kw:
            continue

        st = float(ov.start_time_sec or 0.0)
        st = max(0.0, min(st, max(0.0, clip_duration - 0.2)))
        if round(st, 2) in used:
            continue
        used.add(round(st, 2))

        dur = float(getattr(ov, "duration_sec", DEFAULT_OVERLAY_DURATION) or DEFAULT_OVERLAY_DURATION)
        dur = max(0.8, min(dur, 6.0))

        img_path = generate_ai_image(
            keyword=kw,
            full_clip_text=full_clip_text,
            person=person,
            out_dir=img_dir,
            api_key=gemini_key,
            model_name=settings.imagen_model_name,
        )
        if not img_path:
            continue

        out.append(VisualOverlay(asset_path=img_path, start_time=st, duration=dur))

    return out


def process_one_file(mp4_path: Path, gemini_key: str):
    label = mp4_path.stem
    out_path = OUTPUT_DIR / f"{label}.mp4"
    if out_path.exists():
        logging.info(f"Skipping (exists): {out_path.name}")
        return

    settings = Settings(
        language="",
        output_dir=OUTPUT_DIR,
        download_dir=WORK_DIR,
        gemini_config_path=GEMINI_CONFIG_PATH,
        max_clips=1,
        gemini_model_name=GEMINI_VIDEO_MODEL,
        imagen_model_name=GEMINI_IMAGE_MODEL,
        commentary_min=2,
        commentary_max=COMMENTARY_MAX,
        commentary_min_sec=3.0,
        commentary_max_sec=8.0,
        commentary_background_volume_factor=COMMENTARY_BG_VOLUME,
        commentary_blur_radius=COMMENTARY_BLUR_RADIUS,
        commentary_tts_gain=COMMENTARY_TTS_GAIN,
        keep_captions_during_commentary=KEEP_CAPTIONS_DURING_COMMENTARY,
    )

    with VideoFileClip(str(mp4_path)) as full:
        duration = float(full.duration or 0.0)
        if duration <= 0.2:
            logging.warning(f"Bad duration, skipping: {mp4_path.name}")
            return

        captions = transcribe_clip_with_assemblyai(full, label)
        transcript_text = captions_to_text(captions)
        transcript_words = captions_word_set(captions)

        plan = call_gemini_for_video_plan(
            mp4_path=mp4_path,
            duration_sec=duration,
            transcript_text=transcript_text,
            transcript_words=transcript_words,
            api_key=gemini_key,
            model_name=settings.gemini_model_name,
            commentary_max=settings.commentary_max,
        )

        title = _normalize_whitespace(plan.title) if plan.title else ""
        rage_q = _normalize_whitespace(plan.ragebait_question) if plan.ragebait_question else ""

        tts_path: Optional[Path] = None
        tts_dur = 0.0
        if rage_q:
            tts_path = generate_elevenlabs_tts(rage_q, WORK_DIR / "tts", f"{label}_ragebait")
            if tts_path:
                try:
                    _a = AudioFileClip(str(tts_path))
                    tts_dur = float(_a.duration or 0.0)
                    _a.close()
                except Exception:
                    tts_dur = 0.0

        main_sub = full.subclip(0, duration)

        commentary_tts_paths: List[Path] = []
        commentary_tts_durs: List[float] = []
        commentary_texts: List[str] = []
        commentary_proposed_times: List[float] = []

        beats_in = list(plan.commentary_beats or [])[: int(settings.commentary_max)]
        for bi, beat in enumerate(beats_in, start=1):
            txt = _normalize_whitespace(beat.commentary_text) if beat.commentary_text else ""
            if not txt:
                continue

            proposed = float(beat.start_time_sec or 0.0)
            proposed = max(0.0, min(proposed, max(0.0, duration - 2.2)))

            tts_cmt = generate_elevenlabs_tts(txt, WORK_DIR / "tts", f"{label}_commentary_{bi}")
            if not tts_cmt:
                continue

            try:
                _c = AudioFileClip(str(tts_cmt))
                dur_c = float(_c.duration or 0.0)
                _c.close()
            except Exception:
                dur_c = 0.0

            if dur_c <= 0.05:
                try:
                    if tts_cmt.exists():
                        tts_cmt.unlink()
                except Exception:
                    pass
                continue

            commentary_texts.append(txt)
            commentary_proposed_times.append(proposed)
            commentary_tts_paths.append(tts_cmt)
            commentary_tts_durs.append(dur_c)

        desired_end = duration + tts_dur if tts_dur > 0.0 else duration
        sub = full.subclip(0, min(float(full.duration), desired_end))

        if tts_dur > 0.0:
            desired_total = float(main_sub.duration) + float(tts_dur)
            missing = desired_total - float(sub.duration)
            if missing > 0.02:
                try:
                    #frame = sub.get_frame(max(0.0, sub.duration - 1e-3))
                    t_last = max(0.0, float(sub.duration) - 0.1)  # 100 ms od końca (bezpieczniej)
                    frame = sub.get_frame(t_last)
                    still = ImageClip(frame).set_duration(missing)
                except Exception:
                    still = ColorClip(size=sub.size, color=(0, 0, 0)).set_duration(missing)
                sub = concatenate_videoclips([sub, still], method="compose")

        commentary_schedule: List[Tuple[str, Path, float, float]] = []
        commentary_intervals_for_captions: List[Tuple[float, float]] = []

        if commentary_tts_paths and commentary_tts_durs:
            beats = []
            for i, txt in enumerate(commentary_texts):
                beats.append(TimedCommentaryBeat(start_time=float(commentary_proposed_times[i]), text=txt, anchor_index=-1))

            if tts_dur > 0.0:
                outro_blur_start = max(0.0, float(main_sub.duration) - 1.0)
                hard_end = max(0.0, outro_blur_start - 0.25)
            else:
                hard_end = float(main_sub.duration)

            scheduled = _schedule_commentary_beats(
                beats=beats,
                main_duration=float(main_sub.duration),
                tts_durations=commentary_tts_durs,
                hard_end_time=hard_end,
                min_gap=0.6,
                min_start=1.0,
            )[: int(settings.commentary_max)]

            for (beat_obj, s, e), tts_cmt_path in zip(scheduled, commentary_tts_paths):
                commentary_schedule.append((beat_obj.text, tts_cmt_path, float(s), float(e)))
                commentary_intervals_for_captions.append((float(s), float(e)))

        visual_overlays = build_overlays_from_plan(
            plan_overlays=list(plan.visual_overlays or []),
            full_clip_text=transcript_text,
            person=_normalize_whitespace(getattr(plan, 'person', '') or ''),
            settings=settings,
            gemini_key=gemini_key,
            clip_duration=float(sub.duration),
        )

        if not visual_overlays and plan.visual_keywords and captions:
            visual_overlays = match_visuals_to_transcript(
                keywords=[k.strip().lower() for k in (plan.visual_keywords or []) if k and k.strip()],
                captions=captions,
                full_clip_text=transcript_text,
                person=_normalize_whitespace(getattr(plan, 'person', '') or ''),
                settings=settings,
            )

        #scieżki odglosow
        CLICK_SFX_PATH = Path(r"C:\Users\filip\PycharmProjects\Inter\odglosy\mouse_click.mp3")
        TITLE_WOOSH_PATH = Path(r"C:\Users\filip\PycharmProjects\Inter\odglosy\woosh.mp3")

        final_base = build_vertical_video(
            sub,
            title,
            captions,
            visual_overlays,
            subtitle_exclude_intervals=commentary_intervals_for_captions,
            show_subtitles_during_commentary=bool(settings.keep_captions_during_commentary),
            sfx_path=CLICK_SFX_PATH,
            title_sfx_path=TITLE_WOOSH_PATH,          # <-- DODAJ TO
            title_slide_in_sec=0.7,                   # <-- opcjonalnie (jak długo ma wjeżdżać)
            title_sfx_volume=1.2,                     # <-- opcjonalnie (głośność woosha),
        )

        final = final_base
        try:
            if commentary_schedule:
                final = add_commentary_interleaves(
                    clip=final_base,
                    beats=commentary_schedule,
                    blur_lead_seconds=0.2,
                    background_volume_factor=float(settings.commentary_background_volume_factor),
                    blur_radius=float(settings.commentary_blur_radius),
                    text_fontsize=140,
                    text_stroke_width=6,
                    tts_gain=float(settings.commentary_tts_gain),
                )
        except Exception as e:
            logging.error(f"Commentary interleaves failed (skipping): {e}")
            final = final_base

        try:
            if rage_q and tts_path:
                final = add_ragebait_outro(
                    clip=final,
                    question=rage_q,
                    tts_mp3_path=tts_path,
                    question_start=float(main_sub.duration),
                    blur_lead_seconds=1.0,
                    background_volume_factor=0.15,
                    blur_radius=10.0,
                    question_fontsize=140,
                    question_stroke_width=6,
                )
        except Exception as e:
            logging.error(f"Ragebait outro failed (skipping): {e}")

        #dodanie musyki
        final = add_background_music_to_clip(
            final,
            music_dir=MUSIC_DIR,
            volume=BG_MUSIC_VOLUME,
            fade=BG_MUSIC_FADE,
        )

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        WORK_DIR.mkdir(parents=True, exist_ok=True)

        final.write_videofile(
            str(out_path),
            fps=24,
            codec="libx264",
            audio_codec="aac",
            temp_audiofile=str(WORK_DIR / f"temp_{label}.m4a"),
            remove_temp=True,
            verbose=False,
            logger=None,
        )
        logging.info(f"Saved: {out_path}")

        try:
            if final.audio:
                final.audio.close()
        except Exception:
            pass


        for p in ([tts_path] if tts_path else []) + commentary_tts_paths:
            try:
                if p and Path(p).exists():
                    Path(p).unlink()
            except Exception:
                pass

        for ov in visual_overlays:
            try:
                p = ov.asset_path
                if p and Path(p).exists():
                    Path(p).unlink()
            except Exception:
                pass

        try:
            final.close()
        except Exception:
            pass
        try:
            final_base.close()
        except Exception:
            pass


def main():
    setup_logging()

    gemini_key = load_gemini_api_key(GEMINI_CONFIG_PATH)

    aai_key = os.getenv("ASSEMBLYAI_API_KEY")
    if aai_key:
        aai.settings.api_key = aai_key
    else:
        logging.warning("ASSEMBLYAI_API_KEY not set — subtitles/keyword constraints may be empty.")

    if not INPUT_DIR.exists():
        logging.error(f"INPUT_DIR not found: {INPUT_DIR}")
        return

    files = sorted(INPUT_DIR.glob("*.mp4"))
    if MAX_FILES and MAX_FILES > 0:
        files = files[:MAX_FILES]

    if not files:
        logging.error(f"No .mp4 files in: {INPUT_DIR}")
        return

    for i, mp4_path in enumerate(files, start=1):
        logging.info(f"[{i}/{len(files)}] Processing: {mp4_path.name}")
        try:
            process_one_file(mp4_path, gemini_key)
        except Exception as e:
            logging.error(f"FAILED {mp4_path.name}: {e}")


if __name__ == "__main__":
    main()
