# AI Personal Assistant

A Telegram bot that turns Instagram Reels into structured notes. Send it a reel link and a few minutes later a clean markdown note lands in the notes dashboard.

I used to save a lot of reels and never look at them again. So I built this personal assistant that saves all as notes for me. Each reel becomes a short note I can search later, instead of another line in my likes or saved posts. It is a personal tool, not a product, and it runs for my own accounts on my Modal workspace. You can easily deploy the same setup for yourself.

## How it works

```
reel link -> download (yt-dlp) -> transcribe (whisper) -> pick frames (ffmpeg) -> LangGraph agents -> markdown note
```

The video is downloaded, the audio is transcribed with timestamps, and a handful of frames are pulled out. Frames are chosen from scene cuts and away from speech, so they tend to show something useful on screen. When the transcript looks unreliable, the pipeline skips the scene-cut logic and falls back to evenly spaced frames, so a silent or music-only reel still gets visual context.

Then the LangGraph takes over.

## The agent graph

The graph is small on purpose: a title node names the note, a router picks a category, a writer drafts the note, and a critic reviews it.

```mermaid
graph LR
    title --> router
    router --> w{{writer: culinary, travel, entertainment, coding or general}}
    w --> critic
    critic -->|approved| compile
    critic -->|wants changes| w
    compile --> done
```

LangChain provides the model clients. Every node calls its chain of candidate models one after the other, so a slow or dead provider does not kill the pipeline. `MAX_REVISIONS = 2` caps the writer/critic loop, and after two rounds the note is compiled anyway.

The writer and the critic both see the extracted frames. A detail that is only visible on screen counts as evidence even when it never shows up in the audio, so the critic only flags claims that match neither the frames nor the transcript.

## Observability

Every LangGraph run is traced to LangSmith: the title, router, writer, critic, and each model attempt in the fallback chain. It runs on environment variables (`LANGCHAIN_TRACING_V2`, `LANGCHAIN_PROJECT`, `LANGSMITH_API_KEY` in the Modal secret), so there is no tracing code in the app to maintain. I use it to see which model served each step and to read the full prompt and response when a note comes out wrong. The Generation Info in each note records the same model choices inside the app.

## Asking your notes

When a note is saved, its text is embedded by a local model and placed in a LanceDB vector store on the same data volume, so all your data stays in one place. `/ask` on Telegram (or the question box in the dashboard) searches that store semantically, builds the answer only from the retrieved notes, and lists which notes it used. If no note comes close, it says so instead of guessing.

## Models

Each task keeps its own model list in `core/config.py`, all free tier on OpenRouter. The lists are plain and easy to swap when a provider retires one.

Each agent tries its list in order. If every model fails, the Telegram message lists each one and what it returned, so the real situation is clear instead of a single random provider error. The full error is also kept in the Failed section of the dashboard.

## Stack

- Python 3.11+
- Modal for deployment, with a GPU for transcription
- LangGraph and LangChain for the agent flow
- LanceDB for vector search
- fastembed for local embeddings
- OpenRouter for free models
- OpenAI Whisper for transcription
- yt-dlp and ffmpeg for download and frames
- Streamlit for the dashboard
- Telegram Bot API

## Layout

```
modal_agent/
  app.py
  dashboard.py
  core/
    config.py       model lists per task
    state.py        graph state
    graph.py        the agent graph
    llm.py          model clients, retry and fallback handling
    processor.py    download, transcribe, frames, note storage
    rag.py          semantic search over the saved notes
```

## Setup on Modal

You need a Modal account and the CLI.

1. Create the secret:

```
modal secret create personal-assistant-secrets
```

Add `TELEGRAM_BOT_TOKEN`, `OPENROUTER_API_KEY`, and `UI_URL`.

2. Deploy:

```
cd modal_agent
modal deploy app.py
```

The data volume is created automatically on the first run.

3. Point Telegram at the webhook:

```
curl -F "url=https://<workspace>--personal-assistant-telegram_webhook.modal.run" \
  https://api.telegram.org/bot<token>/setWebhook
```

Send a reel link to the bot. It replies with "Processing reel in background..." and then a message with the note title once it is done.

## Usage

- send any public reel URL to the bot
- `/notes` lists saved notes
- `/find <keyword>` searches their content
- `/ask <question>` asks your notes a question and answers from their content
- `/link` prints the dashboard URL

The dashboard shows the notes with filters and a search box. Each note has a Generation Info popup with the frames, the raw transcript, every writer/critic round, and the models used in each step. Failed notes have a Retry button. Finished notes have a Redo button, which moves the note to the failed list and reprocesses the same link.

## Roadmap

- **Phase 1 (Completed):** Extract audio from reels, run Whisper transcription, and generate structured notes with an LLM.
- **Phase 2 (Completed):** Add a LangGraph critic-refiner agent loop to evaluate draft notes and enforce structure.
- **Phase 3 (Completed):** Extract key video frames at scene changes so the agents can also have the context from the visuals on the screen.
- **Phase 4 (Completed):** Ask your notes questions in plain language. Notes are embedded locally and searched semantically, and answers come only from the retrieved notes, with the sources named.
- **Phase 5 (Future):** Add human-in-the-loop Telegram prompts for fallback routing when rate-limited (retry with paid model or add post to queue), and expand support to other platforms, like YouTube and TikTok. The frames extraction 


## Screenshots

![Notes dashboard](docs/screenshots/dashboard.png)

Notes table with categories, creators, and saved dates. Click Open to read a note.

![Generation info popup](docs/screenshots/generation_info.png)

Generation Info for one note: frames, transcript, writer/critic rounds, and models used.