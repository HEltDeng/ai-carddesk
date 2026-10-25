# Storefront comparison — 2026-10-25

final result: passed

## Source and scope

Reference: https://vipcz.vip/, captured in Chrome. User explicitly selected this layout, with original brand colors on white and no explanatory small print. Preserve our own catalog, prices, backend, single-account purchase and card-redemption flow. No copying of the source merchant's identity, contact details, payment destination, delivery guarantees or sales claims.

Source evidence (relative to this project):

- `../outputs/vipcz-reference/desktop-all.png`, 1470 × 746 pixels / CSS viewport.
- `../outputs/vipcz-reference/mobile-all.png`, 390 × 844 pixels / CSS viewport.
- `../outputs/vipcz-reference/product-desktop.png`, `product-mobile.png`, `checkout-desktop.png`, `order-mobile.png`.
- Captured public stylesheet `../outputs/vipcz-reference/site.css`; rendered DOM measurements and computed fonts/colors.

Implementation evidence:

- `../outputs/carddesk-v3-desktop.png`, 1470 × 746 pixels / CSS viewport.
- `../outputs/carddesk-v3-mobile.png`, 390 × 844 pixels / CSS viewport.
- `../outputs/carddesk-v3-checkout.png`, `carddesk-v3-order-mobile.png`.
- Full comparisons: `../outputs/vipcz-comparison-desktop.png` and `../outputs/vipcz-comparison-mobile.png`.
- Focused comparison: `../outputs/vipcz-comparison-card.png`.

Screenshots use matching 1:1 pixel/CSS dimensions. Paired images were combined in the same comparison image and opened for visual review; no density resampling. State: default all-products catalog. Catalog contents differ intentionally; the first Plus card is used for the focused typography/card comparison.

## Findings and fidelity surfaces

No outstanding actionable P0/P1/P2 findings.

- **Fonts:** reference Avenir Next/PingFang/Helvetica display stack and Helvetica body stack; 60px desktop / 40px mobile hero, 24px card title, 48px price. Required product titles remain readable. At 320px the hero scales to 36px.
- **Layout:** reference 64px header, 1068px grid, 252px desktop cards, 20px gaps, 28px radii and white surfaces. Four desktop columns, two tablet columns, one mobile column. The page is shorter because the user explicitly removed supporting prose. Mobile categories stay in one horizontal capsule as requested.
- **Colors:** reference #18181A ink, #FFB319 yellow, #E6E6EA borders, #F7F7F8 footer; active category uses yellow. No unsupported “hot-selling” label or blue promotion frame is added to our products.
- **Assets:** locally served Lobe Icons vectors for ChatGPT, Claude and Grok. Product glyphs are 44px inside a white 96px region, with no dark background, border or shadow. Claude's original orange is preserved. Source menu and bottom-curve paths are recorded in the asset provenance file. No broken images or external asset requests.
- **Copy:** primary pages contain names, monthly/renewal type, prices, fields and actions. No setup explanation, gray disclaimer or merchant promotional text. Blank prices display an em dash and cannot create orders. Three-month sales tier removed. Original Plus price retained; no copied competitor pricing.
- **Purchase behavior:** the original split product/purchase-panel shape wraps our existing Session verification and checkout. No external merchant cart, credentials, payment link or checkout is used. Claude/Grok have their correct logos and disabled actions; no ChatGPT Session link is shown for them.

## Interaction and responsive checks

- All/ChatGPT/Claude/Grok category selection; semantic tabs and keyboard arrow/Home/End bindings.
- Product-card navigation, individual tier links, fragment routes and browser navigation.
- Mobile menu open, navigation and close; accessible expanded state, Escape handling and focus style.
- Invalid Session rejected; synthetic local Session resolves to account confirmation. Consent starts unchecked. Payment disabled while the provider/payment configuration is empty; no real payment or credentials used.
- Invalid card error and order-query screen verified. Existing order/fulfillment tests cover the protected backend state transitions.
- Local administrator product edit and save verified with blank price; category/tier form has no three-month field.
- 1470px desktop, 768px tablet, 390px and 320px mobile rendered. DOM overflow width equals viewport at 768/390/320. No clipped navigation or broken brand images.
- Browser console checked during the primary flow: no JavaScript errors. An intentional invalid-card request returns the expected 400 business error.
- 18 backend tests pass. JavaScript syntax and Git whitespace checks pass.

## Comparison history

The first completed side-by-side review found no actionable P0/P1/P2 mismatch after applying the user's explicit changes. Earlier implementation refinement reduced glyphs to 44px, aligned card titles and ordered the first row to expose all three brands before the comparison pass. No unresolved visual correction loop remains.

## Follow-up limits

The actual payment and upstream fulfillment integration remain outside this visual revision. This QA does not establish live subscription delivery or represent the competitor's catalog, customer support or terms as our own.
