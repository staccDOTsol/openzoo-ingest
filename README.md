# openzoo-ingest

Everything your machine produces, bound into your own memory. Local by default.

```bash
curl -fsSL https://raw.githubusercontent.com/staccDOTsol/openzoo-ingest/main/install.sh | bash
```

Ten minutes later, and every ten minutes after:

```
● 4,812 items 38.2M chars | agents:3901 clipboard:412 history:388 notifications:74 screenshots:37 | last run 40s ago local
```

That line is `openzoo-ingest watch`. The same numbers arrive as a desktop
notification after each run that bound something, so you know it is working
without opening anything.

## What gets ingested

| source | what | how |
|---|---|---|
| `clipboard` | Omarchy clipboard history (text, and images described) | `~/.local/state/omarchy/clipboard-history.json` |
| `notifications` | every toast that expired or was dismissed | `~/.local/state/omarchy/notifications/history/` |
| `screenshots` | `screenshot-*.png` in your Pictures dir | tesseract OCR (free), then a vision model on your local openzoo proxy, capped per run |
| `agents` | Claude Code, Codex, pi, omp, opencode transcripts | incremental — only the new tail of each session binds |
| `history` | zsh / bash / fish history | incremental |
| `tmux` | the current pane's scrollback | on demand |
| `file PATH` | text-ish files, pdf via pdftotext | on demand |
| `url URL` | a page, stripped to text | on demand |

Each source is its own leCore context, appended forever. A content-hash ledger
makes every run idempotent; a byte-offset ledger makes transcripts incremental.
`openzoo-ingest recall "…"` fans a query across all of them.

## Where the data goes

**To a leCore daemon on 127.0.0.1:8787, and nowhere else.** The installer
starts that daemon as a user service (`openzoo-lecore.service`); it binds to
loopback and this package never changes that. The vendored daemon is the
`hrr` sidecar over the [leCore](https://github.com/staccDOTsol/leCore) engine,
cloned from git into `~/.local/share/openzoo-ingest/leCore`.

The one other network call: screenshot descriptions go to the local openzoo
proxy (`localhost:8402`) you already run — your own paid call, capped at
`OPENZOO_VISION_CAP` per run, OCR tried first. Set the cap to 0 for OCR only.

## The shared brain (the only egress)

Set these in `~/.config/openzoo-ingest/env` and every item bound locally is
also bound to that endpoint, so a team or an organisation recalls across
machines:

```
OPENZOO_BRAIN_URL=https://api.openzoo.fun
OPENZOO_BRAIN_KEY=…
```

Unset, that code path is never executed. There is no default brain and no
silent opt-in.

## Commands

```
openzoo-ingest run [source ...]     default: clipboard notifications screenshots agents history
openzoo-ingest file PATH ...
openzoo-ingest url URL
openzoo-ingest tmux
openzoo-ingest recall QUERY [-k N]  daemon-shaped JSON, merged across contexts
openzoo-ingest status               JSON: per-source totals, contexts, last run, errors
openzoo-ingest watch                live one-line bar
```

State lives in `~/.local/state/openzoo-ingest/` (`status.json`, `bound.jsonl`,
`ingest.log`, ledgers). Delete the directory to start over; the daemon keeps
its data under `~/.local/share/openzoo-ingest/lecore-memory`.

## Works alongside

- [`openzoo.fun/omarchy`](https://openzoo.fun/omarchy) — openzoo as an Omarchy coding agent (Claude Code, paid per call, no key)
- [`openzoo.fun/bar`](https://openzoo.fun/bar) — the Omarchy bar widget: ask any model, recall over your bound corpus
- [`openzoo.fun/omarchymax`](https://openzoo.fun/omarchymax) — all three in one line

MIT.
