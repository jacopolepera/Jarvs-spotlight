import os
import json
from datetime import datetime

LOG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "requests.json")
MAX_LOGS = 20

class Logger:
    @staticmethod
    def log_interaction(prompt, response):
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        
        logs = []
        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, "r", encoding="utf-8") as f:
                    logs = json.load(f)
            except json.JSONDecodeError:
                pass
                
        # Append new interaction
        logs.append({
            "timestamp": datetime.now().isoformat(),
            "prompt": prompt,
            "response": response
        })
        
        # Keep only the most recent MAX_LOGS
        if len(logs) > MAX_LOGS:
            logs = logs[-MAX_LOGS:]
            
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(logs, f, indent=4, ensure_ascii=False)
