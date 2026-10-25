# Storefront and support QA — 2026-10-25

final result: passed

## Target and intentional changes

Visual source: https://vipcz.vip/. User explicitly requested white original-color brand marks, no explanatory gray small print, reference prices, an interactive teaching section before the catalog, original tutorials, customer support and protected bulk operations. The source merchant's identity, payment links, contacts and guarantees are not copied. The new teaching section intentionally shifts the catalog below its original position.

## Evidence

- Source desktop: `../outputs/vipcz-reference/desktop-all.png`, 1470 × 746.
- Source mobile: `../outputs/vipcz-reference/mobile-all.png`, 390 × 844.
- Implementation desktop: `../outputs/carddesk-v4-desktop.png`, full page 1470 × 2977, viewport 1470 × 746.
- Implementation mobile: `../outputs/carddesk-v4-mobile.png`, 390 × 844, scrollY 0; all five category controls completely visible, final Codex tab ends at x=364 inside a 390px viewport.
- Narrow mobile: `../outputs/carddesk-v4-mobile-320.png`, 320 × 740.
- Combined full-view comparisons: `../outputs/carddesk-v4-comparison-desktop.png`, `../outputs/carddesk-v4-comparison-mobile.png`. Desktop full-page capture is cropped to the matching viewport height for comparison; no density rescaling.
- Combined focused card comparison: `../outputs/carddesk-v4-comparison-card.png`; original and current first Plus card cropped at equal 252px widths and aligned at card tops.
- Additional states: `../outputs/carddesk-v4-help-mobile.png`, `../outputs/carddesk-v4-ticket.png`.

All captures have 1:1 pixel/CSS dimensions. Both source and implementation were opened together for visual comparison. Original desktop captures remain relevant to unchanged reference styling; current reference Codex and Claude product pages and purchase tutorial were revisited in Chrome.

## Findings and corrections

- P1, fixed: early admin script edit removed the render initializer, producing `html is not defined`. Restored separate refresh/render functions, changed asset version, then reopened the real browser dashboard, ticket list and reply view successfully.
- P2, fixed: at 320px, onboarding choices overflowed and step labels wrapped. Changed narrow-layout choice cards to vertical contents and kept step labels on one line. Recaptured 320px: no horizontal document overflow or clipped teaching cards.
- P2, fixed: the fifth category was clipped at 390px. Reduced mobile pill padding; screenshot and DOM bounds now show all five categories completely. At 320px the intentional horizontal strip scrolls, with keyboard navigation retained.
- No remaining actionable P0/P1/P2 issues found. The new tutorial section occupies first-screen space as explicitly requested; catalog position is an intentional product change rather than unexplained source drift.

## Fidelity surfaces

- Typography: reference system/Avenir Next/PingFang stack; desktop 60px and mobile 40px hero (36px at 320), 24px card titles and 48px prices. Tutorial uses 16px body text and 25–27px step titles; no gray microcopy.
- Spacing/layout: 64px header, 1068px four-column grid, 252px cards with 20px gaps and 28px radii. New teaching panel shares the same width and rounds. Mobile has single-column catalog, stacked help/support layouts and compact teaching controls.
- Colors: #18181A text, #FFB319 yellow, #E6E6EA borders and #F7F7F8 soft surfaces; original Claude orange. No source blue promotional labels, dark icon tiles or floating ads.
- Assets: local original brand SVGs for ChatGPT, Claude, Grok and Codex; no broken images, remote image requests or faux brand glyphs. Teaching diagrams use semantic content cards, receipt rows and progress states, not replacement logos.
- Copy/content: all 19 listed products have positive numeric reference prices; monthly subscription labels and Codex one-time integral amounts are distinct. Tutorials cover purchase/redemption, Session, ChatGPT, Codex, Claude/Grok and support. No payment-configuration warning appears in the main flow.

## Functional checks

- Teaching tab selection and Next; product/category routing; Codex prices 84/162/305/595; original brand color/white backgrounds.
- Desktop, 390px and 320px browser views; no document overflow at 390/320 and all images loaded.
- Tutorials and Codex article rendered; narrow layouts readable.
- Synthetic Session locally parsed; confirmation email shown, consent unchecked and payment button disabled. No real credentials or payment used.
- Local support submission creates a ticket and query code; backend reads and replies; customer view displays that same reply and status. Saved evidence contains only synthetic local data.
- Admin dashboard, ticket list and reply flow rendered successfully after correction. Backend tests cover staff permissions, claiming, revocation and bulk operations.
- Browser error log after corrected flows: empty.
- 35 backend tests pass, JS syntax checks pass, Git whitespace check passes.

## Limits

This QA verifies the implemented storefront and first-party operations. It does not claim real payment, external account verification, email delivery, upstream recharge or membership fulfillment. Payments and upstream services remain disabled. Support is a site mailbox operated from the backend, without third-party customer-service contact details.
