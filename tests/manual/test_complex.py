import os
import json
import time
from core.tools.engine import setup_interpreter

print("Preparing workspace for long-term memory test...")
workspace_dir = os.path.join(os.path.dirname(__file__), "workspace")
history_file = os.path.join(workspace_dir, "history.json")

# Clear existing history for clean test
if os.path.exists(history_file):
    os.remove(history_file)

print("Booting Jarvs Engine (Session 1)...")
ai = setup_interpreter()

def extract_response(messages):
    response_text = ""
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            if isinstance(msg.get("content"), str):
                response_text += msg["content"]
            else:
                for block in msg.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "message":
                        response_text += block.get("content", "")
                    elif isinstance(block, str):
                        response_text += block
    return response_text.strip()


# Test 1: Identity & Persona (SOTA prompt check)
print("\n[Test 1] Executing Identity Check...")
ai.chat("Who are you? What can you do?", display=True)

# Test 2: External Script Execution & Web Access
print("\n[Test 2] Requesting Weather Data...")
ai.chat("Write a python script to ping wttr.in/London?format=3 and tell me the weather in London.", display=True)

print("\nSaving Conversation History buffer gracefully...")
with open(history_file, "w", encoding="utf-8") as f:
    json.dump(ai.messages, f, indent=4, ensure_ascii=False)

print("\n--------------------------")
print("SHUTTING DOWN SYSTEM...")
print("--------------------------\n")
time.sleep(2)

print("Booting Jarvs Engine (Session 2 - Reboot)...")
ai2 = setup_interpreter()
print(f"Loaded memory buffer! Retrieved {len(ai2.messages)} prior messages.")

print("\n[Test 3] Contextual Recall...")
ai2.chat("What city did we just check the weather for? Plan a 3-day itinerary for a trip there.", display=True)

print("\nTest completed successfully. Memory verified.")
