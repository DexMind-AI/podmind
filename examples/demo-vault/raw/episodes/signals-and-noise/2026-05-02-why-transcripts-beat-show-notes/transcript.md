# Why Transcripts Beat Show Notes

**Maya Brennan:** Welcome back to Signals & Noise. I'm Maya Brennan, and this week I'm talking to Tomas Ek, who did something I've threatened to do for years: he built a search engine over every podcast he's ever listened to.

**Tomas Ek:** Threatened is the right word. I threatened too, for about three years, until I realized the blocker was never the search part. It was the transcripts.

**Maya Brennan:** Because most shows don't publish them?

**Tomas Ek:** Right. Maybe one in fifty feeds carries a real transcript tag. Everyone else gives you show notes, which are marketing copy. Show notes tell you what the producer hoped the episode was about; the transcript tells you what was actually said.

**Maya Brennan:** That's a sharp distinction. I write our show notes and I can confirm they're aspirational.

**Tomas Ek:** And that's fine! But you can't search aspirations. When I want to find the episode where someone explained why SQLite beats a hosted database for personal tools, I need the sentence, not the summary.

**Maya Brennan:** So walk me through the cascade. You don't transcribe everything from scratch?

**Tomas Ek:** No, that would be wasteful. First I check the RSS feed for a published transcript. Then I check whether the episode exists on YouTube, because auto-captions are free and surprisingly good. Only when both fail do I run Whisper locally, overnight, while the laptop is plugged in.

**Maya Brennan:** What fraction ends up needing local transcription?

**Tomas Ek:** About a third. The economics flipped around 2024 — a consumer laptop now transcribes faster than real time, so the backlog problem solved itself. The hard part stopped being compute and became plumbing.

**Maya Brennan:** And once you had the text, what changed about how you listen?

**Tomas Ek:** Everything. The shift is that listening stopped being ephemeral. An episode used to evaporate a week after I heard it; now it's a page I can grep, link, and argue with.

**Maya Brennan:** Argue with?

**Tomas Ek:** I annotate. When a guest makes a claim that ages badly, I add a note next to the quote. My listening history became a record of who was right.

**Maya Brennan:** That feels like the actual headline: the transcript isn't the product, the accumulated cross-references are.

**Tomas Ek:** Exactly. A transcript on its own is a haystack. Two hundred transcripts with a wiki on top is a memory.

**Maya Brennan:** Tomas Ek, thank you. The search engine is called Earhorn, links in the — well, in the show notes, ironically.

**Tomas Ek:** The irony is fully intended. Thanks, Maya.
