"""The planner: turns the forecast, each level's house model and its comfort bands into a 24-hour
setpoint plan through one Claude call, then applies deterministic guardrails to it.

So far it only plans (`climatectl plan`): every run and its plan are stored in `plan_run`, and
nothing is written to a thermostat. Comfort bands are the constraint and energy the objective:
the cheapest plan that keeps each level inside the band in force."""
