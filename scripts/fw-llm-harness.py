#!/usr/bin/env python3
"""fw-llm-harness — exploratory test rig for LLM-based firewall log review.

Feeds a hand-crafted synthetic digest (the kind of structured input that a
real pre-processor would produce from BGW320 syslog in Loki) to one or more
Ollama models and prints (a) the model's verdict and (b) latency / token
throughput numbers pulled from Ollama's response metadata.

No external dependencies — stdlib only.

Usage:
  ./scripts/fw-llm-harness.py
  ./scripts/fw-llm-harness.py --model qwen3.6:27b
  ./scripts/fw-llm-harness.py --model qwen3.6:27b,deepseek-r1:latest --warm
  ./scripts/fw-llm-harness.py --show-prompt          # dump the full prompt and exit
  ./scripts/fw-llm-harness.py --host 192.168.1.235:11434 --runs 2
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

DEFAULT_HOST = "192.168.1.235:11434"
DEFAULT_MODELS = [
    "qwen3.6:27b",
    "deepseek-r1:latest",
    "deepseek-v4-flash:cloud",
]

# ---------------------------------------------------------------------------
# Synthetic digest — what the pre-processor would emit after bucketing 15 min
# of BGW320 firewall syslog by (src_ip, dst_port, action), enriching with
# geo/ASN/rDNS, and diffing against a 30-day baseline.
# ---------------------------------------------------------------------------
NETWORK_CONTEXT = {
    "wan_ip": "73.218.42.117",
    "lan_subnet": "192.168.1.0/24",
    "port_forwards": [
        {"wan_port": 32400, "proto": "tcp", "lan_target": "192.168.1.50",
         "purpose": "Plex media server"},
        {"wan_port": 25565, "proto": "tcp", "lan_target": "192.168.1.60",
         "purpose": "Minecraft server (friends/family)"},
        {"wan_port": 51820, "proto": "udp", "lan_target": "192.168.1.10",
         "purpose": "WireGuard VPN (personal)"},
    ],
    "known_outbound_peers": [
        {"ip": "5.181.234.12", "purpose": "Mullvad VPN endpoint (DE)"},
    ],
    "internal_hosts_of_note": [
        {"ip": "192.168.1.50", "purpose": "Plex / NAS"},
        {"ip": "192.168.1.60", "purpose": "Game server box"},
        {"ip": "192.168.1.10", "purpose": "Unraid (this monitoring host)"},
    ],
}

DIGEST_WINDOW_MIN = 15
DIGEST_FINDINGS = [
    # Telnet flood — classic IoT-botnet recon. Very obvious.
    {"src_ip": "185.234.219.45", "dst_port": 23, "proto": "tcp",
     "action": "BLOCK", "count": 847, "unique_dst_ports": 1,
     "country": "RU", "asn": "AS49505 Selectel",
     "rdns": None, "first_seen_days_ago": None,
     "baseline_p95_per_window": 0,
     "notes": "Never observed before. Pure TCP SYN flood to telnet."},

    # SSH brute-force, novel source.
    {"src_ip": "212.193.30.21", "dst_port": 22, "proto": "tcp",
     "action": "BLOCK", "count": 412, "unique_dst_ports": 1,
     "country": "RU", "asn": "AS49505 Selectel",
     "rdns": None, "first_seen_days_ago": None,
     "baseline_p95_per_window": 0,
     "notes": "Never observed before. SYN to 22, suggests SSH brute-force."},

    # Wide port scan from one IP.
    {"src_ip": "92.118.39.74", "dst_port": "MULTI(178)", "proto": "tcp",
     "action": "BLOCK", "count": 1240, "unique_dst_ports": 178,
     "country": "NL", "asn": "AS202425 IP Volume",
     "rdns": "scan-22.security.internet-census.org",
     "first_seen_days_ago": 92,
     "baseline_p95_per_window": 60,
     "notes": "Recurring scanner. Hits a wide port range. rDNS suggests "
              "internet-census research project."},

    # SIP brute, US datacenter (common, low actionability).
    {"src_ip": "192.241.213.7", "dst_port": 5060, "proto": "udp",
     "action": "BLOCK", "count": 312, "unique_dst_ports": 1,
     "country": "US", "asn": "AS14061 DigitalOcean",
     "rdns": "zg-0428a-103.stretchoid.com",
     "first_seen_days_ago": 47,
     "baseline_p95_per_window": 280,
     "notes": "Within baseline. Common SIP scanner."},

    # Plex hits — should be IGNORED, this is the user's published service.
    {"src_ip": "MULTI(34 unique)", "dst_port": 32400, "proto": "tcp",
     "action": "ACCEPT", "count": 2104, "unique_dst_ports": 1,
     "country": "MIXED (US:78%, CA:12%, GB:6%, other:4%)",
     "asn": "MIXED (mostly residential ISPs)",
     "rdns": None, "first_seen_days_ago": "ongoing",
     "baseline_p95_per_window": 2400,
     "notes": "Inbound to published Plex port. Within baseline."},

    # Minecraft hits, also legit forward, but with one flagged source.
    {"src_ip": "MULTI(8 unique)", "dst_port": 25565, "proto": "tcp",
     "action": "ACCEPT", "count": 56, "unique_dst_ports": 1,
     "country": "MIXED", "asn": "MIXED residential",
     "rdns": None, "first_seen_days_ago": "ongoing",
     "baseline_p95_per_window": 80,
     "notes": "Inbound to published Minecraft port. Within baseline."},

    # WireGuard from the user's own phone IP range — legit.
    {"src_ip": "73.45.122.88", "dst_port": 51820, "proto": "udp",
     "action": "ACCEPT", "count": 1180, "unique_dst_ports": 1,
     "country": "US", "asn": "AS7922 Comcast",
     "rdns": None, "first_seen_days_ago": 14,
     "baseline_p95_per_window": 1500,
     "notes": "Recurring inbound to WireGuard port. Likely user's mobile."},

    # Outbound to Mullvad — declared in context, should be benign.
    {"src_ip": "192.168.1.10", "dst_ip": "5.181.234.12", "dst_port": 51820,
     "proto": "udp", "action": "ACCEPT", "count": 18402,
     "unique_dst_ports": 1, "country": "DE", "asn": "AS9009 M247 (Mullvad)",
     "rdns": "de-fra-wg-101.mullvad.net",
     "first_seen_days_ago": 220, "baseline_p95_per_window": 19000,
     "notes": "Declared VPN endpoint. Within baseline."},

    # Suspicious outbound — internal host beaconing to a low-rep IP.
    {"src_ip": "192.168.1.78", "dst_ip": "45.155.205.233", "dst_port": 4444,
     "proto": "tcp", "action": "ACCEPT", "count": 96,
     "unique_dst_ports": 1, "country": "BG", "asn": "AS200019 AlexHost",
     "rdns": None, "first_seen_days_ago": None,
     "baseline_p95_per_window": 0,
     "notes": "Novel outbound from an internal host to a bulletproof-hosting "
              "ASN on a non-standard port. Roughly periodic (~every 9s)."},

    # Outbound DNS to a non-Google resolver.
    {"src_ip": "192.168.1.50", "dst_ip": "94.140.14.14", "dst_port": 53,
     "proto": "udp", "action": "ACCEPT", "count": 4521,
     "unique_dst_ports": 1, "country": "CY", "asn": "AS212238 AdGuard",
     "rdns": "dns.adguard-dns.com",
     "first_seen_days_ago": 60, "baseline_p95_per_window": 4400,
     "notes": "Recurring. AdGuard DNS — likely intentional."},
]


SYSTEM_PROMPT = """You are a security analyst reviewing a 15-minute digest of \
firewall events from a residential AT&T BGW320 gateway. The digest has been \
pre-processed: raw nflog_log_fw() lines are bucketed by chain (INPUT / FORWARD \
/ OUTPUT) and key fields, with counts and TCP-flag summaries.

BGW reason codes you will see:
- POLICY-INPUT-GEN-DISCARD: WAN-side packet hit no allow rule. Standard for \
  inbound scans/probes from the internet — usually noise unless volume or \
  pattern is unusual.
- IP-SRC: source IP did not match the interface's expected subnet (anti-spoof \
  check). On INPUT this means a forged WAN packet; on FORWARD this means a \
  LAN host's source IP didn't match what the BGW expects on that interface — \
  often indicates downstream NAT/routing misconfiguration, asymmetric routing, \
  or a stale connection-tracking entry.
- POLICY-ICMP-ECHO: ICMP echo (ping) blocked.

You will receive a JSON digest with: NETWORK_CONTEXT (known-good config), an \
optional OPERATOR_NOTE (a specific question or symptom the operator wants \
investigated), and BY_CHAIN (the bucketed events grouped by netfilter hook). \
If OPERATOR_NOTE is present, address it directly — the operator wants signal \
on that specific concern even if the underlying traffic is benign.

Respond with a single JSON object — no prose before or after — matching this \
schema exactly:

{
  "overall_severity": "low" | "medium" | "high" | "critical",
  "summary": "<1-2 sentences for the operator>",
  "operator_question_response": "<2-4 sentences directly addressing OPERATOR_NOTE, or null if no note>",
  "findings": [
    {
      "tuple": "<chain> <src> -> <dst>:<dst_port>/<proto> <action> reason=<reason>",
      "severity": "low" | "medium" | "high" | "critical",
      "category": "scan" | "brute_force" | "service_abuse" | "exfil" | "c2" | "misconfig" | "legit" | "unknown",
      "explanation": "<1-2 sentences>",
      "recommended_action": "<concrete suggestion or 'none'>"
    }
  ]
}

Rules:
- Suppress benign baseline traffic in `findings`. Only include items at least \
  'medium' severity OR directly relevant to OPERATOR_NOTE.
- 'critical' is reserved for evidence of compromise or active exfiltration.
- For misconfig findings, recommend a specific debugging step.
- Be terse. Operator is technical."""


def build_user_message(digest=None, operator_note=None):
    if digest is None:
        digest = {
            "window_minutes": DIGEST_WINDOW_MIN,
            "network_context": NETWORK_CONTEXT,
            "findings": DIGEST_FINDINGS,
        }
    if operator_note:
        digest = {"operator_note": operator_note, **digest}
    return "DIGEST:\n" + json.dumps(digest, indent=2)


# ---------------------------------------------------------------------------
# Loki → digest
# ---------------------------------------------------------------------------
BGW_FIELD_RES = {
    "action": re.compile(r"\baction=(\w+)"),
    "reason": re.compile(r"\breason=([\w-]+)"),
    "hook":   re.compile(r"\bhook=(\w+)"),
    "in":     re.compile(r"\bIN=([^\s]*)"),
    "out":    re.compile(r"\bOUT=([^\s]*)"),
    "src":    re.compile(r"\bSRC=([\d.]+)"),
    "dst":    re.compile(r"\bDST=([\d.]+)"),
    "proto":  re.compile(r"\bPROTO=(\w+)"),
    "spt":    re.compile(r"\bSPT=(\d+)"),
    "dpt":    re.compile(r"\bDPT=(\d+)"),
    "type":   re.compile(r"\bTYPE=(\d+)"),
    "code":   re.compile(r"\bCODE=(\d+)"),
}
TCP_FLAGS = ("SYN", "ACK", "FIN", "RST", "PSH", "URG")
SUPPRESSED_RE = re.compile(r"Last message .+ repeated \d+ times")


def parse_bgw_line(text):
    """Return dict of parsed BGW firewall fields, or None if not a packet line."""
    if SUPPRESSED_RE.search(text):
        return None
    if "FIREWALL[" not in text or "nflog_log_fw" not in text:
        return None
    rec = {}
    for k, rx in BGW_FIELD_RES.items():
        m = rx.search(text)
        if m:
            rec[k] = m.group(1)
    if "src" not in rec or "hook" not in rec:
        return None
    if rec.get("proto") == "TCP":
        # Negative lookahead excludes "ACK=0" (numeric ack-number field)
        # vs. " ACK " (the bare flag keyword).
        flags = [f for f in TCP_FLAGS if re.search(rf"\b{f}\b(?!=)", text)]
        rec["flags"] = "+".join(flags) if flags else "-"
    return rec


def build_digest_from_loki(path, window_min=DIGEST_WINDOW_MIN):
    """Read a Loki query_range JSON and produce a digest dict."""
    with open(path) as f:
        d = json.load(f)
    raw_lines = []
    for s in d.get("data", {}).get("result", []):
        for ts, line in s["values"]:
            raw_lines.append((int(ts), line))
    raw_lines.sort()

    parsed, skipped = [], 0
    for _ts, line in raw_lines:
        rec = parse_bgw_line(line)
        if rec is None:
            skipped += 1
        else:
            parsed.append(rec)

    wan_ip = None
    for r in parsed:
        if r.get("hook") == "INPUT" and r.get("dst"):
            wan_ip = r["dst"]
            break

    by_chain = {}
    for r in parsed:
        by_chain.setdefault(r.get("hook", "?"), []).append(r)

    def bucket_input(records):
        buckets = {}
        for r in records:
            key = (r.get("src"), r.get("dpt", "-"), r.get("proto", "-"),
                   r.get("reason", "-"), r.get("flags", "-"))
            buckets.setdefault(key, 0)
            buckets[key] += 1
        rows = [
            {"src": k[0], "dst_port": k[1], "proto": k[2],
             "reason": k[3], "flags": k[4], "count": v}
            for k, v in sorted(buckets.items(), key=lambda kv: -kv[1])
        ]
        return rows

    def bucket_forward(records):
        buckets = {}
        for r in records:
            key = (r.get("src"), r.get("dst"), r.get("dpt", "-"),
                   r.get("proto", "-"), r.get("reason", "-"),
                   r.get("flags", "-"))
            buckets.setdefault(key, 0)
            buckets[key] += 1
        rows = [
            {"src": k[0], "dst": k[1], "dst_port": k[2], "proto": k[3],
             "reason": k[4], "flags": k[5], "count": v}
            for k, v in sorted(buckets.items(), key=lambda kv: -kv[1])
        ]
        return rows

    digest = {
        "window_minutes": window_min,
        "wan_ip": wan_ip,
        "raw_lines_total": len(raw_lines),
        "raw_lines_parsed": len(parsed),
        "raw_lines_skipped": skipped,
        "network_context": {
            "wan_ip": wan_ip,
            "note": "BGW LAN side observed at 192.168.1.254. "
                    "FORWARD chain shows traffic from 192.168.0.0/24 — "
                    "subnet provenance not declared by operator.",
        },
        "by_chain": {},
    }

    for chain, records in by_chain.items():
        if chain == "INPUT":
            top = bucket_input(records)
        else:
            top = bucket_forward(records)
        unique_srcs = len({r.get("src") for r in records})
        unique_reasons = sorted({r.get("reason", "?") for r in records})
        digest["by_chain"][chain] = {
            "total_drops": len(records),
            "unique_sources": unique_srcs,
            "reasons": unique_reasons,
            "top_buckets": top[:25],
            "additional_buckets_truncated": max(0, len(top) - 25),
        }

    return digest


# ---------------------------------------------------------------------------
# Ollama client
# ---------------------------------------------------------------------------
def ollama_chat(host: str, model: str, system: str, user: str,
                timeout: int = 600, num_ctx: int = 16384,
                num_predict: int = 4096) -> dict:
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {
            "temperature": 0.2,
            "num_ctx": num_ctx,
            "num_predict": num_predict,
        },
    }).encode()
    req = urllib.request.Request(
        f"http://{host}/api/chat",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def extract_json(text: str):
    """Strip reasoning tags, then pull the first balanced {...} block."""
    cleaned = THINK_RE.sub("", text).strip()
    start = cleaned.find("{")
    if start < 0:
        return None, cleaned
    depth = 0
    for i in range(start, len(cleaned)):
        c = cleaned[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                blob = cleaned[start:i + 1]
                try:
                    return json.loads(blob), cleaned
                except json.JSONDecodeError:
                    return None, cleaned
    return None, cleaned


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------
def fmt_tps(tokens, duration_ns):
    if not duration_ns:
        return "n/a"
    return f"{tokens / (duration_ns / 1e9):.1f} tok/s"


def print_run(model: str, resp: dict, run_idx: int, runs: int):
    msg = resp.get("message", {}).get("content", "")
    parsed, cleaned = extract_json(msg)

    total_ns = resp.get("total_duration", 0)
    load_ns = resp.get("load_duration", 0)
    p_tok = resp.get("prompt_eval_count", 0)
    p_ns = resp.get("prompt_eval_duration", 0)
    c_tok = resp.get("eval_count", 0)
    c_ns = resp.get("eval_duration", 0)

    bar = "─" * 70
    label = f"[{run_idx + 1}/{runs}]" if runs > 1 else ""
    print(f"\n{bar}\n  {model} {label}\n{bar}")
    print(f"  total       : {total_ns / 1e9:7.2f} s   (load: {load_ns / 1e9:.2f} s)")
    print(f"  prompt      : {p_tok:5d} tok  @ {fmt_tps(p_tok, p_ns):>12}")
    print(f"  completion  : {c_tok:5d} tok  @ {fmt_tps(c_tok, c_ns):>12}")

    if parsed is None:
        print("  parse       : FAILED — raw response below")
        print("  ---")
        print("  " + cleaned.replace("\n", "\n  ")[:2000])
        return

    sev = parsed.get("overall_severity", "?")
    summary = parsed.get("summary", "")
    op_resp = parsed.get("operator_question_response")
    findings = parsed.get("findings", []) or []
    print(f"  parse       : ok — overall_severity={sev}, {len(findings)} findings")
    print(f"\n  summary: {summary}")
    if op_resp:
        print(f"\n  operator question response:\n    {op_resp}")
    if findings:
        print("\n  findings:")
        for f in findings:
            print(f"    - [{f.get('severity', '?'):>8}] "
                  f"{f.get('category', '?'):<13} "
                  f"{f.get('tuple', '')}")
            exp = f.get("explanation", "")
            if exp:
                print(f"        {exp}")
            act = f.get("recommended_action", "")
            if act and act.lower() != "none":
                print(f"        action: {act}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=DEFAULT_HOST,
                    help=f"ollama host:port (default: {DEFAULT_HOST})")
    ap.add_argument("--model", action="append", default=None,
                    help="model name; pass multiple times or comma-separate. "
                         f"Default: {','.join(DEFAULT_MODELS)}")
    ap.add_argument("--runs", type=int, default=1,
                    help="how many times to call each model (for variance)")
    ap.add_argument("--warm", action="store_true",
                    help="send a 1-token throwaway call first so load_duration "
                         "doesn't pollute timing")
    ap.add_argument("--show-prompt", action="store_true",
                    help="print the full prompt that would be sent and exit")
    ap.add_argument("--from-loki", default=None,
                    help="path to a Loki query_range JSON; replaces synthetic "
                         "digest with one built from real BGW lines")
    ap.add_argument("--operator-note", default=None,
                    help="a specific concern/symptom to inject into the "
                         "digest (the model will address it directly)")
    ap.add_argument("--show-digest", action="store_true",
                    help="print the digest JSON and exit (no LLM call)")
    args = ap.parse_args()

    models = []
    for m in (args.model or DEFAULT_MODELS):
        models.extend(p.strip() for p in m.split(",") if p.strip())

    if args.from_loki:
        digest = build_digest_from_loki(args.from_loki)
        digest_label = (f"loki:{args.from_loki} "
                        f"({digest['raw_lines_parsed']} parsed / "
                        f"{digest['raw_lines_total']} lines)")
    else:
        digest = None
        digest_label = f"synthetic ({len(DIGEST_FINDINGS)} findings)"

    user_msg = build_user_message(digest=digest,
                                  operator_note=args.operator_note)

    if args.show_digest:
        print(json.dumps(digest if digest else {
            "synthetic": True,
            "findings": DIGEST_FINDINGS,
            "network_context": NETWORK_CONTEXT,
        }, indent=2))
        return 0

    if args.show_prompt:
        print("=== SYSTEM ===\n" + SYSTEM_PROMPT)
        print("\n=== USER ===\n" + user_msg)
        return 0

    print(f"host    : {args.host}")
    print(f"models  : {', '.join(models)}")
    print(f"runs    : {args.runs}{'  (warmup on)' if args.warm else ''}")
    print(f"digest  : {digest_label}, {len(user_msg)} chars user message")
    if args.operator_note:
        print(f"note    : {args.operator_note}")

    for model in models:
        if args.warm:
            try:
                ollama_chat(args.host, model, "You answer in one word.",
                            "Say 'ok'.", timeout=600)
            except Exception as e:
                print(f"\n!! warmup failed for {model}: {e}")
                continue

        for run_idx in range(args.runs):
            t0 = time.time()
            try:
                resp = ollama_chat(args.host, model, SYSTEM_PROMPT, user_msg)
            except urllib.error.HTTPError as e:
                print(f"\n!! {model} HTTP {e.code}: {e.read()[:200]!r}")
                break
            except Exception as e:
                print(f"\n!! {model} failed after {time.time() - t0:.1f}s: {e}")
                break
            print_run(model, resp, run_idx, args.runs)

    return 0


if __name__ == "__main__":
    sys.exit(main())
