import base64
import requests

def transcribe_audio_gemini(audio_path, api_key):
    """
    Sends the audio to the Gemini API for transcription.
    We use gemini-1.5-flash as it is fast, cheap, and has excellent multilingual audio understanding.
    """
    if not api_key:
        return "[Error: GEMINI_API_KEY is missing. Please set it in Settings.]"
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    
    try:
        with open(audio_path, "rb") as f:
            audio_data = f.read()
    except Exception as e:
        return f"[Error reading audio file: {e}]"
        
    b64_audio = base64.b64encode(audio_data).decode("utf-8")
    
    import mimetypes
    mime_type, _ = mimetypes.guess_type(audio_path)
    if not mime_type:
        mime_type = "audio/wav" # Fallback

    # Prompt instructs the model to just transcribe, no markdown, no conversational text.
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": "Transcribe this audio exactly as spoken in its original language, with no additional commentary or markdown blocks. Just the transcription."},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": b64_audio
                        }
                    }
                ]
            }
        ]
    }
    
    headers = {"Content-Type": "application/json"}
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 200:
            data = response.json()
            try:
                # Extract text from response
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
            except (KeyError, IndexError):
                return "[Error: Unexpected API response structure]"
        else:
            return f"[Error API {response.status_code}: {response.text}]"
    except Exception as e:
        return f"[Error: Network or Request failed: {e}]"
