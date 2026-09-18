<img src="docs/banner.svg" alt="Jev Ultrafast · Browser Use × TypeSafe" width="100%" />

# Jev Ultrafast ⚡

**A browser agent with a dynamic, indexed action space.**

Give it one goal. [TypeSafe's Jev](https://docs.typesafe.ai/introduction) picks an operation and an element. A small LLM supplies field values, search queries, and authorized sample-file content.

**Zürich → London on Google Flights in 7.1 seconds.** One natural-language goal, actual text generation, and loading waits included.

<a href="docs/demo.mp4"><img src="docs/demo.gif" alt="A real Google Flights search at 1× speed, with generated city names and dynamic operation/target decisions" width="100%" /></a>

[Watch the MP4](docs/demo.mp4) · [Measurements](docs/performance.md) · [Read the loop](jev_ultrafast/agent.py)

## The action space

Every observation produces a new element table:

```text
[1] button    Change ticket type · Round trip
[2] combobox  Where from?        · San Francisco
[3] combobox  Where to?          · empty
[4] textbox   Departure          · empty
...
```

On this experimental branch, TypeSafe ranks complete actions in one choice:

```text
page + fields + prior evidence
  -> CLICK [7] | TYPE_TEXT [3] | SCROLL_TO [12] | SET_VALUE [4] | ...
  -> execute the selected observed action
  -> observe the result and continue
```

This keeps operation and target coupled. Each choice carries the field type, value, nearby text,
constraints and control state. Supported operations include click, text, native select/date/range,
scrolling to an offscreen control, page scrolling, Enter/Escape, back, reload, web search, and creating
an authorized plain-text sample file for a file input. The text helper runs only when a value is needed.

The state includes up to 16,000 characters of rendered document text, current viewport text, up to 240
observed actions, twelve recent actions, and bounded evidence from previous pages. Model requests compact
repeated labels and context to a 28,000-character budget; omitted controls are counted, with required
and goal-relevant controls prioritized. Empty page-body focus never contributes hidden success text. Open shadow roots and
same-origin frames are traversed. Recently repeated controls are temporarily omitted; rejected values
become feedback. The first BLOCKED choice gets one fresh observation, and three unchanged actions produce
recovery feedback instead of immediately ending the task. The action and decision budgets still apply.

This is a state/action experiment. The Flights video and timings below describe the earlier implementation,
not measurements of this branch. The original broad benchmark was 1/100, and runtime-only fixes were 0/100;
the fresh 20-task comparison scored **0/20 baseline versus 4/20 redesigned**, but manual evidence review
found two false-positive judge passes. Only **2/20 candidate completions were supported**: Newegg product
research in 28 seconds and a hidden-label form in 8.5 seconds. The candidate used $1.56 in agent/browser/proxy
costs across all 20 attempts, excluding judging and one call with missing cost telemetry. Broad reliability
remains poor. [Full results, costs, traces and audit](https://github.com/browser-use/new-eval-platform/blob/codex/jev-state-action-hillclimb/docs/jev-hillclimb-results.md).

There are no site-specific action scripts or prepared field strings in the policy. The Flights example supplies a goal and independently verifies the outcome. The screenshot renderer adds labels afterward; it does not drive the browser.

## Try it

This branch adds an optional checkpoint planner:

```python
with Agent(url, goal, planner_model="google/gemini-3.5-flash") as agent:
    for state in agent.run():
        print(state.get("controller"), state["status"])
```

The planner keeps an immutable requirements list, gives Jev one immediate objective, and reviews progress after
eight actions, repeated failures, or Jev requesting completion. Its completion checks cite exact captured
observations; invalid required fields prevent form completion. It makes at most 16 requests, including retries.
The planner never executes actions or emits selectors. Mercury still supplies typed values. Without `planner_model`,
the normal Jev loop remains available. `PLANNER_API_KEY` defaults to `TEXT_MODEL_API_KEY`; the endpoint defaults to
OpenRouter. Enable it in the local inspector with `JEV_PLANNER_MODEL=google/gemini-3.5-flash uv run jev`, or add
`--planner-model google/gemini-3.5-flash` to `examples/run.py`. The inspector shows the current objective and
requirement checks. New planner reliability and cost have not yet been measured.

Observation fixes also cover visible SVG text, labeled hidden native controls, rich-text editor labels and authorized
PNG sample uploads. Fields remain visible in the state even when offscreen. Unrelated text changes no longer cancel
typing or a web-search navigation.

```bash
git clone https://github.com/browser-use/jev-ultrafast.git
cd jev-ultrafast
uv sync
cp .env.example .env
# Add TYPESAFE_API_KEY and TEXT_MODEL_API_KEY.
uv run jev
```

Open **http://127.0.0.1:8766** and click **Start demo → Run automatically**. The inspector shows numbered elements, operation probabilities, target probabilities, and executed actions. **Choose next** pauses before execution.

Chrome connects through [Browser Harness](https://github.com/browser-use/browser-harness), installed by `uv sync`. Run `uv run browser-harness --doctor` if it needs connecting. Allow remote debugging in Chrome when prompted.

`TEXT_MODEL_API_KEY` is an OpenRouter key in the example configuration. The current demo uses `inception/mercury-2.5` with reasoning disabled. Gemini, GLM, and DeepSeek can also use the OpenAI-compatible text helper; configure the appropriate model, endpoint, and reasoning setting.

## Use the library

```python
from jev_ultrafast import Agent

with Agent(
    "https://www.google.com/travel/flights?hl=en",
    "Find one-way flights from Zurich to London on September 20, 2026, "
    "for one adult in economy. Stop when matching flight options are visible.",
) as agent:
    for state in agent.run():
        print(state["elapsed_ms"], state["status"])
```

Run with `uv run --env-file .env python your_script.py`. The same policy can run a different task:

```bash
uv run --env-file .env python examples/run.py \
  --url https://en.wikipedia.org/wiki/Main_Page \
  --goal 'Find and open the Wikipedia article about Gödel’s incompleteness theorems.'
```

`uv run --env-file .env python examples/flights.py --keep-open` performs the flight search, checks the actual route/date/results, and saves its trace. It does not select or book a flight.

## Why it moves

- **One request per decision cycle.** Complete operation/target choices share the same observed state.
- **No screenshots in the default agent loop.** Jev consumes structured state. The inspector opts into screenshots; the video uses a separate continuous screencast.
- **One browser call per snapshot.** Read visible controls, their names, values, and text atomically. Keep references to the actual DOM nodes.
- **Validate the selected target.** Clicks check the document, form values, target, and nearby context. Animation alone does not force another prediction. Resolve current geometry and reject covered controls before input.
- **Wait for useful state.** After typing into a combobox, wait for visible suggestions, capped at 200 ms. Other interactions get at most two animation frames or 50 ms. These reads happen after execution is logged.
- **Keep hidden tabs rendering.** Focus emulation prevents background animation throttling without switching Chrome's visible tab.
- **Send bounded document text.** Rendered offscreen content and previous pages remain available as evidence.
- **Reuse an interrupted text request.** A generated value survives a stale-page retry only if the entire text-helper input is unchanged.

Every executed target is resolved from an observed node. The executor rechecks page freshness and click occlusion. Model output never becomes selectors, coordinates, shell commands, or executable JavaScript. Text-helper output must parse as a small JSON object before typing.

## Small enough to read

| File | Job |
| --- | --- |
| [agent.py](jev_ultrafast/agent.py) | The complete loop and text-helper handoff |
| [snapshot.js](jev_ultrafast/snapshot.js) | Atomic DOM snapshot, indexed controls, freshness guards |
| [browser.py](jev_ultrafast/browser.py) | Browser connection, current geometry, execution |
| [model.py](jev_ultrafast/model.py) | Joint action selection and text generation |
| [questions.py](jev_ultrafast/questions.py) | Model instructions |
| [demo.py](jev_ultrafast/demo.py) | Local inspector |

## Evidence and limits

The current video is a **7,073 ms** Google Flights run. Timing starts after initial page observation and includes model calls, generated text, browser work, stale decisions, and loading waits. A fresh independent check verifies the one-way setting, Zürich, London, September 20, 2026, and visible flight options. The video plays at 1×, with no opening hold and a 0.5-second final hold.

In six alternating runs with identical models and settings, both versions passed **3/3**. Median task time went from **9.450 s → 7.092 s**, a **25% reduction**; median browser protocol calls went from **1,092 → 101**. This is three repeats of one task on one browser profile, not a general reliability benchmark.

The same policy opened the requested Wikipedia article in **2.798 s** and passed a local hotel search/filter task in **1.896 s**. Runs, failures, source hashes, and measurement boundaries are in [performance.md](docs/performance.md).

A `DONE` choice still requires independent outcome verification. The DOM reader handles common HTML and ARIA controls, not the full accessible-name specification. Cross-origin frames, closed shadow roots, canvas vision, existing-file uploads, pop-up tabs, and arbitrary keyboard widgets remain outside this experiment. Sample uploads support plain text and PNG images. Owned tabs share the existing Chrome profile.

## Development

```bash
uv run ruff check .
uv run pytest
node --check jev_ultrafast/static/app.js
node --check jev_ultrafast/snapshot.js
uv build
```

Tests are offline. `uv run python scripts/check_guards.py` checks real controls in a local browser without model calls. Live examples and recording scripts make paid API calls. `scripts/record_flights.py <new-folder>` captures original browser timestamps; `scripts/render_demo.py <recording-folder>` renders that verified run at 1× and crops out the Google account strip. Credentials and raw traces stay ignored.

---

[Browser Use](https://github.com/browser-use/browser-use) · [Browser Harness](https://github.com/browser-use/browser-harness) · [TypeSafe speculative fan-out](https://docs.typesafe.ai/patterns/fan-out)
