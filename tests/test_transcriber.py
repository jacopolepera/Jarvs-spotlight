import os
import sys

# Add root dir to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.transcriber import transcribe_audio_gemini
from tools.settings_manager import SettingsManager

def test_transcription():
    print("Testing Gemini Audio Transcription...")
    
    # Needs a real audio file in the Jarvs directory
    test_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_files", "test_audio.ogg")
    
    if not os.path.exists(test_file):
        print(f"Test file not found at {test_file}. Please run the download command first.")
        return
        
    settings = SettingsManager()
    api_key = settings.get_api_key()
    
    if not api_key:
        print("Error: GEMINI_API_KEY is not set in your .env file.")
        return
        
    print(f"Sending {test_file} to Gemini API...")
    
    # We might need to ensure the mime_type in transcriber.py supports ogg if we downloaded ogg.
    # The API natively supports audio/ogg and audio/wav. Let's let the API try.
    result = transcribe_audio_gemini(test_file, api_key)
    
    print("\n--- Transcription Result ---")
    print(result)
    print("---------------------------\n")
    
    if "[Error" not in result:
        print("✅ Transcription successful!")
    else:
        print("❌ Transcription failed.")

if __name__ == "__main__":
    test_transcription()
