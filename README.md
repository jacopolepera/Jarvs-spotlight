# Jarvs Spotlight

Jarvs Spotlight is a fully local, lightning-fast AI agent workspace built with a beautiful frameless PyQt6 graphical interface. It acts as an OS-level "Spotlight Search" bar that seamlessly connects your desktop to powerful terminal execution via Open Interpreter and the Gemini AI engine.

## ✨ Features
- **Frameless Glass UI**: A gorgeous, draggable, and fully resizable PyQt6 interface that floats above your workspace. 
- **Voice Commands (Push-To-Talk)**: Hold `F4` globally from anywhere on your OS, speak your command into your microphone, and Jarvs will instantly transcribe and execute it.
- **Multi-Modal Vision & Files**: Click the `+` button in the UI to attach `.png` images or extract text from `.pdf` documents to feed rich context to the AI.
- **Autonomous Execution**: Powered by Open Interpreter running under the hood, Jarvs can write files, navigate your OS, run Python scripts, and test code dynamically.
- **Persistent Settings**: Saves your preferred window dimensions, themes, and LLM models locally.

## 🚀 Setup

1. **Install PortAudio** (Required for Voice Recording)
   - *Windows*: Pre-compiled wheels are usually available over PIP.
   - *Mac*: `brew install portaudio`
   - *Linux*: `sudo apt install portaudio19-dev`

2. **Install Python Dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure Environment**
   Create a `.env` file in the root directory and add your key:
   ```env
   GEMINI_API_KEY=your_key_here
   ```

## 💻 Usage

Run Jarvs in the background using the main Python UI file:
```bash
python tools/jarvs_ui.py
```
- A spotlight bar will appear on your screen.
- **Global Hotkey**: Press `Alt + Space` (by default) to summon or hide the interface instantly from anywhere.
- **Push To Talk**: Hold `F4` to record your voice. Release forms the instruction.

All AI workspace operations are contained safely within the isolated `workspace/` folder.
