"""Modern Lab - start the window.

The application lives in app/application.py and the panel in ui/panel.py; this file
only opens a root window and starts the event loop. It also re-exports the names the
application uses at module scope (messagebox, attached_scopes), because callers that
used to reach them through main still do - patching main.messagebox has to patch the
object the application actually calls.
"""
import tkinter as tk
from tkinter import messagebox

from modernlab.settings.bench import attached_scopes

from app.application import App

__all__ = ["App", "attached_scopes", "main", "messagebox"]


def main():
    root = tk.Tk()
    app = App(root)
    root.update_idletasks()
    width = root.winfo_width()
    height = root.winfo_height()
    x = (root.winfo_screenwidth() // 2) - (width // 2)
    y = (root.winfo_screenheight() // 2) - (height // 2)
    root.geometry(f"{width}x{height}+{x}+{y}")
    root.mainloop()


if __name__ == "__main__":
    main()
