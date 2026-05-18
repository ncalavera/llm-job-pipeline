"""Static assets: calavera favicon SVG."""

# Day of the Dead calavera — SVG favicon in terracotta + coral palette.
# Compact enough for 16x16 / 32x32 browser tabs.
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <defs>
    <linearGradient id="sk" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#F5F0E8"/>
      <stop offset="100%" stop-color="#EDE5D8"/>
    </linearGradient>
  </defs>
  <!-- skull shape -->
  <path d="M16 2C9.5 2 5 7 5 13c0 3.5 1.2 5.8 3 7.5V24c0 1 .8 2 2 2h1v2.5c0 .8.7 1.5 1.5 1.5h1c.8 0 1.5-.7 1.5-1.5V26h2v2.5c0 .8.7 1.5 1.5 1.5h1c.8 0 1.5-.7 1.5-1.5V26h1c1.2 0 2-1 2-2v-3.5c1.8-1.7 3-4 3-7.5C27 7 22.5 2 16 2z" fill="url(#sk)" stroke="#C2522A" stroke-width="1"/>
  <!-- left eye -->
  <ellipse cx="12" cy="13" rx="2.8" ry="3" fill="#C2522A"/>
  <ellipse cx="12" cy="12.5" rx="1.2" ry="1.4" fill="#F5F0E8"/>
  <!-- right eye -->
  <ellipse cx="20" cy="13" rx="2.8" ry="3" fill="#C2522A"/>
  <ellipse cx="20" cy="12.5" rx="1.2" ry="1.4" fill="#F5F0E8"/>
  <!-- nose -->
  <path d="M15 17.5l1-2 1 2z" fill="#C2522A"/>
  <!-- teeth -->
  <rect x="12" y="20" width="1.5" height="2" rx=".3" fill="#C2522A"/>
  <rect x="14.5" y="20" width="1.5" height="2" rx=".3" fill="#C2522A"/>
  <rect x="17" y="20" width="1.5" height="2" rx=".3" fill="#C2522A"/>
  <!-- forehead flower -->
  <circle cx="16" cy="7.5" r="1.6" fill="#F97316" opacity=".85"/>
  <circle cx="16" cy="7.5" r=".6" fill="#D97706"/>
  <!-- brow decorations -->
  <circle cx="9.5" cy="9" r=".7" fill="#F97316" opacity=".6"/>
  <circle cx="22.5" cy="9" r=".7" fill="#F97316" opacity=".6"/>
</svg>"""
