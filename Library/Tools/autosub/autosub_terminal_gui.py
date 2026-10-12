#!/usr/bin/env python3
import os
import sys
import subprocess
import threading
import signal
import atexit
import tkinter as tk
from tkinter import font
import queue
import re
import time

def get_style_segments(styles):
    if not styles:
        return []
    segments = []
    start = 0
    current_style = styles[0]
    for i in range(1, len(styles)):
        if styles[i] != current_style:
            segments.append((start, i, current_style))
            start = i
            current_style = styles[i]
    segments.append((start, len(styles), current_style))
    return segments

class TerminalGUI:
    def __init__(self, root, cmd):
        self.root = root
        self.cmd = cmd
        self.proc = None
        self.queue = queue.Queue()
        self.ansi_running = False
        self.ansi_buffer = ""
        self.engine_started = False
        self._needs_full_flush = False
        
        # Screen buffer for flicker-free double buffering
        self.virtual_lines = [[]]
        self.virtual_styles = [[]]
        self.cursor_row = 0
        
        # Off-Screen Back-Buffers for Parsing
        self.back_lines = [[]]
        self.back_styles = [[]]
        self.back_cursor_row = 0
        
        self.current_style = "normal"
        self.current_fg = None
        self.current_bold = False
        
        self.displayed_lines = []
        self.written_rows = set()
        self.last_data_time = 0.0
        self.last_render_time = 0.0
        
        # Premium Dark Sleek Styling
        self.root.title("AutoSub Engine Console")
        self.root.geometry("1100x650")
        self.root.configure(bg="#0B0F19")  # Deep space dark background

        # Set Window Icon (also sets Dock/Cmd+Tab icon on macOS via -default)
        _dir = os.path.dirname(os.path.abspath(__file__))
        _icon_name = "autosub_launcher_neon.png" if sys.platform == "darwin" else "autosub.ico"
        _icon_path = os.path.join(_dir, _icon_name)
        if os.path.exists(_icon_path):
            try:
                if sys.platform == "darwin":
                    self._icon_img = tk.PhotoImage(file=_icon_path)
                    self.root.tk.call('wm', 'iconphoto', self.root._w, '-default', self._icon_img)
                else:
                    self.root.iconbitmap(_icon_path)
            except Exception:
                pass
        
        # Configure Grid: row 0 = text area, row 1 = input bar (initially visible)
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_rowconfigure(1, weight=0)
        self.root.grid_columnconfigure(0, weight=1)
        
        # Select premium Mac monospace fonts
        self.custom_font = font.Font(family="Courier", size=15)
        if sys.platform == "darwin":
            preferred_fonts = [
                ("Sarasa Mono SC", 15), ("Sarasa Mono HC", 15), ("Sarasa Mono TC", 15),
                ("Sarasa Mono J", 15), ("Menlo", 13), ("Monaco", 13), ("Courier New", 15)
            ]
            available = font.families()
            for f_name, f_size in preferred_fonts:
                if f_name in available:
                    self.custom_font = font.Font(family=f_name, size=f_size)
                    break
        
        custom_font = self.custom_font
        
        # High-Tech Monospace Terminal Display Area
        self.text_area = tk.Text(
            self.root, wrap="none", bg="#0B0F19", fg="#38BDF8",  # Cyberpunk sky blue text
            insertbackground="#0B0F19", selectbackground="#1E293B",
            insertwidth=0, insertofftime=0, font=custom_font, borderwidth=0, highlightthickness=0,
            padx=16, pady=16, state="disabled"
        )
        self.text_area.grid(row=0, column=0, sticky="nsew")
        
        # Premium color palette matching the sleek deep space theme
        self.color_map = {
            "30": "#1E293B",  # Slate 800 (Dark Slate)
            "31": "#EF4444",  # Red (Coral Red)
            "32": "#22C55E",  # Green (Emerald Green)
            "33": "#EAB308",  # Yellow (Amber Gold)
            "34": "#3B82F6",  # Blue (Royal Blue)
            "35": "#D946EF",  # Magenta (Electric Pink)
            "36": "#06B6D4",  # Cyan (Vibrant Cyan)
            "37": "#E2E8F0",  # Slate 200 (Default Light Text)
            "90": "#64748B",  # Bright Black (Slate 500)
            "91": "#F87171",  # Bright Red (Soft Coral)
            "92": "#4ADE80",  # Bright Green (Vibrant Emerald)
            "93": "#FACC15",  # Bright Yellow (Premium Gold)
            "94": "#60A5FA",  # Bright Blue (Sky Blue)
            "95": "#F472B6",  # Bright Magenta (Soft Pink)
            "96": "#38BDF8",  # Bright Cyan (Cyberpunk Sky Blue)
            "97": "#F8FAFC",  # Bright White (Pure Slate White)
        }
        
        self.bold_font = font.Font(family=self.custom_font.cget("family"), size=self.custom_font.cget("size"), weight="bold")
        
        # Configure tags in Text widget
        # NOTE: We intentionally do NOT attach font=self.bold_font to any tag.
        # Menlo's bold variant renders 103 Unicode chars (box-drawing, block
        # elements, etc.) at a different width (17px or 10px instead of 11px).
        # Attaching the bold font to styled text causes grid misalignment
        # whenever a row's ANSI bold tag is applied or changes.
        # Visual emphasis is carried by the bright color palette (codes 90-97),
        # which already differs from the normal palette (30-37).
        for code, hex_color in self.color_map.items():
            self.text_area.tag_configure(f"fg_{code}", foreground=hex_color)
            self.text_area.tag_configure(f"fg_{code}_bold", foreground=hex_color)
            
        self.text_area.tag_configure("bold")
        self.text_area.tag_configure("normal", font=self.custom_font)
        
        # Cache style tag names for fast tag_remove (avoids calling tag_names() per line)
        self._style_tag_cache = [f"fg_{c}" for c in self.color_map] + [f"fg_{c}_bold" for c in self.color_map] + ["bold"]

        # Custom Scrollbar
        scrollbar = tk.Scrollbar(self.root, command=self.text_area.yview, bg="#0B0F19", borderwidth=0, highlightthickness=0)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.text_area.config(yscrollcommand=scrollbar.set)
        
        # --- Bottom Input Bar (for launcher prompts: URLs, backspace editing) ---
        self.input_frame = tk.Frame(self.root, bg="#131927", highlightbackground="#1E293B", highlightthickness=1)
        self.input_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=0, pady=0)
        
        input_label = tk.Label(self.input_frame, text=" ❯ ", bg="#131927", fg="#00F5FF", font=custom_font)
        input_label.pack(side="left")
        
        self.input_entry = tk.Entry(
            self.input_frame, bg="#131927", fg="#00F5FF",
            insertbackground="#00F5FF", insertwidth=3,
            font=custom_font, borderwidth=0, highlightthickness=0
        )
        self.input_entry.pack(side="left", fill="x", expand=True, padx=(0, 16), pady=4)
        
        # Bind Return/Enter on Entry
        self.input_entry.bind("<Return>", self.on_entry_return)
        self.input_entry.bind("<KP_Enter>", self.on_entry_return)
        
        # Bind global key events directly to root window
        self.root.bind("<Key>", self.on_global_key)
        
        # Lock cursor/focus to entry during launcher, but allow selection
        self.text_area.bind("<Button-1>", self.on_text_click)
        self.text_area.bind("<B1-Motion>", lambda e: None)
        self.text_area.bind("<Double-Button-1>", lambda e: None)
        self.text_area.bind("<Triple-Button-1>", lambda e: None)
        
        # Bind resize on the text_area (the main display region)
        self.text_area.bind("<Configure>", self.on_resize)
        
        # Bring window to front and force focus on launch
        if sys.platform == "darwin":
            self.root.update_idletasks()
            self.root.lift()
            self.root.focus_force()
            
        # Initial focus on the input entry
        self.root.after(100, lambda: self.input_entry.focus_set())
            
        # Safe Window Termination
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        
        # Launch engine process
        self.start_process()
        
        # Start GUI queue poller
        self.root.after(50, self.poll_queue)
        
    def start_process(self):
        try:
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONUNBUFFERED"] = "1"
            env["TERM"] = "xterm-color"  # Hint to command line tools
            
            preexec = None
            if sys.platform != "win32":
                preexec = os.setsid
                
            self.proc = subprocess.Popen(
                self.cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=os.path.dirname(os.path.abspath(__file__)),
                preexec_fn=preexec
            )
            
            # Start background stdout listener thread
            threading.Thread(target=self.read_output, daemon=True).start()
            
            # Register cleanup handlers so force-quit / terminal close also kills children
            atexit.register(self._kill_children)
            for sig in (signal.SIGTERM, signal.SIGHUP):
                try:
                    signal.signal(sig, lambda s, f: self._kill_children())
                except (OSError, ValueError):
                    pass
        except Exception as e:
            self.text_area.config(state="normal")
            self.text_area.insert("end", f"❌ Failed to start engine: {e}\n")
            self.text_area.config(state="disabled")
            
    def read_output(self):
        try:
            import codecs
            decoder = codecs.getincrementaldecoder('utf-8')()
            
            while self.proc and self.proc.poll() is None:
                # Read in chunks for efficiency — this dramatically reduces
                # thread context switches and ensures more of each frame
                # arrives together, making frame boundary detection reliable.
                chunk = self.proc.stdout.read1(4096) if hasattr(self.proc.stdout, 'read1') else self.proc.stdout.read(1)
                if not chunk:
                    break
                
                text = decoder.decode(chunk)
                if text:
                    self.queue.put(text)
        except Exception as e:
            self.queue.put(f"\n[Console Error] Reader thread closed: {e}\n")
        finally:
            # Auto-close the window when the process completes
            try:
                self.root.after(100, self.on_close)
            except Exception:
               pass

    def _process_char(self, char):
        """Process a single character through the ANSI state machine into back-buffer."""
        if char == '\x1b':
            self.ansi_running = True
            self.ansi_buffer = '\x1b'
            return
        elif self.ansi_running:
            self.ansi_buffer += char
            if char.isalpha() and char != '[':
                self.ansi_running = False
                cmd = char
                params = [int(x) for x in re.findall(r'\d+', self.ansi_buffer)]
                val = params[0] if params else 1
                if cmd in ('H', 'f'):
                    if self.engine_started:
                        self._swap_frame()
                    self.back_cursor_row = 0
                    self.written_rows.clear()
                elif cmd == 'J':
                    self.back_lines = [[]]
                    self.back_styles = [[]]
                    self.back_cursor_row = 0
                    self.written_rows.clear()
                elif cmd == 'A':
                    self.back_cursor_row = max(0, self.back_cursor_row - val)
                elif cmd == 'B':
                    self.back_cursor_row += val
                    while self.back_cursor_row >= len(self.back_lines):
                        self.back_lines.append([])
                        self.back_styles.append([])
                elif cmd == 'K':
                    if 0 <= self.back_cursor_row < len(self.back_lines):
                        self.back_lines[self.back_cursor_row] = []
                        self.back_styles[self.back_cursor_row] = []
                elif cmd == 'm':
                    if not params or params == [0]:
                        self.current_style = "normal"
                        self.current_fg = None
                        self.current_bold = False
                    else:
                        for p in params:
                            if p == 0:
                                self.current_fg = None
                                self.current_bold = False
                            elif p == 1:
                                self.current_bold = True
                            elif p == 22:
                                self.current_bold = False
                            elif 30 <= p <= 37:
                                self.current_fg = str(p)
                            elif p == 39:
                                self.current_fg = None
                            elif 90 <= p <= 97:
                                self.current_fg = str(p)
                        if self.current_fg:
                            if self.current_bold:
                                self.current_style = f"fg_{self.current_fg}_bold"
                            else:
                                self.current_style = f"fg_{self.current_fg}"
                        elif self.current_bold:
                            self.current_style = "bold"
                        else:
                            self.current_style = "normal"
                elif cmd == 'h' or cmd == 'l':
                    pass
                self.ansi_buffer = ""
            elif char == '?' or char.isdigit() or char == ';':
                pass
            return
        
        if char == '\n':
            self.back_cursor_row += 1
            while self.back_cursor_row >= len(self.back_lines):
                self.back_lines.append([])
                self.back_styles.append([])
            if len(self.back_lines) > 2000:
                self.back_lines = self.back_lines[-2000:]
                self.back_styles = self.back_styles[-2000:]
                self.back_cursor_row = max(0, self.back_cursor_row - 1)
        elif char == '\r':
            if 0 <= self.back_cursor_row < len(self.back_lines):
                self.back_lines[self.back_cursor_row] = []
                self.back_styles[self.back_cursor_row] = []
        else:
            while self.back_cursor_row >= len(self.back_lines):
                self.back_lines.append([])
                self.back_styles.append([])
            if self.engine_started:
                if self.back_cursor_row not in self.written_rows:
                    self.back_lines[self.back_cursor_row] = []
                    self.back_styles[self.back_cursor_row] = []
                    self.written_rows.add(self.back_cursor_row)
            self.back_lines[self.back_cursor_row].append(char)
            self.back_styles[self.back_cursor_row].append(self.current_style)

    def poll_queue(self):
        current_time = time.time()
        has_new_content = False
        
        # Skip rendering when window is not visible (minimized or background).
        # Back-buffer still updates; we flush the latest frame on return.
        is_visible = self.root.state() == "normal" and self.root.winfo_viewable()
        if not is_visible:
            self._needs_full_flush = True
            while not self.queue.empty():
                text_chunk = self.queue.get_nowait()
                for char in text_chunk:
                    self._process_char(char)
            self.root.after(100, self.poll_queue)
            return
        
        # Window just became visible again — flush latest frame immediately
        if self._needs_full_flush:
            self._needs_full_flush = False
            self._flush_to_display(current_time)
        
        # Process at most a few chunks per tick to avoid starving the Tkinter
        # mainloop when the engine produces output faster than we can render.
        chunks_this_tick = 0
        while not self.queue.empty() and chunks_this_tick < 3:
            text_chunk = self.queue.get_nowait()
            has_new_content = True
            chunks_this_tick += 1
            
            # Process each character in the chunk
            for char in text_chunk:
                self._process_char(char)
        
        if has_new_content:
            self.last_data_time = current_time
            
        # Determine if we should flush the back-buffer to the display.
        should_flush = False
        if not self.engine_started:
            # During launcher scrolling log stage, flush instantly to keep logs real-time.
            should_flush = has_new_content
        else:
            # During engine mode, only flush on idle (stream paused for 120ms+).
            # Frame-start flushes are handled by _swap_frame() above when \x1b[H arrives.
            if has_new_content and current_time - self.last_data_time > 0.12:
                should_flush = True
            elif not has_new_content and self.last_data_time > self.last_render_time and current_time - self.last_data_time > 0.12:
                # No new data but we have unrendered content from a recent burst
                should_flush = True

        if should_flush:
            self._flush_to_display(current_time)
                
        self.root.after(30, self.poll_queue)
    
    def _swap_frame(self):
        """Called when \\x1b[H is detected during engine mode.
        The back-buffer contains the previous complete frame — render it now."""
        # Only swap if the back-buffer has meaningful content
        if len(self.back_lines) > 1 or (len(self.back_lines) == 1 and self.back_lines[0]):
            self._flush_to_display(time.time())
    
    def _flush_to_display(self, current_time):
        """Copy back-buffer to display and render to Tkinter widget."""
        # Guard against back_lines growing unbounded in engine mode
        if self.engine_started and len(self.back_lines) > 200:
            self.back_lines = self.back_lines[-200:]
            self.back_styles = self.back_styles[-200:]
            if self.back_cursor_row >= len(self.back_lines):
                self.back_cursor_row = len(self.back_lines) - 1

        # Clean up trailing unwritten lines in back-buffer
        if self.engine_started and self.written_rows:
            max_row = max(max(self.written_rows), self.back_cursor_row)
            if max_row < len(self.back_lines) - 1:
                self.back_lines = self.back_lines[:max_row + 1]
                self.back_styles = self.back_styles[:max_row + 1]

        # Copy parsed back-buffer safely to the active display buffer
        self.virtual_lines = [list(line) for line in self.back_lines]
        self.virtual_styles = [list(style) for style in self.back_styles]
        self.cursor_row = self.back_cursor_row

        self.text_area.configure(tabs=())

        new_text = "\n".join("".join(line) for line in self.virtual_lines)
        
        current_displayed = self.text_area.get("1.0", "end-1c")
        
        # Content Identity Check — skip rendering if nothing changed
        if new_text != current_displayed:
            current_displayed_lines = current_displayed.split("\n")
            if len(self.virtual_lines) == len(current_displayed_lines):
                # Surgical line-by-line updates for zero flicker
                self.text_area.config(state="normal")
                for y, line_chars in enumerate(self.virtual_lines):
                    line_text = "".join(line_chars)
                    if line_text != current_displayed_lines[y]:
                        self.text_area.replace(f"{y+1}.0", f"{y+2}.0", line_text + "\n")
                        # Clean up old style tags on this specific line (batch)
                        for style_tag in self._style_tag_cache:
                            self.text_area.tag_remove(style_tag, f"{y+1}.0", f"{y+2}.0")
                        # Apply new style tags
                        line_styles = self.virtual_styles[y]
                        segments = get_style_segments(line_styles)
                        for start_x, end_x, style in segments:
                            if style != "normal":
                                self.text_area.tag_add(style, f"{y+1}.{start_x}", f"{y+1}.{end_x}")
                self.text_area.config(state="disabled")
            else:
                # Full refresh when line count shifts
                y_scroll = self.text_area.yview()
                self.text_area.config(state="normal")
                self.text_area.replace("1.0", "end", new_text)
                
                for y, line_styles in enumerate(self.virtual_styles):
                    segments = get_style_segments(line_styles)
                    for start_x, end_x, style in segments:
                        if style != "normal":
                            self.text_area.tag_add(style, f"{y+1}.{start_x}", f"{y+1}.{end_x}")
                            
                self.text_area.config(state="disabled")
                self.text_area.yview_moveto(y_scroll[0])
        
        # Cache the rendered state
        self.displayed_lines = [list(line) for line in self.virtual_lines]
        self.last_render_time = current_time
        
        # Only auto-scroll during the launcher stage.
        if not self.engine_started:
            self.text_area.see("end")
        
        # Dynamically hide the bottom input bar when engine starts
        if not self.engine_started:
            current_text = "".join("".join(line) for line in self.virtual_lines)
            if "P+ID" in current_text or "Waiting for command" in current_text:
                self.engine_started = True
                self.input_frame.grid_forget()
                self.text_area.focus_set()


    def _send_stdin_bytes(self, data: bytes):
        """Send raw bytes to child stdin immediately and synchronously in the main thread."""
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
        except Exception:
            pass

    def on_entry_return(self, event):
        """Handle Enter in the input entry — send entire text + newline to child stdin."""
        text = self.input_entry.get().strip()
        if text:
            # Send text + standard newline in a single atomic write
            self._send_stdin_bytes(text.encode('utf-8') + b'\n')
            self.input_entry.delete(0, "end")
        else:
            self._send_stdin_bytes(b'\n')
        return "break"

    def on_global_key(self, event):
        """Handle global keypresses: redirect typing to entry during launcher, forward directly if engine started."""
        # If the entry already has focus during launcher, let it handle everything normally
        if not self.engine_started and event.widget == self.input_entry:
            return
        
        # Arrow Up/Down — forward directly as escape sequences for pagination
        if event.keysym == "Up":
            self._send_stdin_bytes(b'\x1b[A')
            return "break"
        elif event.keysym == "Down":
            self._send_stdin_bytes(b'\x1b[B')
            return "break"
        
        # Enter
        if event.keysym in ("Return", "KP_Enter"):
            self._send_stdin_bytes(b'\n')
            return "break"
            
        # Escape
        if event.keysym == "Escape":
            self._send_stdin_bytes(b'\x1b')
            return "break"
            
        # Backspace
        if event.keysym in ("BackSpace", "Delete"):
            if not self.engine_started:
                self.input_entry.focus_set()
                return
            else:
                self._send_stdin_bytes(b'\x7f')
                return "break"
            
        # Printable character
        if event.char and event.char.isprintable():
            if not self.engine_started:
                # Focus entry and insert character
                self.input_entry.focus_set()
                self.input_entry.insert("end", event.char)
                return "break"
            else:
                self._send_stdin_bytes(event.char.encode('utf-8'))
                return "break"

    def on_text_click(self, event):
        """Handle clicks on text area. During launcher, redirect focus to bottom input entry."""
        if not self.engine_started:
            self.input_entry.focus_set()
            return "break"
        return None
        
    def _kill_children(self):
        """Kill the child process group. Safe to call multiple times."""
        if self.proc and self.proc.poll() is None:
            try:
                if sys.platform != "win32":
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                else:
                    self.proc.kill()
            except:
                try:
                    self.proc.kill()
                except:
                    pass

    def on_close(self):
        self._kill_children()
        try:
            self.root.destroy()
        except:
            pass

    def on_resize(self, event):
        # Prevent processing extremely small/spurious events during init
        if event.width < 100 or event.height < 100:
            return
        
        # Only send resize messages after the engine has fully started
        if not self.engine_started:
            current_text = "".join("".join(line) for line in self.virtual_lines)
            if "P+ID" in current_text or "Waiting for command" in current_text:
                self.engine_started = True
                self.input_frame.grid_forget()
                self.text_area.focus_set()
            else:
                return
        
        # Calculate character dimensions
        char_width = self.custom_font.measure("A")
        char_height = self.custom_font.metrics("linespace")
        
        # Compute columns and rows (minus padding)
        cols = max(40, (event.width - 32) // char_width)
        rows = max(10, (event.height - 32) // char_height)
        
        # Send the resize control message
        self._send_stdin_bytes(f"\x1e{rows},{cols}\n".encode("utf-8"))

def find_table_column_indices(lines):
    # Search for a separator/border line
    # A separator line is composed of box characters (like ═, ─, ╪, ┼, ┬, ┴, ╷, ╵, ┌, ┐, └, ┘, ├, ┤, │)
    # and spaces, with absolutely no letters, digits, or CJK characters.
    # It must also contain at least one column intersection/divider character.
    divider_chars = set("│┼╪╷╵┬┴┌┐└┘├┤╤╧╭╮╰╯")
    allowed_chars = divider_chars.union(set(" ═─"))
    
    for line in lines:
        if not line.strip():
            continue
        # Check if all characters are in allowed_chars
        if all(c in allowed_chars for c in line):
            # This is a separator line!
            # Let's find all indices of divider characters in terms of cell widths
            indices = []
            cell_x = 0
            for char in line:
                if char in divider_chars:
                    indices.append(cell_x)
                cell_x += 1
            if len(indices) >= 2:
                return indices
    return None

def main():
    root = tk.Tk()
    # Set app icon early (before any widgets) so Cmd+Tab switcher picks it up
    try:
        from mac_icon import set_macos_app_icon
        _dir = os.path.dirname(os.path.abspath(__file__))
        _icon_name = "autosub_launcher_neon.png" if sys.platform == "darwin" else "autosub.ico"
        set_macos_app_icon(root, os.path.join(_dir, _icon_name))
    except Exception:
        pass
    
    CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
    PYTHON_EXE = sys.executable
    
    # Support dynamic command forwarding
    if len(sys.argv) > 1:
        cmd = sys.argv[1:]
    else:
        cmd = [PYTHON_EXE, os.path.join(CURRENT_DIR, "autosub_launcher.py")]
    
    app = TerminalGUI(root, cmd)
    root.mainloop()

if __name__ == "__main__":
    main()
