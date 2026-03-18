"""Entrypoint: run FastAPI backend for frontend. Default: paper (testing) mode."""
import os
import uvicorn

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=os.getenv("RELOAD", "").lower() == "true")
