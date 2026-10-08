from typing import TypedDict


class AgentState(TypedDict, total=False):
    url: str
    video_path: str
    transcript: str
    description: str
    images: list[str]
    title: str
    category: str
    writer_history: list[str]
    critique: str
    critique_history: list[str]
    model_log: list[dict]
    final_note: str
    visual_extraction: str
    coverage: list[str]
    drafts: int
    review_status: str
    raw_media_ids: list[int]
    routing_degraded: bool
    writer_failed: bool
