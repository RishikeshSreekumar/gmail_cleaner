"""Measuring the classifier against mail a person has labelled by hand.

    mclean eval export -n 300 -o sample.csv   # random messages, labels blank
    (fill in true_category and true_protected in a spreadsheet)
    mclean eval score sample.csv               # precision / recall per category

The score that matters most is **missed protection**: messages the person
marked as protected that the classifier left unprotected. Those are the only
classifier mistakes that can lead to losing mail, so they are reported first
and on their own, before any accuracy number.

Scoring re-runs the current classifier (and the account's rules), so the same
file measures the effect of a rule, a locale pack or a classifier change.
"""

from __future__ import annotations

import csv
import json
import random
import sqlite3
import time
from collections import Counter
from pathlib import Path

from . import sync

EXPORT_COLUMNS = ("msg_key", "from_email", "subject", "list_id", "predicted_category",
                  "predicted_protected", "true_category", "true_protected")


def export_sample(conn: sqlite3.Connection, path: Path, n: int = 300,
                  seed: int | None = None) -> int:
    keys = [r["msg_key"] for r in conn.execute("SELECT msg_key FROM emails")]
    random.Random(seed).shuffle(keys)
    keys = keys[:n]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=EXPORT_COLUMNS)
        w.writeheader()
        for k in keys:
            r = conn.execute("SELECT * FROM emails WHERE msg_key=?", (k,)).fetchone()
            w.writerow({
                "msg_key": r["msg_key"], "from_email": r["from_email"],
                "subject": r["subject"], "list_id": r["list_id"] or "",
                "predicted_category": r["category"],
                "predicted_protected": r["protected"],
                "true_category": "", "true_protected": "",
            })
    return len(keys)


def _truthy(v) -> bool | None:
    s = str(v or "").strip().lower()
    if s in ("1", "true", "yes", "y", "p", "protected"):
        return True
    if s in ("0", "false", "no", "n", ""):
        return False if s else None
    return None


def predict_rows(rows: list[dict], conn: sqlite3.Connection | None = None) -> list[dict]:
    """Classify each labelled row with today's classifier and rules.

    A row whose msg_key is in the index is classified from the full indexed
    message; anything else (a hand-written case) from the columns it carries.
    """
    rules = sync.load_rules(conn) if conn else []
    ctx = sync.load_context(conn) if conn else {}
    now = int(time.time())
    out = []
    for row in rows:
        m = None
        if conn is not None and row.get("msg_key"):
            r = conn.execute("SELECT * FROM emails WHERE msg_key=?",
                             (row["msg_key"],)).fetchone()
            if r:
                m = dict(r)
                m["labels"] = json.loads(m["labels"] or "[]")
                m["age_days"] = max(0, (now - (m["received_at"] or now)) // 86400)
        if m is None:
            email = (row.get("from_email") or "").strip().lower()
            m = {
                "from_email": email,
                "from_domain": email.split("@")[-1] if "@" in email else "",
                "subject": row.get("subject") or "",
                "list_id": row.get("list_id") or "",
                "unsubscribe": int(bool(row.get("list_id"))),
                "labels": [l for l in (row.get("labels") or "").split("|") if l],
                "is_unread": int(_truthy(row.get("unread")) or 0),
                "is_inbox": 1,
                "is_starred": int(_truthy(row.get("starred")) or 0),
                "age_days": int(row.get("age_days") or 10),
            }
        e = sync.enrich(m, rules, ctx)
        out.append({**row, "pred_category": e["category"],
                    "pred_protected": bool(e["protected"])})
    return out


def score(rows: list[dict]) -> dict:
    """Per-category precision/recall plus the protection error counts."""
    labelled = [r for r in rows if (r.get("true_category") or "").strip()]
    tp, fp, fn = Counter(), Counter(), Counter()
    correct = 0
    for r in labelled:
        t = r["true_category"].strip().lower()
        p = (r["pred_category"] or "").lower()
        if t == p:
            tp[t] += 1
            correct += 1
        else:
            fp[p] += 1
            fn[t] += 1
    cats = sorted(set(tp) | set(fp) | set(fn))
    per = []
    for c in cats:
        prec = tp[c] / (tp[c] + fp[c]) if tp[c] + fp[c] else 0.0
        rec = tp[c] / (tp[c] + fn[c]) if tp[c] + fn[c] else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        per.append({"category": c, "support": tp[c] + fn[c], "precision": round(prec, 3),
                    "recall": round(rec, 3), "f1": round(f1, 3)})

    prot = [r for r in rows if _truthy(r.get("true_protected")) is not None]
    missed = [r for r in prot if _truthy(r["true_protected"]) and not r["pred_protected"]]
    over = [r for r in prot if not _truthy(r["true_protected"]) and r["pred_protected"]]
    return {
        "labelled": len(labelled),
        "accuracy": round(correct / len(labelled), 3) if labelled else 0.0,
        "categories": per,
        "protection_labelled": len(prot),
        "missed_protection": len(missed),
        "missed_examples": [{"from_email": r.get("from_email"), "subject": r.get("subject"),
                             "predicted": r["pred_category"]} for r in missed[:20]],
        "over_protection": len(over),
    }


def score_file(path: Path, conn: sqlite3.Connection | None = None) -> dict:
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return score(predict_rows(rows, conn))
