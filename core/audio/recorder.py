import sounddevice as sd
import soundfile as sf
import threading
import queue


class VoiceRecorder:
    """Records audio from the default microphone into a WAV file."""

    def __init__(self, filename="temp_audio.wav", samplerate=44100, channels=1):
        self.filename = filename
        self.samplerate = samplerate
        self.channels = channels
        self.recording = False
        self.q = queue.Queue()
        self._thread = None

    def _record_callback(self, indata, frames, time, status):
        """Called for each audio block by the stream."""
        if status:
            pass  # Ignore warnings silently
        self.q.put(indata.copy())

    def start_recording(self):
        self.recording = True
        self.q = queue.Queue()  # Reset queue
        self._thread = threading.Thread(target=self._recording_thread)
        self._thread.start()

    def _recording_thread(self):
        with sf.SoundFile(self.filename, mode='w', samplerate=self.samplerate, channels=self.channels) as file:
            with sd.InputStream(samplerate=self.samplerate, channels=self.channels, callback=self._record_callback):
                while self.recording:
                    file.write(self.q.get())
                # Drain remaining items before closing
                while not self.q.empty():
                    file.write(self.q.get())

    def stop_recording(self):
        self.recording = False
        if self._thread:
            import numpy as np
            # Unblock the queue.get() in case no new audio arrives
            self.q.put(np.zeros((1, self.channels), dtype=np.int16))
            self._thread.join()
        return self.filename
