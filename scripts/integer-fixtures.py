#!/usr/bin/env python3
"""Create fixed RSA and convex hull reference inputs."""

import argparse
import hashlib
import math
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "benchmarks"
DER = bytes.fromhex("3031300d060960864801650304020105000420")


def prime(bits, seed):
    rng = random.Random(seed)
    while True:
        candidate = rng.getrandbits(bits) | (3 << (bits - 2)) | 1
        if math.gcd(candidate - 1, 65537) != 1:
            continue
        if any(candidate % p == 0 for p in (3, 5, 7, 11, 13, 17, 19, 23, 29, 31)):
            continue
        odd = candidate - 1
        shifts = 0
        while odd % 2 == 0:
            odd //= 2
            shifts += 1
        for base in range(2, 34):
            value = pow(base, odd, candidate)
            if value in (1, candidate - 1):
                continue
            for _ in range(shifts - 1):
                value = value * value % candidate
                if value == candidate - 1:
                    break
            else:
                break
        else:
            return candidate


def rsa_fixture():
    vectors = []
    constants = []
    for bits in (2048, 4096):
        p = prime(bits // 2, bits)
        q = prime(bits // 2, bits + 1)
        modulus = p * q
        exponent = pow(65537, -1, (p - 1) * (q - 1))
        digest = hashlib.sha256(f"AIHC RSA benchmark {bits}".encode()).digest()
        width = bits // 8
        encoded = b"\x00\x01" + b"\xff" * (width - 54) + b"\x00" + DER + digest
        message = int.from_bytes(encoded, "big")
        signature = pow(message, exponent, modulus)
        assert pow(signature, 65537, modulus) == message
        malformed = message ^ (1 << (8 * (width - 3)))
        malformed_signature = pow(malformed, exponent, modulus)
        altered_digest = bytes([digest[0] ^ 1]) + digest[1:]
        for name, value, kind in (
            ("modulus", modulus, "Integer"),
            ("signature", signature, "Integer"),
            ("digest", list(digest), "[Int]"),
            ("alteredDigest", list(altered_digest), "[Int]"),
            ("malformedSignature", malformed_signature, "Integer"),
        ):
            constants.append(f"{name}{bits} :: {kind}\n{name}{bits} = {value}\n")
        for index, (supplied, supplied_name, digest_name, expected) in enumerate((
            (signature, f"signature{bits}", f"digest{bits}", True),
            (signature ^ 1, f"corruptSignature{bits}", f"digest{bits}", False),
            (signature, f"signature{bits}", f"alteredDigest{bits}", False),
            (malformed_signature, f"malformedSignature{bits}", f"digest{bits}", False),
        )):
            recovered = pow(supplied, 65537, modulus)
            constants.append(f"recovered{bits}_{index} :: Integer\nrecovered{bits}_{index} = {recovered}\n")
            vectors.append(
                f"Vector {width} modulus{bits} {supplied_name} {digest_name} "
                f"(Just recovered{bits}_{index}) {str(expected)}"
            )
        constants.append(f"corruptSignature{bits} :: Integer\ncorruptSignature{bits} = {signature ^ 1}\n")
        vectors.append(f"Vector {width} modulus{bits} (-1) digest{bits} Nothing False")
        vectors.append(f"Vector {width} modulus{bits} modulus{bits} digest{bits} Nothing False")
    return (
        "-- Create this file with scripts/integer-fixtures.py.\n"
        "module Fixtures (vectors) where\n\n"
        "import Rsa (Vector (..))\n\n"
        "vectors :: [Vector]\n"
        "vectors = [\n  " + ",\n  ".join(vectors) + "\n  ]\n\n"
        + "\n".join(constants)
    )


def cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def reference_hull(points):
    """Use gift wrap to obtain the reference hull."""
    unique = sorted(set(points))
    if len(unique) < 2:
        return unique
    hull = []
    current = unique[0]
    while True:
        hull.append(current)
        next_point = next(point for point in unique if point != current)
        for point in unique:
            turn = cross(current, next_point, point)
            point_distance = sum((point[i] - current[i]) ** 2 for i in (0, 1))
            next_distance = sum((next_point[i] - current[i]) ** 2 for i in (0, 1))
            if turn < 0 or (turn == 0 and point_distance > next_distance):
                next_point = point
        current = next_point
        if current == hull[0]:
            return hull


def hull_fixture():
    rng = random.Random(20261011)
    points = [(rng.randrange(-10**12, 10**12), rng.randrange(-10**12, 10**12)) for _ in range(512)]
    points += [(i * 10**10, i * 10**10 + delta) for i in range(-80, 81) for delta in (-1, 0, 1)]
    points += points[:16]
    cases = [
        points,
        [],
        [(1, 2)],
        [(1, 2), (1, 2)],
        [(i * 10**12, -i * 10**12) for i in range(-3, 4)],
        [(0, 0), (10**12, 10**12 - 1), (10**12 + 1, 10**12), (2 * 10**12, 2 * 10**12)],
    ]
    render = lambda ps: "[\n    " + ",\n    ".join(f"({x}, {y})" for x, y in ps) + "\n    ]" if ps else "[]"
    return (
        "-- Create this file with scripts/integer-fixtures.py.\n"
        "module Fixtures (cases) where\n\n"
        "import Hull (Point)\n\n"
        "cases :: [([Point], [Point])]\n"
        "cases = [\n  "
        + ",\n  ".join(f"({render(case)}, {render(reference_hull(case))})" for case in cases)
        + "\n  ]\n"
    )


def normalized(text):
    return "".join(line.split("--", 1)[0] for line in text.splitlines()).split()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check the fixed fixture files.")
    args = parser.parse_args()
    for name, text in (("rsa-verify", rsa_fixture()), ("exact-convex-hull", hull_fixture())):
        path = ROOT / name / "Fixtures.hs"
        if args.check:
            assert "".join(normalized(path.read_text())) == "".join(normalized(text)), str(path)
        else:
            path.write_text(text)


if __name__ == "__main__":
    main()
