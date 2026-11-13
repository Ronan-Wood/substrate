"""Print the real vault-a vector statistics next to same-size synthetic corpora.

usage: .venv/bin/python calibrate.py > results/calibration.txt

Two real sets: every chunk vector (what the generator was tuned against) and passage chunks only (what
the vector arm searches; outline chunks are near-duplicates of their passages).
"""
import numpy as np

import common as c


def stats(x: np.ndarray) -> dict:
    s = x @ x.T
    np.fill_diagonal(s, -1)
    srt = -np.sort(-s, axis=1)
    pairs = np.random.default_rng(0).integers(0, len(x), (20000, 2))
    pairs = pairs[pairs[:, 0] != pairs[:, 1]]  # the diagonal holds -1, not a pair
    mu = x.mean(0)
    ev = np.linalg.eigvalsh(np.cov((x - mu).T))
    return {
        "random pair cos": float(s[pairs[:, 0], pairs[:, 1]].mean()),
        "nn1 cos": float(srt[:, 0].mean()),
        "nn10 cos": float(srt[:, 9].mean()),
        "nn100 cos": float(srt[:, 99].mean()),
        "|mean|": float(np.linalg.norm(mu)),
        "participation ratio": float(ev.sum() ** 2 / (ev ** 2).sum()),
    }


if __name__ == "__main__":
    every = c.real_vectors("vault-a")
    passages, _, _, _ = c.real_corpus("vault-a", n_queries=0)
    cols = [("vault-a, all chunks", stats(every)), (f"synthetic {len(every):,}", stats(c.synth(len(every))[0])),
            ("vault-a, passages", stats(passages)), (f"synthetic {len(passages):,}", stats(c.synth(len(passages))[0]))]
    print(f"{'statistic':22} " + " ".join(f"{name:>18}" for name, _ in cols))
    for key in cols[0][1]:
        print(f"{key:22} " + " ".join(f"{st[key]:18.3f}" for _, st in cols))
