"""TypeSafe makes choices; an optional small OpenAI-compatible model writes field values."""

import json
import math
import os
import re
import time

import httpx

from .questions import NEXT_ACTION, TEXT_VALUE

CLIENT = httpx.Client(http2=True, timeout=25)


def post_json(url, key, body):
    for attempt in range(3):
        try:
            response = CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError:
            raise RuntimeError("Model connection failed; no action executed.") from None
        if response.status_code in {429, 529, 503} and attempt < 2:
            time.sleep(0.5 * 2**attempt)
            continue
        if response.is_error:
            raise RuntimeError(f"Model provider returned HTTP {response.status_code}; no action executed.")
        return response.json()
    raise RuntimeError("Model unavailable")


def validate_choice(answer, ids):
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Invalid TypeSafe response; no action executed.")
    return answer


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT",
                  "scroll_to": "SCROLL_TO", "set_value": "SET_VALUE", "upload": "UPLOAD_FILE"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {k: action[k] for k in
                       ("role", "value", "checked", "selected", "expanded", "context", "input_type", "required")
                       if k in action}
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    return elements, targets, controls


def choose(state, goal, history):
    """Rank executable operation/target pairs jointly, so independent heads cannot disagree."""
    elements, targets, controls = action_space(state["actions"])
    actions, criteria = {}, {}
    for operation, candidates in targets.items():
        for index, action in candidates.items():
            actions[action["id"]] = (operation, index)
            criteria[action["id"]] = {
                "operation": operation, "target": f"[{index}] {action['label']}",
                **{k: action[k] for k in ("role", "input_type", "value", "current_value", "context", "href",
                                          "required", "checked", "selected", "expanded", "validation",
                                          "min", "max", "step", "accept") if k in action},
            }
    for operation, action in controls.items():
        actions[action["id"]] = (operation, None)
        criteria[action["id"]] = action["label"]
    # A failed value is not attempted again on the same unchanged page.
    for rejected in state.get("rejected_actions", []):
        criteria.pop(rejected, None)
    for action in state["actions"]:
        recent = [h for h in history[-4:] if h.get("action") == action["label"] and
                  h.get("kind") == action["kind"] and h.get("url") == state["url"]]
        if len(recent) >= 2 and action["kind"] not in {"wait", "scroll", "search"}:
            criteria.pop(action["id"], None)
    criteria.update(DONE="All requirements are supported by observed evidence; return the final answer.",
                    BLOCKED="Relevant recovery failed; missing information/tools prevent completion.")
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {
            "goal": goal,
            "page": {k: state[k] for k in ("url", "title", "text", "document_text", "ready_state", "focus",
                                            "scroll", "unsupported_frames", "omitted_actions") if k in state},
            "previous_evidence": state.get("memory", []),
            "feedback": state.get("feedback", ""),
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed", "url")} for h in history[-12:]
            ],
        },
        "questions": {"action": {"type": "choice", "criteria": criteria, "instructions": NEXT_ACTION}},
    }
    started = time.perf_counter()
    result = post_json("https://api.typesafe.ai/v1/systemone", os.environ["TYPESAFE_API_KEY"], body)
    answer = validate_choice(result["answers"].get("action", {}), criteria)
    selected = answer["choice"]
    operation, target = actions.get(selected, (selected, None))
    operation_probabilities = {}
    target_probabilities = {}
    for key, probability in answer["probabilities"].items():
        op, ix = actions.get(key, (key, None))
        operation_probabilities[op] = operation_probabilities.get(op, 0) + probability
        if op == operation and ix is not None:
            target_probabilities[ix] = probability
    mass = sum(target_probabilities.values())
    if mass:
        target_probabilities = {k: v / mass for k, v in target_probabilities.items()}
    return {
        "choice": selected, "operation": operation, "target": target,
        "confidence": answer["confidence"], "probabilities": answer["probabilities"],
        "operation_probabilities": operation_probabilities, "target_probabilities": target_probabilities,
        "target_confidence": None, "raw_answers": result["answers"], "model": result["model"],
        "usage": result.get("usage", {}), "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": body,
    }


def parse_json_object(content):
    """Accept one JSON object with optional Markdown fences, never arbitrary trailing prose/objects."""
    if not isinstance(content, str):
        raise ValueError("Expected JSON text")
    content = re.sub(r"^```(?:json)?\s*", "", content.strip(), flags=re.I)
    content = re.sub(r"\s*```$", "", content).strip()
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def field_context(goal, action, page, history):
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value", "kind", "input_type", "context",
                                            "required", "min", "max", "step", "pattern", "accept")},
        "page": {"url": page["url"], "title": page["title"], "text": page["text"][:6000]},
        "previous_evidence": page.get("memory", []),
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-6:]],
    }


def field_text(context):
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError("TYPE_TEXT needs TEXT_MODEL_API_KEY; no text is hardcoded or guessed by the executor.")
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("TEXT_MODEL", "deepseek-chat")
    reasoning = {"thinking": {"type": "disabled"}} if "api.deepseek.com/" in base else {"reasoning": {"effort": "low"}}
    if os.environ.get("TEXT_MODEL_REASONING") == "none":
        reasoning = {"reasoning": {"enabled": False}}
    started = time.perf_counter()
    result = post_json(
        base + "/chat/completions",
        key,
        {
            "model": model,
            "max_tokens": 1024,
            "response_format": {"type": "json_object"},
            **reasoning,
            "messages": [
                {"role": "system", "content": TEXT_VALUE},
                {
                    "role": "user",
                    "content": json.dumps(context),
                },
            ],
        },
    )
    try:
        output = parse_json_object(result["choices"][0]["message"]["content"])
        value = output["text"]
        if set(output) != {"text"} or not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ValueError("Text helper returned no valid field value; nothing typed.") from None
    return value, {
        "model": model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
    }
