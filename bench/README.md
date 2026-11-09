# Receipt benchmark (synthetic)

A generator of made-up receipts with exact ground truth, in Russian, Kazakh and English, at four image qualities, and a way to score any model on them.

```bash
python bench/generate.py bench/data --n 36 --seed 11
docstruc extract bench/data --type receipt --backend claude --model claude-haiku-5-5 --out results.json
python eval/compare.py results.json --truth bench/data/truth.json
```

Qualities: `clean` (flat scan), `photo` (skew, perspective, uneven light, noise, JPEG), `damaged` (a photo with a fold and a finger over one amount; that value is left out of the truth, so the document should be sent to review), `faded` (old thermal paper: low contrast, heavy blur, low resolution).

`eval/compare.py` reports field accuracy, and the number that matters most: **silent errors**, documents with a wrong value that were *not* sent to review.

## Results

| Model | Documents | Field accuracy | Silent errors | Damaged documents flagged | Cost per document |
|---|---|---|---|---|---|
| claude-haiku-5-5 | 36 | 100% (726 / 726) | 0 | 9 / 9 | $0.0005 |
| claude-opus-5-5 | 30 | 100% (578 / 578) | 0 | 9 / 9 | $0.011 |

Prices are from the provider's list at the time of the run. One run each, seed 11 (Haiku) and seed 7 (Opus); not repeated.

## What this does and does not show

- Both models read every printed value correctly, so **this set does not tell models apart**. Clean printed receipts are easy.
- It does show that a value hidden by a finger is not guessed silently: every damaged document was sent to review.
- It says nothing about handwriting, crumpled or torn real receipts, stamps over text or unusual layouts. Real photos are harder: on 8 real receipts a cheaper model made 2 silent tax errors (see the main README), which this set does not reproduce, and that real set cannot be published.
- The Kazakh labels have not been reviewed by a native speaker.

Contributions of harder, real-looking cases (with no personal data) are the most useful thing to add here.
