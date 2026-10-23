# HarvestRadar

A read-only LLM pipeline that reads group-chat screenshots, finds the messages where someone is asking to rent or borrow an item, turns each request into a structured lead, and hands it to a human to follow up.

This repository holds the whole pipeline: the mouse-only screen collector for macOS, the local prefilter, the extractors (local OCR + Qwen3, Gemini, Claude), the processing step that dedupes leads and sends them to the admin backend (a Supabase Edge Function), and the evaluation harness. The labeled datasets and every real screenshot stay private: they contain real people's names and messages.

**About the domain words.** The private project serves one specific marketplace. In this public copy every domain term (item categories, place names, the chat app) is replaced by a neutral placeholder: item types are `alpha` / `beta`, places are invented names like `lakeside` or `riverton`. The code paths, prompt structure and evaluation method are unchanged. The commit history mirrors the private repository one to one: every commit and merge, same order, dates and PR titles, with the same word substitutions. Internal notes and operating docs are left out; group and person names in tests are invented.

## Why

Busy group chats carry real demand ("need one female beta for riverton 3/14–15") buried among offers, carpools, ads and small talk. Reading dozens of groups by hand is slow and misses requests.

HarvestRadar automates "read the groups → spot the requests → write them down". A person still does the last step: reply, quote, close.

## Pipeline

```
 ┌─────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
 │ 1. Capture  │──▶│ 2. Prefilter │──▶│ 3. Extract   │──▶│ 4. Leads     │
 │ scroll each │   │ OCR + small  │   │ vision LLM   │   │ dedupe, send │
 │ group, take │   │ local model  │   │ screenshot → │   │ to the admin │
 │ screenshots │   │ drop screens │   │ JSON         │   │ page, a human│
 │ mouse only  │   │ with no ask  │   │              │   │ replies      │
 └─────────────┘   └──────────────┘   └──────────────┘   └──────────────┘
   spare Mac         same Mac, free     local Qwen3 or      Edge Function
                                         Gemini / Claude
```

- **Capture** (`harvestradar/collector/`): finds each listed group in the chat app's sidebar, scrolls up and screenshots every screen, using only the mouse (no keyboard). It reads the time dividers with OCR to stop at the last bookmark, and stops everything on danger phrases (account restricted, re-login), an unexpected window or a wrong chat title. A person starts each run; it never runs on a timer.
- **Prefilter** (`harvestradar/prefilter.py`): macOS Vision OCR, keyword rules, then a local Qwen3 model through Ollama. It drops screens that clearly contain no request before anything is sent to a cloud model. When in doubt it lets the screen through: lead recall matters more than savings.
- **Extract** (`harvestradar/extract.py`, `harvestradar/gemini.py`): one prompt and one JSON Schema shared by every model, so scores from different providers land in the same table. The provider is chosen from the model name (`gemini-*`, `claude-*`).
- **Local-only extraction** (`harvestradar/local_extract.py`): OCR text plus Qwen3 4B filling the same schema, without looking at the image. Free and offline. It started as a baseline against the vision models and is now the default extractor, with Gemini or Claude available per run (`--model`).
- **Leads** (`harvestradar/process.py`, `harvestradar/leads_sync.py`, `supabase/functions/`): screenshots → prefilter → extraction, deduped by group + sender + text, written to `leads.jsonl` and pushed to the admin app through an Edge Function whose token can only insert rows. The group list is read back from the same function.

## Output schema

Each relevant message in a screenshot becomes one record:

```json
{
  "leads": [
    {
      "sender": "display name, copied as shown",
      "kind": "request",
      "item_type": "beta",
      "location": "riverton",
      "dates": ["03-14", "03-15"],
      "male": 1,
      "female": 1,
      "quote": "original message text",
      "sent_at": "3/9 21:26",
      "notes": ""
    }
  ]
}
```

- `kind`: `request` (what we want), `offer`, `carpool`, `other`
- `item_type`: `alpha`, `beta` or `unknown`; the model is told not to guess
- `location`: lowercase English name, abbreviations expanded (`lks` → `lakeside`)
- `dates`: `MM-DD`, ranges expanded day by day
- `male` / `female`: how many items for each; gender is a hard constraint

The prompt is in Chinese because the chats are. It is versioned (`PROMPT_VERSION` in `harvestradar/prompt.py`); every prompt change bumps the version so eval runs stay comparable.

## Evaluation first

Every prompt or model change is re-scored against a hand-labeled set before it ships.

- A dataset is a folder with `screenshots/*.png` and `labels.jsonl` (one line per screenshot). The format is shown in `evals/labels.example.jsonl`; the names in it are invented.
- Labels list the real requests (`leads`), the distractors (`others`: offers, carpools) and people to `ignore` (names cut off at the screen edge). A field may hold a list of acceptable answers. `dates: null` means the date cannot be known from the screen and is not scored.
- Senders are matched after normalizing whitespace, emoji, full/half-width characters and case. Locations are normalized before comparison.
- Metrics: precision (false alarms waste the operator's time), recall (a missed request is a lost customer), F1, per-field accuracy (item type, location, dates, head count), cost and seconds per screenshot.

Hard cases built into the labels: senders whose display name reads like an offer but who are asking; one message that both offers and asks; relative dates ("tomorrow", "this weekend") resolved from the chat's time dividers; "already found one" follow-ups; carpool requests that mention a place and dates; group-buy ads listing many places; English requests; ambiguous item names.

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=...        # or ANTHROPIC_API_KEY for claude-* models
python -m evals.run_eval --data <dataset folder> --model gemini-3.5-flash
python -m evals.run_eval --data <dataset folder> --model qwen3:4b      # local, needs Ollama on a Mac
python -m evals.compare runs/*/summary.json                             # side-by-side table
python -m evals.run_prefilter --data <dataset folder>                   # prefilter drop rate vs lead recall
python -m unittest discover tests
```

## Results

**Early benchmark** (22 screenshots, prompt v1, M1 MacBook Air):

| Model | Precision | Recall | F1 | Item type | Location | Dates | Head count | Sec / screenshot |
|---|---|---|---|---|---|---|---|---|
| gemini-3.5-flash (vision) | 87% | 90% | 89% | 27/27 | 22/27 | 19/21 | 27/27 | 24 |
| qwen3:4b (OCR text only) | 79% | 87% | 83% | 25/26 | 21/26 | 17/21 | 21/26 | 22 |

Finding requests is close; the local model's extra false alarms come from intent (offer vs. request), which the vision model reads better from layout.

**In production** (the 400-screenshot labeled set is private):

- Precision 72% → 96% and recall 78% → 93% over 5 prompt iterations, benchmarked across Gemini, Claude and GPT.
- The on-device prefilter (OCR + local Qwen3) cut cloud calls and model cost by 71%.

## Safety rules

- **Read-only.** It never sends messages, adds contacts or presses anything that acts on the outside world.
- **Screenshots only.** No hooking, no database decryption, no process injection.
- **Separate account, separate machine.**
- **Privacy.** Real screenshots and labels never enter this repository. Public examples are invented.
- **Secrets** live in environment variables only.

## Layout

```
harvestradar/
  collector/        screen collector: core.py (find group, stop rules, emergency stop), mac.py (windows,
                    screenshots, OCR, layout, mouse), timeparse.py (time dividers), safety.py (danger phrases)
  process.py        collector output → prefilter → extraction → deduped leads.jsonl
  groups.py         which groups to read: backend list → last saved → collector.toml
  leads_sync.py     send new leads to the admin backend
  prompt.py         prompt (versioned) + JSON Schema
  extract.py        screenshot → leads; dispatch by model name; Claude implementation
  gemini.py         Gemini implementation (same prompt and schema, rate-limit retry)
  prefilter.py      local prefilter: Vision OCR + keywords + Qwen3 via Ollama
  local_extract.py  OCR text + Qwen3, no image
supabase/functions/ insert-only endpoint for leads (Deno)
evals/
  run_eval.py       run a model over a dataset, score against labels, save results
  compare.py        merge runs into a Markdown comparison table
  run_prefilter.py  prefilter drop rate vs. lead recall
  labels.example.jsonl
tests/
install.sh, collector.example.toml   setting up the collector Mac
```

## License

MIT, see `LICENSE`.
