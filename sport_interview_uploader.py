#!/usr/bin/env python3
import os
import io
import sys
import time
import random
import logging
from pathlib import Path
import subprocess
import shutil
from datetime import datetime, timedelta, timezone

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
import google.generativeai as genai

# ==========================
# SETTINGS - CONFIGURE THESE
# ==========================

DRIVE_FOLDER_ID = "1fo50klj0VfhZ_9QKmk04ERdz2fPEaz5a"
LOCAL_TEMP_VIDEO = "/home/ubuntu/interviews/tmp_interview_video_sport.mp4"
LOCAL_TEMP_THUMBNAIL = "/home/ubuntu/interviews/tmp_thumbnail_sport.jpg"

# Domyslne dane filmu na YouTube (fallback, gdy Gemini nie zadziala)
DEFAULT_VIDEO_TITLE = "Football interview highlight"
DEFAULT_VIDEO_DESCRIPTION = "#shorts #football #soccer #interview #motivation"

VIDEO_TAGS = [
    "shorts",
    "football",
    "soccer",
    "sports",
    "interview",
    "motivation",
    "mindset",
]
VIDEO_CATEGORY_ID = "17"  # Sports
VIDEO_PRIVACY_STATUS = "public"

# *** NOWE: osobne tokeny ***
DRIVE_TOKEN_FILE = "/home/ubuntu/interviews/token_interview.json"          # główne konto (Drive)
YOUTUBE_TOKEN_FILE = "/home/ubuntu/interviews/token_interview_sport.json"  # kanał sportowy (YouTube)

LOG_FILE = "/home/ubuntu/interviews/uploader_sport.log"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "TWÓJ KLUCZ API!!!") #tu wstaw swój klucz api!!!

# *** NOWE: osobne scope'y ***
DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
]

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
]

# Scheduler: fixed times (start upload) in a fixed GMT+0100 offset (no DST)
TZ = timezone(timedelta(hours=1))  # GMT+0100 (fixed offset)
SCHEDULE_HOURS = (16, 18)


# ==========================
# LOGGING SETUP
# ==========================

def setup_logging():
    """Configure logging to file and console"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout)
        ]
    )


# ==========================
# GOOGLE AUTH – DWA OSOBNE TOKENY
# ==========================

def get_drive_credentials():
    """Returns Credentials object for Google Drive (główne konto)"""
    creds = None

    if os.path.exists(DRIVE_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(DRIVE_TOKEN_FILE, DRIVE_SCOPES)

    if not creds or not creds.valid:
        from google.auth.transport.requests import Request

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                "client_secret.json", DRIVE_SCOPES
            )
            creds = flow.run_console()

        with open(DRIVE_TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    return creds


def get_youtube_credentials():
    """Returns Credentials object for YouTube (kanał sportowy)"""
    creds = None

    if os.path.exists(YOUTUBE_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(YOUTUBE_TOKEN_FILE, YOUTUBE_SCOPES)

    if not creds or not creds.valid:
        from google.auth.transport.requests import Request

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                "client_secret_sport.json", YOUTUBE_SCOPES
            )
            creds = flow.run_console()

        with open(YOUTUBE_TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    return creds


# ==========================
# DRIVE FUNCTIONS
# ==========================

def get_next_drive_video(drive_service):
    """Lists files in DRIVE_FOLDER_ID and returns a random file_id, file_name"""
    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed = false"

    results = drive_service.files().list(
        q=query,
        pageSize=1000,
        fields="files(id, name)"
    ).execute()

    files = results.get("files", [])
    if not files:
        return None, None

    random_file = random.choice(files)
    logging.info(f"Selected file from Drive: {random_file.get('name')} (id={random_file.get('id')})")
    return random_file.get("id"), random_file.get("name")


def download_from_drive(drive_service, file_id, destination_path):
    """Downloads file from Drive by file_id to destination_path"""
    request = drive_service.files().get_media(fileId=file_id)
    fh = io.FileIO(destination_path, "wb")
    downloader = MediaIoBaseDownload(fh, request)

    done = False
    while not done:
        status, done = downloader.next_chunk()
        if status:
            logging.info(f"Downloading from Drive... {int(status.progress() * 100)}%")

    logging.info(f"Downloaded file to: {destination_path}")


# ==========================
# GEMINI
# ==========================

def generate_title_and_description(video_path):
    """Uses Gemini to generate title and description based on the video filename"""
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.5-flash")

    prompt = """Analyze this interview clip about sport/athelte lifestle/motivation.

VIDEO CONTEXT: This is from a sportsman interview series(often football focused).

Generate TWO separate outputs:

=== TITLE ===
[Create a compelling YouTube Shorts TITLE (max 100 characters):
- Use power words and emotional triggers
- Make it controversial or thought-provoking
- Include relevant keywords(football, athlete, etc.)
- Make people WANT to click
Examples: "This Football Star Just Exposed the Truth About Fame", "Striker Reveals Brutal Reality of Pro Football"]

=== DESCRIPTION ===
Generate a YouTube Shorts description (keep given emojis) in this EXACT format:

💡 [Engaging question or bold statement about sports/life of athletes – make it click-worthy]

📌[Person's Name] - [their role and key achievement]

⚠️PERSON RECOGNITION RULES:
- IF you recognize the person (e.g., Cristiano Ronaldo, Lionel Messi): use their real name and main accomplishments (club, national team, trophies)
- IF you DON'T recognize them: describe generically as "Professional Footballer", "Premier League Star", "National Team Player", "Elite Athlete", etc. based on visual/audio context
- NEVER invent names or fake achievements

This clip covers [1–2 sentence summary of the main topic: e.g., pressure in big matches, injuries, money, fame, locker-room stories, mentality, training, etc.].

🎯 Key Information (give between 1-3):
- [POINT_1 - e.g.: What it really feels like to play in front of 60,000 fans]
- [POINT_2 - e.g.: How constant pressure and criticism affects a player’s mental health]
- [POINT_3 - e.g.: The brutal truth about injuries, contracts, or transfers]

⚠️WARNING: [CONTROVERSIAL/INTRIGUING ELEMENT, e.g.: This will change how you see pro athletes | Fans never hear this side of the story | Most clubs don’t want you to know this]
---
🔔Subscribe to Pitch Warrior - daily interviews with top athletes and football stars

#Football #Soccer #Sports #Athlete #Motivation #SportsInterview #Shorts [add 5–8 more relevant hashtags based on specific topic, e.g. #PremierLeague #ChampionsLeague #WorldCup #Striker #Goalkeeper #Mentality #Training #LockerRoomStories]
STYLE REQUIREMENTS:
- Title: controversial, thought-provoking, uses power words, max 100 characters
- Description: Keep it concise but informative
- Use American English
- Hashtags: mix of broad (#Football) and specific (#PremierLeague, #WorldCupStar)


IMPORTANT: Format your response EXACTLY like this:
=== TITLE ===
[Your title here]

=== DESCRIPTION ===
[Your description here]"""

    logging.info("Uploading video to Gemini...")
    try:
        uploaded_file = genai.upload_file(path=str(video_path))
    except Exception as e:
        logging.error(f"Error uploading file to Gemini: {e}")
        return DEFAULT_VIDEO_TITLE, DEFAULT_VIDEO_DESCRIPTION

    logging.info(f"File uploaded: {uploaded_file.name}")
    logging.info(f"Initial status: {uploaded_file.state.name}")

    max_wait_time = 600
    start_time = time.time()

    try:
        while uploaded_file.state.name == "PROCESSING":
            if time.time() - start_time > max_wait_time:
                logging.error("Maximum wait time exceeded (10 minutes)")
                return DEFAULT_VIDEO_TITLE, DEFAULT_VIDEO_DESCRIPTION

            time.sleep(5)
            uploaded_file = genai.get_file(uploaded_file.name)

        if uploaded_file.state.name == "FAILED":
            logging.error("File processing failed in Gemini")
            return DEFAULT_VIDEO_TITLE, DEFAULT_VIDEO_DESCRIPTION

        if uploaded_file.state.name != "ACTIVE":
            logging.warning(f"Unexpected file status: {uploaded_file.state.name}")
            return DEFAULT_VIDEO_TITLE, DEFAULT_VIDEO_DESCRIPTION

        logging.info(f"File ready to use! Status: {uploaded_file.state.name}")

        logging.info("Gemini analyzing video...")
        response = model.generate_content(
            [uploaded_file, prompt],
            request_options={"timeout": 600}
        )
        full_response = response.text or ""

    finally:
        try:
            genai.delete_file(uploaded_file.name)
            logging.info("File deleted from Gemini API")
        except Exception:
            pass

    title = DEFAULT_VIDEO_TITLE
    description = DEFAULT_VIDEO_DESCRIPTION

    if "=== TITLE ===" in full_response and "=== DESCRIPTION ===" in full_response:
        parts = full_response.split("=== DESCRIPTION ===", 1)
        title_part = parts[0].replace("=== TITLE ===", "").strip()
        desc_part = parts[1].strip()
        if title_part:
            title = title_part
        if desc_part:
            description = desc_part
    elif full_response.strip():
        description = full_response.strip()

    logging.info(f"GENERATED TITLE: {title}")
    logging.info(f"GENERATED DESCRIPTION: {description[:100]}...")

    return title, description


# ==========================
# YOUTUBE UPLOAD
# ==========================

def upload_to_youtube(youtube_service, video_path, title, description):
    """Uploads video to YouTube"""
    if not os.path.exists(video_path):
        logging.error(f"Video file does not exist: {video_path}")
        return None

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": VIDEO_TAGS,
            "categoryId": VIDEO_CATEGORY_ID,
        },
        "status": {
            "privacyStatus": VIDEO_PRIVACY_STATUS,
        },
    }

    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)

    request = youtube_service.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            logging.info(f"Uploading... {int(status.progress() * 100)}%")

    logging.info("Video uploaded successfully!")
    video_id = response.get("id")
    if video_id:
        logging.info(f"Video ID: {video_id}")
        logging.info(f"Shorts link: https://www.youtube.com/shorts/{video_id}")

    return video_id


# ==========================
# DRIVE DELETE
# ==========================

def delete_from_drive(drive_service, file_id):
    """Deletes file from Google Drive"""
    try:
        drive_service.files().delete(fileId=file_id).execute()
        logging.info(f"Deleted file from Google Drive (file_id={file_id}).")
    except Exception as e:
        logging.error(f"Failed to delete file from Drive: {e}")


def get_next_run_time(now: datetime) -> datetime:
    """Return the next scheduled run time (16:00 or 18:00) in TZ."""
    today = now.date()

    candidates = []
    for hour in SCHEDULE_HOURS:
        candidate = datetime(today.year, today.month, today.day, hour, 0, 0, tzinfo=TZ)
        if candidate >= now:
            candidates.append(candidate)

    if candidates:
        return min(candidates)

    # If we're past the last slot today, schedule tomorrow at the first slot.
    tomorrow = today + timedelta(days=1)
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, SCHEDULE_HOURS[0], 0, 0, tzinfo=TZ)


def extract_first_frame_thumbnail(video_path: str, thumbnail_path: str) -> None:
    """Generate a JPG thumbnail from the very first decoded video frame using ffmpeg."""
    if not os.path.exists(video_path):
        raise FileNotFoundError(video_path)

    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg not found. Check: `ffmpeg -version`. "
            "Install (Ubuntu/Debian): `sudo apt-get update && sudo apt-get install -y ffmpeg`."
        )

    def run_ffmpeg(q: int) -> None:
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-vf", "select=eq(n\\,0)",   # exactly frame n=0
            "-frames:v", "1",
            "-q:v", str(q),
            thumbnail_path,
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    # Try high quality first
    run_ffmpeg(q=2)

    if not os.path.exists(thumbnail_path) or os.path.getsize(thumbnail_path) == 0:
        raise RuntimeError("Failed to generate thumbnail via ffmpeg.")

    # YouTube thumbnail upload limit is 2MB; recompress if needed.
    max_bytes = 2 * 1024 * 1024
    if os.path.getsize(thumbnail_path) > max_bytes:
        for q in (5, 8, 10, 15, 20, 25, 30):
            run_ffmpeg(q=q)
            if os.path.getsize(thumbnail_path) <= max_bytes:
                break


def set_youtube_thumbnail(youtube_service, video_id: str, thumbnail_path: str) -> None:
    """Upload and set a custom thumbnail on YouTube for a given video."""
    if not os.path.exists(thumbnail_path):
        raise FileNotFoundError(thumbnail_path)

    media = MediaFileUpload(thumbnail_path, mimetype="image/jpeg", resumable=False)
    youtube_service.thumbnails().set(videoId=video_id, media_body=media).execute()
    logging.info("Thumbnail uploaded and set successfully.")


# ==========================
# MAIN PROCESS
# ==========================

def process_single_video():
    """Processes one video - main logic"""
    try:
        # *** NOWE: osobne credsy ***
        drive_creds = get_drive_credentials()
        youtube_creds = get_youtube_credentials()

        from googleapiclient.discovery import build
        drive_service = build("drive", "v3", credentials=drive_creds)
        youtube_service = build("youtube", "v3", credentials=youtube_creds)

        file_id, file_name = get_next_drive_video(drive_service)

        if not file_id:
            logging.warning("No files to process - skipping iteration")
            return False

        logging.info(f"Processing: {file_name} ({file_id})")

        download_from_drive(drive_service, file_id, LOCAL_TEMP_VIDEO)

        try:
            title, description = generate_title_and_description(LOCAL_TEMP_VIDEO)
        except Exception as e:
            logging.error(f"Error generating title/description: {e}")
            title, description = DEFAULT_VIDEO_TITLE, DEFAULT_VIDEO_DESCRIPTION

        try:
            video_id = upload_to_youtube(youtube_service, LOCAL_TEMP_VIDEO, title, description)
            if video_id:
                # Set thumbnail from the first frame (best-effort; upload should still count as success if thumbnail fails)
                try:
                    extract_first_frame_thumbnail(LOCAL_TEMP_VIDEO, LOCAL_TEMP_THUMBNAIL)
                    set_youtube_thumbnail(youtube_service, video_id, LOCAL_TEMP_THUMBNAIL)
                except Exception as e:
                    logging.error(f"Failed to set thumbnail: {e}")

                delete_from_drive(drive_service, file_id)
                return True
        except Exception as e:
            logging.error(f"Error uploading to YouTube: {e}")
            return False
        finally:
            # Always cleanup local file
            try:
                if os.path.exists(LOCAL_TEMP_VIDEO):
                    os.remove(LOCAL_TEMP_VIDEO)
                    logging.info("Deleted local temporary file.")
                if os.path.exists(LOCAL_TEMP_THUMBNAIL):
                    os.remove(LOCAL_TEMP_THUMBNAIL)
                    logging.info("Deleted local thumbnail file.")
            except Exception as e:
                logging.error(f"Failed to delete local file: {e}")

    except Exception as e:
        logging.error(f"Error in process_single_video: {e}", exc_info=True)
        return False


# ==========================
# DAEMON LOOP
# ==========================

def run_daemon():
    """Main daemon loop - runs at 16:00 and 18:00 in fixed GMT+0100."""
    setup_logging()
    logging.info("=" * 80)
    logging.info("DAEMON STARTED - Interview Uploader")
    logging.info("Schedule: 16:00 and 18:00 (GMT+0100, fixed offset)")
    logging.info("=" * 80)

    iteration = 0

    while True:
        now = datetime.now(TZ).replace(microsecond=0)
        next_run = get_next_run_time(now)
        sleep_seconds = max(0, (next_run - now).total_seconds())

        logging.info(f"Now (GMT+0100): {now.strftime('%Y-%m-%d %H:%M:%S')}")
        logging.info(f"Next run (GMT+0100): {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
        logging.info(f"Sleeping {sleep_seconds:.0f}s...")

        time.sleep(sleep_seconds)

        iteration += 1
        logging.info("")
        logging.info("=" * 80)
        logging.info(f"ITERATION #{iteration} - {datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')} (GMT+0100)")
        logging.info("=" * 80)

        try:
            success = process_single_video()
            if success:
                logging.info("Video processed successfully!")
            else:
                logging.warning("No upload done / or completed with errors (will wait for next slot).")
        except Exception as e:
            logging.error(f"Critical error in iteration #{iteration}: {e}", exc_info=True)


# ==========================
# ENTRY POINT
# ==========================

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        # Single run mode (for testing)
        setup_logging()
        logging.info("Test mode - single execution")
        process_single_video()
    else:
        # Daemon mode
        run_daemon()

