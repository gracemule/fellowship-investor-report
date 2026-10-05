import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("chui_reporter.app.main:app_factory", factory=True, host=os.environ.get("HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "8000")), log_level="info", proxy_headers=True,
                forwarded_allow_ips="*", timeout_keep_alive=30)
