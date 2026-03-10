# LinkedIn Post #1: Open Sourcing health-engine

**Status:** Draft (pending Paul review)
**Target:** First standalone LinkedIn post. Follows Feb 27 Paul reply comment.

---

Been building a health scoring engine on the side for the past year. Started as a personal project during a cut. Ended up going way deeper than I expected.

This morning I asked it how I'm doing. It pulled my Garmin data, lab results, weight trend, and habit log, scored everything against CDC population data, and coached me forward. My RHR has dropped from 57 to 50 over 90 days. HRV trending up. Zone 2 cardio strong at 164 min/week. Cardiovascular fitness is improving even in a caloric deficit. Those are real wins.

Then it told me what's dragging the picture down. Sleep. 6.5 hours average, 107 minutes of bedtime variance. That regularity number puts me around the 10th percentile. I built a 10-habit sleep checklist. The data shows almost none of them are sticking. AM sunlight has a 3-day streak. Everything else is at zero.

That's the part most health tools skip. Not just "here are your numbers" but "here's the compound effect, here's what's not working, and here's the one thing to fix today." Recovery, habits, and biomarkers in one picture.

The engine tracks 40+ biomarkers across 20 scored dimensions. Labs, wearables, vitals, self-report. All benchmarked against real NHANES population percentiles, not arbitrary app ranges. It tells you where you stand, what you're missing, and what it costs to close each gap.

Today I'm open sourcing it: github.com/a-deal/health-engine

The timing matters. I've been working with Paul Mederos, who's building Kasane (kasanelife.com), a health coaching app grounded in the same belief: structured health data is what makes coaching personal, not generic advice. Our work kept converging on the same problem from different directions. Open sourcing the scoring layer felt right. This should be shared infrastructure, not locked inside one product.

The repo works out of the box with Claude Code. Clone it, point it at your data, say "how am I doing?" and it coaches you from your actual numbers. No dashboard to check. You just talk to it.

If you're building in health, or just want a clear read on where you stand, it's yours.

---

## Notes

- Hook: sleep/recovery/habits story from this morning's actual check-in (not the insulin story)
- "40+ biomarkers across 20 scored dimensions" matches README language exactly
- Links to README "Why This Exists" section (same framing: what you're missing, coverage score, NHANES)
- Paul/Kasane mention is genuine, not promotional. Names the convergence.
- CTA is soft. "It's yours."
- Reads naturally after the Feb 27 Paul reply (ecosystem framing).
- No em dashes, no heavy quotes, no emojis.
