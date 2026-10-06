# Landing page photos

AI-generated images, created for this project with OpenAI image generation through Codex CLI 0.160 (`codex exec`, 2026-10-06). Generated at 1536x1024, then centre-cropped and resized (hero 1920x1080, spotlights 1200x900) and saved as JPEG (quality 78 for the hero, 82 for the others). `build.py` copies them to `docs/images/`.

The scenes show a fictional retailer: no real brands, logos, readable text or identifiable people.

Common style for every prompt: photorealistic editorial photo, modern sportswear and running-shoe store of a fictional retailer, soft natural daylight, cool tones with a subtle purple accent, shallow depth of field; no text, letters, numbers, logos, brand marks or signage; people only from behind, out of focus, or hands only.

| File | Used for | Scene |
|---|---|---|
| hero.jpg | Hero background | Wide interior of a bright, minimal running-shoe store with a lit wall of shoes and empty floor space |
| spot-stock.jpg | One honest stock number | A shopper's hands holding a phone with an abstract product page and a green availability dot; the same shoe on the shelf behind |
| spot-canary.jpg | Ship a release safely | A person from behind on a sofa completing an online shoe purchase on a laptop |
| spot-shelves.jpg | Shelves refill on time | A store worker from behind placing shoe boxes on a nearly empty shelf, a trolley of plain boxes beside them |
| spot-dashboard.jpg | See it all in Datadog | Two colleagues from behind looking at an abstract monitoring dashboard on a wall screen |

## Screens showing this project's real UI

`spot-stock.jpg`, `spot-canary.jpg` and `spot-dashboard.jpg` keep the AI-generated scenes above, but the phone, laptop and wall screens show real screenshots of this project, composited with a 4-point perspective warp, a rounded screen mask, brightness matched to the original screen and the people or fingers kept in front (2026-10-06):

| File | Screen content | Source |
|---|---|---|
| spot-stock.jpg | Online shop product page, mobile layout (P0042, 9 available online) | Storefront screenshot at phone width, captured for this project |
| spot-canary.jpg | Online shop product page, desktop layout | Storefront screenshot at desktop width, captured for this project |
| spot-dashboard.jpg | Full Datadog UI (left navigation, top bar with title, filter and time picker, stock dashboard widgets) | live capture with `tools/guide-shots` (`wall-ui-full`, 1920x1080, org and user redacted by the tool), no annotation boxes (2026-10-06) |
