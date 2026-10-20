"""Development launcher:  python run.py   (production: see README)."""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")),
                reload=os.getenv("APP_ENV", "development") != "production", proxy_headers=True)
