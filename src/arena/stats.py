import json
import math
import random
import statistics as st
from collections import defaultdict
from pathlib import Path

LEVELS = ["L0", "L1", "L2", "L3", "L4"]
ABLATION = ["L1", "L2d", "L2f", "L2"]
DEV = {"train": "dev", "test": "test"}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def pct(k: int, n: int) -> str:
    if n == 0:
        return "n/a"
    lo, hi = wilson(k, n)
    return f"{100 * k / n:.1f}% [{100 * lo:.1f}, {100 * hi:.1f}] (n={n})"


def load(run: str, kind: str) -> list[dict]:
    p = Path("runs") / run / f"{kind}.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def asr(rows: list[dict]) -> dict:
    """trial-level rate, plus per-attack and per-repeat views for the variance report"""
    n, k = len(rows), sum(r["success"] for r in rows)
    by_atk, by_rep = defaultdict(list), defaultdict(list)
    for r in rows:
        by_atk[r["id"]].append(r["success"])
        by_rep[r["rep"]].append(r["success"])
    rep_rates = [sum(v) / len(v) for v in by_rep.values()]
    ever = sum(any(v) for v in by_atk.values())
    flaky = sum(0 < sum(v) < len(v) for v in by_atk.values())
    return dict(
        k=k,
        n=n,
        attacks=len(by_atk),
        ever=ever,
        flaky=flaky,
        rep_sd=st.pstdev(rep_rates) if len(rep_rates) > 1 else 0.0,
    )


def cell(rows):
    a = asr(rows)
    return pct(a["k"], a["n"]), a


def tables(run: str) -> tuple[str, dict]:
    rep, ben, ada = load(run, "replay"), load(run, "benign"), load(run, "adaptive")
    out, js = [], {}

    def sel(level, split, channel=None, **kw):
        return [
            r
            for r in rep
            if r["level"] == level
            and r["split"] == split
            and (channel is None or r["channel"] == channel)
            and all(r[k] == v for k, v in kw.items())
        ]

    def util(level):
        rows = [r for r in ben if r["level"] == level]
        return sum(r["ok"] for r in rows), len(rows)

    out.append("## Attack success rate (trial level, Wilson 95% CI)\n")
    out.append("| level | split | direct | indirect | utility |\n|---|---|---|---|---|")
    for lv in LEVELS:
        for split in ("test", "train"):
            d, a_d = cell(sel(lv, split, "direct"))
            i, a_i = cell(sel(lv, split, "indirect"))
            js[f"{lv}/{split}"] = dict(direct=a_d, indirect=a_i)
            out.append(
                f"| {lv} | {DEV[split]} | {d} | {i} | {pct(*util(lv)) if split == 'test' else ''} |"
            )

    out.append("\n## Repeat variance (test split)\n")
    out.append(
        "| level | channel | per-repeat ASR sd | attacks with any success | attacks with mixed outcomes |\n|---|---|---|---|---|"
    )
    for lv in LEVELS:
        for ch in ("direct", "indirect"):
            a = js[f"{lv}/test"][ch]
            out.append(
                f"| {lv} | {ch} | {100 * a['rep_sd']:.1f} pts | {a['ever']}/{a['attacks']} | {a['flaky']}/{a['attacks']} |"
            )

    out.append("\n## Ablation (test split)\n")
    out.append("| variant | direct | indirect | utility |\n|---|---|---|---|")
    for lv in ABLATION:
        out.append(
            f"| {lv} | {cell(sel(lv, 'test', 'direct'))[0]} | {cell(sel(lv, 'test', 'indirect'))[0]} | {pct(*util(lv))} |"
        )

    out.append("\n## Per technique, test split (successes/trials)\n")
    techs = sorted({r["technique"] for r in rep if r["split"] == "test"})
    out.append("| technique | " + " | ".join(LEVELS) + " |\n|---|" + "---|" * len(LEVELS))
    for t in techs:
        cells = []
        for lv in LEVELS:
            rows = sel(lv, "test", technique=t)
            cells.append(f"{sum(r['success'] for r in rows)}/{len(rows)}")
        out.append(f"| {t} | " + " | ".join(cells) + " |")

    out.append("\n## Unparseable detector/quarantine replies\n")
    out.append("| level | episodes with a parse failure | episodes |\n|---|---|---|")
    for lv in LEVELS:
        rows = [r for r in rep + ben if r["level"] == lv]
        out.append(f"| {lv} | {sum(r.get('parse_fail', 0) > 0 for r in rows)} | {len(rows)} |")

    out.append("\n## Utility by task type (benign set)\n")
    out.append("| level | pass |\n|---|---|")
    for lv in sorted({r["level"] for r in ben}, key=lambda x: (x not in LEVELS, x)):
        out.append(f"| {lv} | {pct(*util(lv))} |")

    if ada:
        out.append("\n## Adaptive attacker (up to 8 turns, success = judged in code)\n")
        out.append(
            "| level | direct | indirect | mean turns to win | attacker refused |\n|---|---|---|---|---|"
        )
        for lv in LEVELS:
            rows = [r for r in ada if r["level"] == lv]
            cells = []
            for ch in ("direct", "indirect"):
                x = [r for r in rows if r["channel"] == ch]
                cells.append(pct(sum(r["success"] for r in x), len(x)))
            wins = [r["turns"] for r in rows if r["success"]]
            out.append(
                f"| {lv} | {cells[0]} | {cells[1]} | {st.mean(wins):.1f} |"
                if wins
                else f"| {lv} | {cells[0]} | {cells[1]} | - |"
            )
            out[-1] += f" {sum(r['refused'] for r in rows)}/{len(rows)} |"

    tags_p = Path("results/tags.json")
    if tags_p.exists():
        tags = json.loads(tags_p.read_text())
        out.append("\n## Why successful attacks on L3 and L4 worked (hand-tagged)\n")
        out.append("| level | tag | count |\n|---|---|---|")
        c = defaultdict(int)
        for k, v in tags.items():
            c[(k.split("|")[0], v)] += 1
        for (lv, tag), n in sorted(c.items()):
            out.append(f"| {lv} | {tag} | {n} |")
    return "\n".join(out), js


def dump_successes(run: str, n: int = 15, seed: int = 0):
    """sample successful attacks against L3 and L4 for hand reading"""
    rep, ada = load(run, "replay"), load(run, "adaptive")
    rng = random.Random(seed)
    lines = ["# Successful attacks on L3 and L4, sampled for reading\n"]
    for lv in ("L3", "L4"):
        pool = [("replay", r) for r in rep if r["level"] == lv and r["success"]]
        pool += [("adaptive", r) for r in ada if r["level"] == lv and r["success"]]
        rng.shuffle(pool)
        lines.append(f"\n## {lv}: {len(pool)} successes, showing {min(n, len(pool))}\n")
        for src, r in pool[:n]:
            lines.append(f"### {lv}|{r['key']} ({src})\n")
            if src == "replay":
                lines.append(
                    f"- channel {r['channel']}, technique {r['technique']}, goal {r['goal']}, "
                    f"leak={r['leak']} bad_call={r['bad_call']}\n- reply: {r['out']}\n- calls: {r['calls']}"
                )
            else:
                w = r["win"]
                lines.append(
                    f"- adaptive {r['channel']}/{r['goal']} turn {r['turns']}, leak={w['leak']}\n"
                    f"- attempt: {w['attempt'][:600]}\n- reply: {' | '.join(w['out'])[:300]}\n- calls: {w['calls']}"
                )
            lines.append("")
    Path("results").mkdir(exist_ok=True)
    Path("results/l3l4_successes.md").write_text("\n".join(lines))


def plot_bars(js: dict):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    lvs = [lv for lv in LEVELS if f"{lv}/test" in js]
    fig, ax = plt.subplots(figsize=(7, 4))
    for i, (ch, color) in enumerate((("direct", "#1b6ca8"), ("indirect", "#d4572a"))):
        ps, err = [], [[], []]
        for lv in lvs:
            a = js[f"{lv}/test"][ch]
            p = a["k"] / a["n"] if a["n"] else 0
            lo, hi = wilson(a["k"], a["n"]) if a["n"] else (0, 0)
            ps.append(100 * p)
            err[0].append(100 * (p - lo))
            err[1].append(100 * (hi - p))
        ax.bar(
            [x + 0.4 * i for x in range(len(lvs))],
            ps,
            0.4,
            yerr=err,
            color=color,
            label=ch,
            capsize=3,
        )
    ax.set_xticks([x + 0.2 for x in range(len(lvs))], lvs)
    ax.set_ylabel("attack success (%), 95% Wilson CI")
    ax.set_title("attack success by defense level")
    ax.legend(frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig("results/asr_by_level.png", dpi=150)


def plot(js: dict, ben: list[dict]):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(6.4, 4.2))
    for ch, color, mk in (("direct", "#1b6ca8", "o"), ("indirect", "#d4572a", "s")):
        xs, ys, lo, hi = [], [], [], []
        for lv in LEVELS:
            u = [r["ok"] for r in ben if r["level"] == lv]
            a = js[f"{lv}/test"][ch]
            if not u or not a["n"]:
                continue
            p = a["k"] / a["n"]
            lo_, hi_ = wilson(a["k"], a["n"])
            xs.append(100 * sum(u) / len(u))
            ys.append(100 * p)
            lo.append(100 * (p - lo_))
            hi.append(100 * (hi_ - p))
            ax.annotate(lv, (xs[-1], ys[-1]), textcoords="offset points", xytext=(5, 5), fontsize=8)
        ax.errorbar(xs, ys, yerr=[lo, hi], color=color, marker=mk, capsize=3, label=ch, lw=1)
    ax.set_xlabel("benign utility (%)")
    ax.set_ylabel("attack success, test split (%)")
    ax.set_title("security vs utility by defense level")
    ax.legend(frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig("results/tradeoff.png", dpi=150)


def main(run: str):
    Path("results").mkdir(exist_ok=True)
    md, js = tables(run)
    Path("results/summary.md").write_text(md + "\n")
    Path("results/summary.json").write_text(json.dumps(js, indent=1))
    dump_successes(run)
    if load(run, "benign") and load(run, "replay"):
        plot(js, load(run, "benign"))
        plot_bars(js)
    print(md)
