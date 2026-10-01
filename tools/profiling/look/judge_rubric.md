# Look Assist blind judging rubric -- look-rubric-v1 (FROZEN)

This file is hashed (sha256 of its exact bytes, LF line endings) BEFORE any judging. The digest is
recorded in `judge_rubric.lock.json` and in every judging session; a verdict that quotes a different
digest is rejected. Changing one character is a new rubric version, never an edit.

## What you are looking at

One image. It shows TWO renderings of the SAME frame, side by side, separated by a mid-grey gutter:
a LEFT picture and a RIGHT picture. They differ only in how the picture was graded or rendered.
You are not told how, and which one is which is not available to you. Judge only what you can see.

Rules that keep this blind:
- Do not guess which picture came from which process. Do not reason about "the usual look of" any
  software or preset. Only pixels count.
- Position means nothing. The same pair is shown again with left and right swapped; a preference that
  just follows a side is discarded.
- If the two pictures are indistinguishable to you, say `tie`. A forced guess is worse than a tie.
- Judge the picture, not the image's grey gutter or any border.

## Scores: anchored 1-5, for EACH picture separately

Use whole numbers. 3 is "acceptable, nothing special". Do not hand out 5 casually.

### tonal_separation -- can you read the planes of the scene from light and dark alone?
- 1: flat or muddy; foreground, midground and background blur into one tone, or the tones are crushed/blown into blocks.
- 3: planes are readable but contrast is timid or uneven.
- 5: clear separation between planes, deep but detailed blacks, bright but detailed whites, pleasing local contrast.

### highlight_rolloff -- how do the brightest areas end?
- 1: highlights clip to flat white patches or bands, with hard edges and no texture.
- 3: highlights are mostly intact but roll off abruptly or lose some texture.
- 5: highlights compress smoothly toward white, keep texture and colour, no visible clipping edge.

### skin -- do people look healthy and natural? (score `null` when no person/skin is visible in the picture)
- 1: skin is visibly green, magenta, grey, orange or waxy.
- 3: skin is plausible but a little off in hue, brightness or saturation.
- 5: skin looks natural and flattering for the scene's light, with believable shading.

### colour_cast -- is the overall colour balance right for this scene?
- 1: a strong cast over the whole frame (e.g. lavender, teal or yellow wash) that the scene does not justify; neutrals are not neutral.
- 3: a mild cast, or a deliberate-looking tint that is not clearly justified.
- 5: neutrals read neutral; any warmth or coolness is plausibly the scene's own light.

### scene_read -- does the grade match what the scene is?
- 1: the picture reads as the wrong scene (daylight graded as night, dusk as noon) or the exposure is plainly wrong for the light.
- 3: the scene is recognisable but exposure or mood is somewhat off.
- 5: exposure and mood are right for the scene: daylight looks like daylight, night looks like night.

## Pairwise preference

After scoring both pictures, say which picture a viewer would rather watch: `left`, `right` or `tie`.
Base it on the same five criteria, not on sharpness, noise, compression or framing, which are not
what is being compared.

## Answer format -- reply with ONE JSON object and nothing else

```json
{
  "left":  {"tonal_separation": 3, "highlight_rolloff": 3, "skin": null, "colour_cast": 3, "scene_read": 3},
  "right": {"tonal_separation": 3, "highlight_rolloff": 3, "skin": null, "colour_cast": 3, "scene_read": 3},
  "preference": "tie",
  "rationale": "one or two sentences naming what you saw"
}
```

Every score is an integer 1-5; only `skin` may be `null`. `preference` is exactly `left`, `right` or `tie`.
