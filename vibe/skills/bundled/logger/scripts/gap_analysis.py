#!/usr/bin/env python3
"""GCP kubectl Log Gap Analysis — Phase 1-4 processing."""

import json
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

if len(sys.argv) < 2:
    print("Usage: gap_analysis.py <log_file.json> [top_n]")
    sys.exit(1)

LOG_FILE = sys.argv[1]
TOP_N = int(sys.argv[2]) if len(sys.argv) > 2 else 10
CONTEXT_LINES = 5
ABS_FLOOR = 5.0  # seconds

# --- Phase 1: Parse & Normalize ---

with open(LOG_FILE) as f:
    raw = json.load(f)

entries = []
for i, entry in enumerate(raw):
    ts_str = entry.get("timestamp", "")
    if not ts_str:
        continue
    # Parse ISO 8601 with nanosecond precision
    # Truncate to microseconds for Python
    ts_clean = re.sub(r'(\.\d{6})\d*Z$', r'\1+00:00', ts_str)
    if ts_clean == ts_str and ts_str.endswith('Z'):
        ts_clean = ts_str.replace('Z', '+00:00')
    try:
        ts = datetime.fromisoformat(ts_clean)
    except ValueError:
        continue

    payload = ""
    if "jsonPayload" in entry and isinstance(entry["jsonPayload"], dict):
        payload = entry["jsonPayload"].get("message", "")
    elif "textPayload" in entry:
        payload = entry["textPayload"]

    severity = entry.get("severity", "DEFAULT")
    pod = entry.get("resource", {}).get("labels", {}).get("pod_name", "unknown")
    service = entry.get("jsonPayload", {}).get("service", "") if isinstance(entry.get("jsonPayload"), dict) else ""

    entries.append({
        "idx": i,
        "ts": ts,
        "ts_str": ts_str,
        "payload": payload[:500],
        "severity": severity,
        "pod": pod,
        "service": service,
    })

# Sort ascending by timestamp
entries.sort(key=lambda e: e["ts"])

# Detect runs by pod_name (each unique pod = a run)
runs = defaultdict(list)
for e in entries:
    runs[e["pod"]].append(e)

print(f"Total entries parsed: {len(entries)}")
print(f"Unique pods (runs): {len(runs)}")
for pod, elist in sorted(runs.items(), key=lambda x: -len(x[1])):
    print(f"  {pod}: {len(elist)} entries, {elist[0]['ts_str'][:23]} → {elist[-1]['ts_str'][:23]}")

# --- Phase 2: Gap Detection ---

all_gaps = []
for pod, elist in runs.items():
    for i in range(len(elist) - 1):
        delta = (elist[i+1]["ts"] - elist[i]["ts"]).total_seconds()
        all_gaps.append({
            "duration": delta,
            "before": elist[i],
            "after": elist[i+1],
            "run_id": pod,
            "pos_i": i,
            "pos_total": len(elist),
        })

if not all_gaps:
    print("No consecutive log pairs found.")
    exit()

durations = [g["duration"] for g in all_gaps]
mean_d = statistics.mean(durations)
stdev_d = statistics.stdev(durations) if len(durations) > 1 else 0
threshold = max(mean_d + 2 * stdev_d, ABS_FLOOR)

# Check if we need to lower threshold
outlier_gaps = [g for g in all_gaps if g["duration"] > threshold]
if len(outlier_gaps) < 3:
    threshold = max(mean_d + stdev_d, ABS_FLOOR)
    print(f"[Adjustment] Fewer than 3 gaps at mean+2σ; lowered to mean+1σ = {threshold:.3f}s")

outlier_gaps = [g for g in all_gaps if g["duration"] > threshold]
outlier_gaps.sort(key=lambda g: -g["duration"])
top_gaps = outlier_gaps[:TOP_N]

print(f"\nGap statistics:")
print(f"  Mean delta: {mean_d:.3f}s")
print(f"  Stdev: {stdev_d:.3f}s")
print(f"  Threshold: {threshold:.3f}s")
print(f"  Gaps above threshold: {len(outlier_gaps)}")

# --- Phase 3: Context Window Extraction ---

def get_context(run_entries, gap):
    """Get 5 lines before and after the gap position."""
    pos = gap["pos_i"]
    before = run_entries[max(0, pos - CONTEXT_LINES + 1):pos + 1]
    after = run_entries[pos + 1:pos + 1 + CONTEXT_LINES]
    return before, after

# Attach context to each gap
for g in top_gaps:
    run_entries = runs[g["run_id"]]
    g["ctx_before"], g["ctx_after"] = get_context(run_entries, g)

# --- Phase 4: Pattern Frequency Analysis ---

CMD_RE = re.compile(r'\b(kubectl|cp|mv|tar|gzip|git|pip|npm|go|python|java|sh|bash|curl|wget)\b\s+\S+')
EXT_RE = re.compile(r'\.\b(py|go|jar|tar|zip|yaml|json|pb|bin|c|h|cpp|js|ts|tsx|md|sql|csv|parquet|proto)\b')

def extract_tokens(text):
    tokens = []
    for m in CMD_RE.finditer(text):
        tokens.append(("command", m.group().split()[0] + " " + m.group().split()[1][:30]))
    for m in EXT_RE.finditer(text):
        tokens.append(("filetype", "." + m.group(1)))
    return tokens

token_stats = defaultdict(lambda: {"total": 0, "pre": 0, "post": 0, "gaps": set()})

for gi, g in enumerate(top_gaps):
    for entry in g["ctx_before"]:
        for ttype, tval in extract_tokens(entry["payload"]):
            key = (ttype, tval)
            token_stats[key]["total"] += 1
            token_stats[key]["pre"] += 1
            token_stats[key]["gaps"].add(gi)
    for entry in g["ctx_after"]:
        for ttype, tval in extract_tokens(entry["payload"]):
            key = (ttype, tval)
            token_stats[key]["total"] += 1
            token_stats[key]["post"] += 1
            token_stats[key]["gaps"].add(gi)

n_gaps = len(top_gaps)
scored_tokens = []
for (ttype, tval), stats in token_stats.items():
    gap_corr = len(stats["gaps"]) / n_gaps
    pre_w = stats["pre"] / stats["total"] if stats["total"] else 0
    post_w = stats["post"] / stats["total"] if stats["total"] else 0
    scored_tokens.append({
        "token": tval,
        "type": ttype,
        "gap_correlation": gap_corr,
        "pre_gap_weight": pre_w,
        "post_gap_weight": post_w,
        "appearances": stats["total"],
    })

scored_tokens.sort(key=lambda t: -t["gap_correlation"])

# --- Phase 5: Output ---

print("\n" + "="*80)
print("# GCP kubectl Log Gap Analysis Report")
print("="*80)

# 5.1 Run Summary
earliest = entries[0]["ts"].strftime("%Y-%m-%dT%H:%M:%S.%fZ")
latest = entries[-1]["ts"].strftime("%Y-%m-%dT%H:%M:%S.%fZ")
print(f"""
## 5.1 Run Summary
```
Runs analyzed:     {len(runs)}
Total log entries: {len(entries)}
Time span:         {earliest} → {latest}
Total gaps found:  {len(outlier_gaps)} (above threshold)
Threshold used:    {threshold:.3f}s ({mean_d:.3f}s mean + 2σ={stdev_d:.3f}s)
```
""")

# 5.2 Top Gaps Table
print("## 5.2 Top Gaps Table\n")
print("| Rank | Duration | Before (truncated) | After (truncated) | Run ID |")
print("|------|----------|--------------------|-------------------|--------|")
for i, g in enumerate(top_gaps):
    dur = g["duration"]
    if dur >= 60:
        dur_str = f"{int(dur//60)}m {dur%60:.0f}s"
    else:
        dur_str = f"{dur:.3f}s"
    before_msg = g["before"]["payload"][:60].replace("|", "\\|")
    after_msg = g["after"]["payload"][:60].replace("|", "\\|")
    pod_short = g["run_id"][:40]
    print(f"| {i+1} | {dur_str} | {before_msg} | {after_msg} | {pod_short} |")

# 5.3 Pattern Frequency Table
print("\n## 5.3 Pattern Frequency Table\n")
print("| Token | Type | Gap Correlation | Pre-Gap Weight | Post-Gap Weight | Appearances |")
print("|-------|------|----------------|----------------|-----------------|-------------|")
for t in scored_tokens[:20]:
    print(f"| {t['token']} | {t['type']} | {t['gap_correlation']:.2f} | {t['pre_gap_weight']:.2f} | {t['post_gap_weight']:.2f} | {t['appearances']} |")

# 5.4 Optimization Hypotheses
print("\n## 5.4 Optimization Hypotheses\n")
hypo_count = 0
for t in scored_tokens:
    if t["gap_correlation"] >= 0.3:
        pos = "pre" if t["pre_gap_weight"] > 0.6 else ("post" if t["post_gap_weight"] > 0.6 else "both")
        print(f"""```
Token: {t['token']}
Correlation: {t['gap_correlation']:.2f}
Position: {pos}
```
""")
        hypo_count += 1
if hypo_count == 0:
    print("No tokens met the 0.3 correlation threshold.\n")

# 5.5 Gap details
print("\n## 5.5 Raw Gap Context Dump\n")
print("<details>")
print("<summary>Click to expand full context windows</summary>\n")
for i, g in enumerate(top_gaps):
    dur = g["duration"]
    if dur >= 60:
        dur_str = f"{int(dur//60)}m {dur%60:.0f}s"
    else:
        dur_str = f"{dur:.3f}s"
    print(f"### Gap #{i+1} — {dur_str}")
    print(f"```")
    print(f"Gap #{i+1}")
    print(f"  Duration:     {dur_str}")
    print(f"  Start:        {g['before']['ts_str']} — \"{g['before']['payload'][:100]}\"")
    print(f"  End:          {g['after']['ts_str']} — \"{g['after']['payload'][:100]}\"")
    print(f"  Run ID:       {g['run_id']}")
    print(f"  Position:     entry {g['pos_i']} → {g['pos_i']+1} of {g['pos_total']}")
    print(f"```")
    print(f"\n**Before ({len(g['ctx_before'])} lines):**")
    print("```")
    for e in g["ctx_before"]:
        print(f"[{e['severity']}] {e['ts_str'][:26]} {e['payload'][:200]}")
    print("```")
    print(f"\n**After ({len(g['ctx_after'])} lines):**")
    print("```")
    for e in g["ctx_after"]:
        print(f"[{e['severity']}] {e['ts_str'][:26]} {e['payload'][:200]}")
    print("```\n")
print("</details>")
