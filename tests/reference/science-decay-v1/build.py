"""Reference solution for science-decay-v1.

Usage: python build.py --workspace <dir>

Reads data.csv from the workspace, fits an exponential decay plus a constant
background by Poisson maximum likelihood (Fisher scoring), and writes
results.json, fit.png and report.md next to it.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


def expected_counts(theta, start, end):
    rate0, lam, background = theta
    width = end - start
    source = (rate0 / lam) * (np.exp(-lam * start) - np.exp(-lam * end))
    return source + background * width


def jacobian(theta, start, end):
    columns = []
    for index in range(3):
        step = abs(theta[index]) * 1e-6
        up = np.array(theta, float)
        down = np.array(theta, float)
        up[index] += step
        down[index] -= step
        columns.append((expected_counts(up, start, end) - expected_counts(down, start, end)) / (2 * step))
    return np.stack(columns, axis=1)


def fit(start, end, counts):
    width = end - start
    background0 = float(np.mean(counts[-10:] / width[-10:])) * 0.8
    theta = np.array([counts[0] / width[0] - background0, 0.005, background0])
    for _ in range(200):
        mu = expected_counts(theta, start, end)
        jac = jacobian(theta, start, end)
        weights = 1.0 / mu
        info = jac.T @ (jac * weights[:, None])
        score = jac.T @ ((counts - mu) * weights)
        step = np.linalg.solve(info, score)
        scale = 1.0
        while scale > 1e-6:
            trial = theta + scale * step
            if trial[1] > 0 and trial[2] > 0 and np.all(expected_counts(trial, start, end) > 0):
                break
            scale /= 2
        theta = theta + scale * step
        if np.max(np.abs(scale * step) / np.abs(theta)) < 1e-10:
            break
    mu = expected_counts(theta, start, end)
    jac = jacobian(theta, start, end)
    covariance = np.linalg.inv(jac.T @ (jac / mu[:, None]))
    return theta, covariance, mu


def draw_plot(path, start, end, counts, mu):
    width, height = 900, 560
    left, right, top, bottom = 90, 30, 40, 70
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    mid = (start + end) / 2
    rate = counts / (end - start)
    model_rate = mu / (end - start)
    x_max = float(end[-1])
    y_max = float(rate.max()) * 1.08

    def px(x):
        return left + (x / x_max) * (width - left - right)

    def py(y):
        return height - bottom - (y / y_max) * (height - top - bottom)

    draw.rectangle([left, top, width - right, height - bottom], outline="black")
    for tick in range(0, int(x_max) + 1, 200):
        draw.line([px(tick), height - bottom, px(tick), height - bottom + 6], fill="black")
        draw.text((px(tick) - 10, height - bottom + 10), str(tick), fill="black")
    for tick in range(0, int(y_max) + 1, 5):
        draw.line([left - 6, py(tick), left, py(tick)], fill="black")
        draw.text((left - 40, py(tick) - 6), str(tick), fill="black")
    draw.text((width // 2 - 70, height - 30), "Time (s)", fill="black")
    draw.text((10, 12), "Count rate (counts per second): data and Poisson fit", fill="black")
    for x, y in zip(mid, rate):
        draw.ellipse([px(x) - 3, py(y) - 3, px(x) + 3, py(y) + 3], fill=(31, 119, 180))
    draw.line([(px(x), py(y)) for x, y in zip(mid, model_rate)], fill=(214, 39, 40), width=3)
    image.save(path, format="PNG")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    workspace = parser.parse_args().workspace
    with (workspace / "data.csv").open(newline="", encoding="utf-8") as handle:
        table = list(csv.DictReader(handle))
    start = np.array([float(row["bin_start_s"]) for row in table])
    end = np.array([float(row["bin_end_s"]) for row in table])
    counts = np.array([float(row["counts"]) for row in table])
    theta, covariance, mu = fit(start, end, counts)
    lam = theta[1]
    half_life = math.log(2) / lam
    half_life_sigma = math.log(2) / lam**2 * math.sqrt(covariance[1, 1])
    background_sigma = math.sqrt(covariance[2, 2])
    results = {
        "half_life_s": half_life,
        "half_life_uncertainty_s": half_life_sigma,
        "background_rate_per_s": float(theta[2]),
        "background_uncertainty_per_s": background_sigma,
        "method": "Poisson maximum likelihood fit of an exponential decay plus a constant background",
    }
    (workspace / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    draw_plot(workspace / "fit.png", start, end, counts, mu)
    report = f"""# Half-life of the sample

## Method

Each bin holds a count of detector clicks, so the counts follow a Poisson
distribution around a mean that is the integral of a decaying source rate
plus a constant background rate. I fitted three parameters (initial source
rate, decay constant and background rate) by maximising the Poisson
likelihood, using Fisher scoring. The half-life is ln 2 divided by the decay
constant. Uncertainties come from the inverse of the Fisher information
matrix, propagated to the half-life. A fit without a background term would
read the flat tail as a slower decay and give a half-life that is too long.

## Result

- Half-life: {half_life:.1f} +/- {half_life_sigma:.1f} s
- Background: {theta[2]:.2f} +/- {background_sigma:.2f} counts per second

The plot in `fit.png` shows the measured count rate with the fitted curve.

## Sources

- Poisson regression and likelihood fitting: https://en.wikipedia.org/wiki/Poisson_regression
- Radioactive decay and half-life: https://en.wikipedia.org/wiki/Exponential_decay
"""
    (workspace / "report.md").write_text(report, encoding="utf-8")


if __name__ == "__main__":
    main()
