"""Ask the model for a diagnosis, validate the answer, and fall back to the rules if needed.

The model's reply is never shown as-is: it must parse into `LLMAnswer`, and its confidence is
checked against the evidence and the rules before it becomes the `Diagnosis`.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from pfa.ai.context import Prompt, build_prompt
from pfa.ai.providers.base import LLMProvider, ProviderError
from pfa.models import Confidence, Diagnosis, FailureType, Report

_CONFIDENCE_ORDER: list[Confidence] = ["unsure", "low", "medium", "high"]
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


class LLMAnswer(BaseModel):
    """What the model must return. Mirrors ANSWER_SCHEMA."""

    model_config = ConfigDict(extra="forbid")

    failure_type: FailureType
    problem: str
    likely_cause: str
    fix: list[str]
    confidence: Confidence
    evidence_used: list[str]

    @field_validator("problem", "likely_cause", "fix", "evidence_used")
    @classmethod
    def _decode_leftover_escapes(cls, value: str | list[str]) -> str | list[str]:
        """Some models escape twice, so `\\u002F` survives JSON parsing as the text "\\u002F"
        (seen from qwen3:8b: "\\u002Fapp\\u002Fpayment-gateway"). Decode printable ones."""
        if isinstance(value, list):
            return [_LEFTOVER_ESCAPE.sub(_unescape, v) for v in value]
        return _LEFTOVER_ESCAPE.sub(_unescape, value)


_LEFTOVER_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _unescape(m: re.Match[str]) -> str:
    char = chr(int(m.group(1), 16))
    return char if char.isprintable() else m.group(0)


# Written out by hand: structured-output APIs accept only a subset of JSON Schema, and this
# keeps exactly what is sent under review. tests/test_llm.py checks it matches LLMAnswer.
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "failure_type",
        "problem",
        "likely_cause",
        "fix",
        "confidence",
        "evidence_used",
    ],
    "properties": {
        "failure_type": {"type": "string", "enum": [t.value for t in FailureType]},
        "problem": {"type": "string"},
        "likely_cause": {"type": "string"},
        "fix": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": list(_CONFIDENCE_ORDER)},
        "evidence_used": {"type": "array", "items": {"type": "string"}},
    },
}


class InvalidAnswer(Exception):
    pass


def explain(report: Report, provider: LLMProvider, budget_tokens: int = 6000) -> Report:
    """Replace the rules answer with a validated AI answer, or keep it and say why."""
    prompt = build_prompt(report, budget_tokens)
    notes = [f"to fit the prompt budget, {t}" for t in prompt.trimmed]
    try:
        answer = ask(provider, prompt)
    except ProviderError as exc:
        return _keep_rules(report, notes + [f"AI unavailable ({exc}); answered from rules."])
    except InvalidAnswer as exc:
        return _keep_rules(
            report, notes + [f"AI reply was invalid twice ({exc}); answered from rules."]
        )
    diagnosis, checks = check_answer(answer, report, provider.model)
    return report.model_copy(
        update={"diagnosis": diagnosis, "notes": report.notes + notes + checks}
    )


def ask(provider: LLMProvider, prompt: Prompt) -> LLMAnswer:
    """One call, plus one repair call if the reply does not validate."""
    reply = provider.complete(prompt.system, prompt.user, ANSWER_SCHEMA)
    try:
        return parse_answer(reply)
    except InvalidAnswer as first:
        repair = (
            f"{prompt.user}\n\n## Your previous reply was invalid\n{first}\n"
            f"Previous reply:\n{reply[:2000]}\n\n"
            "Reply again with only a JSON object that matches the schema."
        )
        return parse_answer(provider.complete(prompt.system, repair, ANSWER_SCHEMA))


def parse_answer(reply: str) -> LLMAnswer:
    fenced = _FENCE.match(reply)
    text = fenced.group(1) if fenced else reply
    try:
        return LLMAnswer.model_validate(json.loads(text))
    except json.JSONDecodeError as exc:
        raise InvalidAnswer(f"not JSON: {exc.msg} at position {exc.pos}") from exc
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or 'reply'}: {e['msg']}" for e in exc.errors()
        )
        raise InvalidAnswer(f"does not match the schema: {problems}") from exc


def check_answer(answer: LLMAnswer, report: Report, model: str) -> tuple[Diagnosis, list[str]]:
    """Hold the model to the same standard as the rules: confidence must be earned."""
    notes: list[str] = []
    confidence = answer.confidence
    fix = [step.strip() for step in answer.fix if step.strip()]
    evidence = [e.strip() for e in answer.evidence_used if e.strip()]

    if not evidence and _rank(confidence) > _rank("low"):
        confidence = "low"
        notes.append("The AI cited no evidence, so its confidence was lowered to low.")

    classified = report.classification.failure_type
    if answer.failure_type is not classified:
        notes.append(
            f"The AI says {answer.failure_type.value}; the classifier says {classified.value}."
        )
        backed = any(
            h.certainty == "high" and h.failure_type is classified for h in report.rule_hits
        )
        if backed and _rank(confidence) > _rank("medium"):
            confidence = "medium"
            notes.append("A high-certainty rule backs the classifier, so confidence is medium.")

    best = report.rule_hits[0] if report.rule_hits else None
    if best and best.certainty == "high" and confidence == "unsure":
        notes.append(f"The AI was unsure; the rules found a likely cause ({best.rule}).")

    return (
        Diagnosis(
            failure_type=answer.failure_type,
            problem=answer.problem.strip(),
            likely_cause=answer.likely_cause.strip(),
            fix=fix,
            confidence=confidence,
            evidence_used=evidence,
            rule_hits=[h.rule for h in report.rule_hits],
            source="llm",
            model=model,
        ),
        notes,
    )


def _keep_rules(report: Report, notes: list[str]) -> Report:
    return report.model_copy(update={"notes": report.notes + notes})


def _rank(confidence: Confidence) -> int:
    return _CONFIDENCE_ORDER.index(confidence)
