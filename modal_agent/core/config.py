OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

BASIC_MODELS = [
    "poolside/laguna-xs-2.1:free",
    "google/gemma-4-31b-it:free",
    "inclusionai/ling-3.0-flash:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
]

EDITOR_MODELS = [
    "google/gemma-4-31b-it:free",
    "minimax/minimax-m2.7:free",
    "thinkingmachines/inkling:free",
    "nvidia/nemotron-3.5-lightning:free",
    "openai/gpt-oss-20b:free",
]

CONTENT_MODELS = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "minimax/minimax-m3:free",
    "z-ai/glm-5.2:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
]

TITLE_MODELS = BASIC_MODELS
ROUTER_MODELS = BASIC_MODELS
CRITIC_MODELS = EDITOR_MODELS
GENERAL_MODELS = EDITOR_MODELS
CODING_MODELS = EDITOR_MODELS
FINANCE_MODELS = EDITOR_MODELS
CAREER_MODELS = EDITOR_MODELS
CULINARY_MODELS = CONTENT_MODELS
ENTERTAINMENT_MODELS = CONTENT_MODELS
TRAVEL_MODELS = CONTENT_MODELS

VISION_CONTENT_MODELS = [
    "minimax/minimax-m3:free",
    "google/gemma-4-31b-it:free",
    "thinkingmachines/inkling:free",
    "google/gemma-4-26b-a4b-it:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "nvidia/nemotron-nano-12b-v2-vl:free",
]

RAG_MODELS = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "google/gemma-4-31b-it:free",
    "z-ai/glm-5.2:free",
]