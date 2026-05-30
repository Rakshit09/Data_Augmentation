import os
import sys
import time
import socket
import threading
import traceback
import webbrowser
import tkinter as tk
from tkinter import messagebox


APP_NAME = "Data Augmentation"
APP_ICON = "favicon.ico"

HOST = "127.0.0.1"
PORT = 8100
URL = f"http://{HOST}:{PORT}"


def app_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.abspath(".")


LOG_FILE = os.path.join(app_base_dir(), "startup_error.log")


def write_log(text):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 100 + "\n")
            f.write(text)
            f.write("\n")
    except Exception:
        pass


def pause_forever_on_error(error_text):
    write_log(error_text)

    print("\n" + "=" * 100)
    print("APPLICATION FAILED")
    print("=" * 100)
    print(error_text)
    print("=" * 100)
    print(f"\nError log saved here:\n{LOG_FILE}")

    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Application failed",
            f"The application failed to start.\n\nLog file:\n{LOG_FILE}\n\n{error_text[:2000]}"
        )
        root.destroy()
    except Exception:
        pass

    while True:
        try:
            input("\nPress Enter to exit...")
            break
        except Exception:
            time.sleep(10)


def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def local_path(relative_path):
    return os.path.join(app_base_dir(), relative_path)


def is_port_open(host, port):
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def wait_for_server(host, port, timeout=40):
    start = time.time()
    while time.time() - start < timeout:
        if is_port_open(host, port):
            return True
        time.sleep(0.25)
    return False


def create_splash():
    root = tk.Tk()
    root.title("Loading")

    width, height = 340, 140
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    x = (screen_width / 2) - (width / 2)
    y = (screen_height / 2) - (height / 2)
    root.geometry(f"{width}x{height}+{int(x)}+{int(y)}")

    root.overrideredirect(True)
    root.configure(bg="white")

    frame = tk.Frame(root, bg="white")
    frame.pack(expand=True, fill="both", padx=20, pady=20)

    status_label = tk.Label(
        frame,
        text="Starting Application...",
        font=("Helvetica", 14),
        bg="white",
    )
    status_label.pack(pady=5)

    progress_label = tk.Label(
        frame,
        text="Loading modules...",
        font=("Helvetica", 10),
        fg="gray",
        bg="white",
    )
    progress_label.pack(pady=5)

    root.update_idletasks()
    root.update()

    return root, status_label, progress_label


def create_control_window():
    control = tk.Tk()
    control.title("Server Control")

    try:
        control.iconbitmap(resource_path(APP_ICON))
    except Exception:
        pass

    width, height = 300, 130
    screen_width = control.winfo_screenwidth()
    screen_height = control.winfo_screenheight()
    x = screen_width - width - 20
    y = screen_height - height - 70
    control.geometry(f"{width}x{height}+{int(x)}+{int(y)}")

    control.configure(bg="#2c3e50")
    control.attributes("-topmost", False)

    frame = tk.Frame(control, bg="#2c3e50")
    frame.pack(expand=True, fill="both", padx=15, pady=15)

    label = tk.Label(
        frame,
        text="🟢 Server Running",
        font=("Helvetica", 11, "bold"),
        bg="#2c3e50",
        fg="#2ecc71",
    )
    label.pack(pady=5)

    url_label = tk.Label(
        frame,
        text=URL,
        font=("Helvetica", 9),
        fg="#3498db",
        bg="#2c3e50",
        cursor="hand2",
    )
    url_label.pack(pady=2)
    url_label.bind("<Button-1>", lambda e: webbrowser.open(URL))

    btn_frame = tk.Frame(frame, bg="#2c3e50")
    btn_frame.pack(pady=8)

    return control, label, btn_frame


def cleanup():
    print("\nShutting down...")
    os._exit(0)


server_error = None


def build_flask_app():
    """
    Imports are inside this function so import errors are captured and logged.
    """
    from building_lookup_app import create_app

    app = create_app(db_path="", nearest_radius_m=50.0)

    app.config["DB_PATH"] = ""
    app.config["PARQUET_PATH"] = ""
    app.config["UPLOAD_DIR"] = local_path("etl_output/app_uploads")
    app.config["RESULT_DIR"] = local_path("etl_output/app_results")

    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)
    os.makedirs(app.config["RESULT_DIR"], exist_ok=True)

    return app


def run_server():
    global server_error

    try:
        write_log("Starting server thread...")

        from waitress import serve

        flask_app = build_flask_app()

        write_log(f"Serving Flask app at {URL}")
        serve(flask_app, host=HOST, port=PORT, threads=8, _quiet=False)

    except Exception:
        server_error = traceback.format_exc()
        write_log(server_error)
        print(server_error)


def main():
    global server_error

    write_log("Application starting...")

    splash_root, status_label, progress_label = create_splash()

    def update_progress(text):
        try:
            progress_label.config(text=text)
            splash_root.update()
        except Exception:
            pass

    update_progress("Starting local server...")

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    if not wait_for_server(HOST, PORT, timeout=40):
        if server_error:
            raise RuntimeError(server_error)
        raise RuntimeError(
            f"Server did not start at {URL}.\n"
            f"Port {PORT} may already be in use, or Flask failed before startup.\n"
            f"Check log file: {LOG_FILE}"
        )

    update_progress("Opening application...")

    control_window, server_label, btn_frame = create_control_window()

    quit_btn = tk.Button(
        btn_frame,
        text="⏹ Stop Server",
        command=cleanup,
        bg="#e74c3c",
        fg="white",
        font=("Helvetica", 9, "bold"),
        relief="flat",
        cursor="hand2",
        padx=15,
        pady=5,
    )
    quit_btn.pack()

    control_window.protocol("WM_DELETE_WINDOW", cleanup)
    control_window.withdraw()

    try:
        import webview
    except Exception:
        raise RuntimeError("pywebview import failed:\n" + traceback.format_exc())

    def open_app():
        try:
            splash_root.destroy()
        except Exception:
            pass

        webview.create_window(APP_NAME, URL, width=1400, height=900)
        webview.start()

    splash_root.after(500, open_app)
    splash_root.mainloop()

    cleanup()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        error = traceback.format_exc()
        pause_forever_on_error(error)