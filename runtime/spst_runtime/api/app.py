try:
    from fastapi import FastAPI
except Exception:
    FastAPI=None

app = FastAPI(title="SPST Runtime") if FastAPI else None

if app:
    @app.get("/health")
    async def health():
        return {"status":"ok"}
