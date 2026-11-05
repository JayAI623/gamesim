#!/usr/bin/env python3
"""Reproducible per-entity uniform draws for the Game Master.

Usage: python3 dice.py <seed> <round> <name1> <name2> ...
Prints JSON {name: r} with r in [0,1). Same (seed, round, names) -> same draws.
"""
import json
import random
import sys


def draws(seed: int, rnd: int, names: list[str]) -> dict:
    rng = random.Random(seed * 1000 + rnd)
    return {n: round(rng.random(), 3) for n in names}


if __name__ == "__main__":
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    print(json.dumps(draws(int(sys.argv[1]), int(sys.argv[2]), sys.argv[3:]), ensure_ascii=False))
