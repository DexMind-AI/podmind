# The Economics of Cheap LLMs

**Maya Brennan:** Welcome to Signals & Noise. I'm Maya Brennan. Today: what happens to software design when intelligence costs a tenth of a cent? Daniel Voss runs inference infrastructure for a summarization company and thinks about nothing else.

**Daniel Voss:** It's true, I'm very boring at parties. But the number matters: we summarize a one-hour podcast for about a penny and a half, and ninety percent of that penny is reading the transcript, not writing the summary.

**Maya Brennan:** Say more — the cost is in the input?

**Daniel Voss:** Always. People obsess over which model writes prettier prose, but output tokens are a rounding error. Cost-per-episode is really cost-per-input-token times transcript length. Your bill is determined by how much you make the model read, not by how smart it is.

**Maya Brennan:** So the engineering lever is truncation?

**Daniel Voss:** Truncation, caching, and routing. Cap the transcript at a sane length, route short episodes to a small model and long ones to a big one, and never re-send text the provider has already cached. Those three rules cut our bill by two thirds before we touched a single prompt.

**Maya Brennan:** Where does the quality cliff actually sit? Surely the two-dollar model beats the one-cent model.

**Daniel Voss:** For summarization? Barely, and not where users look. We A/B tested it properly: the cheap model misses nuance maybe one episode in twenty. The expensive model also misses nuance, just more eloquently. The honest finding was that the cheapest model that reliably emits valid JSON wins, because a beautiful summary you can't parse is worth nothing.

**Maya Brennan:** That's bleak and liberating at the same time.

**Daniel Voss:** That's inference economics in one sentence, yes.

**Maya Brennan:** What about the failure modes? Hallucinated names, softened claims?

**Daniel Voss:** Real, both of them. Names are the canary — if a model can't copy a guest's surname faithfully out of the transcript, you should not trust its paraphrases either. We keep a regression suite of episodes with hard names and run every candidate model through it.

**Maya Brennan:** A surname benchmark. I love that it came to this.

**Daniel Voss:** Whatever measures the thing you actually ship. Benchmarks you borrow measure someone else's product.

**Maya Brennan:** Last one: does this all change again when prices drop another order of magnitude?

**Daniel Voss:** The strategies survive, the hesitation doesn't. At some price point you stop asking whether an episode is worth summarizing and just summarize everything ever recorded. Curation moves after ingestion. That inversion is bigger than any model release.

**Maya Brennan:** Summarize first, curate later. Daniel Voss, thank you.

**Daniel Voss:** My pleasure, Maya.
