import os
from dotenv import load_dotenv
from interpreter import interpreter
from tools.settings_manager import SettingsManager

# Load settings manager
settings = SettingsManager()


def reload_instructions(interpreter_instance, settings):
    """Reloads the base prompt and the long term memory text buffer from disk."""
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    workspace_dir = settings.get_workspace_path()
    prompt_file = os.path.join(root_dir, "system_prompt.txt")
    
    custom_instructions = ""
    if os.path.exists(prompt_file):
        try:
            with open(prompt_file, "r", encoding="utf-8") as f:
                custom_instructions = f.read()
        except Exception as e:
            print(f"Warning: Failed to load system_prompt.txt: {e}")
            
    # Load long-term background memory
    long_term_memory = ""
    memory_file = os.path.join(workspace_dir, "long_term_memory.txt")
    if os.path.exists(memory_file):
        try:
            with open(memory_file, "r", encoding="utf-8") as f:
                long_term_memory = f.read().strip()
        except Exception as e:
            pass
            
    # Ghost mode tmp directory
    tmp_dir = os.path.join(workspace_dir, ".tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    # Append hard constraint about absolute paths and past context
    custom_instructions += f"\n\n[CRITICAL SYSTEM ENFORCEMENT]\nYour absolute workspace path is: {workspace_dir}\nYou MUST execute relative commands or save files inside this exact absolute path.\n\n[GHOST MODE SILENCE]\nYou MUST use the hidden directory `{tmp_dir}` for caching data, web scraping downloads, temporary images, or writing heavy background scripts to keep the root directory pristine."
    
    if long_term_memory:
        custom_instructions += f"\n\n[LONG TERM MEMORY CONTEXT]\nThis is a summary of your previous interactions with the user over past sessions. Use this to remember their preferences and ongoing projects:\n{long_term_memory}"
    
    interpreter_instance.custom_instructions = custom_instructions


def setup_interpreter():
    """
    Phase 2: Link.
    """
    api_key = settings.get_api_key()
    if not api_key or api_key == "inserisci_qui_la_tua_chiave":
        raise ValueError(
            "CRITICAL: GEMINI_API_KEY not found or not set in settings.\n"
            "Please open the Settings menu and set your real API key."
        )

    interpreter.llm.model = settings.get_model()
    interpreter.llm.api_key = api_key
    interpreter.llm.supports_vision = True  # Enable multimodal vision capabilities
    interpreter.auto_run = True
    
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    workspace_dir = settings.get_workspace_path()
    
    # Ensure workspace exists
    os.makedirs(workspace_dir, exist_ok=True)
    
    # Hydrate custom instructions and memory
    reload_instructions(interpreter, settings)

    # Hydrate short-term Memory (Context Persistency)
    import json
    history_file = os.path.join(workspace_dir, "history.json")
    if os.path.exists(history_file):
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                interpreter.messages = json.load(f)
        except Exception as e:
            print(f"Warning: Failed to load history.json: {e}")

    return interpreter


if __name__ == "__main__":
    # Phase 1 Handshake Test
    print("=" * 40)
    print("  Jarvs Spotlight: Phase 1 Handshake")
    print("=" * 40)
    print("Initiating Link with Gemini API...")

    try:
        ai = setup_interpreter()
        # In open-interpreter 0.4.x, chat() called with stream=False returns messages list
        messages = ai.chat("Say exactly and only: 'Link Established.'", stream=False, display=False)
        
        # Debug print
        print(f"Raw messages type: {type(messages)}")
        print(f"Raw messages content: {messages}")

        # Extract the last assistant message
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
            elif isinstance(msg, str):
                response_text += msg

        print(f"Extracted Response: {response_text}")
        if response_text.strip():
            print("\n✅ HANDSHAKE SUCCESSFUL. Ready for Phase 2.")
        else:
            print("\n⚠️  Got a response but no text extracted. Check message structure.")
    except ValueError as e:
        print(f"\n❌ {e}")
    except Exception as e:
        print(f"\n❌ Unexpected error: {type(e).__name__}: {e}")

