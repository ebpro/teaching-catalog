/**
 * Tailwind v3 build-time purge config for the generated catalog page.
 *
 * The single content source is catalog.py itself: every class literal lives
 * in its HTML templates (and the `hidden` toggle in its inline JS), so the
 * Tailwind CLI emits exactly the utilities the generated page uses into
 * public/app.css (see the "Build purged CSS" step in
 * .github/workflows/catalog.yml). No CDN at runtime.
 *
 * darkMode: "class" keeps the page light-only (no automatic prefers-color-
 * scheme inversion; the palette is intentionally a single institutional one).
 */
module.exports = {
  content: ["./catalog.py"],
  darkMode: "class",
  theme: {
    extend: {},
  },
  plugins: [],
};
