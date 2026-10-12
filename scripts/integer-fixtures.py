#!/usr/bin/env python3
"""Create fixed RSA, convex hull and arithmoi reference inputs."""

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


def is_prime(candidate, rounds=64):
    """Miller-Rabin with the first 64 primes as bases.

    Deterministic below 3.3e24; the 256-bit inputs are checked against a
    witness set large enough that an error is negligible.
    """
    if candidate < 2:
        return False
    small = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71, 73, 79, 83, 89, 97]
    while len(small) < rounds:
        following = small[-1] + 2
        while any(following % q == 0 for q in small if q * q <= following):
            following += 2
        small.append(following)
    for q in small:
        if candidate == q:
            return True
        if candidate % q == 0:
            return False
    odd, shifts = candidate - 1, 0
    while odd % 2 == 0:
        odd //= 2
        shifts += 1
    for base in small[:rounds]:
        value = pow(base, odd, candidate)
        if value in (1, candidate - 1):
            continue
        for _ in range(shifts - 1):
            value = value * value % candidate
            if value == candidate - 1:
                break
        else:
            return False
    return True


def next_prime(value):
    candidate = value + 1 if value % 2 == 0 else value + 2
    while not is_prime(candidate):
        candidate += 2
    return candidate


def random_prime(rng, bits):
    while True:
        candidate = rng.getrandbits(bits) | (1 << (bits - 1)) | 1
        if is_prime(candidate):
            return candidate


#: Work per profile: (balanced semiprimes, bits per balanced factor, other cases, prime searches, search bits).
#: Each list is run once; the sizes are chosen so native AIHC on an Apple M4 Pro takes about half a second.
ARITHMOI_PROFILES = {
    "O0": (4, 26, 4, 2, 128),
    "Os": (4, 26, 4, 2, 128),
    "O1": (4, 26, 4, 2, 128),
    "O2": (28, 30, 8, 4, 192),
}


def arithmoi_cases(rng, balanced, balanced_bits, others, searches, search_bits):
    """Integers with a known factorisation, and numbers with a known next prime.

    Every factorisation is known by construction: the primes are chosen first
    and multiplied, so no factoring algorithm is involved in the reference.
    """
    factorisations = []

    def product(primes):
        counts = {}
        for q in primes:
            counts[q] = counts.get(q, 0) + 1
        value = 1
        for q, k in counts.items():
            value *= q ** k
        factorisations.append((value, sorted(counts.items())))

    # Balanced semiprimes: the costly case, which trial division cannot split.
    for _ in range(balanced):
        product([random_prime(rng, balanced_bits), random_prime(rng, balanced_bits)])
    # Unbalanced semiprimes: a 16-bit factor found early, a 40-bit cofactor to prove prime.
    for _ in range(others):
        product([random_prime(rng, 16), random_prime(rng, 40)])
    # Three primes of about 19 bits.
    for _ in range(others):
        product([random_prime(rng, 19) for _ in range(3)])
    # Prime powers.
    for exponent in (2, 3, 4, 5, 7, 11, 13, 17)[:others]:
        product([random_prime(rng, 60 // exponent)] * exponent)
    # Smooth numbers with repeated small primes.
    for _ in range(others):
        product([random_prime(rng, rng.randrange(2, 12)) for _ in range(rng.randrange(6, 14))])
    # Primes of about 62 bits, which factorise must recognise as prime.
    for _ in range(others // 2):
        product([random_prime(rng, 62)])
    # Edge cases.
    factorisations.append((1, []))
    factorisations.append((2, [(2, 1)]))
    factorisations.append((2 ** 62, [(2, 62)]))
    factorisations.append((3 ** 39, [(3, 39)]))
    factorisations.append((2 ** 31 - 1, [(2 ** 31 - 1, 1)]))
    factorisations.append(((2 ** 31 - 1) ** 2, [(2 ** 31 - 1, 2)]))

    def render_factors(factors):
        return "[" + ", ".join(f"({q}, {k})" for q, k in factors) + "]"

    cases = [f"Factor {value} {render_factors(factors)}" for value, factors in factorisations]
    for _ in range(searches):
        begin = rng.getrandbits(search_bits) | (1 << (search_bits - 1))
        cases.append(f"NextPrime {begin} {next_prime(begin)}")
    return cases


def arithmoi_fixture():
    """One fixed case list per profile, each run exactly once."""
    rng = random.Random(20261012)
    lists = []
    for profile, parameters in ARITHMOI_PROFILES.items():
        cases = arithmoi_cases(rng, *parameters)
        lists.append(f"cases{profile} :: [Case]\ncases{profile} = [\n  " + ",\n  ".join(cases) + "\n  ]\n")
    return (
        "-- Create this file with scripts/integer-fixtures.py.\n"
        "module Fixtures (cases) where\n\n"
        "import Factorise (Case (..))\n\n"
        "-- | The fixed cases of a profile, sized so that one pass takes about half a second.\n"
        "cases :: String -> Maybe [Case]\n"
        + "".join(f'cases "{profile}" = Just cases{profile}\n' for profile in ARITHMOI_PROFILES)
        + "cases _ = Nothing\n\n"
        + "\n".join(lists)
    )


def normalized(text):
    return "".join(line.split("--", 1)[0] for line in text.splitlines()).split()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check the fixed fixture files.")
    args = parser.parse_args()
    for name, text in (
        ("rsa-verify", rsa_fixture()),
        ("exact-convex-hull", hull_fixture()),
        ("arithmoi-factorise", arithmoi_fixture()),
    ):
        path = ROOT / name / "Fixtures.hs"
        if args.check:
            assert "".join(normalized(path.read_text())) == "".join(normalized(text)), str(path)
        else:
            path.write_text(text)


if __name__ == "__main__":
    main()
