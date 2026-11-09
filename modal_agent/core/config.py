OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Existing paid fallbacks. Availability and final cost are not guaranteed.
CHEAP_PAID_TEXT_FALLBACK = "deepseek/deepseek-chat"
CHEAP_PAID_VISION_FALLBACK = "google/gemini-2.5-flash"

# Basic JSON routing and simple extraction
BASIC_MODELS = [
    "poolside/laguna-s-2.1:free",
    CHEAP_PAID_TEXT_FALLBACK
]

# Editor / Critic nodes (Requires strong reasoning to verify facts and format)
EDITOR_MODELS = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    CHEAP_PAID_TEXT_FALLBACK
]

# Content formatting (Writing prose, creativity, summarization)
CONTENT_MODELS = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    CHEAP_PAID_TEXT_FALLBACK
]

DECISION_MODELS = ["typesafe/jev-1.13"]
TITLE_MODELS = CONTENT_MODELS  # legacy notebook compatibility; no title node
ROUTER_MODELS = DECISION_MODELS
CRITIC_MODELS = EDITOR_MODELS
GENERAL_MODELS = EDITOR_MODELS
CODING_MODELS = EDITOR_MODELS
FINANCE_MODELS = EDITOR_MODELS
CAREER_MODELS = EDITOR_MODELS
CULINARY_MODELS = CONTENT_MODELS
ENTERTAINMENT_MODELS = CONTENT_MODELS
TRAVEL_MODELS = CONTENT_MODELS

# Vision models for analyzing frames
VISION_CONTENT_MODELS = [
    "thinkingmachines/inkling:free",
    CHEAP_PAID_VISION_FALLBACK 
]

# RAG models for semantic synthesis and LanceDB search
RAG_MODELS = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    CHEAP_PAID_TEXT_FALLBACK
]

# Explicit owner voice preference. Current request/preference instructions override it.
ASSISTANT_RESPONSE_STYLE = (
    'Respond as a concise, competent, polite personal assistant. Answer first. '
    'Use short paragraphs, bullets or a compact table when helpful. No routine provenance headings, '
    'filler, repeated introductions, theatrical roleplay or invented system status. '
    'Keep citations. Briefly attribute personal memory, unverified source claims or model knowledge when needed. '
    'Use sir sparingly and naturally, normally in a greeting or acknowledgement; never append it mechanically '
    'or repeat it across an exchange. Never claim an action was completed or evidence verified unless it was. '
    'These are defaults; current user style instructions and explicit preferences take precedence.'
)
