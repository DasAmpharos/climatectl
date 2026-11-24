"""One Messages API call per plan. The system prompt describes the house, the rules and the output
and stays the same from run to run, so it's cached; this run's input goes in the user message as
JSON. Structured outputs (output_config.format) make the reply parse; the guardrails still check
every value. On models that support it, a refused request is retried on a fallback model in
the same call (server-side refusal fallback), so a false-positive safety decline doesn't cost
the run.

The client is passed in, so tests use a fake and nothing here reads the API key."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic

from .config import Config, delta_from_c, from_c
from .context import Context

MAX_TOKENS = 16000  # thinking plus the plan; a plan itself is a few hundred tokens
# List prices per million tokens (input, output, cache read), 2026-11. A cache write costs
# 1.25x input. Used only to log an estimate; unknown models log tokens without a cost.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}

# Models with server-side refusal fallback; "default" lets the API pick the fallback by refusal
# category. Haiku 5.5 has none.
FALLBACK_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM = """\
You plan thermostat setpoints for one house for the next 24 hours. The goal is the least heating
and cooling energy that keeps every level inside its comfort band: comfort is the constraint,
energy the objective.

How the thermostats work:
- Each level has its own thermostat and its own furnace and AC.
- In heat_cool mode a thermostat holds two setpoints. The furnace runs while the room is below
  the heat setpoint, the AC while it is above the cool setpoint, and nothing runs in between. In
  heat or cool mode only that one setpoint matters.
- A comfort band gives the coldest (heat_at_least) and warmest (cool_at_most) acceptable
  temperature for its hours. Setpoints at the band's edges are the cheapest way to stay inside
  it. A setpoint inside the band costs energy, and is only worth it to pre-heat or pre-cool ahead
  of a stricter band the level couldn't otherwise reach in time.
- A band with cool_at_most_rule sets its cool limit by the room's dew point; its cool_at_most is
  already the limit for this plan.
- Each thermostat reads one room, so "inside the band" means at the thermostat.

Each level's house_model is fitted to the logged readings (null where there isn't enough yet).
Per hour, a level changes by:
  -heat_loss_per_hour_per_degree_of_difference * (indoor - outdoor)
  + furnace_degrees_per_hour while the furnace runs, ac_degrees_per_hour (negative) while the
    AC runs
  + sun_degrees_per_hour_per_kw_m2 * sun_w_m2 / 1000
  + other_gains_degrees_per_hour (people, appliances, the other level)
For example a loss of 0.03 with 64 inside and 44 outside is -0.6 an hour. The furnace and AC
rates are effective rates over whole days: they predict how long the equipment runs, but a
recovery after a setback is usually faster than they suggest, so pre-heating needn't start
earlier than they say. typical_miss is how far the fitted model's readings were off, and
outdoor_range_fitted the outdoor range it was fitted over; outside it, it's an extrapolation.
recent_days shows furnace and AC minutes against degree-days, as a check on the model.

Use the forecast and the model to avoid waste: let a level coast toward the band's edge when the
weather or the sun will carry it, and start pre-heating or pre-cooling only as early as the rates
require. Feedback rows are what people said, with the readings at the time; notes are the
preferences learned so far. When feedback and the bands disagree, keep to the bands and say so
in the rationale: the bands are changed in the config, not by the plan.

Your plan is checked in code, and every fix is logged as a correction against the plan. Aim for
none:
- Times are local, written YYYY-MM-DDTHH:MM, on the quarter hour, within 24 hours of now.
- At least {min_interval} minutes between changes on a level.
- Each setpoint moves at most {max_step} per change.
- During every band in force while a change holds: heat at or above the band's heat_at_least,
  cool at or below its cool_at_most.
- Never below {floor} or above {ceiling}, and cool at least {min_gap} above heat.
- Whole degrees for F, half degrees for C.
- A level with planned: false gets no changes.

Output:
- levels: one entry per level. changes lists only the moments a setpoint changes, in time
  order, and each holds until the next. The current setpoints hold until the first change; an
  empty list keeps them all day. Give both heat and cool in every change; in heat or cool mode
  the unused one is ignored.
- rationale: one or two plain sentences per level saying why.
- notes_update: the notes rewritten only when the feedback shows a new or changed preference.
  Keep every earlier preference, add the new one, keep it short. Otherwise an empty string.

Temperatures are in {unit_name}.

The house:
{house}

The levels:
{levels}
"""


NO_CREDENTIALS = (
    "No Anthropic credentials: set ANTHROPIC_API_KEY, or sign in once with `ant auth login`."
)


class PlannerError(RuntimeError):
    """The call failed or the reply can't be used. Nothing is planned this run."""


class Client(Protocol):
    @property
    def messages(self) -> Any: ...


@dataclass(frozen=True)
class Reply:
    raw: dict[str, Any]  # the parsed plan
    model: str  # the model that answered
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int

    @property
    def cost_usd(self) -> float | None:
        prices = PRICES.get(self.model)
        if prices is None:
            return None
        inp, out, cached = prices
        return (
            self.input_tokens * inp
            + self.cache_write_tokens * inp * 1.25
            + self.cache_read_tokens * cached
            + self.output_tokens * out
        ) / 1e6


def system_prompt(config: Config) -> str:
    unit, limits = config.unit, config.limits

    def t(c: float) -> str:
        return f"{from_c(c, unit):g}{unit}"

    def d(c: float) -> str:
        return f"{round(delta_from_c(c, unit), 1):g}{unit}"

    levels = "\n".join(f"- {lv.key}: {lv.about or '(no description)'}" for lv in config.levels)
    return SYSTEM.format(
        min_interval=f"{limits.min_interval.total_seconds() / 60:.0f}",
        max_step=d(limits.max_step_c),
        floor=t(limits.floor_c),
        ceiling=t(limits.ceiling_c),
        min_gap=d(limits.min_gap_c),
        unit_name="degrees Fahrenheit" if unit == "F" else "degrees Celsius",
        house=config.house or "(no description)",
        levels=levels,
    )


def schema(config: Config) -> dict[str, Any]:
    """The reply's JSON schema. Structured outputs take no numeric limits; the guardrails
    enforce those."""
    change = {
        "type": "object",
        "properties": {
            "at": {"type": "string", "description": "local time, YYYY-MM-DDTHH:MM"},
            "heat": {"type": "number"},
            "cool": {"type": "number"},
        },
        "required": ["at", "heat", "cool"],
        "additionalProperties": False,
    }
    level = {
        "type": "object",
        "properties": {
            "level": {"type": "string", "enum": [lv.key for lv in config.levels]},
            "changes": {"type": "array", "items": change},
            "rationale": {"type": "string"},
        },
        "required": ["level", "changes", "rationale"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "levels": {"type": "array", "items": level},
            "notes_update": {"type": "string"},
        },
        "required": ["levels", "notes_update"],
        "additionalProperties": False,
    }


def request(ctx: Context, config: Config, model: str | None = None) -> dict[str, Any]:
    """The Messages API request, as keyword arguments."""
    model = model or config.model
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": [
            {"type": "text", "text": system_prompt(config), "cache_control": {"type": "ephemeral"}}
        ],
        "messages": [
            {
                "role": "user",
                "content": "This run's input:\n" + json.dumps(ctx.payload, indent=1),
            }
        ],
        "output_config": {
            "effort": config.effort,
            "format": {"type": "json_schema", "schema": schema(config)},
        },
    }
    if model in FALLBACK_MODELS:
        kwargs["betas"] = [FALLBACK_BETA]
        kwargs["fallbacks"] = "default"
    return kwargs


def call(client: Client, kwargs: dict[str, Any]) -> Reply:
    # Fallbacks are a beta parameter, so those requests go to the beta endpoint.
    messages = client.beta.messages if "betas" in kwargs else client.messages
    try:
        response = messages.create(**kwargs)
    except anthropic.APIStatusError as e:
        raise PlannerError(f"Claude API error {e.status_code} ({type(e).__name__})") from None
    except anthropic.APIError as e:  # connection failures and timeouts
        raise PlannerError(f"Claude API error ({type(e).__name__})") from None
    except anthropic.CredentialsError:
        raise PlannerError(NO_CREDENTIALS) from None
    if response.stop_reason == "refusal":
        raise PlannerError("Claude declined to plan this run (stop_reason refusal).")
    if response.stop_reason == "max_tokens":
        raise PlannerError(f"The reply hit max_tokens ({kwargs['max_tokens']}) before finishing.")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise PlannerError(f"The reply had no text (stop_reason {response.stop_reason}).")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        raise PlannerError("The reply wasn't valid JSON.") from None
    usage = response.usage
    return Reply(
        raw=raw,
        model=response.model,
        stop_reason=response.stop_reason,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
        cache_write_tokens=usage.cache_creation_input_tokens or 0,
    )
