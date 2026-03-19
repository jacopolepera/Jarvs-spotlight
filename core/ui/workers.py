"""
core.ui.workers — Background QThread workers for Jarvs Spotlight.

Classes:
    GlobalWheelFilter         — App-level event filter blocking Ctrl+Scroll on QComboBox
    AIWorker                  — Streams LLM responses off the main GUI thread
    VoiceTranscriptionThread  — Calls the Gemini STT API off the main GUI thread
"""
import os
import json

from PyQt6.QtWidgets import QComboBox
from PyQt6.QtCore import Qt, QObject, QEvent, QThread, pyqtSignal


# ── GlobalWheelFilter ─────────────────────────────────────────────────────────

class GlobalWheelFilter(QObject):
    """Blocks Ctrl+Scroll on QComboBox only — prevents QFont size <= 0 crashes
    in native popups. ChatInputBox and OutputBox handle their own zoom internally.
    """
    def eventFilter(self, obj, event):
        if (
            event.type() == QEvent.Type.Wheel
            and event.modifiers() == Qt.KeyboardModifier.ControlModifier
            and isinstance(obj, QComboBox)
        ):
            return True  # Swallow the event
        return super().eventFilter(obj, event)


# ── AIWorker ──────────────────────────────────────────────────────────────────

class AIWorker(QThread):
    """Streams LLM responses from open-interpreter off the main GUI thread.

    Signals:
        chunk_received(str)  — emitted for each streamed text chunk
        finished(str)        — emitted with the full response when done
    """
    chunk_received = pyqtSignal(str)
    finished = pyqtSignal(str)

    def __init__(self, prompt: str, engine):
        super().__init__()
        self.prompt = prompt
        self.engine = engine
        self.full_response = ""

    def run(self):
        try:
            for chunk in self.engine.chat(self.prompt, stream=True, display=False):
                if not isinstance(chunk, dict):
                    continue

                role = chunk.get("role")
                type_ = chunk.get("type")

                if role not in ["assistant", "computer"]:
                    continue

                if chunk.get("start"):
                    if type_ == "code":
                        lang = chunk.get("format", "python")
                        prefix = f"\n```{lang}\n"
                        self.full_response += prefix
                        self.chunk_received.emit(prefix)
                    elif type_ == "console":
                        prefix = "\n```console\n"
                        self.full_response += prefix
                        self.chunk_received.emit(prefix)

                elif chunk.get("end"):
                    if type_ in ["code", "console"]:
                        suffix = "\n```\n"
                        self.full_response += suffix
                        self.chunk_received.emit(suffix)

                elif "content" in chunk:
                    content = chunk["content"]

                    # Skip active line number noise from console chunks
                    if type_ == "console" and chunk.get("format") == "active_line":
                        continue

                    if isinstance(content, str):
                        # Truncate massive base64 strings from console output
                        if type_ == "console" and len(content) > 1000 and " " not in content[:100]:
                            content = "\n\n*[Large binary/base64 output omitted]*\n\n"
                        self.full_response += content
                        self.chunk_received.emit(content)
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "message":
                                text = block.get("content", "")
                                self.full_response += text
                                self.chunk_received.emit(text)

        except Exception as e:
            err_msg = f"\n[Error: {str(e)}]"
            self.full_response += err_msg
            self.chunk_received.emit(err_msg)
        finally:
            self.finished.emit(self.full_response)
            self._persist_history()

    def _persist_history(self):
        """Dump conversation history to workspace/history.json for session continuity."""
        root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        history_file = os.path.join(root_dir, "workspace", "history.json")
        try:
            with open(history_file, "w", encoding="utf-8") as f:
                json.dump(self.engine.messages, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Failed to save long-term memory: {e}")


# ── VoiceTranscriptionThread ──────────────────────────────────────────────────

class VoiceTranscriptionThread(QThread):
    """Sends recorded audio to the Gemini STT API off the GUI thread.

    Signals:
        finished(str) — emitted with the transcribed text or an error string
    """
    finished = pyqtSignal(str)

    def __init__(self, audio_file_path: str, api_key: str):
        super().__init__()
        self.audio_file_path = audio_file_path
        self.api_key = api_key

    def run(self):
        try:
            from core.audio.transcriber import transcribe_audio_gemini
            text = transcribe_audio_gemini(self.audio_file_path, self.api_key)
            self.finished.emit(text)
        except Exception as e:
            self.finished.emit(f"[Voice Error: {e}]")
