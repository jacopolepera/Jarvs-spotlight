"""
core.vision.capture — Screen capture utility.

Provides a single function that grabs the primary screen and saves it to
the workspace's .tmp directory, returning the path to the saved image.
"""
import os
from PyQt6.QtWidgets import QApplication


def capture_screen(workspace_path: str) -> str:
    """
    Grabs the primary screen and saves it as vision_context.png
    inside <workspace_path>/.tmp/.

    Returns the absolute path to the saved PNG file.
    Raises RuntimeError if no QApplication is running.
    """
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("capture_screen requires a running QApplication.")

    screen = app.primaryScreen()
    img = screen.grabWindow(0)

    tmp_dir = os.path.join(workspace_path, ".tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    vision_path = os.path.join(tmp_dir, "vision_context.png")
    img.save(vision_path, "PNG")
    return vision_path
