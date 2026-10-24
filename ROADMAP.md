# AI Personal Assistant — Feature Roadmap

Feature status checked against the implementation and offline tests on 24 October 2026. F02 and F03 are implemented; live deployment acceptance checks remain in the README. Improve and stabilise the existing assistant before implementing these items. Each ID is a stable reference: ask to implement “F03”, for example. Mark an item complete only after its behaviour is implemented and tested.

## Suggested implementation order

F02 → F03 → F04 → F05 → F06. Add F01 platforms incrementally alongside these features. Implement the remaining items when they become useful.

- [ ] **F01 — Additional platforms and direct media uploads**  
  Support YouTube, TikTok, X, Reddit, Facebook, LinkedIn, articles, and other frequently used sources through platform-specific extractors. Track supported content types and limitations; accept uploaded media or screenshots when a link cannot be extracted.

- [x] **F02 — Instructions with links and follow-up messages**  
  Accept instructions in the same message as a link or immediately afterwards: desired structure, focus, detail, and research depth. Associate replies with the correct source, preserve instructions, and revise using cached evidence.

- [x] **F03 — Personal memory and preferences**  
  Remember explicit preferences, useful personal facts, and corrections, with their source and date. Retrieve only relevant memory; provide commands to inspect, correct, and forget it.

- [ ] **F04 — Natural list and status management**  
  Maintain watchlists, reading lists, places to visit, and similar collections. Understand updates such as “I watched Arrival”; mark it watched, remove it from the active list, and retain history to prevent re-adding it. Ask when the item is ambiguous.

- [ ] **F05 — Project knowledge and useful connections**  
  Maintain project goals, current capabilities, and constraints. Link a note to one or several projects and save concrete upgrade suggestions with the supporting source and rationale, without inventing project facts.

- [ ] **F06 — Research that deepens an existing note**  
  Research questions raised by a saved note, using external sources and citations. Distinguish original-source content, external findings, and assistant suggestions; respect a configurable depth and spending limit.

- [ ] **F07 — Conversational retrieval and follow-up questions**  
  Support exchanges such as “Which of those fits my project?” or “Explain the second technique.” Keep enough conversation context to resolve references while grounding answers in notes, projects, and relevant memory.

- [ ] **F08 — Action confirmation and undo**  
  Perform clear, reversible personal updates with a brief acknowledgement and undo. Confirm ambiguous changes, bulk deletion, spending, and actions involving other people; record what changed.

- [ ] **F09 — Resurface useful saved information**  
  Bring back relevant notes when discussing a related project or topic. Offer an optional digest of actionable ideas rather than repeatedly sending generic summaries.

- [ ] **F10 — Reminders and lightweight tasks**  
  Turn an explicit request or accepted note takeaway into a dated reminder or task. Respect the user's timezone, track completion, and clarify missing details that affect scheduling.

- [ ] **F11 — Broader decision routing**  
  Extend the current category decision layer to conversational intent, research requests, project relevance, and clarification needs. Compare decision models on real examples; keep permissions and exact state changes in code.

- [ ] **F12 — Feedback and preference refinement**  
  Learn from explicit corrections such as “shorter next time” or “focus on implementation.” Save scoped preferences that can be reviewed or reset, and avoid treating one-off instructions as permanent preferences.

## Scope rule

Implement one requested feature at a time. Preserve the existing Telegram workflow, readable code, source grounding, and cost limits. Avoid adding integrations or autonomous actions until they serve a concrete use case.
