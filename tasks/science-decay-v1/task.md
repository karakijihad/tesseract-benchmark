# Half-life from a Geiger counter

## Context

Your folder contains `data.csv` and `DATA_NOTE.md`. The data are the counts recorded by a Geiger counter placed next to a radioactive sample, in equal time bins. The half-life of the sample is not known. There is also a constant background count rate from the surroundings, and its size is not known either. Read `DATA_NOTE.md` first.

## End goal

Estimate the half-life of the sample and the background count rate from the data, each with an uncertainty, and explain how you did it.

## Requirements

1. Write `results.json` containing one JSON object with these fields:
   - `half_life_s`: your estimate of the half-life, in seconds.
   - `half_life_uncertainty_s`: one standard deviation on the half-life, in seconds.
   - `background_rate_per_s`: your estimate of the background rate, in counts per second.
   - `background_uncertainty_per_s`: one standard deviation on the background rate, in counts per second.
   - `method`: one sentence naming the fitting method you used.
2. Write `fit.png`, a plot of the measured data and your fitted model against time, with labelled axes.
3. Write `report.md`, a short report that explains the method, states the results with their uncertainties, says how well the model describes the data, and lists at least two sources on the method, each as a full http or https URL.

## Constraints

Use any tools you normally use. The counts are random, so treat the uncertainty as part of the answer, not an extra. Everything you produce must be in your folder.

You are the whole system for this task. You may use your own sub-agents, workers, or parallel sessions the way you normally would. Everything they do counts as your work and your cost. Do not get help from outside your own system: no human, and no other contestant.

## References

The data are supplied. You may research the method online. Cite only sources you actually opened.

## Verification expected

Check your fit against the data yourself, for example by looking at the plot and at how the residuals behave, before you report it.

## Final response

State the half-life and background you found with their uncertainties, which files you wrote, and what remains uncertain. Do not claim a result you did not compute.
