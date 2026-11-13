import os
import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=os.getenv("APP_BIND_HOST", "127.0.0.1"),
                port=int(os.getenv("APP_PORT", "3189")), workers=1, access_log=False)
