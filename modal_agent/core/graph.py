import logging
import re
from typing import Literal

from langgraph.graph import END, StateGraph

from core.config import (
    CAREER_MODELS,
    CODING_MODELS,
    CRITIC_MODELS,
    CULINARY_MODELS,
    ENTERTAINMENT_MODELS,
    FINANCE_MODELS,
    GENERAL_MODELS,
    ROUTER_MODELS,
    TITLE_MODELS,
    TRAVEL_MODELS,
    VISION_CONTENT_MODELS,
)
from core.llm import build_vision_messages, get_llm, invoke_with_retry
from core.state import AgentState

logger = logging.getLogger(__name__)

MAX_REVISIONS = 2

WRITER_STYLE = (
    "Write a concise, well-structured personal note about the subject of this reel, so you "
    "can rely on it later without watching the video again. Write facts about the subject "
    "itself, not about the video: do not narrate the presentation ('the presenter says', "
    "'the video shows', 'frame 2 displays', or any staging or set details), and do not "
    "discuss how the creator pitched the content or any reverse-psychology or persuasion "
    "framing. Extract the information itself and write it plainly. Start directly with the "
    "content (no heading, no title line). Do not add summaries as a conclusion, promotional "
    "closing lines, or 'feel free to...' filler."
)


def _run(
    llms,
    model_chain: list[str],
    prompt: str,
    state: AgentState,
    images: list[str] | None = None,
    agent: str = "",
) -> str:
    try:
        if images:
            response = invoke_with_retry(llms, build_vision_messages(prompt, images))
        else:
            response = invoke_with_retry(llms, prompt)
    except Exception as exc:
        exc.partial_state = dict(state)
        raise
    model = response.response_metadata.get("model_name") or model_chain[0]
    if model not in state["models_used"]:
        state["models_used"].append(model)
    if agent:
        if agent in ("title", "router"):
            rnd = 1
        elif agent == "critic":
            rnd = len(state.get("critique_history") or []) + 1
        else:
            rnd = len(state.get("writer_history") or []) + 1
        state["model_log"] = (state.get("model_log") or []) + [
            {"agent": agent, "round": rnd, "model": model, "candidates": list(model_chain)}
        ]
    logger.info(f"LLM response from {model}")
    return response.content.strip()


def title_node(state: AgentState) -> AgentState:
    prompt = (
        "Name a personal note (8 words max) that captures what information it contains. "
        "Name the topic or contents, like someone naming a note in their notes app. "
        "Never name the reel itself: no hooks, no questions, no what-the-presenter-said titles. "
        "Return ONLY the title, without quotes."
        f"\n\nTranscript:\n{state['transcript'][:6000]}"
    )
    state["title"] = _run(get_llm(TITLE_MODELS), TITLE_MODELS, prompt, state, agent="title").strip(" \"'#")
    return state


def router_node(state: AgentState) -> AgentState:
    prompt = (
        "Analyze the transcript, segments, and post description to determine the primary category. "
        "Return ONLY one of: culinary, travel, entertainment, coding, finance, career, general."
        f"\n\nTranscript:\n{state['transcript'][:6000]}"
    )
    description = (state.get("description") or "").strip()
    if description:
        prompt += f"\n\nPost description:\n{description[:1500]}\n"
    prompt += (
        "\nThe audio may be silent or just background music. "
        "When the transcript is empty or looks like music alone, rely on the post description."
    )
    category = _run(get_llm(ROUTER_MODELS), ROUTER_MODELS, prompt, state, agent="router").lower()
    valid = {"culinary", "travel", "entertainment", "coding", "finance", "career", "general"}
    state["category"] = category if category in valid else "general"
    return state


def _worker_prompt(state: AgentState, role: str, task: str) -> str:
    if state["critique"] and state["critique"] != "approved":
        return (
            f"You are the {role} Writer. The Critic asked for changes to your note.\n\n"
            f"Critique:\n{state['critique']}\n\n"
            f"Previous note:\n{state['notes'][-1]}\n\n"
            f"{WRITER_STYLE}\nReturn ONLY the revised note."
        )
    prompt = (
        f"You are the {role} Writer. {task}\n\n"
        f"Transcript:\n{state['transcript']}\n\nSegments:\n{state['segments']}"
    )
    description = (state.get("description") or "").strip()
    if description:
        prompt += f"\n\nPost description:\n{description[:1500]}\n"
    if state.get("images"):
        prompt += (
            "\n\nFrames from the video are attached as additional context. "
            "Use them to recover information the audio misses: menus, on-screen text, "
            "listings, products, tools, UI, ingredients, captions, or anything visible "
            "that belongs in the note. Write it as information about the subject, not as "
            "a description of the video."
        )
    prompt += (
        "\n\nThe transcript, the frames, and the post description are separate sources of "
        "the same underlying content. They should complement each other: use every piece "
        "of subject information they offer. Audio can be silent, music-only, noisy, or "
        "unreliable, and a frame may just be a person talking or a transition that adds "
        "nothing. So treat each source as optional: if a source adds no useful subject "
        "information, simply ignore it. At least one of the sources is usually useful; "
        "sometimes all are, sometimes only one or two."
    )
    return prompt + f"\n\n{WRITER_STYLE}"


def culinary_node(state: AgentState) -> AgentState:
    task = (
        "Write a note with the culinary content about the reel's subject: recipes, "
        "techniques, ingredients, tips. Use markdown sections where useful. Include relevant "
        "timestamps from segments."
    )
    chain = VISION_CONTENT_MODELS if state.get("images") else CULINARY_MODELS
    content = _run(
        get_llm(chain), chain, _worker_prompt(state, "Culinary", task), state, state.get("images"), agent="culinary"
    )
    state["notes"] = [content]
    state["writer_history"] = (state.get("writer_history") or []) + [content]
    return state


def entertainment_node(state: AgentState) -> AgentState:
    task = (
        "Write a note with the entertainment content about the reel's subject: movies, "
        "series, books, plot points, recommendations. "
        "Use markdown sections where useful. Wrap spoilers in <details><summary>SPOILER</summary>...</details>. "
        "Include relevant timestamps from segments."
    )
    chain = VISION_CONTENT_MODELS if state.get("images") else ENTERTAINMENT_MODELS
    content = _run(
        get_llm(chain),
        chain,
        _worker_prompt(state, "Entertainment", task),
        state,
        state.get("images"),
        agent="entertainment",
    )
    state["notes"] = [content]
    state["writer_history"] = (state.get("writer_history") or []) + [content]
    return state


def coding_node(state: AgentState) -> AgentState:
    task = (
        "Write a note with the coding content about the reel's subject: concepts, code "
        "snippets, libraries, techniques, best practices. "
        "Use markdown sections and code blocks. Include relevant timestamps from segments."
    )
    chain = VISION_CONTENT_MODELS if state.get("images") else CODING_MODELS
    content = _run(
        get_llm(chain), chain, _worker_prompt(state, "Coding", task), state, state.get("images"), agent="coding"
    )
    state["notes"] = [content]
    state["writer_history"] = (state.get("writer_history") or []) + [content]
    return state


def general_node(state: AgentState) -> AgentState:
    task = (
        "Write a note with the key insights, ideas, and actionable takeaways about the "
        "reel's subject. Use markdown sections where useful."
    )
    chain = VISION_CONTENT_MODELS if state.get("images") else GENERAL_MODELS
    content = _run(
        get_llm(chain), chain, _worker_prompt(state, "General", task), state, state.get("images"), agent="general"
    )
    state["notes"] = [content]
    state["writer_history"] = (state.get("writer_history") or []) + [content]
    return state


def finance_node(state: AgentState) -> AgentState:
    task = (
        "Write a note with the finance content about the reel's subject: saving, "
        "investing, budgeting, taxes, tools, or money tips worth keeping for later. "
        "Use markdown sections where useful. Include relevant timestamps from segments."
    )
    chain = VISION_CONTENT_MODELS if state.get("images") else FINANCE_MODELS
    content = _run(
        get_llm(chain), chain, _worker_prompt(state, "Finance", task), state, state.get("images"), agent="finance"
    )
    state["notes"] = [content]
    state["writer_history"] = (state.get("writer_history") or []) + [content]
    return state


def career_node(state: AgentState) -> AgentState:
    task = (
        "Write a note with the career content about the reel's subject: job search, "
        "interviews, resumes, applications, useful websites, or workplace advice worth "
        "keeping for later. When relevant, give slightly more weight to advice useful "
        "for AI engineer or data scientist roles in Portugal, without dropping other "
        "useful advice. Use markdown sections where useful. Include relevant timestamps "
        "from segments."
    )
    chain = VISION_CONTENT_MODELS if state.get("images") else CAREER_MODELS
    content = _run(
        get_llm(chain), chain, _worker_prompt(state, "Career", task), state, state.get("images"), agent="career"
    )
    state["notes"] = [content]
    state["writer_history"] = (state.get("writer_history") or []) + [content]
    return state


def travel_node(state: AgentState) -> AgentState:
    task = (
        "Write a note with the travel content about the reel's subject: destinations, "
        "itineraries, transport, costs, tips, culture. "
        "Use markdown sections where useful. Include relevant timestamps from segments."
    )
    chain = VISION_CONTENT_MODELS if state.get("images") else TRAVEL_MODELS
    content = _run(
        get_llm(chain), chain, _worker_prompt(state, "Travel", task), state, state.get("images"), agent="travel"
    )
    state["notes"] = [content]
    state["writer_history"] = (state.get("writer_history") or []) + [content]
    return state


def critic_node(state: AgentState) -> AgentState:
    prompt = (
        "You are a meticulous editor. The note below describes the subject of a reel: a "
        "thing, a tool, a place, steps, or information the viewer can rely on later without "
        "rewatching the video. It must read as a standalone factual note about the subject, "
        "not as a summary of the video or an account of what the presenter did, said, or "
        "showed on screen.\n"
        "Check that the note reports subject facts and does not narrate the video or the "
        "presentation (no 'the presenter says', 'the video shows', no staging or set "
        "details, no framing or reverse-psychology talk), and that it is not padded with "
        "irrelevant on-screen detail. When something appears only in the frames and not in "
        "the transcript, that is still valid subject evidence if it is genuinely about the "
        "subject. Do not ask for such details to be removed just because they are missing "
        "from the audio. Favor completeness: if the sources hold subject information the "
        "note omits, ask for it to be added. Only flag a claim as ungrounded if it matches "
        "neither the transcript, the frames, nor the post description. Fix vague claims and "
        "remove promotional filler.\n\n"
        f"Note:\n{state['notes'][-1]}\n\nTranscript:\n{state['transcript']}\n\n"
        "If the note is good, output exactly:\nVERDICT approve\n\n"
        "If changes are needed, output exactly:\nVERDICT revise\n- specific required change\n- ..."
    )
    chain = VISION_CONTENT_MODELS if state.get("images") else CRITIC_MODELS
    result = _run(get_llm(chain), chain, prompt, state, state.get("images"), agent="critic")
    state["retries"] = state.get("retries", 0) + 1
    history = state.get("critique_history") or []
    match = re.search(r"VERDICT\s*:?\s*(approve|revise)", result, re.IGNORECASE)
    verdict = match.group(1).lower() if match else "approve"
    if verdict == "approve":
        state["critique"] = "approved"
        history.append("approved")
    else:
        feedback = re.sub(r"VERDICT\s*:?\s*revise", "", result, flags=re.IGNORECASE).strip()
        state["critique"] = feedback
        history.append(feedback)
    state["critique_history"] = history
    logger.info(f"Critic verdict: {verdict} (retry {state['retries']}/{MAX_REVISIONS})")
    return state


def route_by_category(state: AgentState) -> Literal["culinary", "travel", "entertainment", "coding", "finance", "career", "general"]:
    return state["category"]


def route_after_critic(state: AgentState) -> Literal["compile", "culinary", "travel", "entertainment", "coding", "finance", "career", "general"]:
    if state["critique"] == "approved" or state["retries"] >= MAX_REVISIONS:
        return "compile"
    return state["category"]


def compile_final_note(state: AgentState) -> AgentState:
    state["final_note"] = "\n\n---\n\n".join(state["notes"])
    return state


def build_graph():
    workflow = StateGraph(AgentState)

    workflow.add_node("title", title_node)
    workflow.add_node("router", router_node)
    workflow.add_node("culinary", culinary_node)
    workflow.add_node("travel", travel_node)
    workflow.add_node("entertainment", entertainment_node)
    workflow.add_node("coding", coding_node)
    workflow.add_node("finance", finance_node)
    workflow.add_node("career", career_node)
    workflow.add_node("general", general_node)
    workflow.add_node("critic", critic_node)
    workflow.add_node("compile", compile_final_note)

    workflow.set_entry_point("title")
    workflow.add_edge("title", "router")
    workflow.add_conditional_edges("router", route_by_category)
    workflow.add_edge("culinary", "critic")
    workflow.add_edge("travel", "critic")
    workflow.add_edge("entertainment", "critic")
    workflow.add_edge("coding", "critic")
    workflow.add_edge("finance", "critic")
    workflow.add_edge("career", "critic")
    workflow.add_edge("general", "critic")
    workflow.add_conditional_edges("critic", route_after_critic)
    workflow.add_edge("compile", END)

    return workflow.compile()


reel_graph = build_graph()