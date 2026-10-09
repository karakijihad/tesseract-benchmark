"""Write the Geiger counter data for the science-decay task.

Usage: python generate_data.py <output.csv>

The expected counts in each bin are the exact integral of an exponentially
decaying source rate plus a constant background rate. Each count is a Poisson
draw made with the standard library generator, so the file is reproducible
on any Python 3 version.
"""
from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

PARAMS = json.loads((Path(__file__).with_name("params.json")).read_text(encoding="utf-8"))


def poisson(rng: random.Random, mean: float) -> int:
    """Draw one Poisson variate by inversion of the cumulative distribution."""
    uniform = rng.random()
    probability = math.exp(-mean)
    cumulative = probability
    k = 0
    while uniform > cumulative:
        k += 1
        probability *= mean / k
        cumulative += probability
    return k


def rows() -> list[tuple[int, int, int]]:
    rng = random.Random(PARAMS["seed"])
    decay_constant = math.log(2) / PARAMS["half_life_s"]
    width = PARAMS["bin_width_s"]
    out = []
    for index in range(PARAMS["bins"]):
        start = index * width
        end = start + width
        source = (PARAMS["initial_rate_per_s"] / decay_constant) * (
            math.exp(-decay_constant * start) - math.exp(-decay_constant * end)
        )
        mean = source + PARAMS["background_rate_per_s"] * width
        out.append((start, end, poisson(rng, mean)))
    return out


def csv_text() -> str:
    lines = ["bin_start_s,bin_end_s,counts"]
    lines += [f"{start},{end},{counts}" for start, end, counts in rows()]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(csv_text(), encoding="utf-8", newline="\n")
