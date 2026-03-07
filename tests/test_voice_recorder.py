import time
import os
import sys

# Add root dir to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.voice_recorder import VoiceRecorder

def test_recorder():
    print("Testing Voice Recorder...")
    audio_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_mic.wav")
    
    recorder = VoiceRecorder(filename=audio_path)
    
    print("🎙 Starting recording... Please speak into the microphone for 3 seconds.")
    recorder.start_recording()
    
    time.sleep(3)
    
    print("⏹ Stopping recording...")
    saved_path = recorder.stop_recording()
    
    if os.path.exists(saved_path):
        size = os.path.getsize(saved_path)
        print(f"✅ Recording successful! File saved at {saved_path} (Size: {size} bytes)")
    else:
        print("❌ Recording failed! File not found.")

if __name__ == "__main__":
    test_recorder()
