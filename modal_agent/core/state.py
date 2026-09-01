from typing import List, Optional
from typing_extensions import TypedDict


class AgentState(TypedDict):
    url: str
    video_path: str
    transcript: str
    segments: List[dict]
    description: str
    images: List[str]
    title: str
    category: str
    notes: List[str]
    writer_history: List[str]
    critique: str
    critique_history: List[str]
    model_log: List[dict]
    retries: int
    final_note: Optional[str]
    error: Optional[str]
    models_used: List[str]