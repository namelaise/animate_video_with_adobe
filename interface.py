import tkinter as tk
from tkinter.scrolledtext import ScrolledText
import threading
import sys
import os
from main import main  # ⚠️ importe bien ta fonction main() ici

class Redirector:
    def __init__(self, text_widget):
        self.text_widget = text_widget

    def write(self, text):
        self.text_widget.insert(tk.END, text)
        self.text_widget.see(tk.END)

    def flush(self):
        pass

def run_script():
    threading.Thread(target=main, daemon=True).start()

def create_gui():
    root = tk.Tk()
    root.title("Générateur Mr Martin 🎬")
    root.geometry("800x600")

    # Texte console
    text_area = ScrolledText(root, wrap=tk.WORD, font=("Courier", 10))
    text_area.pack(expand=True, fill='both')

    # Redirige print → interface
    sys.stdout = Redirector(text_area)
    sys.stderr = Redirector(text_area)

    # Bouton lancement
    launch_button = tk.Button(root, text="Lancer le traitement", command=run_script, font=("Arial", 14))
    launch_button.pack(pady=10)

    root.mainloop()

if __name__ == "__main__":
    create_gui()
