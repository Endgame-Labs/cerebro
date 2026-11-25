"""Made-up stored values of every shape, and a sweep that puts each across a cut at its edges, for a connector's
own tests: a cut a source makes runs inside the app's call, with the mask held (`connector.masking`), so it masks a
value before it cuts it, and must leave no six characters of one."""

from __future__ import annotations

import html
import json
import random
from collections.abc import Callable
from urllib.parse import quote

from cerebro.connector import masking
from cerebro.secrets import Scrubber

_rng = random.Random(5)
_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
PEM = ("-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join("".join(_rng.choice(_B64) for _ in range(64)) for _ in range(3))
       + "\n-----END RSA PRIVATE KEY-----")  # made up
SHAPES = {"token": "tok_Ln5QbX8vKz3WcM7pRt2YdH9sJf4G", "spaced": "Quarzite7 vellum9 drifted",
          "lines": "nl-Yh3Wq\n7Zp4Kd-Rt5", "markup": 'pa<ss-w&rd"77x', "pem": PEM}
MASK = Scrubber.of(SHAPES.values())


def leaks(secret: str, text: str) -> list[str]:
    """Every run of six characters of the secret, as written, escaped or folded, that `text` holds."""
    forms = {secret, html.escape(secret), json.dumps(secret)[1:-1], quote(secret, safe=""), " ".join(secret.split())}
    return sorted({f[i:i + 6] for f in forms for i in range(len(f) - 5) if f[i:i + 6] in text})


def bodies(limit: int, secret: str) -> list[str]:
    """Text with `secret` ending just before byte `limit` and at it, then across it by 1, 5, 6, 7, half, all but 6
    and all but 1 of its bytes, then starting at it and after it: after words, after one long run with no white
    space, and after multi-byte letters. Each offset between answers as its neighbours do."""
    size = len(secret.encode())
    across = (-2, -1, 0, 1, 5, 6, 7, size // 2, size - 6, size - 1, size, size + 1)
    out = []
    for start in sorted({max(limit - size + k, 0) for k in across}):
        for filler in (("lorem ipsum " * (start // 12 + 1))[:start], "x" * start, ("ü" * start)[:start // 2]):
            out.append(filler + secret + " and the rest of the line\nand one more line")
    return out


def swept(cut: Callable[[str], str], limit: int) -> dict[str, str]:
    """For each shape, the first text `cut` leaves part of it in, with the mask held; empty when none does."""
    found = {}
    with masking(MASK):
        for name, secret in SHAPES.items():
            for body in bodies(limit, secret):
                if leaks(secret, out := cut(body)):
                    found[name] = out[-80:]
                    break
    return found
