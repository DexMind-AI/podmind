---
show: "Signals & Noise"
date: 2026-05-16
listened: false
played_up_to: 707
duration_min: 31
guests: ["Daniel Voss"]
transcript_source: "whisper"
raw_dir: "signals-and-noise/2026-05-16-the-economics-of-cheap-llms"
---

▶ 38% [[shows/signals-and-noise]] — Daniel Voss on cost-per-token engineering: your LLM bill is input tokens, and the cheapest model that emits valid JSON wins.

## Key takeaways

- Summarization cost is dominated by input tokens (~90%), so the bill scales with transcript length, not model intelligence.
- Three levers — transcript truncation, provider-side prompt caching, and size-based model routing — cut costs by two thirds before any prompt tuning.
- In A/B tests the cheap model missed nuance roughly one episode in twenty; the expensive one missed it too, just more eloquently. JSON validity decided the winner.
- Surname fidelity is the canary metric: a model that can't copy a guest's name out of the transcript can't be trusted to paraphrase it either.
- When prices fall another order of magnitude, the inversion is 'summarize everything, curate after ingestion' — bigger than any model release.

## Notable quotes

> Cost-per-episode is really cost-per-input-token times transcript length. Your bill is determined by how much you make the model read, not by how smart it is. — Daniel Voss (00:04:10)

> The honest finding was that the cheapest model that reliably emits valid JSON wins, because a beautiful summary you can't parse is worth nothing. — Daniel Voss (00:12:45)

## Cross-links

- People: [[people/maya-brennan]], [[people/daniel-voss]]
- Topics: [[topics/llm-cost-engineering]], [[topics/summarization-pipelines]], [[topics/podcast-transcripts]]
