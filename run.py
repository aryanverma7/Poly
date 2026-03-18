"""
Single-command launcher: start backend (FastAPI) and frontend (Vite) together.
Uses 'node' to run Vite so we avoid npm.ps1 (PowerShell execution policy).
"""
import os
import subprocess
import sys
import time

def main():
    root = os.path.dirname(os.path.abspath(__file__))
    web_dir = os.path.join(root, "web")
    env = os.environ.copy()
    env.setdefault("PORT", "8000")

    # Start backend in a subprocess
    backend = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", env["PORT"]],
        cwd=root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(1.5)
        if backend.poll() is not None:
            print("Backend failed to start. Run: python main.py")
            sys.exit(1)
        print("Backend running at http://127.0.0.1:8000")
    except Exception:
        backend.kill()
        raise

    # Run Vite with node (no npm.ps1)
    vite_script = os.path.join(web_dir, "node_modules", "vite", "bin", "vite.js")
    if not os.path.isfile(vite_script):
        print("Run once: cd web && npm install  (or: cmd /c \"cd web && npm install\")")
        backend.terminate()
        sys.exit(1)

    print("Frontend starting at http://127.0.0.1:5173")
    print("Press Ctrl+C to stop both.")
    try:
        subprocess.run(
            ["node", vite_script, "--host"],
            cwd=web_dir,
            env={**env, "FORCE_COLOR": "1"},
        )
    except KeyboardInterrupt:
        pass
    finally:
        backend.terminate()
        backend.wait(timeout=5)


if __name__ == "__main__":
    main()
