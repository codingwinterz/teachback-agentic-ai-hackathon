"""Start the app: `python -m app`.

Locally this serves on 127.0.0.1:8000. On Render, HOST=0.0.0.0 and PORT come from the environment.
"""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        proxy_headers=True,
    )
