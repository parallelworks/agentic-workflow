#!/usr/bin/env python3
"""validate.py — check a heat-solver field against the golden reference.

This is the backbone of the PORTABILITY exercise. "It ran" is not the same as
"it ran correctly": two clusters are only portable for this workload if they
produce the SAME converged temperature field. We measure that as the relative
L2 norm of the difference between a run's field and the reference field.

Usage:
    validate.py --field <run_dump> --reference <golden.ref> [--tol 1e-6]

Exit code 0 = within tolerance (portable), 1 = diverged, 2 = usage/shape error.
Prints a one-line JSON record so the agents can parse the verdict.

Note on determinism: bit-identical fields are NOT guaranteed across different
compilers, math libraries, or CPU/GPU floating-point ordering. A small nonzero
L2 difference is EXPECTED and fine — that is exactly what the tolerance is for.
A large difference means something is genuinely wrong (wrong stencil, broken
halo exchange, uninitialized boundary), which is the failure the exercise hunts.
"""

import argparse
import json
import math
import sys


def load_field(path):
    """Read a whitespace-separated grid, skipping comment lines starting with #."""
    vals = []
    grid = None
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                # header may carry grid=N; parse it if present
                for tok in line.split():
                    if tok.startswith("grid="):
                        try:
                            grid = int(tok.split("=", 1)[1])
                        except ValueError:
                            pass
                continue
            vals.extend(float(x) for x in line.split())
    return vals, grid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--field", required=True, help="run's dumped field file")
    ap.add_argument("--reference", required=True, help="golden reference field")
    ap.add_argument("--tol", type=float, default=1e-6,
                    help="max relative L2 difference to count as portable")
    args = ap.parse_args()

    try:
        field, gf = load_field(args.field)
        ref, gr = load_field(args.reference)
    except FileNotFoundError as exc:
        print(json.dumps({"ok": False, "error": f"missing file: {exc.filename}"}))
        return 2

    if len(field) != len(ref):
        print(json.dumps({
            "ok": False,
            "error": "shape mismatch",
            "field_cells": len(field),
            "reference_cells": len(ref),
        }))
        return 2

    # Relative L2 norm of the difference.
    num = 0.0
    den = 0.0
    for a, b in zip(field, ref):
        num += (a - b) ** 2
        den += b * b
    rel_l2 = math.sqrt(num) / math.sqrt(den) if den > 0 else math.sqrt(num)

    ok = rel_l2 <= args.tol
    print(json.dumps({
        "ok": ok,
        "relative_l2": rel_l2,
        "tolerance": args.tol,
        "cells": len(field),
        "grid": gf or gr,
    }))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
