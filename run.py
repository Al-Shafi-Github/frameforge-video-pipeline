"""Start the studio on loopback; inference and rendering stay on Modal."""
import os
import uvicorn
import studio_config  # load local .env before reading settings

if __name__ == "__main__":
    uvicorn.run("web.app:app", host="127.0.0.1", port=int(os.getenv("STUDIO_PORT", "8000")), workers=1)
