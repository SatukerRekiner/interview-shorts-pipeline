import os
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# MUSI być taki sam scope jak w głównym skrypcie
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    #"https://www.googleapis.com/auth/drive"
]

# SCOPES = [
#     "https://www.googleapis.com/auth/youtube.upload",
#     "https://www.googleapis.com/auth/youtube.readonly"
# ]

#SCOPES = ["https://www.googleapis.com/auth/drive"]  # read + delete

# Twój plik z Google Cloud (pobrany z Credentials)
#CLIENT_SECRETS_FILE = "credentials_gdrive.json"
CLIENT_SECRETS_FILE = "client_secret_animal.json"


# <<< TUTAJ ZMIENIASZ NA ODPOWIEDNIĄ NAZWĘ DLA KAŻDEGO KANAŁU >>>
TOKEN_FILE = "token_interview_sport.json"  # np. dla pierwszego kanału


def main():
    if not os.path.exists(CLIENT_SECRETS_FILE):
        raise FileNotFoundError(
            f"Nie znalazłem {CLIENT_SECRETS_FILE}. Upewnij się, że plik leży obok tego skryptu."
        )

    # Start logowania OAuth
    flow = InstalledAppFlow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, SCOPES
    )
    creds = flow.run_local_server(port=0)

    # Zapisz token do pliku (forma JSON – zgodna z Twoim niekradnij.py)
    with open(TOKEN_FILE, "w", encoding="utf-8") as token:
        token.write(creds.to_json())

    print(f"\n✅ Zapisano token w pliku: {TOKEN_FILE}")

    # (opcjonalne, ale przydatne) – sprawdź, jaki to kanał
    youtube = build("youtube", "v3", credentials=creds)
    response = youtube.channels().list(
        part="snippet",
        mine=True
    ).execute()

    items = response.get("items", [])
    if items:
        ch = items[0]
        title = ch["snippet"]["title"]
        channel_id = ch["id"]
        print(f"Ten token jest powiązany z kanałem:")
        print(f"  Nazwa:  {title}")
        print(f"  ID:     {channel_id}")
    else:
        print("Nie udało się pobrać informacji o kanale (brak 'mine=True'?).")


if __name__ == "__main__":
    main()
