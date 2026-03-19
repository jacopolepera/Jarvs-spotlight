import os
import sys
import json

# Add root dir to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.tools.settings_manager import SettingsManager
from core.tools.engine import setup_interpreter

def test_history_scrub():
    print("Testing Engine History Scrubber...")
    settings = SettingsManager()
    
    workspace_dir = settings.get_workspace_path()
    os.makedirs(workspace_dir, exist_ok=True)
    history_file = os.path.join(workspace_dir, "history.json")
    
    # Write a fake corrupted image history
    fake_history = [
        {"role": "user", "type": "message", "content": "Here is an image"},
        {"role": "user", "type": "image", "content": "iVBORw0KGgo[Content truncated to save memory]...", "format": "base64.png"}
    ]
    
    with open(history_file, "w", encoding="utf-8") as f:
        json.dump(fake_history, f)
        
    print("Wrote fake corrupted history.json.")
    
    try:
        engine = setup_interpreter()
        print("Engine setup completed successfully.")
        
        # Verify the history was scrubbed
        assert engine.messages[1]["type"] == "message"
        assert engine.messages[1]["content"] == "[Image attached in previous session]"
        print("✅ Scrubbing logic works perfectly!")
        
    except Exception as e:
        print(f"❌ Engine crashed during setup: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_history_scrub()
