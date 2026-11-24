# How ReviewBuddy works (simple version)

## The problem
Happy customers rarely leave Google reviews. They don't refuse, they forget, or they don't know what to write.

## The idea
1. **Ask everyone** at the right moment: a QR code on the table and counter, plus a WhatsApp message from staff.
2. **Make it effortless**: tap emojis, fill a sentence, or just speak (English, Hindi, Marathi, Hinglish).
3. **AI writes the review only from the customer's own words**, and they can edit it.
4. One tap **copies it and opens Google's review box**. The customer posts it from their own account.
5. **One polite reminder** the next day for people who forgot.
6. The owner sees **results + AI insights** on a dashboard.

Google rules: same flow for happy and unhappy customers (no review gating), no rewards.

## How the code is built
```
Browser (HTML/CSS/JS pages)  ──fetch──►  FastAPI server (main.py)  ──►  core.py logic  ──►  SQLite / Supabase
                                                    │
                                                    ├──► Gemini API (writes review, owner insights)
                                                    └──► Google Places API (optional: live review count)
```
| File | What it does |
|---|---|
| `main.py` | The web server: page URLs + API endpoints, PIN/admin checks, spam limit |
| `core.py` | The brain: shops, AI prompt, fallback review, follow-ups, stats, storage |
| `shops.json` | Your businesses (or add them from the dashboard) |
| `static/review.html` | Customer page (4 steps, voice, 3 languages, confetti) |
| `static/staff.html` | Staff: send WhatsApp request + next-day reminders |
| `static/dashboard.html` | Owner: funnel, 7-day chart, moods, AI insights, report, CSV, add business |
| `static/live.html` | Counter display: tablet/phone stand with big QR + live counter |
| `static/poster.html` | Printable A5 table poster with QR |
| `static/home.html` | Landing page explaining everything (use it in your Loom) |

## Data saved (table → what)
`events` (scan, ask, remind, generate, copy_post, google_count) · `drafts` (review text + mood + language) · `asks` (name, phone for one reminder; hidden after 7 days) · `shops` (added from the dashboard)

## How "new reviews" is measured
Google review count **now** minus the count **before** you started (`baseline_reviews`). Enter it on the dashboard, or set `GOOGLE_PLACES_KEY` to fetch it automatically.
