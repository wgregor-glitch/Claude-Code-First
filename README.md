# Dominic Jeffares — Website

A refreshed, self-contained static website for **Dominic Jeffares**, a brand
strategy and marketing consultant for luxury and lifestyle brands.

## What's here

| File | Purpose |
| --- | --- |
| `index.html` | Single-page site markup (semantic, accessible) |
| `styles.css` | Design system + responsive layout |
| `script.js` | Sticky header, mobile nav, scroll-reveal, footer year |

No build step and no runtime dependencies — open `index.html` in a browser or
serve the folder with any static host (GitHub Pages, Netlify, Vercel, S3, …).

```bash
# local preview
python3 -m http.server 8000
# then open http://localhost:8000
```

## Improvements over the previous site

- **Refined luxury design** — editorial serif/sans pairing (Cormorant Garamond +
  Inter), warm paper palette, gold accent, generous whitespace.
- **Fully responsive** — mobile-first layout with an accessible hamburger menu.
- **Accessibility** — semantic landmarks, skip link, visible focus states,
  `aria` on the nav, and `prefers-reduced-motion` support.
- **Performance** — no frameworks; fonts preconnected; scripts deferred; SVG
  favicon inlined as a data URI.
- **SEO & sharing** — descriptive title/meta, Open Graph + Twitter cards,
  canonical URL, and `Person` JSON-LD structured data.
- **Subtle motion** — IntersectionObserver scroll reveals and a sticky,
  blur-backed header that responds to scroll.

## Content

Sections: Hero, Bio, Services (Define your position · Shape the narrative ·
Connect strategy + design), Selected Engagements, and Contact.

> Brand names, imagery slots, and copy are placeholders drawn from the live
> site — swap in final assets and verify the LinkedIn URL before publishing.
