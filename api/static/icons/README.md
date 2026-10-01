# Operator dashboard icon

`bitcoin.ico` is the browser-tab icon for the operator pages, served at
`/favicon.ico`.

- Design: the Bitcoin logo, an orange (`#F7931A`) circle with a white ₿ tilted
  14 degrees clockwise. The logo design is in the public domain.
- Glyph: the Bitcoin sign (U+20BF) from Segoe UI Bold, drawn as vector outlines.
- Built by: `tools/build_bitcoin_icon.ps1` (Windows, .NET `System.Drawing`, no downloads)
- Sizes: 16, 24, 32, 48, 64, 128 and 256 px, each a 32-bit PNG inside the .ico
- SHA-256: `7BF393B9DDA053D5C51115DF4082B4B72B9F6BE8F9C0047B4B29BB37F8549C04`

Change this icon only through a reviewed pull request that rebuilds it with the
script and updates this record. The checksum test fails when the file and this
record disagree.
