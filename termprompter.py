"""TermPrompter for Windows: offline, voice-following teleprompter.
Black screen, neon-green terminal text. Speech recognition is on-device (Vosk); no network is used."""
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
import zipfile
from tkinter import filedialog, messagebox, simpledialog

from core import Lang, Matcher, Profile, split_phrases, tokenize

NEON = "#39FF14"
DIM = "#1B6B10"
NEON_BG = "#0D330D"
AMBER = "#FFB000"
AMBER_BG = "#332400"
BLACK = "#000000"

APP_NAME = "TermPrompter"
DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
SCRIPT_DIR = os.path.join(DATA_DIR, "scripts")
USER_MODELS = os.path.join(DATA_DIR, "models")
SETTINGS = os.path.join(DATA_DIR, "settings.json")
APP_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
BUNDLED_MODELS = os.path.join(APP_DIR, "models")

SAMPLE = (
    "Welcome to TermPrompter.\n"
    "Read this text out loud, and the screen will follow your voice.\n"
    "If you stop to add your own comments, the text will wait for you.\n"
    "When you return to the script and finish the line, it moves on again.\n"
    "Click the text or press space to pause and resume. Press H for settings."
)

HELP = ("Space / click: play, pause, resume     R: reset     H: settings panel\n"
        "F11: full screen     Esc: leave full screen     + / -: text size     Ctrl+Q: quit")


# ---------------------------------------------------------------- models

def model_dirs(lang):
    return [os.path.join(BUNDLED_MODELS, lang.model_key), os.path.join(USER_MODELS, lang.model_key)]


def installed_dir(lang):
    for d in model_dirs(lang):
        if os.path.isdir(os.path.join(d, "am")) or os.path.isdir(os.path.join(d, "conf")):
            return d
    return None


def resolve_model(lang):
    """(dir, is_fallback). Bisaya falls back to the Filipino model if no Bisaya model is installed."""
    d = installed_dir(lang)
    if d:
        return d, False
    if lang == Lang.BISAYA:
        d = installed_dir(Lang.FILIPINO)
        if d:
            return d, True
    return None


def install_zip(path, lang):
    target = os.path.join(USER_MODELS, lang.model_key)
    if os.path.isdir(target):
        import shutil
        shutil.rmtree(target)
    os.makedirs(target, exist_ok=True)
    top = {"am", "conf", "graph", "ivector", "rescore", "rnnlm", "README"}
    canon = os.path.realpath(target)
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            parts = [p for p in info.filename.replace("\\", "/").split("/") if p]
            rel = parts[1:] if len(parts) > 1 and parts[0] not in top else parts
            if not rel:
                continue
            dest = os.path.realpath(os.path.join(target, *rel))
            if not dest.startswith(canon + os.sep):
                raise ValueError("Bad zip entry")
            if info.is_dir():
                os.makedirs(dest, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(info) as src, open(dest, "wb") as out:
                    out.write(src.read())
    if not (os.path.isdir(os.path.join(target, "am")) or os.path.isdir(os.path.join(target, "conf"))):
        import shutil
        shutil.rmtree(target, ignore_errors=True)
        raise ValueError("That zip does not look like a Vosk model (no am/ or conf/ folder).")


# ---------------------------------------------------------------- microphone + recognizer

class Listener:
    """Runs Vosk on a worker thread. Posts (session, text, is_final) / errors to a queue."""

    def __init__(self, out_q):
        self.q = out_q
        self.model = None
        self.model_path = None
        self.stop_evt = threading.Event()
        self.thread = None
        self.session = 0

    def start(self, model_dir):
        self.stop()
        self.session += 1
        sess = self.session
        self.stop_evt = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(model_dir, sess, self.stop_evt), daemon=True)
        self.thread.start()
        return sess

    def stop(self):
        self.stop_evt.set()
        self.session += 1

    def _run(self, model_dir, sess, stop_evt):
        try:
            import sounddevice as sd
            import vosk
            vosk.SetLogLevel(-1)
            if self.model is None or self.model_path != model_dir:
                self.model = vosk.Model(model_dir)
                self.model_path = model_dir
            rec = vosk.KaldiRecognizer(self.model, 16000)
            audio = queue.Queue()

            def cb(indata, frames, t, status):
                audio.put(bytes(indata))

            with sd.RawInputStream(samplerate=16000, blocksize=4000, dtype="int16", channels=1, callback=cb):
                self.q.put(("ready", sess, None))
                while not stop_evt.is_set():
                    try:
                        data = audio.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    if rec.AcceptWaveform(data):
                        self.q.put(("text", sess, (json.loads(rec.Result()).get("text", ""), True)))
                    else:
                        p = json.loads(rec.PartialResult()).get("partial", "")
                        if p:
                            self.q.put(("text", sess, (p, False)))
        except Exception as e:  # noqa: BLE001
            self.q.put(("error", sess, f"{type(e).__name__}: {e}"))


# ---------------------------------------------------------------- app

class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        root.configure(bg=BLACK)
        root.geometry("1000x700")
        os.makedirs(SCRIPT_DIR, exist_ok=True)

        s = self.load_settings()
        self.lang = next((l for l in Lang if l.code == s.get("lang")), Lang.ENGLISH)
        self.timer_mode = bool(s.get("timer", False))
        self.tol = int(s.get("tol", 3))
        self.speed = float(s.get("speed", 60))          # pixels per second (timer mode)
        self.size = int(s.get("size", 32))
        self.script = s.get("script") or SAMPLE

        self.matcher = Matcher()
        self.phrases = []
        self.playing = False
        self.started = False
        self.busy = False
        self.session = 0
        self.cur = 0
        self.adlib = False
        self.anim_target = None
        self.ui_q = queue.Queue()
        self.listener = Listener(self.ui_q)

        self.font = tkfont.Font(family="Consolas", size=self.size)
        self.font_bold = tkfont.Font(family="Consolas", size=self.size, weight="bold")

        self.text = tk.Text(root, bg=BLACK, fg=NEON, insertbackground=BLACK, relief="flat", bd=0,
                            highlightthickness=0, wrap="word", cursor="arrow", padx=60,
                            font=self.font, state="disabled", spacing1=10, spacing3=10)
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("read", foreground=DIM)
        self.text.tag_configure("cur", foreground=NEON, background=NEON_BG, font=self.font_bold)
        self.text.tag_configure("first", spacing1=200)
        self.text.tag_configure("last", spacing3=400)
        self.text.tag_raise("cur")
        self.text.bind("<Button-1>", self.on_click)
        self.text.bind("<B1-Motion>", lambda e: "break")
        self.text.bind("<Double-Button-1>", lambda e: "break")
        self.text.bind("<Configure>", self.on_resize)

        self.status = tk.Label(root, bg=BLACK, fg=NEON, font=("Consolas", 10), anchor="w", padx=8, pady=3)
        self.status.place(x=0, y=0)
        self.menu_btn = tk.Label(root, text=" ⋮ ", bg=BLACK, fg=NEON, font=("Consolas", 20), cursor="hand2")
        self.menu_btn.place(relx=1.0, y=0, anchor="ne")
        self.menu_btn.bind("<Button-1>", lambda e: self.toggle_panel())

        self.build_panel()
        self.panel_visible = True

        for key, fn in (("<space>", self.toggle_play), ("r", self.reset), ("R", self.reset),
                        ("h", self.toggle_panel), ("H", self.toggle_panel), ("<F11>", self.toggle_fullscreen),
                        ("<Escape>", self.leave_fullscreen), ("<plus>", lambda: self.bump(2)),
                        ("<equal>", lambda: self.bump(2)), ("<minus>", lambda: self.bump(-2)),
                        ("<Control-q>", self.quit)):
            root.bind(key, lambda e, f=fn: (f(), "break")[1])

        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.fullscreen = False
        self.set_script(self.script)
        self.refresh_buttons()
        self.set_status()
        self.root.after(50, self.poll)
        self.root.after(100, self.toggle_fullscreen)    # start full screen

    # ---------- settings
    def load_settings(self):
        try:
            with open(SETTINGS, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return {}

    def save_settings(self):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump({"lang": self.lang.code, "timer": self.timer_mode, "tol": self.tol,
                           "speed": self.speed, "size": self.size, "script": self.script}, f)
        except Exception:  # noqa: BLE001
            pass

    # ---------- panel
    def build_panel(self):
        self.panel = tk.Frame(self.root, bg=BLACK, highlightbackground=NEON, highlightthickness=1)
        self.panel.place(relx=0, rely=1.0, relwidth=1.0, anchor="sw")

        def btn(parent, text, cmd):
            b = tk.Button(parent, text=text, command=cmd, bg=BLACK, fg=NEON, activebackground=NEON_BG,
                          activeforeground=NEON, relief="flat", bd=0, highlightthickness=1,
                          highlightbackground=NEON, font=("Consolas", 11), padx=10, pady=4, takefocus=0)
            b.pack(side="left", padx=4, pady=4)
            return b

        row1 = tk.Frame(self.panel, bg=BLACK)
        row1.pack(fill="x", padx=8, pady=(8, 0))
        self.btn_play = btn(row1, "Play", self.toggle_play)
        btn(row1, "Reset", self.reset)
        btn(row1, "Script ▾", self.script_menu)
        self.btn_lang = btn(row1, "Lang", self.lang_menu)
        self.btn_mode = btn(row1, "Mode", self.toggle_mode)
        btn(row1, "Full screen", self.toggle_fullscreen)
        btn(row1, "Quit", self.quit)

        def slider(label, lo, hi, init, cb):
            f = tk.Frame(self.panel, bg=BLACK)
            f.pack(fill="x", padx=12, pady=2)
            lb = tk.Label(f, bg=BLACK, fg=NEON, font=("Consolas", 10), width=44, anchor="w")
            lb.pack(side="left")
            sc = tk.Scale(f, from_=lo, to=hi, orient="horizontal", showvalue=False, bg=BLACK, fg=NEON,
                          troughcolor=NEON_BG, highlightthickness=0, bd=0, activebackground=NEON, length=360,
                          sliderrelief="flat", takefocus=0, command=lambda v: cb(int(float(v)), lb))
            sc.set(init)
            sc.pack(side="left", fill="x", expand=True)
            cb(init, lb)
            return sc

        slider("size", 14, 120, self.size, self.on_size)
        slider("speed", 10, 400, int(self.speed), self.on_speed)
        slider("tol", 1, 5, self.tol, self.on_tol)

        tk.Label(self.panel, text=HELP, bg=BLACK, fg=DIM, font=("Consolas", 9), justify="left",
                 anchor="w").pack(fill="x", padx=12, pady=(4, 8))

    def toggle_panel(self):
        if self.panel_visible:
            self.panel.place_forget()
        else:
            self.panel.place(relx=0, rely=1.0, relwidth=1.0, anchor="sw")
            self.panel.lift()
        self.panel_visible = not self.panel_visible

    def on_size(self, v, lb):
        self.size = v
        self.font.configure(size=v)
        self.font_bold.configure(size=v)
        lb.config(text=f"Text size: {v} pt")
        self.save_settings()

    def on_speed(self, v, lb):
        self.speed = float(v)
        lb.config(text=f"Timer-mode speed: {v} px/s")
        self.save_settings()

    def on_tol(self, v, lb):
        self.tol = v
        self.matcher.profile = Profile(v, self.lang)
        names = ["strict", "tight", "balanced", "relaxed", "very forgiving"]
        lb.config(text=f"Voice tolerance: {v}/5  ({names[v - 1]})")
        self.save_settings()

    def bump(self, d):
        self.size = max(14, min(120, self.size + d))
        self.font.configure(size=self.size)
        self.font_bold.configure(size=self.size)
        self.save_settings()

    # ---------- script
    def set_script(self, text):
        self.pause()
        self.script = text
        self.phrases = split_phrases(text, self.lang)
        self.matcher.profile = Profile(self.tol, self.lang)
        self.matcher.load(self.phrases)
        self.cur, self.adlib, self.started = 0, False, False
        t = self.text
        t.config(state="normal")
        t.delete("1.0", "end")
        for p in self.phrases:
            t.insert("end", p.text + "\n")
        t.config(state="disabled")
        t.tag_remove("first", "1.0", "end")
        t.tag_remove("last", "1.0", "end")
        if self.phrases:
            t.tag_add("first", "1.0", "2.0")
            n = len(self.phrases)
            t.tag_add("last", f"{n}.0", f"{n + 1}.0")
        self.on_resize(None)
        self.paint()
        t.yview_moveto(0)
        self.refresh_buttons()
        self.set_status()
        self.save_settings()

    def on_resize(self, _e):
        h = max(200, self.text.winfo_height())
        self.text.tag_configure("first", spacing1=int(h * 0.25))
        self.text.tag_configure("last", spacing3=int(h * 0.75))

    def paint(self):
        t = self.text
        for tag in ("read", "cur"):
            t.tag_remove(tag, "1.0", "end")
        n = len(self.phrases)
        c = max(0, min(self.cur, n))
        if c > 0:
            t.tag_add("read", "1.0", f"{c + 1}.0")
        if c < n:
            t.tag_add("cur", f"{c + 1}.0", f"{c + 2}.0")
            t.tag_configure("cur", foreground=AMBER if self.adlib else NEON,
                            background=AMBER_BG if self.adlib else NEON_BG)

    def script_menu(self):
        m = tk.Menu(self.root, tearoff=0, bg=BLACK, fg=NEON, activebackground=NEON_BG, activeforeground=NEON)
        m.add_command(label="Paste / edit script…", command=self.edit_dialog)
        m.add_command(label="Open text file…", command=self.open_file)
        m.add_separator()
        m.add_command(label="Save script…", command=self.save_script)
        m.add_command(label="Load saved script…", command=self.load_saved)
        m.add_command(label="Delete saved script…", command=self.delete_saved)
        x = self.root.winfo_pointerx()
        y = self.root.winfo_pointery()
        m.tk_popup(x, y)

    def edit_dialog(self):
        win = tk.Toplevel(self.root)
        win.title("Paste or type your script")
        win.configure(bg=BLACK)
        win.geometry("760x520")
        win.transient(self.root)
        box = tk.Text(win, bg=BLACK, fg=NEON, insertbackground=NEON, font=("Consolas", 13), wrap="word",
                      relief="flat", highlightthickness=1, highlightbackground=NEON, padx=10, pady=10)
        box.pack(fill="both", expand=True, padx=8, pady=8)
        box.insert("1.0", self.script)
        box.focus_set()
        bar = tk.Frame(win, bg=BLACK)
        bar.pack(fill="x", padx=8, pady=(0, 8))

        def use():
            txt = box.get("1.0", "end").strip("\n")
            win.destroy()
            self.set_script(txt)

        for label, cmd in (("Use", use), ("Cancel", win.destroy)):
            tk.Button(bar, text=label, command=cmd, bg=BLACK, fg=NEON, activebackground=NEON_BG,
                      activeforeground=NEON, relief="flat", highlightthickness=1, highlightbackground=NEON,
                      font=("Consolas", 11), padx=14, pady=4).pack(side="left", padx=4)

    def open_file(self):
        path = filedialog.askopenfilename(title="Open script", filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        for enc in ("utf-8-sig", "utf-16", "cp1252"):
            try:
                with open(path, "r", encoding=enc) as f:
                    self.set_script(f.read())
                return
            except Exception:  # noqa: BLE001
                continue
        messagebox.showerror(APP_NAME, "Could not read that file.")

    def save_script(self):
        name = simpledialog.askstring(APP_NAME, "Script name:", parent=self.root)
        if not name or not name.strip():
            return
        safe = "".join(c if c.isalnum() or c in " _.-" else "_" for c in name.strip())[:60] or "script"
        with open(os.path.join(SCRIPT_DIR, safe + ".txt"), "w", encoding="utf-8") as f:
            f.write(self.script)

    def load_saved(self):
        path = filedialog.askopenfilename(title="Load saved script", initialdir=SCRIPT_DIR,
                                          filetypes=[("Text files", "*.txt")])
        if path:
            with open(path, "r", encoding="utf-8") as f:
                self.set_script(f.read())

    def delete_saved(self):
        path = filedialog.askopenfilename(title="Delete which saved script?", initialdir=SCRIPT_DIR,
                                          filetypes=[("Text files", "*.txt")])
        if path and os.path.dirname(os.path.realpath(path)) == os.path.realpath(SCRIPT_DIR):
            if messagebox.askyesno(APP_NAME, f"Delete {os.path.basename(path)}?"):
                os.remove(path)

    # ---------- language / models
    def lang_menu(self):
        m = tk.Menu(self.root, tearoff=0, bg=BLACK, fg=NEON, activebackground=NEON_BG, activeforeground=NEON)
        for l in Lang:
            r = resolve_model(l)
            state = "model installed" if r and not r[1] else ("using Filipino model" if r else "no model: import one")
            m.add_command(label=("● " if l == self.lang else "○ ") + f"{l.label}   [{state}]",
                          command=lambda l=l: self.set_lang(l))
        m.add_separator()
        m.add_command(label="Import voice model (.zip)…", command=self.import_model)
        m.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

    def set_lang(self, l):
        self.pause()
        self.lang = l
        self.set_script(self.script)
        self.refresh_buttons()
        if not resolve_model(l):
            messagebox.showinfo(APP_NAME, f"No offline voice model for {l.label} yet.\n"
                                          "Use Lang > Import voice model (.zip), or use Timer mode.")

    def import_model(self):
        win = tk.Toplevel(self.root)
        win.title("Import voice model")
        win.configure(bg=BLACK)
        win.transient(self.root)
        tk.Label(win, text="Import the model for:", bg=BLACK, fg=NEON, font=("Consolas", 11)).pack(padx=16, pady=(12, 6))

        def pick(l):
            win.destroy()
            path = filedialog.askopenfilename(title=f"Vosk model zip for {l.label}", filetypes=[("Zip", "*.zip")])
            if not path:
                return
            self.busy = True
            self.set_status("Installing model…")

            def work():
                err = None
                try:
                    install_zip(path, l)
                except Exception as e:  # noqa: BLE001
                    err = str(e)
                self.ui_q.put(("imported", 0, (l, err)))

            threading.Thread(target=work, daemon=True).start()

        for l in Lang:
            tk.Button(win, text=l.label, command=lambda l=l: pick(l), bg=BLACK, fg=NEON, activebackground=NEON_BG,
                      activeforeground=NEON, relief="flat", highlightthickness=1, highlightbackground=NEON,
                      font=("Consolas", 11), width=24, pady=4).pack(padx=16, pady=3)
        tk.Frame(win, height=10, bg=BLACK).pack()

    # ---------- playback
    def toggle_play(self):
        if self.playing:
            self.pause()
        else:
            self.play()

    def on_click(self, _e):
        self.toggle_play()
        return "break"

    def toggle_mode(self):
        self.pause()
        self.timer_mode = not self.timer_mode
        self.save_settings()
        self.refresh_buttons()
        self.set_status()

    def play(self):
        if self.busy:
            return
        if not self.phrases:
            messagebox.showinfo(APP_NAME, "Add a script first.")
            return
        if self.timer_mode:
            self.playing = self.started = True
            self.last_t = time.perf_counter()
            self.rem = 0.0
            self.acc = 0.0
            if self.panel_visible:
                self.toggle_panel()
            self.refresh_buttons()
            self.set_status()
            self.tick()
            return
        res = resolve_model(self.lang)
        if not res:
            messagebox.showinfo(APP_NAME, f"No offline voice model for {self.lang.label}.\n"
                                          "Use Lang > Import voice model (.zip), or switch to Timer mode.")
            return
        self.busy = True
        self.set_status("Loading voice model…")
        self.matcher.end_utterance()
        self.session = self.listener.start(res[0])

    def pause(self):
        if not self.playing and not self.busy:
            return
        self.playing = False
        self.busy = False
        self.listener.stop()
        self.matcher.end_utterance()
        self.refresh_buttons()
        self.set_status()

    def reset(self):
        self.pause()
        self.started = False
        self.matcher.reset()
        self.cur, self.adlib = 0, False
        self.paint()
        self.text.yview_moveto(0)
        self.refresh_buttons()
        self.set_status()

    def poll(self):
        try:
            while True:
                kind, sess, payload = self.ui_q.get_nowait()
                if kind == "imported":
                    self.busy = False
                    l, err = payload
                    if err:
                        messagebox.showerror(APP_NAME, err)
                    else:
                        messagebox.showinfo(APP_NAME, f"{l.label} model installed.")
                    self.set_status()
                    continue
                if sess != self.session:
                    continue
                if kind == "ready" and self.busy:
                    self.busy = False
                    self.playing = self.started = True
                    if self.panel_visible:
                        self.toggle_panel()
                    self.refresh_buttons()
                    self.apply_state()
                elif kind == "error":
                    self.pause()
                    messagebox.showerror(APP_NAME, "Voice error: " + str(payload))
                elif kind == "text" and self.playing:
                    text, final = payload
                    changed = self.matcher.feed(tokenize(text, self.lang))
                    if final:
                        self.matcher.end_utterance()
                    if changed:
                        self.apply_state()
        except queue.Empty:
            pass
        self.root.after(40, self.poll)

    def apply_state(self):
        m = self.matcher
        self.adlib = m.adlib
        if m.finished:
            self.cur = len(self.phrases)
            self.paint()
            self.pause()
            self.started = False
            self.refresh_buttons()
            self.set_status("END OF SCRIPT")
            return
        if m.cur != self.cur:
            self.cur = m.cur
            self.paint()
            self.glide_to(self.cur)
        else:
            self.paint()
        self.set_status()

    def glide_to(self, i):
        first = self.anim_target is None
        self.anim_target = i
        if first:
            self.animate()

    def animate(self):
        i = self.anim_target
        if i is None:
            return
        info = self.text.dlineinfo(f"{i + 1}.0")
        if info is None:
            self.text.see(f"{i + 1}.0")
            self.root.after(16, self.animate)
            return
        err = info[1] - int(self.text.winfo_height() * 0.25)
        if abs(err) <= 2:
            self.anim_target = None
            return
        step = int(err * 0.18)
        if step == 0:
            step = 1 if err > 0 else -1
        before = self.text.yview()
        self.text.yview_scroll(step, "pixels")
        if self.text.yview() == before:          # hit the end of the document
            self.anim_target = None
            return
        self.root.after(16, self.animate)

    # Timer mode: constant-speed scroll; highlight follows the reading line.
    def tick(self):
        if not (self.playing and self.timer_mode):
            return
        now = time.perf_counter()
        dt = now - self.last_t
        self.last_t = now
        self.rem += self.speed * dt
        dy = int(self.rem)
        if dy:
            self.text.yview_scroll(dy, "pixels")
            self.rem -= dy
        self.acc += dt
        if self.acc > 0.25:
            self.acc = 0
            line = int(self.text.index(f"@20,{int(self.text.winfo_height() * 0.25) + 2}").split(".")[0])
            p = max(0, min(len(self.phrases) - 1, line - 1))
            if p != self.cur:
                self.cur = p
                self.paint()
        if self.text.yview()[1] >= 0.999:
            self.pause()
            self.set_status("END OF SCRIPT")
            return
        self.root.after(16, self.tick)

    # ---------- misc ui
    def refresh_buttons(self):
        self.btn_play.config(text="Pause" if self.playing else ("Resume" if self.started else "Play"))
        self.btn_mode.config(text="Mode: Timer" if self.timer_mode else "Mode: Voice")
        self.btn_lang.config(text=f"Lang: {self.lang.code.upper()}")

    def set_status(self, msg=None):
        if msg is None:
            if self.busy:
                msg = "WORKING…"
            elif not self.started:
                msg = "READY"
            elif not self.playing:
                msg = "PAUSED"
            elif self.timer_mode:
                msg = "TIMER"
            elif self.matcher.adlib:
                msg = "AD-LIB: waiting for script"
            else:
                msg = "LISTENING"
        res = None if self.timer_mode else resolve_model(self.lang)
        extra = " (Filipino model)" if res and res[1] else ""
        self.status.config(text=f"{msg} · {self.lang.code.upper()}{extra}", fg=AMBER if self.adlib and self.playing else NEON)

    def toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        self.root.attributes("-fullscreen", self.fullscreen)

    def leave_fullscreen(self):
        if self.fullscreen:
            self.fullscreen = False
            self.root.attributes("-fullscreen", False)

    def quit(self):
        self.pause()
        self.save_settings()
        self.root.destroy()


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        root = tk.Tk()
        App(root)
        root.mainloop()
    except Exception:  # noqa: BLE001
        with open(os.path.join(DATA_DIR, "crash.log"), "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
