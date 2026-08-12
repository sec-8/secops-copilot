import json
from fastapi import FastAPI
from pydantic import BaseModel
from app.my_extract import extract_alert
from fastapi.responses import StreamingResponse
from app.agent import run_agent_stream

app = FastAPI(title="SecOps Copilot")


class ExtractReq(BaseModel):
    text: str


class ChatReq(BaseModel):
    text: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/extract")
def extract(req: ExtractReq):
    return {"input": req.text, "parsed": extract_alert(req.text)}

@app.post("/chat/stream")
def chat_stream(req: ChatReq):
    """SSE 流式聊天"""
    def event_generator():
        for event in run_agent_stream(req.text):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream"
    )
